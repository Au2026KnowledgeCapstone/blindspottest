"""Browser integration of frozen checkout expectations; no LLM or credentials."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from playwright.sync_api import sync_playwright
from discovery.fingerprint import fingerprint
from discovery.page_inspector import inspect_with_page
from runner.flow_runner import FlowRunner
from knowledge.flows import PathStep, evaluate_instance
from baseline.saver import flow_test_record
from baseline.loader import retarget_instance
from regression.findings import Direction
from test_behavior_regression import instance, compare


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        broken = self.path.startswith('/broken')
        path = self.path.removeprefix('/broken')
        prefix = '/broken' if broken else ''
        if path == '/api/order':
            body = b'{"total":249}' if broken else b'{"total":278}'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
        else:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            if path == '/cart':
                body = f'<h1>Cart</h1><a id="place" href="{prefix}/confirmation">Place order</a>'.encode()
            elif path == '/optional':
                body = f'<h1>Cart</h1><a id="place" href="{prefix}/upsell">Place order</a>'.encode()
            elif path == '/upsell':
                body = f'<h1>Optional offer</h1><a id="skip" href="{prefix}/confirmation">Skip offer</a>'.encode()
            else:
                total = 249 if broken else 278
                body = f'<h1>Order confirmed</h1><span class="line">249</span><span class="line">29</span><strong id="total">{total}</strong>'.encode()
        self.end_headers()
        self.wfile.write(body)


class BrowserContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.context = self.browser.new_context()
        self.page = self.context.new_page()

    def tearDown(self):
        self.context.close()

    def prepared(self, optional=False):
        i = instance(self.base, confirmed=True)
        entry = self.base + ('/optional' if optional else '/cart')
        self.page.goto(entry)
        setup = fingerprint(inspect_with_page(self.page)).id
        self.page.goto(self.base + '/confirmation')
        goal = fingerprint(inspect_with_page(self.page)).id
        steps = i.goal
        if optional:
            steps += (PathStep('click', 'Skip offer', {'selector': '#skip'}),)
        return replace(i, entry_url=entry, setup_state_id=setup, goal_state_id=goal, goal=steps)

    def run_record(self, i):
        result = FlowRunner(self.page, timeout_ms=400, settle_ms=0).run(i)
        verdict = evaluate_instance(i, result)
        return result, flow_test_record(i, result, verdict)

    def test_sound_broken_and_stale_path(self):
        i = self.prepared()
        result, sound = self.run_record(i)
        self.assertEqual(sound.status, 'holds', result.to_dict())
        moved = retarget_instance(i, self.base, self.base + '/broken')
        result, broken = self.run_record(moved)
        self.assertEqual(broken.status, 'violated', result.to_dict())
        self.assertEqual(compare(sound, broken).findings[0].direction, Direction.REGRESSED)
        stale = replace(moved, goal=(PathStep('click', 'Missing button', {'selector': '#gone'}),))
        result, failed = self.run_record(stale)
        self.assertEqual(failed.status, 'inconclusive')
        self.assertFalse(compare(sound, failed).regressions)

    def test_optional_upsell_valid(self):
        result, record = self.run_record(self.prepared(optional=True))
        self.assertEqual(record.status, 'holds', result.to_dict())

    def test_independent_api_check_and_transport_failure(self):
        i = self.prepared()
        config = {**i.contract, 'api_assertions': [{'path': '/api/order', 'json_path': ['total'], 'expected': 278, 'timeout_ms': 500}]}
        i = replace(i, contract=config)
        result, sound = self.run_record(i)
        self.assertEqual(sound.status, 'holds', result.to_dict())
        result, broken = self.run_record(retarget_instance(i, self.base, self.base + '/broken'))
        self.assertEqual(broken.observations.get('api_outcome'), False, result.to_dict())
        self.assertTrue(result.notes['api_evidence'])
        config = {**config, 'api_assertions': [{'path': '/api/order', 'json_path': ['missing'], 'expected': 278, 'timeout_ms': 100}]}
        result, failed = self.run_record(replace(i, contract=config))
        self.assertEqual(failed.status, 'inconclusive')

    def test_cleanup_failure_is_inconclusive(self):
        i = self.prepared()
        config = {**i.contract, 'cleanup': [{'kind': 'click', 'selector': '#missing'}],
                  'cleanup_assertions': [{'selector': '#missing', 'kind': 'visible'}]}
        result, failed = self.run_record(replace(i, contract=config))
        self.assertFalse(result.reset)
        self.assertEqual(failed.status, 'inconclusive')

    def test_cli_baseline_export_review_and_replay(self):
        i = self.prepared()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidates = root / "candidates.json"
            candidates.write_text(json.dumps({"candidates": [{
                "capability": i.capability, "setup_state_id": i.setup_state_id,
                "goal_state_id": i.goal_state_id,
                "bindings": [b.to_dict() for b in i.bindings], "probe": None,
                "applicability_confidence": 1, "reasoning_summary": "Known checkout fixture"
            }]}))
            contracts = root / "reviewed.json"
            contracts.write_text(json.dumps({i.test_id: i.contract}))
            baseline = root / "v1"
            common = [sys.executable, "main.py", "--entry", "/cart", "--runs-dir", str(root / "runs"), "--timeout", "1000"]
            recorded = subprocess.run(common + ["--baseline", "--baseline-dir", str(baseline),
                "--expectation-mode", "rules", "--flow-candidates", str(candidates), "--contracts", str(contracts),
                "--export-contracts", str(root / "export.json"), self.base], capture_output=True, text=True)
            self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)
            self.assertEqual(json.loads((root / "export.json").read_text())[i.test_id]['review_status'], 'confirmed')
            clean = subprocess.run(common + ["--regression", "--baseline-dir", str(baseline),
                "--no-interpret", self.base], capture_output=True, text=True)
            self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)
            replay = subprocess.run(common + ["--regression", "--baseline-dir", str(baseline),
                "--no-interpret", self.base + "/broken"], capture_output=True, text=True)
            self.assertEqual(replay.returncode, 1, replay.stdout + replay.stderr)
            report = json.loads(sorted((root / 'runs' / 'regressions').glob('*.json'))[-1].read_text())
            self.assertEqual(report['summary']['regressed'], 1, report)
            self.assertEqual(report['coverage']['executed_tests'], 1)
            self.assertTrue(report['suggestions'])

    def test_status_only_api_mismatch(self):
        i = self.prepared()
        config = {**i.contract, 'api_assertions': [{'path': '/api/order', 'status': 404, 'timeout_ms': 300}]}
        result, failed = self.run_record(replace(i, contract=config))
        self.assertEqual(failed.status, 'violated', result.to_dict())
        self.assertFalse(failed.observations['api_outcome'])
