"""Read AI-requested evidence at verified points in the frozen flow walk."""
from discovery.fingerprint import fingerprint, url_template
from discovery.page_inspector import inspect_with_page
from discovery.policy import identity_snapshot
from knowledge.flows import BindingSpec
from knowledge.contracts import UIAssertion
from runner.contract_checks import assert_ui
from runner.persistence_runner import StepError


def capture(ctx,phase,index):
    from runner.flow_runner import read_binding
    for point in ctx.instance.observation_points:
        if (point['phase'],point['after_step']) != (phase,index):
            continue
        binding = point['binding']
        name = binding['name']
        try:
            assert_ui(ctx.page,[UIAssertion.model_validate(c) for c in point['ready']],ctx.timeout_ms)
            if phase!='reload':
                snapshot = inspect_with_page(ctx.page)
                policy = ctx.instance.identity_policy
                snapshot = identity_snapshot(snapshot,policy.get('id_segment_patterns',[]),policy.get('input_states',False))
                template = url_template(snapshot.get('identity_url',snapshot.get('url','')),strip_prefix=ctx.instance.route_prefix)
                actual = fingerprint(snapshot,strip_prefix=ctx.instance.route_prefix,
                    ignore=frozenset(policy.get('volatile',{}).get(template,[])))
                expected = policy.get('state_aliases',{}).get(point['state_id'],point['state_id'])
                if actual.id!=expected:
                    raise StepError(f'observation state differs: expected {expected}, got {actual.id}')
            value = read_binding(ctx.page,BindingSpec(name,binding['selector'],binding['extract'],binding.get('frame')),ctx.timeout_ms)
            ctx.observations[name] = value
            ctx.scratch.setdefault('observation_evidence',[]).append({
                'name':name,'phase':phase,'after_step':index,'state_id':point['state_id'],
                'url':ctx.page.url,'selector':binding['selector'],'value':value})
        except Exception as exc:
            ctx.scratch.setdefault('observation_errors',[]).append({'name':name,'phase':phase,'after_step':index,'detail':str(exc)})


def reload_observations(ctx):
    if not any(p['phase']=='reload' for p in ctx.instance.observation_points):
        return
    ctx.scratch['phase'] = 'verification'
    try:
        ctx.page.reload(timeout=ctx.timeout_ms,wait_until='domcontentloaded')
        if ctx.settle_ms:
            ctx.page.wait_for_timeout(ctx.settle_ms)
        capture(ctx,'reload',0)
    except Exception as exc:
        ctx.scratch.setdefault('observation_errors',[]).append({'phase':'reload','detail':str(exc)})
