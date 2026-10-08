"""Compare the surface of two builds: controls, labels, and rendered values.

Invariants catch behaviour. They are silent about a page that still works
perfectly and has lost its heading, dropped the accessible name off its save
button, or started rendering an empty table — none of which changes a
relation, and all of which someone wants to know about.

Two surfaces are compared, because the two discovery passes capture different
things and a regression can hide in either:

    controls   from the per-state snapshots the crawl stored. A button that
               vanished, a field that became disabled, a control that lost
               the accessible name a screen reader needs.
    values     from the readable surface. A selector that stopped matching,
               or a collection whose row count moved.

States are matched by route with the entry prefix stripped, the same way
`regression.differ` matches them, so two deployments at different prefixes
still line up.

Severity, and why most of this is `changed` rather than `regressed`:
a reworded button is not a defect, and a report that treats every textual
difference as one is a report that gets ignored. Only losses are called
regressions — a control that disappeared, a name that went missing, a
selector that stopped matching — because those are the differences that take
capability away rather than merely move it.
"""

from __future__ import annotations

from typing import Any

from discovery.fingerprint import element_signature
from discovery.graph import AppGraph
from regression.findings import Direction, Finding, Kind

# A page holding fifty changed rows produces fifty near-identical findings,
# which drowns everything else in the report. Each category is capped and the
# remainder is summarized in one line.
_MAX_PER_STATE = 6


def _routes_to_states(graph: AppGraph) -> dict[str, Any]:
    entry = graph.states.get(graph.entry)
    prefix = (entry.url_template or "/").rstrip("/") if entry else ""

    out: dict[str, Any] = {}
    for state in graph.states.values():
        route = state.url_template
        if prefix and route.startswith(prefix):
            route = route[len(prefix):] or "/"
        # One representative state per route. Several states can share a
        # route — a cart with and without items — and comparing every
        # pairing would report their differences as changes.
        out.setdefault(route, state)
    return out


def _controls(state) -> dict[str, dict]:
    """A state's controls, keyed by what they are rather than where they sit.

    Signature-keyed because snapshot element ids (`e1`, `e2`) are only stable
    within one snapshot: inserting a single element renumbers everything
    after it, and an id-keyed comparison would report the whole page as
    changed.
    """
    snapshot = state.snapshot or {}
    out: dict[str, dict] = {}
    for element in snapshot.get("elements", []):
        out.setdefault(element_signature(element), element)
    return out


def _named(element: dict) -> str:
    return (
        element.get("accessible_name")
        or element.get("label")
        or element.get("text")
        or element.get("placeholder")
        or element.get("selector", "?")
    )


def _capped(findings: list[Finding], route: str, what: str) -> list[Finding]:
    if len(findings) <= _MAX_PER_STATE:
        return findings
    kept = findings[:_MAX_PER_STATE]
    kept.append(Finding(
        kind=Kind.UI,
        direction=findings[0].direction,
        subject=route,
        summary=f"{len(findings) - _MAX_PER_STATE} further {what} on {route}",
        detail="shown truncated; the full surface is in the saved records",
    ))
    return kept


def _diff_controls(route: str, before, after) -> list[Finding]:
    out: list[Finding] = []
    old, new = _controls(before), _controls(after)

    gone = [old[s] for s in sorted(set(old) - set(new))]
    out.extend(_capped([
        Finding(
            kind=Kind.UI,
            direction=Direction.REGRESSED,
            subject=route,
            summary=f"control {_named(element)!r} is gone from {route}",
            detail=(
                f"the baseline had a {element.get('role') or element.get('tag')} "
                f"here; this build does not"
            ),
            before=_named(element),
            after=None,
        )
        for element in gone
    ], route, "controls removed"))

    added = [new[s] for s in sorted(set(new) - set(old))]
    out.extend(_capped([
        Finding(
            kind=Kind.UI,
            direction=Direction.CHANGED,
            subject=route,
            summary=f"control {_named(element)!r} is new on {route}",
            detail=f"a {element.get('role') or element.get('tag')} that was not "
                   f"in the baseline",
            before=None,
            after=_named(element),
        )
        for element in added
    ], route, "controls added"))

    # Accessibility losses among controls that are still present. An element
    # keeping its signature but losing its name means the page still works
    # for a mouse and stopped working for a screen reader, and it also breaks
    # every semantic locator aimed at it — so this is the one UI difference
    # that reliably predicts a test becoming inconclusive later.
    shared = sorted(set(old) & set(new))
    losses: list[Finding] = []
    for signature in shared:
        was, now = old[signature], new[signature]
        if was.get("accessible_name") and not now.get("accessible_name"):
            losses.append(Finding(
                kind=Kind.UI,
                direction=Direction.REGRESSED,
                subject=route,
                summary=(
                    f"control {was['accessible_name']!r} on {route} lost its "
                    f"accessible name"
                ),
                detail="semantic locators and screen readers both rely on it",
                before=was.get("accessible_name"),
                after=None,
            ))
        if not was.get("disabled") and now.get("disabled"):
            losses.append(Finding(
                kind=Kind.UI,
                direction=Direction.REGRESSED,
                subject=route,
                summary=f"control {_named(now)!r} on {route} is now disabled",
                detail="it was actionable in the baseline",
                before="enabled",
                after="disabled",
            ))
    out.extend(_capped(losses, route, "accessibility losses"))

    return out


def _diff_values(route: str, before: dict, after: dict) -> list[Finding]:
    """Compare two readable surfaces for one state."""
    old = {v["selector"]: v for v in before.get("values", [])}
    new = {v["selector"]: v for v in after.get("values", [])}

    out: list[Finding] = []

    gone = [old[s] for s in sorted(set(old) - set(new))]
    out.extend(_capped([
        Finding(
            kind=Kind.UI,
            direction=Direction.REGRESSED,
            subject=route,
            summary=f"{entry['selector']} no longer renders anything on {route}",
            detail=(
                f"the baseline matched {entry['count']} element(s), "
                f"e.g. {entry.get('samples', ['?'])[0]!r}"
            ),
            before=entry.get("samples"),
            after=None,
        )
        for entry in gone
    ], route, "value selectors removed"))

    moved: list[Finding] = []
    for selector in sorted(set(old) & set(new)):
        was, now = old[selector], new[selector]
        if was["count"] != now["count"]:
            # A collection that shrank is the readable trace of a deletion,
            # a filter change, or data loss. Which of those it is belongs to
            # the interpreter; that it moved belongs here.
            moved.append(Finding(
                kind=Kind.UI,
                direction=Direction.CHANGED,
                subject=route,
                summary=(
                    f"{selector} on {route} matches {now['count']} elements, "
                    f"was {was['count']}"
                ),
                detail=f"samples now {now.get('samples', [])[:3]}",
                before=was["count"],
                after=now["count"],
            ))
    out.extend(_capped(moved, route, "value counts changed"))

    return out


def diff_ui(baseline, current) -> list[Finding]:
    """Compare the control and value surfaces of two baselines.

    Takes `BaselineRecord`s, like the rest of the regression layer, so the
    caller never has to unpack a graph to ask for a UI comparison.
    """
    before_graph = AppGraph.from_dict(baseline.graph)
    after_graph = AppGraph.from_dict(current.graph)

    before_states = _routes_to_states(before_graph)
    after_states = _routes_to_states(after_graph)

    out: list[Finding] = []

    # Only routes present on both sides. A route that appeared or vanished is
    # a structural finding `regression.differ` already reports, and repeating
    # it here as "every control on it is gone" would bury the rest.
    for route in sorted(set(before_states) & set(after_states)):
        out.extend(_diff_controls(route, before_states[route], after_states[route]))

    before_readables = baseline.readables or {}
    after_readables = current.readables or {}

    # Readables are keyed by state id, which differs between deployments, so
    # they are re-keyed by route before comparing — the same reason the
    # structural diff compares routes.
    before_by_route = {
        route: before_readables[state.id]
        for route, state in before_states.items()
        if state.id in before_readables
    }
    after_by_route = {
        route: after_readables[state.id]
        for route, state in after_states.items()
        if state.id in after_readables
    }

    for route in sorted(set(before_by_route) & set(after_by_route)):
        out.extend(_diff_values(route, before_by_route[route], after_by_route[route]))

    return out
