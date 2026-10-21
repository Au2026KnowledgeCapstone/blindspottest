"""Controlled two-build benchmark. No remote site, LLM, accounts or payments.

python -m benchmarks.regression --output runs/regression-benchmark
"""
import argparse
import json
import threading
from pathlib import Path
from types import SimpleNamespace

from baseline import loader, saver
from demo_app.regression_fixture import server
from discovery.graph import AppGraph
from discovery.page_inspector import inspect_with_page
from knowledge.contracts import BehaviorContract
from knowledge.flows import BindingSpec, FlowEngine, FlowTestInstance, PathStep, evaluate_instance
from regression.differ import diff
from reporting.regression_report import build_regression_record, write_regression_record
from runner.flow_runner import FlowRunner, _act


def materialize(page, base):
    """Reviewed fixture contracts; expectations are specified by fixture source."""
    rules = FlowEngine.load()
    graph = AppGraph(base_url=base)
    result = []
    definitions = [
        ('quantity', '/cart', 'POSITIVE_QUANTITIES', [BindingSpec('quantities', '#quantity', 'value')]),
        ('quantity-nan', '/cart', 'POSITIVE_QUANTITIES', [BindingSpec('quantities', '#quantity', 'value')]),
        ('payment', '/payment', 'PAYMENT_SELECTION_CONSISTENCY', [BindingSpec('selected_method', 'input[name=method]', 'selected'), BindingSpec('reviewed_method', '#reviewed', 'text')]),
        ('runtime', '/anonymous', 'SAFE_RENDER', [BindingSpec('body_count', 'body', 'count')]),
        ('history', '/history-start', 'CONTROLLED_ERROR_HANDLING', [BindingSpec('rendered_messages', 'main', 'texts')]),
    ]
    for name, start, invariant, bindings in definitions:
        page.goto(base + start, wait_until='domcontentloaded')
        source = graph.observe(inspect_with_page(page))
        if not graph.entry:
            graph.entry = source.id
        selector = '#go'
        steps = []
        if name == 'payment':
            steps += [PathStep('select', 'Shipping', {'selector': '#shipping'}, 'express'),
                      PathStep('check', 'Terms', {'selector': '#terms'}, True),
                      PathStep('click', 'Stripe', {'selector': '#stripe'})]
        # Query variant is an explicit recorded link locator, not a mutated test input.
        if name == 'quantity-nan':
            # The entry query determines which link the fixture renders.
            page.goto(base + '/cart?nan', wait_until='domcontentloaded')
            source = graph.observe(inspect_with_page(page))
        steps.append(PathStep('click', 'Continue', {'selector': selector}))
        ctx = SimpleNamespace(page=page, timeout_ms=2000, settle_ms=0, highlight_ms=0)
        for step in steps:
            _act(ctx, step)
        goal = graph.observe(inspect_with_page(page))
        graph.unexplored[source.id] = list(source.affordances)
        relation = 'body_count == 1' if invariant == 'SAFE_RENDER' else rules.get(invariant).expected_relation.source
        checks = []
        if name.startswith('quantity'):
            checks = [
                {'id': 'FINITE_AMOUNTS', 'relation': 'finite(amounts) == true', 'bindings': [{'name': 'amounts', 'selector': '.amount', 'extract': 'texts'}]},
                {'id': 'NONNEGATIVE_AMOUNTS', 'relation': 'nonnegative(amounts) == true', 'bindings': [{'name': 'amounts', 'selector': '.amount', 'extract': 'texts'}]},
            ]
        contract = BehaviorContract(
            capability='access_control' if name in ('runtime', 'history') else 'multi_step_transaction',
            invariant=invariant, expected_relation=relation, precondition='goal_reached == true',
            review_status='confirmed', reviewed_by='controlled fixture specification',
            requirement_source='demo_app/regression_fixture.py', fixture_version='regression-fixtures-1',
            preconditions=[{'selector': '#go', 'kind': 'count', 'expected': 1}],
            goal_path='/admin' if name == 'runtime' else None,
            ready=[{'selector': 'body', 'kind': 'count', 'expected': 1}], checks=checks)
        result.append(FlowTestInstance(name, invariant, contract.capability, base,
            base + start + ('?nan' if name == 'quantity-nan' else ''), source.id, goal.id,
            (), tuple(steps), tuple(bindings), None, '', ('walk_setup', 'walk_goal', 'observe_bindings'),
            ('reset_session',), relation, 'goal_reached == true', contract=contract.model_dump(mode='json')))
    return graph, result


def execute(page, instances, target, original, artifacts=None):
    records = []
    runner = FlowRunner(page, timeout_ms=1200, settle_ms=0, artifacts_dir=artifacts)
    for instance in instances:
        aimed = loader.retarget_instance(instance, original, target)
        result = runner.run(aimed)
        verdict = evaluate_instance(aimed, result)
        records.append(saver.flow_test_record(aimed, result, verdict))
    return records


def run(output):
    from playwright.sync_api import sync_playwright
    output = Path(output)
    app = server()
    thread = threading.Thread(target=app.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{app.server_port}'
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                graph, instances = materialize(page, base)
                records = execute(page, instances, base, base, output / 'evidence' / 'baseline')
                baseline = saver.build(graph=graph, tests=records, model='reviewed local fixture contracts')
                saver.save(baseline, output / 'baseline')
                saved = loader.load(output / 'baseline')
                replay = [instance for _, instance in loader.instances(saved)]
                summaries = {}
                for label, target in [('working', base), ('broken', base + '/broken')]:
                    records = execute(page, replay, target, base, output / 'evidence' / label)
                    current = saver.build(graph=AppGraph(base_url=target), tests=records,
                        coverage={'structural_comparison': False, 'ui_comparison': False})
                    found = diff(saved, current, include_structure=False, include_ui=False)
                    record = build_regression_record(baseline_dir=output / 'baseline', url=target,
                        baseline=saved, current=current, found=found)
                    write_regression_record(record, output / label)
                    summaries[label] = {'findings': found.counts(),
                        'holds': sum(t.expectation_status == 'holds' for t in records),
                        'violated': sum(t.expectation_status == 'violated' for t in records),
                        'execution_failures': sum(t.execution['status'] == 'failed' for t in records)}
                assert summaries['working']['holds'] == len(instances), summaries
                assert summaries['working']['findings']['regressed'] == 0, summaries
                assert summaries['broken']['violated'] == len(instances), summaries
                (output / 'summary.json').write_text(json.dumps(summaries, indent=2) + '\n')
                print(json.dumps(summaries, indent=2))
            finally:
                browser.close()
    finally:
        app.shutdown(); app.server_close(); thread.join()
    return summaries


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=Path('runs/regression-benchmark'))
    args = parser.parse_args()
    run(args.output)
