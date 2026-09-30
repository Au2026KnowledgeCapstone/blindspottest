"""Compare two application graphs.

Two uses, one computation. Against two crawls of the same build this measures
whether the crawler can reproduce itself; against two builds it is the
regression signal. The second is only meaningful once the first is clean — a
crawler whose graphs drift on their own cannot tell you that an application
changed, because every diff looks the same as its own noise.

What this does *not* do is decide whether a difference is a defect. A route
that disappeared might be a regression or a deliberate removal, and a graph
has no way to tell. This reports structural change and leaves the verdict to
the knowledge engine, which is the only part of the system that holds rules.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from discovery.graph import AppGraph, graph_diff


def _routes(graph: AppGraph) -> dict[str, str]:
    return {state.url_template: state.id for state in graph.states.values()}


def report(before: AppGraph, after: AppGraph, *,
           before_name: str = "before", after_name: str = "after") -> str:
    """A readable structural comparison of two graphs."""
    lines: list[str] = [
        f"{before_name}: {len(before.states)} states, {len(before.actions)} actions"
        f"{'' if before.is_closed() else '  (OPEN)'}",
        f"{after_name}: {len(after.states)} states, {len(after.actions)} actions"
        f"{'' if after.is_closed() else '  (OPEN)'}",
        "",
    ]

    if not (before.is_closed() and after.is_closed()):
        lines += [
            "WARNING: at least one graph is open — some affordances were never",
            "fired, so a missing route may simply be one the crawl did not reach.",
            "",
        ]

    # Routes are compared rather than state ids because the two builds live at
    # different prefixes (/cart against /broken/cart), so their fingerprints
    # never match even where the page is identical.
    before_routes, after_routes = _routes(before), _routes(after)

    def strip(route: str, graph: AppGraph) -> str:
        entry = graph.states.get(graph.entry)
        prefix = (entry.url_template or "/").rstrip("/") if entry else ""
        if prefix and route.startswith(prefix):
            return route[len(prefix):] or "/"
        return route

    before_set = {strip(r, before) for r in before_routes}
    after_set = {strip(r, after) for r in after_routes}

    only_before = sorted(before_set - after_set)
    only_after = sorted(after_set - before_set)
    shared = sorted(before_set & after_set)

    lines.append(f"routes in both: {len(shared)}")
    if only_before:
        lines.append(f"\nonly in {before_name}:")
        lines += [f"  - {route}" for route in only_before]
    if only_after:
        lines.append(f"\nonly in {after_name}:")
        lines += [f"  + {route}" for route in only_after]
    if not only_before and not only_after:
        lines.append("\nboth builds expose exactly the same routes.")
        lines.append(
            "A behavioural defect does not change the graph's shape, which is\n"
            "why discovery alone cannot find one — it says where to look, and\n"
            "the invariants say what to check when you get there."
        )

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="blindspot-compare")
    parser.add_argument("before")
    parser.add_argument("after")
    parser.add_argument("--identical", action="store_true",
                        help="exit non-zero unless the graphs match exactly "
                             "(for the stability check)")
    args = parser.parse_args(argv)

    for path in (args.before, args.after):
        if not Path(path).exists():
            parser.error(f"{path} does not exist — run the crawl that writes it first")

    before = AppGraph.load(args.before)
    after = AppGraph.load(args.after)

    print(report(before, after,
                 before_name=Path(args.before).stem,
                 after_name=Path(args.after).stem))

    if args.identical:
        result = graph_diff(before, after)
        if not result["identical"]:
            print("\ngraphs are not identical")
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
