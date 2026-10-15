import json
import os
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from baseline.record_schema import BaselineRecord, TestRecord, BaselineError, check_version
from baseline.saver import save, flow_test_record
from baseline.loader import load, retarget_instance
from discovery.graph import AppGraph
from knowledge.contracts import BehaviorContract, APIAssertion, suggestions
from knowledge.flows import FlowTestInstance, PathStep, BindingSpec, evaluate_instance
from regression.differ import diff
from regression.findings import Direction, Finding, FindingSet, Kind, _DIRECTION_RANK
from reporting.regression_report import exit_code, build_regression_record


def contract(confirmed=False):
    return BehaviorContract(
        capability="multi_step_transaction", invariant="TOTAL_CONSISTENCY",
        expected_relation="sum(line_totals) == order_total",
        precondition="goal_reached == true",
        review_status="confirmed" if confirmed else "inferred",
        reviewed_by="test owner" if confirmed else None,
        requirement_source="checkout acceptance criteria" if confirmed else None,
        fixture_version="cart-v1" if confirmed else None,
        preconditions=[{"selector": "#place", "kind": "visible", "expected": True}],
        ready=[{"selector": "#total", "kind": "visible", "expected": True}],
        branches=[{"branch": "payment_failure", "explored": False,
                   "evidence": "Only successful payment was exercised",
                   "missing": "Retry not observed", "recommendation": "Test a declined payment"}],
    )


def instance(base="http://localhost", confirmed=False):
    c = contract(confirmed)
    return FlowTestInstance(
        test_id="flow-001", invariant=c.invariant, capability=c.capability,
        base_url=base, entry_url=base + "/cart", setup_state_id="setup", goal_state_id="goal",
        setup=(), goal=(PathStep("click", "Place order", {"selector": "#place"}),),
        bindings=(BindingSpec("line_totals", ".line", "numbers"), BindingSpec("order_total", "#total", "number")),
        probe_mode=None, probe_path="", procedure=("walk_setup", "walk_goal", "observe_bindings"),
        teardown=("reset_session",), expected_relation=c.expected_relation, precondition=c.precondition,
        contract=c.model_dump(mode="json"),
    )


def record(status, confirmed=False, error=None):
    i = instance(confirmed=confirmed)
    return TestRecord(i.test_id, "flow", i.invariant, i.to_dict(),
                      {"fixture_ready": True, "goal_reached": True}, {"status": status},
                      error=error, evidence={"cleanup_verified": True})


def compare(a, b):
    return diff(BaselineRecord("http://localhost", "", "", tests=[a]),
                BaselineRecord("http://localhost", "", "", tests=[b]),
                include_structure=False, include_ui=False)


class RegressionTests(unittest.TestCase):
    def test_confirmed_and_inferred_are_distinct(self):
        self.assertEqual(compare(record("holds", True), record("violated", True)).findings[0].direction, Direction.REGRESSED)
        self.assertEqual(compare(record("holds"), record("violated")).findings[0].direction, Direction.SUSPECTED)

    def test_legacy_is_never_confirmed(self):
        a, b = record("holds"), record("violated")
        a.instance.pop("contract"); b.instance.pop("contract")
        self.assertEqual(compare(a, b).findings[0].direction, Direction.SUSPECTED)
        check_version({"schema_version": "1.0"})

    def test_execution_and_existing_defects(self):
        found = compare(record("holds", True), record("inconclusive", True, "selector missing"))
        self.assertFalse(found.regressions)
        self.assertEqual(exit_code(found), 2)
        self.assertEqual(compare(record("violated"), record("violated")).findings[0].direction, Direction.EXISTING)

    def test_changed_contract_not_regression(self):
        a, b = record("holds", True), record("violated", True)
        b.instance["contract"]["fixture_version"] = "cart-v2"
        self.assertEqual(compare(a, b).findings[0].direction, Direction.CHANGED)

    def test_missing_fixture_evidence_not_confirmed(self):
        a, b = record("holds", True), record("violated", True)
        b.observations.pop("fixture_ready")
        self.assertEqual(compare(a, b).findings[0].direction, Direction.SUSPECTED)

    def test_saved_rule_and_verified_arrival(self):
        i = instance()
        result = SimpleNamespace(error=None, reset=True, observations={
            "line_totals": [249, 29], "order_total": 249, "goal_reached": True})
        self.assertEqual(evaluate_instance(i, result).status.value, "violated")
        result.observations["goal_reached"] = False
        self.assertEqual(evaluate_instance(i, result).status.value, "inconclusive")
        result.observations["order_total"] = 278
        self.assertEqual(evaluate_instance(i, result).status.value, "inconclusive")

    def test_contract_validation_and_suggestions(self):
        with self.assertRaises(ValueError):
            BehaviorContract(capability="checkout", invariant="total", expected_relation="a == b", review_status="confirmed")
        for path in ("https://evil.test/api", "//evil.test/api", "/\\evil.test"):
            with self.assertRaises(ValueError):
                APIAssertion(path=path, expected=True)
        self.assertEqual(suggestions(contract())[0]["status"], "not_observed")

    def test_atomic_immutable_baseline_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "v1"
            original = BaselineRecord("http://localhost", "", "today", tests=[record("holds", True)])
            save(original, target)
            with self.assertRaises(BaselineError):
                save(original, target)
            restored = load(target)
            self.assertEqual(restored.tests[0].to_dict(), original.tests[0].to_dict())
            moved = retarget_instance(FlowTestInstance.from_dict(restored.tests[0].instance), "http://localhost", "http://localhost/broken")
            self.assertEqual(moved.contract, original.tests[0].instance["contract"])
            self.assertEqual(moved.goal, instance().goal)

    def test_every_direction_ranks_distinctly_below_a_regression(self):
        ranks = [_DIRECTION_RANK[d] for d in Direction]
        self.assertEqual(len(set(ranks)), len(ranks), "shared ranks let the tiebreakers order the report")
        self.assertEqual(min(ranks), _DIRECTION_RANK[Direction.REGRESSED])
        # Subjects are named so that ordering by subject — the last
        # tiebreaker — would invert this if the directions tied.
        found = FindingSet()
        for direction, subject in ((Direction.FIXED, "a-fixed"),
                                   (Direction.CHANGED, "b-changed"),
                                   (Direction.SUSPECTED, "y-suspected"),
                                   (Direction.REGRESSED, "z-regressed")):
            found.add(Finding(Kind.VERDICT, direction, subject, direction.value))
        self.assertEqual([f.subject for f in found.ranked()],
                         ["z-regressed", "y-suspected", "b-changed", "a-fixed"])

    def test_empty_read_only_baseline_is_a_gap_not_a_failure(self):
        def compare_empty(coverage):
            empty = BaselineRecord("http://localhost", "", "", tests=[], coverage=coverage)
            return diff(empty, empty, include_structure=False, include_ui=False)

        wanted = compare_empty({"behavioral": True})
        self.assertEqual(wanted.findings[0].direction, Direction.EXECUTION)
        self.assertEqual(exit_code(wanted), 2)
        # A pre-1.1 baseline says nothing; silence has to read as "expected".
        self.assertEqual(compare_empty({}).findings[0].direction, Direction.EXECUTION)

        read_only = compare_empty({"behavioral": False})
        self.assertEqual(read_only.findings[0].direction, Direction.COVERAGE)
        self.assertEqual(exit_code(read_only), 0)

    def test_published_baseline_is_as_readable_as_any_directory(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "v1"
            save(BaselineRecord("http://localhost", "", "today", tests=[record("holds")]), target)
            reference = Path(root) / "reference"
            reference.mkdir()
            self.assertEqual(stat.S_IMODE(target.stat().st_mode),
                             stat.S_IMODE(reference.stat().st_mode),
                             "a baseline nobody but its recorder can read is not an artifact")
            self.assertEqual([p.name for p in Path(root).iterdir() if p.name.startswith(".")], [],
                             "staging directories must not survive a successful save")
            self.assertTrue(os.access(target / "manifest.json", os.R_OK))

    def test_report_contains_execution_coverage_and_suggestions(self):
        baseline = BaselineRecord("http://localhost", "", "today", tests=[record("holds")])
        current = replace(baseline, tests=[record("inconclusive")])
        found = compare(baseline.tests[0], current.tests[0])
        report = build_regression_record(baseline_dir="v1", url=current.base_url,
                                         baseline=baseline, current=current, found=found)
        self.assertEqual(report["coverage"]["conclusive_tests"], 0)
        self.assertEqual(report["suggestions"][0]["status"], "not_observed")
        json.dumps(report)


if __name__ == "__main__":
    unittest.main()
