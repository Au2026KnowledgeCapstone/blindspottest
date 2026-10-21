"""Walk an application exhaustively and record what it exposes as a graph.

The crawl is breadth-first over *affordances*, not URLs. A URL crawler follows
links and never learns that submitting the contact form lands on shipping;
that transition only exists if someone fills the form and presses the button.
Since the flows worth testing are exactly the ones behind buttons, the
frontier is a queue of `(state, affordance)` pairs and the crawl is finished
only when every pair has been fired.

Exhaustiveness is the point. A partial crawl cannot distinguish "the
application has no path to confirmation" from "I did not look", so a planner
built on it reports failures it cannot justify. `AppGraph.is_closed()` is the
claim this module exists to earn, and anything left in `unexplored` is a
retraction of it.

Getting back to a state
-----------------------
Firing an affordance leaves the state, so the next affordance of the same
state needs the application put back. Because the demo app keys all state to
a session cookie, clearing cookies is a complete reset — much cheaper than
restarting the process — and the state is then re-reached by replaying the
recorded path from the entry. Replay is verified rather than assumed: if the
page does not fingerprint as the state we meant to reach, the affordance is
recorded as unexplored instead of being fired somewhere unintended.

Pages that will not hold still
------------------------------
All of the above assumes a page has one identity. A page with an autoplaying
carousel does not — its fingerprint changes on a timer, so it is discovered
again as a new state every rotation, and every affordance on it is explored
once per phantom. `discovery.stability` owns the detection and the remedy;
this module routes every "where am I?" question through its `StateReader`
rather than calling `fingerprint` directly, so that the crawl and the graph
cannot end up disagreeing about which state the browser is standing in.

What this module does not do
----------------------------
No LLM, and no judgement about what anything means. It records that pressing
"Continue" in one state lands in another; whether that constitutes
CREATE_RESOURCE is a question for the semantic layer, working from the
projections in `discovery.projections`. Keeping labelling out of the crawl is
what makes a crawl cacheable and re-runnable for free.

WARNING: crawling fires every button it finds, including destructive ones.
Point this at a development application you can reset, never at anything
real. `CrawlConfig.deny_destructive` provides a text-matching guard, which is
a seatbelt and not a safety net.
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from discovery.fingerprint import Fingerprint
from discovery.graph import Affordance, Action, AppGraph, State
from discovery.page_inspector import inspect_with_page
from discovery.stability import DEFAULT_GAP_MS, DEFAULT_READS, StateReader
from runner.highlight import DEFAULT_DWELL_MS, caption_for, clear, highlight
from runner.persistence_runner import locate
from discovery.checkpoints import CheckpointMixin

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

# Labels that usually mean "this cannot be undone". Matched case-insensitively
# as whole words against an affordance's visible text.
DESTRUCTIVE_WORDS = frozenset({
    "delete", "remove", "destroy", "deactivate", "close account",
    "cancel subscription", "unsubscribe", "revoke", "purge", "wipe",
})

# Values used to fill a form before submitting it. Keyed by input type, then
# by a substring of the field's label. Deliberately boring: the crawl is
# mapping structure, and inventing interesting values is the test generator's
# job, not discovery's.
DEFAULT_VALUES: dict[str, str] = {
    "email": "blindspot@example.com",
    "password": "demo123",
    "tel": "5551234567",
    "number": "1",
    "date": "2026-01-01",
    "url": "https://example.com",
    "search": "a",
    "text": "Blindspot",
}


@dataclass
class CrawlConfig:
    """Budgets and safety rails.

    The budgets exist because crawl cost is O(edges x replay depth), not
    O(states): every affordance needs the application put back before it can
    be fired, so a graph twice as deep costs far more than twice as much.
    """

    max_states: int = 60
    max_actions: int = 400
    max_depth: int = 10
    max_attempts: int = 2000
    max_seconds: float = 120
    restore_failure_limit: int = 2
    id_segment_patterns: tuple[str, ...] = ()
    checkpoint_path: str | None = None
    resume: bool = False
    retry_failed: bool = False
    explore_inputs: bool = True
    progress_every: int = 10
    readiness: tuple[dict, ...] = ()
    on_progress: Callable[[str], None] | None = None
    timeout_ms: int = 8_000
    # How long to wait for a page to be ready after navigating.
    #
    # `networkidle` costs ~520ms per navigation because Playwright defines it
    # as 500ms of network silence — an unavoidable floor paid on every single
    # transition. `domcontentloaded` costs ~17ms. Across a crawl that is the
    # difference between two minutes and ten seconds, and it is the dominant
    # term in crawl time by a wide margin.
    #
    # `domcontentloaded` is correct for a server-rendered application. A
    # client-rendered one paints after its first XHR, so it needs either
    # `networkidle` or a non-zero `settle_ms` — otherwise the crawl snapshots
    # a skeleton and fingerprints every page identically.
    wait_until: str = "domcontentloaded"
    # Extra fixed grace after each transition, for async work that completes
    # without a navigation (an autosave fetch, a client-side re-render).
    settle_ms: int = 0
    # How many times a page is read before its identity is believed, and how
    # long to wait between those reads. One read disables the check and
    # restores the original behaviour.
    #
    # `settle_ms` waits for a page to finish arriving. This is for the page
    # that never finishes: an autoplaying carousel changes how many copies of
    # a link are on the page every rotation, so its fingerprint moves on a
    # timer and the crawl mints a fresh state for each one. No wait fixes
    # that, because there is nothing to wait for — see `discovery.stability`.
    #
    # Paid once per URL template, not per snapshot.
    identity_reads: int = DEFAULT_READS
    identity_gap_ms: int = DEFAULT_GAP_MS
    deny_destructive: bool = False
    # Paint a box over each control for this many ms before touching it, and
    # narrate the intent. Zero disables it entirely — no overlay is injected
    # and no extra round-trip is paid.
    #
    # `--headed --slow-mo` shows the pages but not which control moved
    # between them, which is the one thing you are watching for. Paired with
    # a visible browser this answers it; headless it only costs time.
    highlight_ms: int = 0
    # Follow links and nothing else: never fire a button, never submit a form,
    # never type into a field.
    #
    # This is the mode for an application you cannot restore. `deny_destructive`
    # is a text match on an English word list and misses "Archive", "Void",
    # "Send to all", and every label in another language — it is a seatbelt.
    # Read-only is a different claim: the crawl issues GETs and nothing else,
    # so it cannot write regardless of what any control is called.
    #
    # The cost is honest and unavoidable: everything behind a button is
    # invisible, so the graph is NOT closed and `is_closed()` says so. Every
    # unfired control is recorded in `unexplored`, which is exactly what stops
    # a read-only map being mistaken for a complete one.
    #
    # It also cannot sign in — a login is a form submission — so a read-only
    # crawl sees what an anonymous visitor sees. Reaching an authenticated
    # region without writing needs a pre-authenticated browser session rather
    # than credentials.
    read_only: bool = False
    # Credentials to use when a password field is present. Without these an
    # authenticated region of the app is simply unreachable, and the crawl
    # will honestly report it as such rather than pretend it is not there.
    credentials: dict[str, str] = field(default_factory=dict)
    # Paths the crawl must not leave. Anything off-site is recorded as a link
    # and never followed.
    same_origin_only: bool = True
    # Route prefixes the crawl must not enter. The demo app serves a second,
    # deliberately broken build under /broken, and following the link to it
    # merges two applications into one graph — which both doubles the crawl
    # and destroys the comparison the two builds exist to support. They are
    # two crawls whose graphs get diffed, not one crawl.
    exclude_prefixes: tuple[str, ...] = ()
    # The inverse, and the one you want when the region under test is a
    # subtree rather than the whole site: everything outside this prefix is
    # out of region. Exclusion cannot express "stay under /broken", because
    # every route on the site starts with the sound build's "/".
    stay_under: str = ""
    # How many distinct states an affordance must behave identically in
    # before it is treated as site-wide navigation and stops being fired.
    # Two is too eager (a coincidence), and much above three wastes most of
    # the saving on a large app.
    global_after: int = 3
    on_event: Callable[[str], None] | None = None
    # Called with (page, state) the first time each state is reached, while
    # the browser is still on it.
    #
    # This exists because some states are only ever arrived at by POST. A
    # checkout confirmation has an `example_url`, but re-requesting it with a
    # GET does not render the confirmation — so anything a later stage needs
    # to read off that page has to be captured now, during the one visit
    # there will be. The flow layer's value bindings are exactly that.
    #
    # Deliberately a callback rather than a flag: the crawl stays a crawl and
    # has no opinion about what anyone wants from a page.
    on_state: Callable[[object, object], None] | None = None

    def __post_init__(self):
        if min(self.max_states,self.max_actions,self.max_attempts,self.max_depth,self.max_seconds,self.progress_every)<0:
            raise ValueError('crawl budgets must be nonnegative')
        if self.restore_failure_limit<1 or self.timeout_ms<1:
            raise ValueError('restoration retry limit and timeout must be positive')
        if self.resume and not self.checkpoint_path:
            raise ValueError('resuming requires a checkpoint path')
        if self.retry_failed and not self.resume:
            raise ValueError('retry_failed requires resume')


# --------------------------------------------------------------------------
# Crawler
# --------------------------------------------------------------------------


@dataclass
class _FrontierItem:
    state_id: str
    affordance_key: str
    depth: int
    kind: str = "click"
    value: str | bool | None = None


class Crawler(CheckpointMixin):
    """Drives a Playwright page to build an `AppGraph`."""

    def __init__(self, page, base_url: str, config: CrawlConfig | None = None):
        self.page = page
        self.base_url = base_url.rstrip("/")
        self.config = config or CrawlConfig()
        self._original_page = page
        self._owned_context = None
        self._checkpoints = {}
        self._node_refs = {}
        self._choice_groups = set()
        self._unsupported_frames = {}
        self._active_recipe = []
        self._active_item = None
        self._task_results = {}
        self.readables = {}
        self._stop_reason = None
        self._seed = None
        self._elapsed_before = 0
        self.graph = AppGraph(base_url=self.base_url)
        self._frontier: deque[_FrontierItem] = deque()
        self._queued: set[tuple[str, str]] = set()
        self._actions_fired = 0
        self._attempts = 0
        self._restore_failures = {}
        self._metrics = {"restore_ms": 0, "action_ms": 0, "restore_failures": 0, "suppressed_actions": 0,
                         "checkpoint_hits": 0, "recipe_restores": 0}
        for pattern in self.config.id_segment_patterns:
            re.compile(pattern)
        # Where each affordance key has been fired from, and where it landed.
        # An affordance that lands in the same place from many different
        # states is navigation chrome, and re-firing it teaches nothing.
        self._observed_targets: dict[str, set[str]] = {}
        self._observed_sources: dict[str, set[str]] = {}
        self._global: dict[str, str] = {}
        # Every state id that has been folded into another, and what it
        # became. Chased rather than composed, so a state merged twice
        # resolves in one pass.
        self._rekeyed: dict[str, str] = {}
        # Owns the answer to "what state is this?", including for pages that
        # give a different answer each time they are asked.
        self._reader = StateReader(
            reads=self.config.identity_reads,
            gap_ms=self.config.identity_gap_ms,
            on_event=self._say,
        )

    # -- logging -----------------------------------------------------------

    def _say(self, message: str) -> None:
        if self.config.on_event:
            self.config.on_event(message)

    def _note_state(self, state: State) -> None:
        """Offer a newly discovered state to the observer, if there is one.

        Only on the first visit, and never allowed to fail the crawl: an
        observer is a bystander, and a map is still worth having when
        something optional raised on one page.
        """
        self._remember(state)
        from urllib.parse import urlsplit
        origin = urlsplit(self.base_url).netloc
        self._unsupported_frames[state.id] = [f.url for f in self.page.frames
            if f != self.page.main_frame and urlsplit(f.url).netloc == origin]
        if state.id not in self.readables:
            from discovery.readable import readable_with_page
            self.readables[state.id] = readable_with_page(self.page)
        if self.config.on_state is None or state.visits != 1:
            return
        try:
            self.config.on_state(self.page, state)
        except Exception as exc:
            self._say(f"  [observer] {state.id}: {type(exc).__name__}: {exc}")

    # -- entry point -------------------------------------------------------

    def crawl(self, entry_path: str = "/") -> AppGraph:
        self._entry_path = entry_path
        self._entry_url = self.base_url + entry_path
        self._started = time.monotonic()
        self._run_attempts = self._attempts
        self._run_actions = self._actions_fired
        try:
            return self._crawl(entry_path)
        except KeyboardInterrupt:
            self._stop_reason = 'interrupted'
            self._save_checkpoint()
            raise

    def _crawl(self, entry_path: str) -> AppGraph:
        started = time.monotonic()
        stop_reason = None
        entry_url = self.base_url + entry_path

        if self.config.resume:
            self._load_checkpoint()
            self._run_attempts = self._attempts
            self._run_actions = self._actions_fired
            (self.config.on_progress or self._say)(f'resumed: {len(self.graph.states)} states, {len(self._frontier)} queued tasks')
        if not self.graph.entry:
            self._reset()
            self._seed = self._browser_state()
            self._goto(entry_url)
            entry_state = self._observe()
            self._note_state(entry_state)
            self.graph.entry = entry_state.id
            self._say(f"entry {entry_state.id} {entry_state.url_template}")
            self._enqueue(entry_state, depth=0)
        self._save_checkpoint()

        while self._frontier:
            if self.config.max_seconds and time.monotonic() - started >= self.config.max_seconds:
                stop_reason = "max_seconds"
                break
            if self.config.max_attempts and self._attempts - self._run_attempts >= self.config.max_attempts:
                stop_reason = "max_attempts"
                break
            if self.config.max_states and len(self.graph.states) >= self.config.max_states:
                stop_reason = "max_states"
                self._say(f"stopping: max_states ({self.config.max_states}) reached")
                break
            if self.config.max_actions and self._actions_fired - self._run_actions >= self.config.max_actions:
                stop_reason = "max_actions"
                self._say(f"stopping: max_actions ({self.config.max_actions}) reached")
                break
            from discovery.policy import priority
            self._frontier = deque(sorted(self._frontier, key=lambda item: priority(item, self.graph)))
            self._attempts += 1
            self._active_item = self._frontier.popleft()
            self._save_checkpoint()
            try:
                self._explore(self._active_item)
            except Exception as exc:
                self._record_task(self._active_item, 'action_failed', str(exc))
                self._mark_unexplored(self._active_item.state_id, self._active_item.affordance_key)
            self._active_item = None
            self._save_checkpoint()
            if self.config.progress_every and self._attempts % self.config.progress_every == 0:
                self._refresh_metrics(None)
                progress = self.config.on_progress or self._say
                progress(f'progress: {len(self.graph.states)} states, {len(self._frontier)} queued, '
                          f'{self._attempts} attempts; restore={self._metrics["restore_ms"]}ms, '
                          f'action={self._metrics["action_ms"]}ms')

        # Anything still queued when a budget ran out is unexplored, and has
        # to be recorded as such — a graph that silently drops frontier items
        # would claim closure it has not earned.
        for item in self._frontier:
            self._mark_unexplored(item.state_id, item.affordance_key)

        self._refresh_metrics(stop_reason)
        self._stop_reason = stop_reason or ('unresolved_actions' if not self.graph.is_closed() else 'exhausted')
        self._refresh_metrics(self._stop_reason)
        self.graph.volatile = {
            template: sorted(signatures)
            for template, signatures in self._reader.volatile.items()
        }
        self._save_checkpoint()

        self._say(
            f"done: {len(self.graph.states)} states, {len(self.graph.actions)} actions, "
            f"{'closed' if self.graph.is_closed() else 'OPEN (frontier not exhausted)'}"
        )
        if self.graph.volatile:
            self._say(
                f"note: {len(self.graph.volatile)} page(s) render on a timer and have "
                f"no stable identity of their own: "
                f"{', '.join(sorted(self.graph.volatile))}"
            )
        return self.graph

    def _refresh_metrics(self, reason):
        from collections import Counter
        self.graph.metrics = {**self._metrics, 'attempted_actions': self._attempts,
            'fired_actions': self._actions_fired, 'elapsed_seconds': round(self._elapsed_before+time.monotonic()-self._started,3),
            'stop_reason': reason, 'queued_actions': len(self._frontier) + bool(self._active_item),
            'walked_transitions': sum(not a.inferred for a in self.graph.actions),
            'inferred_transitions': sum(a.inferred for a in self.graph.actions),
            'task_statuses': dict(Counter(t['status'] for t in self._task_results.values())),
            'unresolved_actions': sum(map(len,self.graph.unexplored.values())),
            'id_segment_patterns': list(self.config.id_segment_patterns)}
        self.graph.metrics['input_states'] = self.config.explore_inputs
        self.graph.metrics['unsupported_frames'] = sum(map(len,self._unsupported_frames.values()))

    def _record_task(self, item, status, detail=None):
        item.state_id = self._live(item.state_id)
        key = repr((item.state_id,item.affordance_key,item.kind,item.value))
        self._task_results[key] = {'state':item.state_id,'action':item.affordance_key,
            'kind':item.kind,'value':item.value,'status':status,'detail':detail}
        self._task_results[key]['depth'] = item.depth
        if status in ('executed', 'inferred'):
            failed = any(t['state']==item.state_id and t['action']==item.affordance_key
                         and t['status'] not in ('executed','inferred') for t in self._task_results.values())
            if not failed and item.affordance_key in self.graph.unexplored.get(item.state_id, []):
                self.graph.unexplored[item.state_id].remove(item.affordance_key)

    # -- the loop ----------------------------------------------------------

    def _explore(self, item: _FrontierItem) -> None:
        state = self.graph.states.get(item.state_id)
        if state is None:
            return
        affordance = state.affordances.get(item.affordance_key)
        if affordance is None:
            return
        if self.config.max_depth and item.depth>self.config.max_depth:
            self._record_task(item,'depth_limited','depth budget')
            self._mark_unexplored(state.id,affordance.key)
            return

        # Checked here as well as at enqueue time. `_enqueue` keeps buttons out
        # of the frontier, but an edge inferred as site-wide navigation or a
        # frontier item queued before a config change could still arrive here,
        # and a read-only guarantee that depends on one code path holding is
        # not a guarantee.
        if self.config.read_only and affordance.kind != "link":
            self._record_task(item, 'unsupported', 'read-only policy')
            self._say(f"  skip (read-only, would submit): {affordance.label}")
            self._mark_unexplored(state.id, affordance.key)
            return

        if self._is_destructive(affordance):
            self._record_task(item, 'unsupported', 'destructive-action policy')
            self._say(f"  skip (destructive): {affordance.label}")
            self._mark_unexplored(state.id, affordance.key)
            return

        # Site-wide navigation: already proven to behave identically from
        # enough states that firing it again is a browser round-trip spent
        # re-learning a known fact. Record the edge and move on.
        from discovery.policy import navigation_key
        nav_key = navigation_key(affordance, state) if item.kind == 'click' else None
        known_target = self._global.get(nav_key) if nav_key else None
        if known_target is not None and known_target != state.id:
            self.graph.connect(Action(
                source=state.id,
                target=known_target,
                affordance_key=affordance.key,
                label=affordance.label,
                effects=("site-wide navigation",),
                inferred=True,
            ))
            self._record_task(item, 'inferred')
            return

        if self._restore_failures.get(state.id, 0) >= self.config.restore_failure_limit:
            self._record_task(item, 'suppressed', 'restoration retry limit')
            self._mark_unexplored(state.id, affordance.key)
            self._metrics["suppressed_actions"] += 1
            return
        restore_started = time.monotonic()
        established = self._establish(state)
        self._metrics["restore_ms"] += int((time.monotonic() - restore_started) * 1000)
        if not established:
            self._record_task(item, 'restoration_failed')
            self._restore_failures[state.id] = self._restore_failures.get(state.id, 0) + 1
            self._metrics["restore_failures"] += 1
            self._say(f"  could not return to {state.id}; leaving {affordance.label} unexplored")
            self._mark_unexplored(state.id, affordance.key)
            return

        inputs = self._fill_form(state, affordance) if item.kind == 'click' else {}

        before = self._identify(inspect_with_page(self.page))
        action_started = time.monotonic()
        step = {'source':state.id, 'affordance':affordance.to_dict(),
                'inputs':list(inputs.items()), 'kind':item.kind, 'value':item.value}
        fired = self._perform(affordance, item.kind, item.value)
        self._metrics["action_ms"] += int((time.monotonic() - action_started) * 1000)
        if not fired:
            self._record_task(item, 'action_failed')
            self._mark_unexplored(state.id, affordance.key)
            return
        self._actions_fired += 1
        self._active_recipe = list(self._checkpoints.get(state.id, {}).get('recipe', self._active_recipe)) + [step]

        after_snapshot, after = self._read()

        # `_read` can discover that a page renders on a timer and fold the
        # states already minted for it together, which may be where this
        # edge started. Resolved rather than trusted, because an edge from a
        # state that no longer exists is invisible to every later query.
        source_id = self._live(state.id)

        # A link's href can be checked before clicking; a button's destination
        # cannot. If firing one left the region under test, record where it
        # went but do not explore onward from there.
        if not self._is_same_origin(after_snapshot.get("url")) or self._is_excluded(after_snapshot.get("url")):
            self._record_task(item, 'outside_region')
            self._say(f"  {affordance.label!r} -> outside region, not followed")
            self._mark_out_of_region(source_id, affordance.key)
            return

        target = self.graph.observe(after_snapshot, after)
        self._note_state(target)

        effects = self._effects(before, after)
        self.graph.connect(Action(
            source=source_id,
            target=target.id,
            affordance_key=affordance.key,
            label=affordance.label,
            inputs=tuple(sorted(inputs.items())),
            effects=effects,
            kind=item.kind,
            value=item.value,
        ))
        self._record_task(item, 'executed')

        arrow = "self" if target.id == source_id else target.id
        self._say(f"  {affordance.label!r} -> {arrow} {target.url_template}")

        if nav_key:
            self._note_outcome(nav_key, source_id, target.id)

        if target.visits == 1:
            self._enqueue(target, depth=item.depth + 1)

    def _note_outcome(self, key: str, source: str, target: str) -> None:
        """Promote an affordance to site-wide navigation once it has earned it.

        The test is deliberately strict: the same affordance must have been
        fired from `global_after` distinct states and landed in exactly one
        place every time. "Continue" moves through checkout to a different
        step each time and so never qualifies, which is the point — only
        genuinely position-independent controls are skipped.
        """
        if key in self._global:
            return
        self._observed_sources.setdefault(key, set()).add(source)
        self._observed_targets.setdefault(key, set()).add(target)

        sources = self._observed_sources[key]
        targets = self._observed_targets[key]
        if len(sources) >= self.config.global_after and len(targets) == 1:
            destination = next(iter(targets))
            # A control that always lands where it was pressed is a no-op, not
            # navigation; leave it alone so genuine self-loops stay visible.
            if destination in sources and len(sources) == 1:
                return
            self._global[key] = destination
            self._say(f"  [chrome] {key.split('|')[-1]!r} is site-wide -> {destination}")

    def _enqueue(self, state: State, depth: int) -> None:
        """Queue every affordance of a newly discovered state.

        Fields are not queued: filling one is part of firing a submit, not a
        transition in its own right. Queuing them would double the frontier
        and record edges that loop back to the same state.
        """
        for key, affordance in state.affordances.items():
            if affordance.kind == "field":
                if self.config.explore_inputs and not self.config.read_only:
                    self._enqueue_choices(state, affordance, depth)
                else:
                    element = next((e for e in (state.snapshot or {}).get('elements',[]) if e.get('id')==affordance.element_id),{})
                    if element.get('tag')=='select' or element.get('type') in ('radio','checkbox'):
                        self._record_task(_FrontierItem(state.id,key,depth),'unsupported','input choices disabled by policy')
                        self._mark_unexplored(state.id,key)
                continue
            # Under read-only, anything that is not a link is recorded as
            # unexplored rather than silently dropped. That is what keeps
            # `is_closed()` false and makes the resulting map honest about
            # being partial.
            if self.config.read_only and affordance.kind != "link":
                self._mark_unexplored(state.id, key)
                continue
            if affordance.kind == "link" and not self._is_same_origin(affordance.href):
                self._mark_out_of_region(state.id, key)
                continue
            if self._is_excluded(affordance.href):
                self._mark_out_of_region(state.id, key)
                continue
            pair = (state.id, key, 'click', None)
            if pair in self._queued:
                continue
            self._queued.add(pair)
            self._frontier.append(_FrontierItem(state.id, key, depth))

    def _enqueue_choices(self, state, affordance, depth):
        element = next((e for e in (state.snapshot or {}).get('elements', []) if e.get('id')==affordance.element_id), {})
        kind = None
        if element.get('tag') == 'select':
            if element.get('multiple') or element.get('role')=='listbox':
                self._record_task(_FrontierItem(state.id,affordance.key,depth),'unsupported','multi-select combinations require an explicit fixture')
                self._mark_unexplored(state.id,affordance.key)
                return
            locator = self.page.locator(affordance.selector)
            values = locator.locator('option').evaluate_all('es=>es.filter(e=>!e.disabled).map(e=>e.value)')
            kind = 'select'
        elif element.get('type') in ('radio','checkbox'):
            values = [True] if element.get('type')=='radio' else [False, True]
            kind = 'check'
        else:
            if element.get('role') in ('switch','combobox','listbox','slider'):
                self._record_task(_FrontierItem(state.id,affordance.key,depth),'unsupported','custom widget requires an explicit fixture')
                self._mark_unexplored(state.id,affordance.key)
            return  # text inputs are filled as part of submit actions
        structural = dict(state.snapshot or {})
        structural.pop('input_state',None)
        group = (self._reader.identify(structural).id, affordance.key)
        if group in self._choice_groups:
            return
        self._choice_groups.add(group)
        for value in values:
            if (kind=='select' and value==element.get('value')) or (kind=='check' and value==element.get('checked')):
                continue
            pair = (state.id,affordance.key,kind,value)
            if pair not in self._queued:
                self._queued.add(pair)
                self._frontier.append(_FrontierItem(state.id,affordance.key,depth,kind,value))

    # -- getting into a state ---------------------------------------------

    def _establish(self, state: State) -> bool:
        """Put the application in `state`, verifying that it worked.

        Cheap path first: if the page already fingerprints as the state we
        want, nothing needs doing. That happens whenever an action lands
        somewhere with several affordances left to try.
        """
        if state.id in self._checkpoints:
            return self._restore_checkpoint(state)
        try:
            if self._identify(inspect_with_page(self.page)).id == state.id:
                return True
        except Exception:
            pass

        path = self.graph.path_to(state.id)
        if path is None:
            return False

        self._reset()
        self._goto(self.graph.states[self.graph.entry].example_url or self.base_url)

        for action in path:
            current = self._identify(inspect_with_page(self.page))
            source = self.graph.states.get(current.id)
            if source is None:
                return False
            affordance = source.affordances.get(action.affordance_key)
            if affordance is None:
                return False
            for name, value in action.inputs:
                self._fill_one(name, value)
            if not self._fire(affordance):
                return False

        return self._identify(self._snapshot()).id == state.id

    def _reset(self) -> None:
        """Return the application to its seed state.

        Two places a browser holds state the application put there, and both
        have to go. Cookies cover a server-side session. `localStorage` and
        `sessionStorage` cover a client-side one — a React app with its cart,
        its signed-in user, and in a mocked build its entire database, none
        of which a cookie clear touches. An application whose state lives
        anywhere else needs its own hook here — a fixture reload, a database
        snapshot — and the crawl is only as repeatable as that hook.

        Why this matters more than it looks: an incomplete reset does not
        fail, it drifts. `_establish` navigates to the entry, replays a
        recorded path, and verifies where it arrived — so a cart that
        survived the reset changes the pages along the way, the verification
        stops matching, and affordances are written off as unexplored. The
        crawl still finishes and still writes a graph; the graph is just no
        longer reproducible, which makes every regression diff against it
        noise.
        """
        try:
            self.page.context.clear_cookies()
        except Exception:
            pass
        self._clear_web_storage()

    def _clear_web_storage(self) -> None:
        """Empty localStorage and sessionStorage for the current origin.

        Storage is per-origin and only reachable from a page on it, so this
        runs against wherever the browser currently is rather than through
        the context. Failure is routine and not an error: `about:blank` has
        no origin to clear, and a page whose origin blocks storage access
        throws on the property itself — in both cases there is nothing
        there to carry over.
        """
        try:
            self.page.evaluate(
                "() => { try { localStorage.clear(); } catch (e) {} "
                "try { sessionStorage.clear(); } catch (e) {} }"
            )
        except Exception:
            pass

    def _goto(self, url: str) -> None:
        self.page.goto(url, timeout=self.config.timeout_ms,
                       wait_until=self.config.wait_until)
        self._settle()

    def _settle(self) -> None:
        """Wait for the page to be ready to read.

        Cheap by default and deliberately so — see `CrawlConfig.wait_until`.
        A failed wait is not an error: a click that triggers no navigation
        leaves the page already loaded, which is the common case for buttons.
        """
        try:
            self.page.wait_for_load_state(
                self.config.wait_until, timeout=self.config.timeout_ms
            )
        except Exception:
            pass
        if self.config.settle_ms:
            self.page.wait_for_timeout(self.config.settle_ms)
        if self.config.readiness:
            from knowledge.contracts import UIAssertion
            from runner.contract_checks import assert_ui
            assert_ui(self.page,[UIAssertion.model_validate(item) for item in self.config.readiness],self.config.timeout_ms)

    def _snapshot(self) -> dict:
        self._settle()
        return inspect_with_page(self.page)

    def _identify(self, snapshot: dict) -> Fingerprint:
        """Where the page is, by the cheap single-read rule.

        Every "am I where I think I am?" check in the crawl goes through
        here rather than calling `fingerprint` directly, because a page
        already known to render on a timer is identified by a reduced
        signature and a caller using the raw rule would disagree with the
        graph about which state it was standing in.
        """
        from discovery.policy import identity_snapshot
        return self._reader.identify(self._identity_snapshot(snapshot))

    def _identity_snapshot(self, snapshot):
        from discovery.policy import identity_snapshot
        return identity_snapshot(snapshot, self.config.id_segment_patterns, self.config.explore_inputs)

    def _read(self, snapshot: dict | None = None) -> tuple[dict, Fingerprint]:
        """Read the page, paying for certainty only when it is about to matter.

        The expensive multi-read check runs when this snapshot would create
        something — the first sight of a URL template, or a second identity
        for a template that already has a state. Those are exactly the
        moments a timer-driven page does its damage; everywhere else the
        single read is both correct and free.
        """
        if snapshot is None:
            snapshot = self._snapshot()
        snapshot = self._identity_snapshot(snapshot)
        if not self._reader.enabled:
            return snapshot, self._identify(snapshot)

        template = self._reader.template_of(snapshot)
        known = {s.url_template for s in self.graph.states.values()}
        print_id = self._identify(snapshot)
        novel = template not in known or print_id.id not in self.graph.states
        if not self._reader.should_settle(template, novel=novel):
            return snapshot, print_id

        before = self._reader.volatile.get(template)
        snapshot, print_id = self._reader.settle(
            lambda: self._identity_snapshot(inspect_with_page(self.page)),
            self.page.wait_for_timeout,
            snapshot,
        )
        # Compared rather than merely tested for presence: a template already
        # known to move can have its volatile set widened here, and that
        # changes the identity of every state recorded for it just as much as
        # the first discovery did.
        if self._reader.volatile.get(template) != before:
            self._absorb_volatility()
            print_id = self._identify(snapshot)
        return snapshot, print_id

    def _absorb_volatility(self) -> None:
        """Fold away states minted before the page was known to be unstable.

        Learning that a page renders on a timer retrospectively invalidates
        the identities already handed out for it. Re-deriving them collapses
        the duplicates, and the frontier has to be dragged along: an item
        naming a state that no longer exists is silently dropped by
        `_explore`, which would lose the affordance without recording it as
        unexplored — a hole in the map wearing a closure claim.
        """
        self.graph.volatile = {
            template: sorted(signatures)
            for template, signatures in self._reader.volatile.items()
        }
        rekeyed = self.graph.recanonicalise(self._identify)
        if not rekeyed:
            return

        self._frontier = deque(
            _FrontierItem(
                rekeyed.get(item.state_id, item.state_id),
                item.affordance_key,
                item.depth,
                item.kind,
                item.value,
            )
            for item in self._frontier
        )
        self._queued = {
            (rekeyed.get(pair[0], pair[0]), *pair[1:])
            for pair in self._queued
        }
        self._observed_sources = {
            key: {rekeyed.get(s, s) for s in sources}
            for key, sources in self._observed_sources.items()
        }
        self._observed_targets = {
            key: {rekeyed.get(t, t) for t in targets}
            for key, targets in self._observed_targets.items()
        }
        self._global = {
            key: rekeyed.get(target, target)
            for key, target in self._global.items()
        }
        self._rekeyed.update(rekeyed)
        for old,new in rekeyed.items():
            if old in self._checkpoints:
                self._checkpoints.setdefault(new,self._checkpoints[old])
            if old in self.readables:
                self.readables.setdefault(new,self.readables[old])
            if old in self._restore_failures:
                self._restore_failures[new] = max(self._restore_failures.get(new,0),self._restore_failures[old])
        merged = len(set(rekeyed) - set(rekeyed.values()))
        self._say(f"  [unstable] folded {merged} duplicate state(s) back together")

    def _live(self, state_id: str) -> str:
        """Where a state id ended up, if it has since been folded away."""
        seen: set[str] = set()
        while state_id in self._rekeyed and state_id not in seen:
            seen.add(state_id)
            state_id = self._rekeyed[state_id]
        return state_id

    def _observe(self) -> State:
        snapshot, print_id = self._read()
        return self.graph.observe(snapshot, print_id)

    # -- doing things ------------------------------------------------------

    def _preview(self, locator, caption: str, kind: str) -> None:
        """Announce what is about to be touched, on screen and in the trace."""
        if not self.config.highlight_ms:
            return
        self._say(f"  about to {caption}")
        highlight(self.page, locator, caption, kind=kind,
                  dwell_ms=self.config.highlight_ms)

    def _unpreview(self) -> None:
        """Take the overlay down before anything inspects the page.

        Not optional and not cosmetic: `_settle` is followed by a snapshot,
        and a stray element in it would change the state's fingerprint.
        """
        if self.config.highlight_ms:
            clear(self.page)

    def _fire(self, affordance: Affordance) -> bool:
        """Click an affordance. False when it could not be actioned at all."""
        try:
            locator = self._locator(affordance)
            locator.wait_for(state="visible", timeout=self.config.timeout_ms)
            self._preview(locator, caption_for("click", affordance.label), "click")
            locator.click(timeout=self.config.timeout_ms)
        except Exception as exc:
            self._say(f"  could not click {affordance.label!r}: {type(exc).__name__}")
            return False
        finally:
            self._unpreview()
        self._settle()
        return True

    def _locator(self, affordance):
        if affordance.selector:
            locator = self.page.locator(affordance.selector)
            if locator.count()==1:
                return locator
        from runner.locators import resolve
        return resolve(self.page, affordance, self.config.timeout_ms)

    def _perform(self, affordance, kind='click', value=None):
        if kind=='click':
            return self._fire(affordance)
        try:
            locator = self._locator(affordance)
            if kind=='select':
                locator.select_option(str(value),timeout=self.config.timeout_ms)
            elif kind=='check':
                if locator.is_checked(timeout=self.config.timeout_ms) != value:
                    locator.click(timeout=self.config.timeout_ms)
            else:
                return False
            self._settle()
            return True
        except Exception as exc:
            self._say(f'could not {kind} {affordance.label!r}: {exc}')
            return False

    def _execute_saved(self, step):
        source = self.graph.states.get(self._live(step['source']))
        if source is None:
            return False
        for key,value in step['inputs']:
            if not self._fill_one(key,value,source.affordances.get(key)):
                return False
        return self._perform(Affordance.from_dict(step['affordance']),step['kind'],step['value'])

    def _fill_form(self, state: State, affordance: Affordance) -> dict[str, str]:
        """Fill every field in the state before firing a submit.

        Submitting an empty required form lands on a validation error, which
        is a real state and worth recording — but if that is the *only* thing
        the crawl ever does with a form, everything behind it stays invisible.
        Filling first means the happy path gets discovered; the validation
        path is reachable later by an explicit empty-input variant.
        """
        if affordance.kind not in ("submit", "button"):
            return {}

        used: dict[str, str] = {}
        for key, field_affordance in state.affordances.items():
            if field_affordance.kind != "field":
                continue
            value = self._value_for(field_affordance)
            if value is None:
                continue
            if self._fill_one(key, value, affordance=field_affordance):
                used[key] = value
        return used

    def _fill_one(self, key: str, value: str, affordance: Affordance | None = None) -> bool:
        if affordance is None:
            state_id = self._identify(inspect_with_page(self.page)).id
            state = self.graph.states.get(state_id)
            affordance = state.affordances.get(key) if state else None
        if affordance is None:
            return False
        try:
            locator = locate(self.page, affordance)
            if locator.count() == 0:
                return False
            self._preview(
                locator, caption_for("fill", affordance.label, value), "fill"
            )
            locator.fill(value, timeout=self.config.timeout_ms)
            return True
        except Exception:
            return False
        finally:
            self._unpreview()

    def _value_for(self, affordance: Affordance) -> str | None:
        """Pick a value for a field from its type and label."""
        strategy = affordance.locator or {}
        label = re.sub(r"[^a-z0-9 ]", "", (affordance.label or "").lower())
        role = strategy.get("role", "")

        # Non-text controls are left alone. Selects already carry a default,
        # and toggling a checkbox is a state change worth its own edge rather
        # than a side effect of filling a form.
        if role in ("checkbox", "radio", "switch", "combobox", "listbox"):
            return None

        for name, value in self.config.credentials.items():
            if re.sub(r"[^a-z0-9 ]", "", name.lower()) in label:
                return value

        for hint, value in DEFAULT_VALUES.items():
            if hint in label:
                return value

        if "email" in label:
            return DEFAULT_VALUES["email"]
        if "post" in label or "zip" in label:
            return "SW1A 1AA"
        return DEFAULT_VALUES["text"]

    # -- guards ------------------------------------------------------------

    def _is_destructive(self, affordance: Affordance) -> bool:
        if not self.config.deny_destructive:
            return False
        text = (affordance.label or "").lower()
        return any(re.search(rf"\b{re.escape(word)}\b", text) for word in DESTRUCTIVE_WORDS)

    def _is_same_origin(self, href: str | None) -> bool:
        if not href or not self.config.same_origin_only:
            return True
        from urllib.parse import urljoin, urlsplit
        target=urlsplit(urljoin(self.base_url+'/',href))
        base=urlsplit(self.base_url)
        return (target.scheme,target.netloc)==(base.scheme,base.netloc)

    def _is_excluded(self, url: str | None) -> bool:
        """Whether a destination is outside the region being crawled."""
        if not url:
            return False
        path = url[len(self.base_url):] if url.startswith(self.base_url) else url
        if not path.startswith("/"):
            path = "/" + path
        bare = path.split("?")[0].split("#")[0]

        under = self.config.stay_under.rstrip("/")
        if under and not (bare == under or bare.startswith(under + "/")):
            return True

        return any(
            bare == prefix.rstrip("/") or bare.startswith(prefix.rstrip("/") + "/")
            for prefix in self.config.exclude_prefixes
        )

    def _mark_unexplored(self, state_id: str, key: str) -> None:
        self.graph.unexplored.setdefault(state_id, [])
        if key not in self.graph.unexplored[state_id]:
            self.graph.unexplored[state_id].append(key)

    def _mark_out_of_region(self, state_id: str, key: str) -> None:
        self.graph.out_of_region.setdefault(state_id, [])
        if key not in self.graph.out_of_region[state_id]:
            self.graph.out_of_region[state_id].append(key)

    @staticmethod
    def _effects(before, after) -> tuple[str, ...]:
        """The transition's effect, as a short diff.

        This is what an LLM is shown for an edge instead of two full
        snapshots — roughly fifty tokens against four thousand — and it is
        also the raw material for the `establishes` annotation a planner
        needs.
        """
        effects: list[str] = []
        if before.url_template != after.url_template:
            effects.append(f"url:{before.url_template}->{after.url_template}")

        before_set, after_set = set(before.signature), set(after.signature)
        for added in sorted(after_set - before_set)[:6]:
            effects.append(f"+{added}")
        for removed in sorted(before_set - after_set)[:6]:
            effects.append(f"-{removed}")
        if not effects:
            effects.append("no observable change")
        return tuple(effects)


# --------------------------------------------------------------------------
# Convenience entry point
# --------------------------------------------------------------------------


def crawl_app(
    base_url: str,
    entry_path: str = "/",
    *,
    headless: bool = True,
    slow_mo: int = 0,
    config: CrawlConfig | None = None,
) -> AppGraph:
    """Open a browser, crawl, and return the graph.

    `slow_mo` pauses before each browser action. It exists for watching a
    crawl with `headless=False`: Playwright fires actions faster than anyone
    can follow, so a visible browser without it shows a blur rather than a
    walk. It changes the pace of a crawl, never its result.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, slow_mo=slow_mo)
        try:
            page = browser.new_page()
            return Crawler(page, base_url, config).crawl(entry_path)
        finally:
            browser.close()


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(prog="blindspot-crawl")
    parser.add_argument("base_url", nargs="?", default="http://127.0.0.1:3000")
    parser.add_argument("--entry", default="/")
    # Not `runs/`: `reporting.dashboard` globs `runs/*.json` and reads every
    # hit as a persistence run record. A graph in there is picked up as a
    # malformed run.
    parser.add_argument("--out", default="runs/graphs/graph.json")
    parser.add_argument("--max-states", type=int, default=60)
    parser.add_argument("--max-actions", type=int, default=400)
    parser.add_argument('--max-depth',type=int,default=10)
    parser.add_argument('--max-attempts',type=int,default=2000)
    parser.add_argument('--crawl-seconds',type=float,default=120)
    parser.add_argument('--crawl-checkpoint')
    parser.add_argument('--resume-crawl')
    parser.add_argument('--retry-crawl-failures',action='store_true')
    parser.add_argument('--no-input-choices',action='store_true')
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--slow-mo", type=int, default=0, metavar="MS",
                        help="pause this many ms before each browser action; "
                             "pair with --headed to watch the crawl "
                             "(300-500 is readable)")
    parser.add_argument("--highlight", nargs="?", type=int, default=0,
                        const=DEFAULT_DWELL_MS, metavar="MS",
                        help=f"box each control and name it before clicking, "
                             f"for this long (default {DEFAULT_DWELL_MS}ms). "
                             f"Implies --headed: it is there to be watched")
    parser.add_argument("--safe", action="store_true",
                        help="skip affordances whose text looks destructive")
    parser.add_argument("--exclude", action="append", default=[],
                        metavar="PREFIX",
                        help="route prefix the crawl must not enter "
                             "(repeatable, e.g. --exclude /broken)")
    parser.add_argument("--stay-under", default="", metavar="PREFIX",
                        help="confine the crawl to this route subtree "
                             "(e.g. --stay-under /broken)")
    parser.add_argument("--repeat", type=int, default=1, metavar="N",
                        help="crawl N times and diff the graphs; any "
                             "difference is crawler instability, not a "
                             "change in the application")
    parser.add_argument("--slow", action="store_true",
                        help="wait for network idle after each transition "
                             "(~30x slower; needed for client-rendered apps)")
    parser.add_argument("--read-only", action="store_true",
                        help="follow links only — never fire a button, submit "
                             "a form, or type into a field. The map will be "
                             "incomplete and will say so")
    parser.add_argument("--credential", action="append", default=[],
                        metavar="LABEL=VALUE",
                        help="value to type into fields whose label contains "
                             "LABEL, e.g. --credential password=hunter2 "
                             "(repeatable)")
    parser.add_argument("--settle", type=int, default=0, metavar="MS",
                        help="wait this long after each transition; also "
                             "throttles the crawl, which a real host may need")
    parser.add_argument("--identity-reads", type=int, default=DEFAULT_READS,
                        metavar="N",
                        help=f"read a page N times before believing its "
                             f"identity (default {DEFAULT_READS}; 1 disables). "
                             f"Catches pages that render on a timer, which "
                             f"otherwise crawl as one state per rotation")
    parser.add_argument("--identity-gap", type=int, default=DEFAULT_GAP_MS,
                        metavar="MS",
                        help=f"gap between those reads (default "
                             f"{DEFAULT_GAP_MS}). The window they span must "
                             f"exceed the animation's period to see it move")
    args = parser.parse_args()
    if args.resume_crawl and (args.repeat!=1 or args.crawl_checkpoint):
        parser.error('--resume-crawl requires one run and cannot use --crawl-checkpoint')
    if args.retry_crawl_failures and not args.resume_crawl:
        parser.error('--retry-crawl-failures requires --resume-crawl')

    # Boxing controls in a browser nobody can see is pure cost, and asking
    # for the box is unambiguously asking to watch.
    if args.highlight:
        args.headed = True

    def parse_credentials(pairs: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for pair in pairs:
            if "=" not in pair:
                parser.error(
                    f"--credential expects LABEL=VALUE, got {pair!r}"
                )
            label, _, value = pair.partition("=")
            out[label.strip().lower()] = value
        return out

    credentials = parse_credentials(args.credential)

    def run_once() -> tuple[AppGraph, float]:
        started = time.time()
        graph = crawl_app(
            args.base_url,
            args.entry,
            headless=not args.headed,
            slow_mo=args.slow_mo,
            config=CrawlConfig(
                max_states=args.max_states,
                max_actions=args.max_actions,
                max_depth=args.max_depth,max_attempts=args.max_attempts,max_seconds=args.crawl_seconds,
                checkpoint_path=args.resume_crawl or args.crawl_checkpoint or args.out+'.crawl.json',
                resume=bool(args.resume_crawl),retry_failed=args.retry_crawl_failures,
                explore_inputs=not args.no_input_choices,
                deny_destructive=args.safe,
                exclude_prefixes=tuple(args.exclude),
                stay_under=args.stay_under,
                wait_until="networkidle" if args.slow else "domcontentloaded",
                settle_ms=args.settle,
                read_only=args.read_only,
                highlight_ms=args.highlight,
                identity_reads=args.identity_reads,
                identity_gap_ms=args.identity_gap,
                # No default credentials. Shipping demo/demo123 in the library
                # meant pointing the crawler at any real login form typed those
                # into it; the demo's own credentials belong in the demo's
                # invocation, not in the tool.
                credentials=credentials,
                on_event=(lambda m: print(m, flush=True)) if args.repeat == 1 else None,
            ),
        )
        return graph, time.time() - started

    graphs = []
    for attempt in range(max(1, args.repeat)):
        if args.repeat > 1:
            print(f"crawl {attempt + 1} of {args.repeat} ...", flush=True)
        graph, elapsed = run_once()
        graphs.append(graph)
        print(f"  {len(graph.states)} states, {len(graph.actions)} actions, "
              f"{'closed' if graph.is_closed() else 'OPEN'}, {elapsed:.1f}s",
              flush=True)

    path = graphs[0].save(args.out)
    print(f"\nwrote {path}")

    # Whole-graph reproducibility is per-state stability to the Nth power, so
    # instability that looks negligible on one page is fatal across an
    # application. A crawl that cannot reproduce itself cannot be used to
    # detect regressions, because every diff is indistinguishable from noise.
    if len(graphs) > 1:
        from discovery.graph import graph_diff

        print("\n--- stability ---")
        unstable = False
        for i in range(1, len(graphs)):
            report = graph_diff(graphs[0], graphs[i])
            if report["identical"]:
                print(f"crawl 1 vs {i + 1}: identical")
                continue
            unstable = True
            print(f"crawl 1 vs {i + 1}: DIFFERS")
            for field_name in ("states_added", "states_removed",
                               "edges_added", "edges_removed"):
                for entry in report[field_name]:
                    print(f"  {field_name[:-1].replace('_', ' '):14} {entry}")
        if unstable:
            raise SystemExit(1)
