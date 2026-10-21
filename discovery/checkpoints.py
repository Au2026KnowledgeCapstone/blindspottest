"""Private, atomic crawl journals and verified browser-state restoration."""
import hashlib
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

VERSION = 2


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name+'.',suffix='.tmp',dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


class CheckpointMixin:
    def _policy(self, entry):
        keys = ('read_only', 'deny_destructive', 'exclude_prefixes', 'stay_under',
                'id_segment_patterns', 'identity_reads', 'identity_gap_ms', 'explore_inputs','readiness')
        return {'base_url': self.base_url, 'entry': entry,
                **{key: getattr(self.config, key) for key in keys},
                'credentials_hash': hashlib.sha256(json.dumps(self.config.credentials, sort_keys=True).encode()).hexdigest()}

    def _browser_state(self):
        state = self.page.context.storage_state(indexed_db=True)
        sessions = {}
        try:
            origin = self.page.evaluate('location.origin')
            sessions[origin] = self.page.evaluate("Object.fromEntries(Object.entries(sessionStorage).filter(([k]) => !k.startsWith('__blindspot_restore_')))")
        except Exception:
            pass
        return {'storage': state, 'session': sessions}

    def _hydrate(self, data):
        """Hydrate before the app runs. Never close the caller's page/context."""
        context = self._original_page.context.browser.new_context(storage_state=data['storage'])
        import uuid
        marker = '__blindspot_restore_' + uuid.uuid4().hex
        context.add_init_script('''(() => {
          const sessions = %s, marker = %s;
          if (sessions[location.origin] && !sessionStorage.getItem(marker)) {
            for (const [key, value] of Object.entries(sessions[location.origin])) sessionStorage.setItem(key, value);
            sessionStorage.setItem(marker, '1');
          }
        })();''' % (json.dumps(data.get('session', {})), json.dumps(marker)))
        previous = self._owned_context
        self._owned_context = context
        self.page = context.new_page()
        if previous:
            previous.close()

    @staticmethod
    def _controls(snapshot):
        return [(e.get('selector'), e.get('value'), e.get('checked'))
                for e in snapshot.get('elements', [])
                if e.get('tag') in ('input', 'select', 'textarea')]

    def _remember(self, state):
        if state.id not in self._checkpoints:
            self._checkpoints[state.id] = {'browser': self._browser_state(),
                'url': self.page.url, 'controls': self._controls(state.snapshot or {}),
                'recipe': list(self._active_recipe)}

    def _matches(self, state, checkpoint, verify_storage=True):
        snapshot = self._snapshot()
        return (self._identify(snapshot).id == state.id
                and (not verify_storage or self._browser_state() == checkpoint['browser'])
                and self._controls(snapshot) == [tuple(c) for c in checkpoint['controls']])

    def _restore_checkpoint(self, state):
        checkpoint = self._checkpoints.get(state.id)
        if checkpoint is None:
            return False
        try:
            if self._matches(state, checkpoint):
                self._active_recipe = list(checkpoint['recipe'])
                return True
            self._hydrate(checkpoint['browser'])
            self._goto(checkpoint['url'])
            if self._matches(state, checkpoint):
                self._metrics['checkpoint_hits'] += 1
                self._active_recipe = list(checkpoint['recipe'])
                return True
        except Exception:
            pass
        # GET may mutate state (e.g. /cart/p1 adds an item). Replay the actual
        # path, including removal, instead of trusting the target URL.
        try:
            self._hydrate(self._seed)
            self._goto(self._entry_url)
            for step in checkpoint['recipe']:
                if self._identify(self._snapshot()).id != self._live(step['source']):
                    return False
                if not self._execute_saved(step):
                    return False
            if self._matches(state, checkpoint, verify_storage=False):
                self._metrics['recipe_restores'] += 1
                self._active_recipe = list(checkpoint['recipe'])
                return True
        except Exception:
            pass
        return False

    def close(self):
        if self._owned_context:
            self._owned_context.close()
            self._owned_context = None
        self.page = self._original_page

    def _save_checkpoint(self):
        if not self.config.checkpoint_path:
            return
        self._refresh_metrics(self._stop_reason)
        graph = self.graph.to_dict()
        directory = Path(str(self.config.checkpoint_path)+'.data')
        directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        os.chmod(directory,0o700)
        nodes = []
        for state in graph['states']:
            sid = state['id']
            visits = state.pop('visits',0)
            reference = self._node_refs.get(sid)
            if reference is None:
                node = {'state':state,'checkpoint':self._checkpoints.get(sid),
                        'readable':self.readables.get(sid)}
                reference = hashlib.sha256(json.dumps(node,sort_keys=True).encode()).hexdigest()
                destination = directory/(reference+'.json')
                if not destination.exists():
                    atomic_json(destination,node)
                self._node_refs[sid] = reference
            nodes.append({'id':sid,'visits':visits,'ref':reference})
        graph['states'] = nodes
        payload = {'version': VERSION, 'policy': self._policy(self._entry_path),
            'graph': graph, 'frontier': [asdict(i) for i in self._frontier],
            'active': asdict(self._active_item) if self._active_item else None,
            'queued': list(self._queued), 'attempts': self._attempts,
            'actions_fired': self._actions_fired, 'restore_failures': self._restore_failures,
            'seed': self._seed,
            'metrics': self._metrics, 'task_results': self._task_results,
            'observed_sources': {k: list(v) for k,v in self._observed_sources.items()},
            'observed_targets': {k: list(v) for k,v in self._observed_targets.items()},
            'global': self._global, 'rekeyed': self._rekeyed}
        payload['choice_groups'] = list(self._choice_groups)
        payload['unsupported_frames'] = self._unsupported_frames
        payload['graph']['volatile'] = {k:list(v) for k,v in self._reader.volatile.items()}
        atomic_json(self.config.checkpoint_path, payload)

    def _load_checkpoint(self):
        from discovery.crawler import _FrontierItem
        from discovery.graph import AppGraph
        from collections import deque
        data = json.loads(Path(self.config.checkpoint_path).read_text())
        # JSON canonicalization normalizes tuples/lists without changing policy.
        if data.get('version') != VERSION or data.get('policy') != json.loads(json.dumps(self._policy(self._entry_path))):
            raise ValueError('crawl checkpoint version, target, entry, credentials, or exploration policy differs')
        states = []
        directory = Path(str(self.config.checkpoint_path)+'.data')
        import re
        for entry in data['graph']['states']:
            reference = entry['ref']
            if not re.fullmatch('[0-9a-f]{64}',reference):
                raise ValueError('invalid crawl snapshot reference')
            node = json.loads((directory/(reference+'.json')).read_text())
            if hashlib.sha256(json.dumps(node,sort_keys=True).encode()).hexdigest()!=reference:
                raise ValueError('crawl snapshot integrity check failed')
            if node['state']['id']!=entry['id']:
                raise ValueError('crawl snapshot identity differs')
            states.append({**node['state'],'visits':entry['visits']})
            if node['checkpoint'] is not None:
                self._checkpoints[entry['id']] = node['checkpoint']
            if node['readable'] is not None:
                self.readables[entry['id']] = node['readable']
            self._node_refs[entry['id']] = reference
        data['graph']['states'] = states
        self.graph = AppGraph.from_dict(data['graph'])
        self._elapsed_before = self.graph.metrics.get('elapsed_seconds',0)
        self._frontier = deque(_FrontierItem(**i) for i in data['frontier'])
        if data['active']:
            self._frontier.appendleft(_FrontierItem(**data['active']))
        self._queued = {tuple(i) for i in data['queued']}
        self._attempts = data['attempts']
        self._actions_fired = data['actions_fired']
        self._restore_failures = data['restore_failures']
        self._seed = data['seed']
        self._metrics = data['metrics']
        self._task_results = data['task_results']
        self._observed_sources = {k:set(v) for k,v in data['observed_sources'].items()}
        self._observed_targets = {k:set(v) for k,v in data['observed_targets'].items()}
        self._global = data['global']
        self._rekeyed = data['rekeyed']
        self._choice_groups = {tuple(i) for i in data['choice_groups']}
        self._unsupported_frames = data.get('unsupported_frames',{})
        self._reader.volatile = {k:set(v) for k,v in self.graph.volatile.items()}
        existing = {(i.state_id,i.affordance_key,i.kind,i.value) for i in self._frontier}
        for task in self._task_results.values():
            key = (task['state'],task['action'],task['kind'],task['value'])
            if (task['status']=='depth_limited' and key not in existing
                    and (not self.config.max_depth or task.get('depth',0)<=self.config.max_depth)):
                self._frontier.append(_FrontierItem(task['state'],task['action'],task.get('depth',0),task['kind'],task['value']))
                self._queued.add(key)
                existing.add(key)
        if self.config.retry_failed:
            self._restore_failures = {}
            for task in self._task_results.values():
                key = (task['state'],task['action'],task['kind'],task['value'])
                if task['status'] in ('restoration_failed','suppressed','action_failed') and key not in existing:
                    self._frontier.append(_FrontierItem(task['state'],task['action'],task.get('depth',0),task['kind'],task['value']))
                    existing.add(key)
