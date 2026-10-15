"""Compare two baselines and report what moved.

Both sides of a comparison are `BaselineRecord`s. That symmetry is
deliberate: a regression run produces the same structure a baseline run does,
so a regression result can itself be saved as the next baseline, and the diff
has exactly one shape to understand rather than one per direction.

Three independent comparisons, in descending order of how much they usually
matter:

    verdicts        an invariant that held and now fails. The signal the whole
                    system exists to produce.
    observations    the same verdict reached from different numbers. A total
                    that moved while still agreeing with its lines is not a
                    defect, but it is often the first visible trace of one.
    structure       a route or transition that appeared or vanished. Cheap to
                    compute from the graph and the only thing that catches a
                    feature disappearing entirely, which no invariant can
                    report because its test simply stops being instantiated.

What this does *not* do is decide whether a difference is a defect. A removed
route might be a deliberate deletion; a changed total might be a price rise.
This reports movement and leaves the reading to `regression.interpreter` and,
after that, to a person.

One precondition the report is explicit about: a comparison is only as
trustworthy as the crawl underneath it. If either graph is open — some
affordance was never fired — then a route "missing" from one side may simply
be one that crawl did not reach, and a structural finding is not evidence of
anything. `make crawl-stable` exists to establish that the crawler reproduces
itself before any of this is believed.
"""

from __future__ import annotations

import math
from typing import Any
from urllib.parse import urlsplit

from baseline.record_schema import BaselineRecord, TestRecord
from discovery.graph import AppGraph
from regression.findings import Direction, Finding, FindingSet, Kind
from regression.ui_differ import diff_ui

# Observed money and ratings are floats parsed out of rendered text, so two
# runs can produce values that differ only in representation. The same
# tolerance the relation engine compares with is used here, for the same
# reason: a report full of 20.000000000000004 != 20.0 is a report nobody
# reads.
_TOLERANCE = 1e-6


def _same(left: Any, right: Any) -> bool:
    """Whether two observed values are the same fact.

    Numbers compare within a tolerance; lists compare element-wise so a
    changed row is reported rather than the whole collection; everything else
    compares exactly.
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isclose(float(left), float(right), abs_tol=_TOLERANCE)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _same(a, b) for a, b in zip(left, right)
        )
    return left == right


def _brief(value: Any, limit: int = 90) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------
# Verdicts and observations
# --------------------------------------------------------------------------

# Execution health and correctness are separate; only reviewed expectations
# under matching fixture contracts can produce confirmed regressions.
def _verdict_finding(before: TestRecord, after: TestRecord) -> Finding | None:
    was, now = before.status, after.status
    old_contract = before.instance.get("contract", {})
    new_contract = after.instance.get("contract", {})
    # Only compare the same recorded semantics and fixture declaration.
    execution_keys = ("capability", "setup", "goal", "bindings", "procedure", "probe_mode", "probe_path")
    comparable = (all(before.instance.get(k) == after.instance.get(k) for k in execution_keys) and
                  old_contract == new_contract and
                  before.instance.get("expected_relation") == after.instance.get("expected_relation") and
                  before.instance.get("precondition") == after.instance.get("precondition"))
    if not comparable:
        return Finding(Kind.VERDICT, Direction.CHANGED, after.test_id,
                       "Expectation or fixture contract changed; results are not directly comparable",
                       invariant=after.invariant, before=was, after=now)
    if now == "inconclusive" or after.error or after.evidence.get("cleanup_verified") is False:
        direction = Direction.EXECUTION
    elif now == "violated" and was == "violated":
        direction = Direction.EXISTING
    elif now == "violated" and was == "holds":
        confirmed = (new_contract.get("review_status") == "confirmed"
                     and new_contract.get("reviewed_by")
                     and new_contract.get("requirement_source")
                     and new_contract.get("fixture_version")
                     and before.observations.get("fixture_ready") is True
                     and after.observations.get("fixture_ready") is True
                     and not before.error
                     and before.evidence.get("cleanup_verified") is not False)
        direction = Direction.REGRESSED if confirmed else Direction.SUSPECTED
    elif now == "violated":
        direction = Direction.SUSPECTED
    elif was != now and now == "holds":
        direction = Direction.FIXED
    else:
        return None
    return Finding(
        kind=Kind.VERDICT, direction=direction, subject=after.test_id,
        invariant=after.invariant, severity=after.verdict.get("severity"),
        summary=f"{after.invariant}: {was} -> {now}",
        detail=after.verdict.get("detail", ""), before=was, after=now,
    )


def _observation_findings(before: TestRecord, after: TestRecord) -> list[Finding]:
    """Differences in what was observed, independent of the verdict.

    Reported even when the verdict is unchanged. A checkout whose total and
    lines both moved by the same amount still satisfies its invariant, and
    is still the most useful thing to show someone asking what this build
    did differently.
    """
    out: list[Finding] = []
    names = set(before.observations) | set(after.observations)

    ignored = after.instance.get("contract", {}).get("ignored_observations", {})
    for name in sorted(names):
        if name in ignored:
            continue
        had, has = name in before.observations, name in after.observations
        old = before.observations.get(name)
        new = after.observations.get(name)

        if had and not has:
            out.append(Finding(
                kind=Kind.OBSERVATION,
                direction=Direction.EXECUTION,
                subject=after.test_id,
                invariant=after.invariant,
                summary=f"{name} is no longer observed",
                detail=(
                    f"the baseline recorded {name}={_brief(old)}; this run "
                    f"recorded nothing, so the relation reading it cannot be "
                    f"evaluated"
                ),
                before=old,
                after=None,
            ))
            continue
        if has and not had:
            out.append(Finding(
                kind=Kind.OBSERVATION,
                direction=Direction.CHANGED,
                subject=after.test_id,
                invariant=after.invariant,
                summary=f"{name} is observed for the first time",
                detail=f"this run recorded {name}={_brief(new)}",
                before=None,
                after=new,
            ))
            continue
        if not _same(old, new):
            out.append(Finding(
                kind=Kind.OBSERVATION,
                direction=Direction.CHANGED,
                subject=after.test_id,
                invariant=after.invariant,
                summary=f"{name} changed",
                detail=f"{_brief(old)} -> {_brief(new)}",
                before=old,
                after=new,
            ))

    return out


def _run_finding(before: TestRecord, after: TestRecord) -> Finding | None:
    if before.error == after.error:
        return None
    if after.error and not before.error:
        return Finding(
            kind=Kind.RUN,
            direction=Direction.EXECUTION,
            subject=after.test_id,
            invariant=after.invariant,
            summary=f"{after.invariant} now fails to run",
            detail=after.error or "",
            before=None,
            after=after.error,
        )
    if before.error and not after.error:
        return Finding(
            kind=Kind.RUN,
            direction=Direction.FIXED,
            subject=after.test_id,
            invariant=after.invariant,
            summary=f"{after.invariant} runs again",
            detail=f"the baseline failed with: {before.error}",
            before=before.error,
            after=None,
        )
    return Finding(
        kind=Kind.RUN,
        direction=Direction.CHANGED,
        subject=after.test_id,
        invariant=after.invariant,
        summary=f"{after.invariant} fails to run differently",
        detail=f"{before.error} -> {after.error}",
        before=before.error,
        after=after.error,
    )


def diff_tests(baseline: BaselineRecord, current: BaselineRecord) -> list[Finding]:
    """Compare the two runs' tests, matched by test id."""
    out: list[Finding] = []
    before_by_id = baseline.by_test_id()
    after_by_id = current.by_test_id()

    for test_id in sorted(set(before_by_id) | set(after_by_id)):
        before = before_by_id.get(test_id)
        after = after_by_id.get(test_id)

        if before is not None and after is None:
            # A test that stopped being run protects nothing, and its absence
            # looks identical to a pass in any summary that only counts
            # failures. It has to be a finding in its own right.
            out.append(Finding(
                kind=Kind.MISSING,
                direction=Direction.EXECUTION,
                subject=test_id,
                invariant=before.invariant,
                summary=f"{before.invariant} was not run against this build",
                detail=(
                    f"the baseline ran it and got {before.status}; this run has "
                    f"no record of it"
                ),
                before=before.status,
                after=None,
            ))
            continue
        if after is not None and before is None:
            out.append(Finding(
                kind=Kind.MISSING,
                direction=Direction.CHANGED,
                subject=test_id,
                invariant=after.invariant,
                summary=f"{after.invariant} is new in this run",
                detail=f"it was not in the baseline; it got {after.status}",
                before=None,
                after=after.status,
            ))
            continue

        verdict = _verdict_finding(before, after)
        if verdict:
            out.append(verdict)
        run = _run_finding(before, after)
        if run:
            out.append(run)
        out.extend(_observation_findings(before, after))

    return out


# --------------------------------------------------------------------------
# Structure
# --------------------------------------------------------------------------


def _routes(graph: AppGraph) -> dict[str, str]:
    """Routes, with the entry's own prefix stripped off.

    Compared by route rather than by state id because two deployments of one
    application live at different prefixes — `/cart` against `/broken/cart` —
    so their fingerprints never match even where the page is identical.
    """
    prefix = urlsplit(graph.base_url).path.rstrip("/")

    out: dict[str, str] = {}
    for state in graph.states.values():
        route = state.url_template
        if prefix and (route == prefix or route.startswith(prefix + "/")):
            route = route[len(prefix):] or "/"
        out[route] = state.id
    return out


def diff_structure(baseline: BaselineRecord, current: BaselineRecord) -> list[Finding]:
    """Compare the two applications' exposed routes."""
    before_graph = AppGraph.from_dict(baseline.graph)
    after_graph = AppGraph.from_dict(current.graph)

    out: list[Finding] = []

    # An open graph cannot support a claim about absence, so the caveat is
    # attached to the findings rather than left in a docstring nobody reads
    # at the point of being misled.
    open_sides = [
        name
        for name, graph in (("baseline", before_graph), ("current", after_graph))
        if not graph.is_closed()
    ]
    caveat = (
        f" (the {' and '.join(open_sides)} crawl did not exhaust its frontier, "
        f"so this may be a route the crawl missed rather than one that changed)"
        if open_sides
        else ""
    )

    before_routes, after_routes = _routes(before_graph), _routes(after_graph)

    for route in sorted(set(before_routes) - set(after_routes)):
        out.append(Finding(
            kind=Kind.STRUCTURE,
            direction=Direction.CHANGED,
            subject=route,
            summary=f"route {route} is no longer exposed",
            detail=(
                f"the baseline reached it; this crawl did not{caveat}"
            ),
            before=route,
            after=None,
        ))

    for route in sorted(set(after_routes) - set(before_routes)):
        out.append(Finding(
            kind=Kind.STRUCTURE,
            direction=Direction.COVERAGE,
            subject=route,
            summary=f"route {route} is new",
            detail=f"this crawl reached it; the baseline did not{caveat}",
            before=None,
            after=route,
        ))

    return out


# --------------------------------------------------------------------------
# The whole comparison
# --------------------------------------------------------------------------


def diff(
    baseline: BaselineRecord,
    current: BaselineRecord,
    *,
    include_structure: bool = True,
    include_ui: bool = True,
) -> FindingSet:
    """Everything that moved between a baseline and a later run."""
    found = FindingSet()
    if not baseline.tests:
        found.add(Finding(Kind.MISSING, Direction.EXECUTION, "test-suite",
                          "Baseline contains no tests; behavioral regression coverage is empty"))
    for side, record in (("baseline", baseline), ("current", current)):
        if record.graph and not AppGraph.from_dict(record.graph).is_closed():
            found.add(Finding(Kind.STRUCTURE, Direction.COVERAGE, side,
                              f"{side} crawl is incomplete; absence claims require review"))
    for skipped in baseline.coverage.get("skipped", []):
        found.add(Finding(Kind.MISSING, Direction.COVERAGE,
                          skipped.get("invariant", "unsupported"),
                          "Discovered behavior has no runnable check",
                          detail="; ".join(skipped.get("reasons", []))))
    found.extend(diff_tests(baseline, current))
    if include_structure:
        found.extend(diff_structure(baseline, current))
    if include_ui:
        found.extend(diff_ui(baseline, current))
    return found
