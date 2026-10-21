"""Acceptance tests for corrected/broken builds, execution health and crawl budgets."""
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from playwright.sync_api import sync_playwright
from baseline import loader, saver
from baseline.record_schema import BaselineRecord, TestRecord
from benchmarks.regression import materialize, execute
from demo_app.regression_fixture import server
from discovery.crawler import Crawler, CrawlConfig
from discovery.fingerprint import fingerprint
from discovery.policy import identity_snapshot, navigation_key, priority
from knowledge.contracts import BehaviorContract
from knowledge.engine import RELATION_FUNCTIONS
from knowledge.flows import BindingSpec, PathStep
from regression.differ import diff
from regression.findings import Direction
from regression.results import comparable
from runner.flow_runner import _act, read_binding
from runner.persistence_runner import StepError


class PredicateTests(unittest.TestCase):
    def test_numeric_validity_is_not_arithmetic_consistency(self):
        for value in (['NaN'], ['Infinity'], [], [True], ['not a number']):
            self.assertFalse(RELATION_FUNCTIONS['finite'](value))
        self.assertTrue(RELATION_FUNCTIONS['finite'](['$600.00', '$0', '$90']))
        self.assertFalse(RELATION_FUNCTIONS['nonnegative'](['$-600']))
        self.assertFalse(RELATION_FUNCTIONS['positive_integers']('0'))
        self.assertFalse(RELATION_FUNCTIONS['positive_integers']('1.5'))
        self.assertTrue(RELATION_FUNCTIONS['positive_integers']('16'))

    def test_id_grouping_is_explicit_and_preserves_real_navigation(self):
        a = {'url': 'https://example.test/order/o4', 'elements': []}
        b = {**a, 'url': 'https://example.test/order/o5'}
        self.assertNotEqual(fingerprint(a).id, fingerprint(b).id)
        grouped = identity_snapshot(a, [r'o\d+'])
        self.assertEqual(grouped['url'], a['url'])
        self.assertEqual(fingerprint(grouped).id, fingerprint(identity_snapshot(b, [r'o\d+'])).id)

    def test_contract_and_rule_changes_are_not_product_regressions(self):
        base = TestRecord('a', 'flow', 'test', {'expected_relation': 'a == 1', 'rule_version': '1'})
        changed = replace(base, instance={**base.instance, 'rule_version': '2'})
        self.assertFalse(comparable(base, changed))
        changed = replace(base, provenance={'evaluator_version': 'new'})
        self.assertFalse(comparable(base, changed))

    def test_contract_checks_validate_all_operands(self):
        with self.assertRaises(ValueError):
            BehaviorContract(capability='test', invariant='test', expected_relation='a == b',
                checks=[{'id': 'amount', 'relation': 'finite(amounts) == true', 'bindings': []}])

    def test_global_navigation_never_crosses_auth_context_or_infers_buttons(self):
        aff = SimpleNamespace(kind='link', key='cart', href='/cart')
        anonymous = SimpleNamespace(snapshot={'elements': [{'href': '/signin'}]})
        signed_in = SimpleNamespace(snapshot={'elements': [{'text': 'Sign out'}]})
        self.assertNotEqual(navigation_key(aff, anonymous), navigation_key(aff, signed_in))
        self.assertIsNone(navigation_key(replace_aff(aff, kind='button'), signed_in))


def replace_aff(aff, **changes):
    return SimpleNamespace(**{**vars(aff), **changes})


class BrowserRegressionV2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = server()
        cls.thread = threading.Thread(target=cls.app.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.app.server_port}'
        cls.p = sync_playwright().start()
        cls.browser = cls.p.chromium.launch()
        page = cls.browser.new_page()
        cls.graph, cls.instances = materialize(page, cls.base)
        page.close()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.p.stop()
        cls.app.shutdown(); cls.app.server_close(); cls.thread.join()

    def setUp(self):
        self.page = self.browser.new_page()

    def tearDown(self):
        self.page.context.close()

    def run_build(self, target):
        return execute(self.page, self.instances, target, self.base)

    def compare(self, a, b):
        return diff(saver.build(graph=self.graph, tests=a), saver.build(graph=self.graph, tests=b),
                    include_structure=False, include_ui=False)

    def test_correct_build_repeatable_and_all_four_defect_families_detected(self):
        good = self.run_build(self.base)
        repeated = self.run_build(self.base)
        broken = self.run_build(self.base + '/broken')
        self.assertTrue(all(t.expectation_status == 'holds' for t in good), [t.to_dict() for t in good])
        self.assertEqual(len(self.compare(good, repeated)), 0)
        self.assertTrue(all(t.expectation_status == 'violated' for t in broken), [t.to_dict() for t in broken])
        findings = self.compare(good, broken)
        affected = {f.subject.split('/')[0] for f in findings.regressions}
        self.assertEqual(affected, {i.test_id for i in self.instances})
        runtime = next(t for t in broken if t.test_id == 'runtime')
        self.assertEqual(runtime.status, 'holds')  # reached route; independent error check fails
        self.assertTrue(runtime.evidence['notes']['runtime_errors'])
        self.assertEqual(runtime.execution['status'], 'completed')
        self.assertTrue(self.compare(broken, broken).of_direction(Direction.EXISTING))

    def test_missing_selector_is_execution_failure_not_violation(self):
        from runner.flow_runner import FlowRunner
        from knowledge.flows import evaluate_instance
        i = replace(self.instances[0], goal=(PathStep('click', 'Missing', {'selector': '#missing'}),))
        result = FlowRunner(self.page, timeout_ms=100, settle_ms=0).run(i)
        record = saver.flow_test_record(i, result, evaluate_instance(i, result))
        self.assertEqual(record.status, 'inconclusive')
        self.assertEqual(record.execution['status'], 'failed')
        self.assertNotEqual(record.expectation_status, 'violated')
        runtime = next(o for o in record.outcomes if o['id'] == 'RUNTIME_ERRORS')
        self.assertEqual(runtime['status'], 'inconclusive')

    def test_frames_selects_checks_and_ambiguous_locator(self):
        self.page.goto(self.base + '/frame')
        ctx = SimpleNamespace(page=self.page, timeout_ms=1000, settle_ms=0, highlight_ms=0)
        _act(ctx, PathStep('check', 'Choice', {'selector': '#choice', 'frame': '#embedded'}, True))
        _act(ctx, PathStep('select', 'Pick', {'selector': '#pick', 'frame': '#embedded'}, 'b'))
        self.assertTrue(read_binding(self.page, BindingSpec('chosen', '#choice', 'checked', '#embedded'), 1000))
        self.assertEqual(read_binding(self.page, BindingSpec('pick', '#pick', 'selected', '#embedded'), 1000), 'b')
        self.page.goto(self.base + '/ambiguous')
        with self.assertRaises(StepError):
            _act(ctx, PathStep('click', 'Continue', {'selector': 'button'}))

    def test_invalid_numeric_text_is_evidence_not_a_zero(self):
        self.page.goto(self.base + '/broken/quantity?nan')
        self.assertFalse(read_binding(self.page, BindingSpec('valid', '.amount', 'numeric_valid'), 1000))
        with self.assertRaises(StepError):
            read_binding(self.page, BindingSpec('amount', '.amount', 'number'), 1000)

    def test_crawl_counts_failed_attempts_and_suppresses_reconstruction(self):
        class CannotRestore(Crawler):
            def _establish(self, state):
                return False
        crawler = CannotRestore(self.page, self.base, CrawlConfig(identity_reads=1, settle_ms=0,
            max_attempts=2, max_seconds=10))
        graph = crawler.crawl()
        self.assertEqual(graph.metrics['stop_reason'], 'max_attempts')
        self.assertEqual(graph.metrics['attempted_actions'], 2)
        self.assertFalse(graph.is_closed())
        crawler = CannotRestore(self.page, self.base, CrawlConfig(identity_reads=1, settle_ms=0,
            max_attempts=0, max_seconds=10, restore_failure_limit=2))
        graph = crawler.crawl()
        self.assertEqual(graph.metrics['restore_failures'], 2)
        self.assertGreaterEqual(graph.metrics['suppressed_actions'], 2)
        self.assertFalse(graph.is_closed())

    def test_cli_replays_frozen_baseline_without_crawling_and_saves_evidence(self):
        records = self.run_build(self.base)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            saver.save(saver.build(graph=self.graph, tests=records), root / 'baseline')
            # /ambiguous would add a state if --entry were crawled. Replay ignores it.
            command = [sys.executable, 'main.py', '--regression', '--baseline-dir', str(root / 'baseline'),
                       '--entry', '/ambiguous', '--no-interpret', '--timeout', '1200',
                       '--runs-dir', str(root / 'runs')]
            good = subprocess.run(command + [self.base], capture_output=True, text=True)
            self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
            bad = subprocess.run(command + [self.base + '/broken'], capture_output=True, text=True)
            self.assertEqual(bad.returncode, 1, bad.stdout + bad.stderr)
            reports = sorted((root / 'runs' / 'regressions').glob('*.json'))
            report = json.loads(reports[-1].read_text())
            self.assertEqual(report['comparison_scope'], {'structure': False, 'ui': False})
            self.assertEqual(report['current']['counts']['states'], 0)
            self.assertEqual(len(report['test_comparisons']), 5)
            self.assertTrue(report['finding_groups'])
            evidence = report['results'][0]['evidence']['notes']
            self.assertTrue(Path(evidence['screenshot']).exists())
            self.assertTrue(Path(evidence['trace']).exists())


if __name__ == '__main__':
    unittest.main()
