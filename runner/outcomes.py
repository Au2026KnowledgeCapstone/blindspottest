"""Independent outcomes, including initial-load errors and explicit reload checks."""
import re
from knowledge.engine import Relation, decide
from knowledge.flows import BindingSpec


def collect(ctx, contract):
    from runner.flow_runner import read_binding
    from runner.contract_checks import assert_ui
    results = []

    def read_checks(checks, prefix='', failed=None):
        for check in checks:
            observations = {}
            error = failed
            if not ctx.observations.get('goal_reached'):
                error = 'goal was not verified; outcome bindings were not read'
            if not error:
                try:
                    for binding in check.bindings:
                        observations[binding.name] = read_binding(ctx.page, BindingSpec(
                            binding.name, binding.selector, binding.extract.value, binding.frame), ctx.timeout_ms)
                except Exception as exc:
                    error = str(exc)
            item = ({'status':'inconclusive','detail':error,'observations':observations} if error else
                    decide(check.id, Relation.parse(check.relation), None, observations,
                           on_violation={'severity':check.severity}).to_dict())
            results.append({'id':prefix+check.id,'version':check.version,'relation':check.relation,**item})

    read_checks(contract.checks)
    if contract.reload_checks:
        failure = None
        if ctx.observations.get('goal_reached'):
            ctx.scratch['phase'] = 'verification'
            try:
                ctx.page.reload(timeout=ctx.timeout_ms, wait_until='domcontentloaded')
                if ctx.settle_ms:
                    ctx.page.wait_for_timeout(ctx.settle_ms)
                assert_ui(ctx.page, contract.reload_ready, ctx.timeout_ms)
            except Exception as exc:
                failure = 'reload verification: ' + str(exc)
        read_checks(contract.reload_checks, 'RELOAD/', failure)

    errors = []
    for event in ctx.scratch.get('runtime_errors', []):
        matched = next((rule for rule in contract.runtime_error_rules
                        if event['phase'] in rule.phases and re.search(rule.pattern,event['message'])), None)
        if matched:
            event['record_only_reason'] = matched.reason
        elif event['phase'] in ('fixture','setup','goal','verification'):
            errors.append(event)
    if contract.runtime_errors == 'fail':
        observations = {'runtime_error_count':len(errors)}
        if not ctx.observations.get('goal_reached') and not errors:
            item = {'status':'inconclusive','detail':'goal was not verified','observations':observations}
        else:
            item = decide('RUNTIME_ERRORS',Relation.parse('runtime_error_count == 0'),None,
                          observations,on_violation={'severity':'high'}).to_dict()
        results.append({'id':'RUNTIME_ERRORS','version':'2','relation':'runtime_error_count == 0',**item})
    ctx.scratch['outcomes'] = results
