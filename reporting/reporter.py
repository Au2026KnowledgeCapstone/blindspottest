"""Terminal output and machine-readable run records.

Two deliberate framings here, both of which change what the output means:

1. Confidence belongs to APPLICABILITY, never to the result. The model's 0.94
   is "the persistence invariant probably applies to this field". Once the
   test runs, `committed_value == final_value` either held or it didn't —
   there is no confidence attached to a string comparison. The report never
   prints "94% sure this is a bug".

2. BlindSpot reports evidence, not verdicts about intent. A failed relation is
   strong deterministic evidence, but whether persistence *should* apply here
   is still a human call — so the wording is "potential regression detected"
   and "human review recommended", not "BUG CONFIRMED".
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from knowledge.engine import Status

RUNS_DIR = Path("runs")


# --------------------------------------------------------------------------
# Terminal
# --------------------------------------------------------------------------


class _Style:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, t):  return self._wrap("1", t)
    def dim(self, t):   return self._wrap("2", t)
    def red(self, t):   return self._wrap("31;1", t)
    def green(self, t): return self._wrap("32;1", t)
    def amber(self, t): return self._wrap("33;1", t)
    def blue(self, t):  return self._wrap("34;1", t)


def _supports_color(stream) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return hasattr(stream, "isatty") and stream.isatty()


class ConsoleReporter:
    """Streams progress as the pipeline runs."""

    def __init__(self, stream=None, color: bool | None = None):
        self.out = stream or sys.stdout
        self.s = _Style(_supports_color(self.out) if color is None else color)

    def _p(self, text: str = "") -> None:
        print(text, file=self.out)

    def header(self, url: str, version: str = "0.1") -> None:
        self._p()
        self._p(self.s.bold(f"BlindSpot {version}"))
        self._p(self.s.dim(url))
        self._p()

    def inspected(self, snapshot: dict) -> None:
        n = len(snapshot.get("elements", []))
        editable = sum(1 for e in snapshot["elements"] if e.get("editable"))
        self._p("Inspecting application...")
        self._p(f"  Found {n} interactive elements ({editable} editable).")
        self._p()

    def analyzing(self, model: str) -> None:
        self._p(f"Analyzing interactions... {self.s.dim(f'({model})')}")
        self._p()

    def classified(self, result) -> None:
        n = len(result.candidates)
        self._p(f"  {n} candidate{'' if n == 1 else 's'} identified.")
        for rejected in result.rejected:
            self._p(self.s.dim(f"  discarded: {rejected.reason}"))
        self._p()

    def candidate(self, index: int, instance, candidate) -> None:
        self._p(self.s.bold(f"Candidate #{index}"))
        self._p(f"  Type:    Persistent Mutation")
        self._p(f"  Field:   {instance.target.accessible_name or instance.target.selector}")
        commit = instance.commit_action
        self._p(f"  Commit:  {commit.accessible_name if commit else '(autosave)'}")
        self._p(
            f"  Applicability confidence: "
            f"{candidate.applicability_confidence:.2f}"
        )
        self._p()
        self._p("Applying rule:")
        self._p(f"  {instance.invariant}")
        self._p()

    def skipped(self, skip) -> None:
        self._p(self.s.dim(f"  skipped {skip.invariant_id}: {', '.join(skip.unmet)}"))

    def testing(self, result) -> None:
        obs = result.observations
        self._p("Testing...")
        if "initial_value" in obs:
            self._p(f"  Original: {obs['initial_value']!r}")
        if "committed_value" in obs:
            self._p(f"  Mutation: {obs['committed_value']!r}")
        if obs.get("success_observed") is True:
            self._p("  Save confirmation observed.")
        elif obs.get("success_observed") is False:
            self._p(self.s.dim("  No save confirmation observed."))
        if any(s.name == "reload" and s.ok for s in result.steps):
            self._p("  Reloading...")
        self._p()

    def verdict(self, verdict, result) -> None:
        obs = verdict.observations
        expected = obs.get("committed_value")
        observed = obs.get("final_value")

        if verdict.status is Status.HOLDS:
            self._p(self.s.green("PASS"))
            self._p(f"  Expected: {expected!r}")
            self._p(f"  Observed: {observed!r}")
        elif verdict.status is Status.VIOLATED:
            self._p(self.s.red("POTENTIAL REGRESSION DETECTED"))
            self._p(f"  Invariant: {verdict.invariant_id}")
            self._p(f"  Expected:  {expected!r}")
            self._p(f"  Observed:  {observed!r}")
            self._p()
            self._p("  Evidence:")
            self._p(f"    {verdict.summary or verdict.detail}")
            self._p(
                "    The deterministic relation "
                f"`{result.instance.expected_relation}` did not hold."
            )
            self._p()
            self._p(self.s.amber("  Human review recommended."))
            self._p(
                self.s.dim(
                    "  BlindSpot verified the value did not survive a reload. "
                    "Whether\n  persistence was intended for this field is a "
                    "judgement call."
                )
            )
        else:
            self._p(self.s.blue("INCONCLUSIVE"))
            self._p(f"  {verdict.detail}")

        if result.restored is False:
            self._p()
            self._p(self.s.amber(
                "  WARNING: could not restore the original value; "
                "test data may remain."
            ))
        self._p()

    def summary(self, verdicts, path: Path | None) -> None:
        counts = {s: 0 for s in Status}
        for v in verdicts:
            counts[v.status] += 1
        self._p(self.s.bold("Summary"))
        self._p(f"  {counts[Status.HOLDS]} passed, "
                f"{counts[Status.VIOLATED]} potential regression(s), "
                f"{counts[Status.INCONCLUSIVE]} inconclusive")
        if path:
            self._p(f"  Results: {path}")
        self._p()

    # -- flow-level output -------------------------------------------------
    #
    # Kept on the same reporter rather than split onto a second one, because
    # a run produces both kinds of test and interleaving two output styles
    # reads as two programs reporting on one application.

    def crawling(self, base_url: str) -> None:
        self._p(f"Mapping the application... {self.s.dim(base_url)}")

    def crawled(self, graph) -> None:
        closed = graph.is_closed()
        self._p(
            f"  {len(graph.states)} states, {len(graph.actions)} actions, "
            + (self.s.green("closed") if closed else self.s.amber("OPEN"))
        )
        if not closed:
            pending = sum(len(v) for v in graph.unexplored.values())
            self._p(self.s.dim(
                f"  {pending} affordance(s) unexplored — absence of a route "
                f"proves nothing from this crawl."
            ))
        self._p()

    def reading_values(self, count: int) -> None:
        self._p(f"Reading the value surface of {count} state(s)...")
        self._p()

    def flow_classified(self, result) -> None:
        n = len(result.candidates)
        self._p(f"  {n} capability candidate{'' if n == 1 else 's'} identified.")
        for candidate in result.candidates:
            self._p(self.s.dim(
                f"    {candidate.capability.value} -> {candidate.goal_state_id}"
            ))
        for rejected in result.rejected:
            self._p(self.s.dim(f"  discarded: {rejected.reason}"))
        self._p()

    def flow_candidate(self, index: int, instance, candidate) -> None:
        self._p(self.s.bold(f"Flow #{index}"))
        self._p(f"  Capability: {instance.capability}")
        self._p(f"  Walk:       {' -> '.join(s.label for s in instance.goal) or '(none)'}")
        if instance.probe_mode:
            target = instance.probe_path or f"({instance.probe_mode})"
            self._p(f"  Probe:      {target}")
        self._p(
            f"  Applicability confidence: "
            f"{candidate.applicability_confidence:.2f}"
        )
        self._p()
        self._p("Applying rule:")
        self._p(f"  {instance.invariant}")
        self._p()

    def flow_skipped(self, skip) -> None:
        self._p(self.s.dim(
            f"  skipped {skip.invariant_id} for "
            f"{skip.candidate.capability.value}: {', '.join(skip.unmet)}"
        ))

    def flow_testing(self, result) -> None:
        obs = result.observations
        self._p("Walking the flow...")
        for state in result.states:
            arrived = state["state_id"] == state["expected_state_id"]
            mark = "ok" if arrived else "NOT the expected state"
            self._p(f"  {state['phase']:5} -> {state['url_template']} ({mark})")
        if obs.get("goal_reached") is False:
            self._p(self.s.amber("  the flow did not complete"))
        for name, value in obs.items():
            if name in ("setup_reached", "goal_reached", "captured_url"):
                continue
            self._p(f"  {name}: {value!r}")
        self._p()

    def flow_verdict(self, verdict, result) -> None:
        if verdict.status is Status.HOLDS:
            self._p(self.s.green("PASS"))
            self._p(f"  {verdict.detail}")
        elif verdict.status is Status.VIOLATED:
            self._p(self.s.red("POTENTIAL REGRESSION DETECTED"))
            self._p(f"  Invariant: {verdict.invariant_id}")
            self._p(f"  {verdict.detail}")
            self._p()
            self._p("  Evidence:")
            self._p(f"    {verdict.summary or verdict.detail}")
            self._p()
            self._p(self.s.amber("  Human review recommended."))
        else:
            self._p(self.s.blue("INCONCLUSIVE"))
            self._p(f"  {verdict.detail}")
            if result.error:
                self._p(self.s.dim(f"  {result.error}"))

        if result.reset is False:
            self._p()
            self._p(self.s.amber(
                "  WARNING: could not reset the session; the flow's "
                "mutations may remain."
            ))
        self._p()

    def baselined(self, directory, description: str) -> None:
        self._p(self.s.bold("Baseline recorded"))
        self._p(f"  {description}")
        self._p(f"  {directory}")
        self._p()
        self._p(self.s.dim(
            "  Re-run against a later build with:\n"
            f"    python main.py --regression --baseline-dir {directory} <url>"
        ))
        self._p()


# --------------------------------------------------------------------------
# Machine-readable record
# --------------------------------------------------------------------------


def build_record(
    url: str,
    model: str,
    entries: list[dict],
    skipped: list,
    rejected: list | None = None,
) -> dict:
    """Assemble the JSON record for one execution.

    `skipped` and `rejected` are different failures and are kept apart:
    a rejected candidate is something the model got wrong about the page;
    a skipped one is a real candidate no invariant could be applied to.
    """
    return {
        "blindspot_version": "0.1",
        "url": url,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": model,
        "tests": entries,
        "skipped": [
            {
                "invariant": s.invariant_id,
                "field_id": s.candidate.field_id,
                "unmet_requirements": list(s.unmet),
            }
            for s in skipped
        ],
        "rejected_candidates": [
            {
                "field_id": r.candidate.field_id,
                "commit_action_id": r.candidate.commit_action_id,
                "applicability_confidence": r.candidate.applicability_confidence,
                "reason": r.reason,
            }
            for r in (rejected or [])
        ],
        "summary": {
            "passed": sum(1 for e in entries if e["result"] == "holds"),
            "violations": sum(1 for e in entries if e["result"] == "violation"),
            "inconclusive": sum(1 for e in entries if e["result"] == "inconclusive"),
            "skipped": len(skipped),
            "rejected": len(rejected or []),
        },
    }


def build_entry(instance, candidate, result, verdict) -> dict:
    """One test's record. Note `applicability_confidence` sits beside a
    `result` that carries no confidence of its own — that separation is the
    point."""
    status_word = {
        Status.HOLDS: "holds",
        Status.VIOLATED: "violation",
        Status.INCONCLUSIVE: "inconclusive",
    }[verdict.status]

    return {
        "test_id": instance.test_id,
        "invariant": instance.invariant,
        "target": instance.target.accessible_name,
        "locator": instance.target.locator_strategy,
        "commit_action": (
            instance.commit_action.accessible_name if instance.commit_action else None
        ),
        "applicability_confidence": candidate.applicability_confidence,
        "applicability_reasoning": candidate.reasoning_summary,
        "original": result.observations.get("initial_value"),
        "mutation": instance.mutation_value,
        "committed": result.observations.get("committed_value"),
        "observed_after_reload": result.observations.get("final_value"),
        "expected_relation": instance.expected_relation,
        # How the commit was confirmed, and what the model predicted — without
        # these a mispredicted banner is indistinguishable from a failed save.
        "success_signal": instance.success_signal,
        "success_method": result.notes.get("success_method"),
        "predicted_text_missing": result.notes.get("predicted_text_missing"),
        "result": status_word,
        "detail": verdict.detail,
        "severity": verdict.severity,
        "restored": result.restored,
        "error": result.error,
        "duration_ms": result.duration_ms,
        # The step trace turns "inconclusive" into "step 5 of 7 failed, and
        # here is what it said". Teardown steps are flagged so a failed
        # restore is distinguishable from a failed test.
        "steps": [step.to_dict() for step in result.steps],
    }


def build_flow_entry(instance, candidate, result, verdict) -> dict:
    """One flow test's record.

    The walk is recorded as labels rather than locators, and the states as
    the fingerprints actually reached beside the ones expected. Together
    those turn "inconclusive" into "the goal walk ended on /projects/<id>
    when it should have ended on /projects", which is the difference between
    a result someone can act on and one they have to reproduce by hand.
    """
    status_word = {
        Status.HOLDS: "holds",
        Status.VIOLATED: "violation",
        Status.INCONCLUSIVE: "inconclusive",
    }[verdict.status]

    return {
        "test_id": instance.test_id,
        "kind": "flow",
        "invariant": instance.invariant,
        "capability": instance.capability,
        "setup_walk": [s.label for s in instance.setup],
        "goal_walk": [s.label for s in instance.goal],
        "bindings": [b.to_dict() for b in instance.bindings],
        "probe": (
            {"mode": instance.probe_mode, "path": instance.probe_path}
            if instance.probe_mode
            else None
        ),
        "applicability_confidence": candidate.applicability_confidence
        if candidate
        else None,
        "applicability_reasoning": candidate.reasoning_summary if candidate else None,
        "expected_relation": instance.expected_relation,
        "precondition": instance.precondition,
        "observations": result.observations,
        "states": result.states,
        "result": status_word,
        "detail": verdict.detail,
        "severity": verdict.severity,
        "reset": result.reset,
        "error": result.error,
        "duration_ms": result.duration_ms,
        "steps": [step.to_dict() for step in result.steps],
    }


def build_mixed_record(
    url: str,
    model: str,
    entries: list[dict],
    skipped: list | None = None,
    rejected: list | None = None,
    *,
    graph_summary: dict | None = None,
) -> dict:
    """A run record covering page-level and flow-level tests together.

    `build_record` assumes every entry is a page-level persistence test and
    describes its skips in those terms. This is the app-level equivalent, for
    runs that produce both kinds — kept separate so existing records and the
    dashboard that reads them stay exactly as they were.
    """
    return {
        "blindspot_version": "0.2",
        "url": url,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": model,
        "graph": graph_summary or {},
        "tests": entries,
        "skipped": [
            {
                "invariant": s.invariant_id,
                "subject": getattr(
                    getattr(s.candidate, "capability", None), "value", None
                )
                or getattr(s.candidate, "field_id", "?"),
                "unmet_requirements": list(s.unmet),
            }
            for s in (skipped or [])
        ],
        "rejected_candidates": [
            {
                "reason": r.reason,
                "subject": getattr(
                    getattr(r.candidate, "capability", None), "value", None
                )
                or getattr(r.candidate, "field_id", "?"),
            }
            for r in (rejected or [])
        ],
        "summary": {
            "passed": sum(1 for e in entries if e["result"] == "holds"),
            "violations": sum(1 for e in entries if e["result"] == "violation"),
            "inconclusive": sum(1 for e in entries if e["result"] == "inconclusive"),
            "page_tests": sum(1 for e in entries if e.get("kind") != "flow"),
            "flow_tests": sum(1 for e in entries if e.get("kind") == "flow"),
            "skipped": len(skipped or []),
            "rejected": len(rejected or []),
        },
    }


def write_record(record: dict, directory: Path | str = RUNS_DIR) -> Path:
    """Write `runs/<timestamp>.json`. Timestamps are filename-safe UTC."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S")
    path = directory / f"{stamp}.json"
    path.write_text(json.dumps(record, indent=2) + "\n")
    return path
