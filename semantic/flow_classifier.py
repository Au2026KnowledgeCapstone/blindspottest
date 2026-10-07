"""Flow mapper: hand an application map to an LLM, get back capability candidates.

The page-level classifier is shown one snapshot and asked which *element*
holds persistent state. This one is shown the summary projection of a crawled
`AppGraph` — the whole application at roughly fifteen tokens per state — plus
the readable value surface of its states, and asked which *regions* are
capabilities and where each one's outcome can be read.

What the model is and is not allowed to decide:

    decides     which capability a region represents
    decides     which state a capability ends in
    decides     which selectors expose that outcome as named values
    never       the path. `AppGraph.path_to` computes it, breadth-first and
                deterministically. A model asked to invent a checkout path
                produces something that reads well and cannot be walked.

Same boundary as everywhere else: the model says "a DELETE_RESOURCE lives
here and its outcome is readable there", never "this is broken". Nothing
downstream of `classify_flows()` treats a candidate as a defect.

Defaults to OpenAI — `BLINDSPOT_LLM_PROVIDER` still wins when set, and
`--provider` beats both.
"""

from __future__ import annotations

import json
from typing import Iterable

from discovery.graph import AppGraph
from discovery.projections import summary
from discovery.readable import summarize as summarize_readable
from semantic.classifier import LLMBackend, backend_for
from semantic.flow_schemas import (
    FlowCandidate,
    FlowCandidateSet,
    FlowClassificationResult,
    RejectedFlowCandidate,
    flow_candidate_set_schema,
)

DEFAULT_PROVIDER = "openai"

SYSTEM_PROMPT = """\
You read a map of a web application and identify the CAPABILITIES it exposes.

A capability is something a user can accomplish — creating a resource, \
deleting one, signing out, completing a purchase, sorting a collection. It is \
a reachability claim, not a fixed sequence of steps: a checkout that gains an \
extra upsell page still has the same capability.

You are given:
  - an application map: states (places with a distinct set of things to do), \
and the actions connecting them. State ids look like `s_1a2b3c4d5e`.
  - for some states, a list of READABLE VALUES: CSS selectors with how many \
elements they match and sample text.

SCOPE — read this carefully:
  - Your job is "this capability is here, and this is where its outcome is \
readable."
  - Your job is NOT "this is broken." You are looking at a static map of an \
application nobody has tested. Nothing you see can tell you whether anything \
works. `reasoning_summary` describes EVIDENCE, never a defect.
  - Do NOT invent a path. Name the start and end states; the system computes \
the walk between them itself.

FOR EACH CAPABILITY, PROVIDE:

  capability        one of the listed enum values.

  goal_state_id     the state the capability ends in. For a deletion that is \
the collection you land back on; for a purchase it is the confirmation; for \
signing out it is wherever sign-out returns you. MUST be a state id present \
in the map.

  setup_state_id    the last state BEFORE the actions that perform the \
capability — the place a user stands when they are about to do it. For a \
deletion that is the resource's own detail page. Null means the capability \
starts at the application's entry.

  bindings          how to read the outcome, as named values. Each binding is \
a name, a CSS selector, and how to extract it. The NAME matters: it has to be \
what the invariant for this capability will look for. Use these names:
      multi_step_transaction : `line_totals` (numbers, the individual line \
amounts — EXCLUDING the grand total) and `order_total` (number, the grand \
total). Prefer selectors that already separate the two, e.g. a \
`tr:not(.grand)` form for lines and a `tr.grand` form for the total.
      sort_collection        : `row_values` (numbers, the column the sort was \
asked for, in displayed order).
      filter_collection      : `row_count` (count of displayed rows) and \
`stated_count` (number, the count the page claims).
      delete_resource        : `row_count` (count of remaining rows) and, \
when the page states a count, `stated_count` (number).
      create_resource        : `resource_present` (visible, something \
identifying the new resource).
      session_termination    : no bindings needed; use a probe.
      authenticate           : `signed_in` (visible, evidence the session is \
active).
    Choose selectors from the READABLE VALUES you were given. A selector \
matching several elements is correct for a collection and wrong for a single \
total — the match count tells you which you have.

  probe             a request to make AFTER the capability runs, to test \
whether something still exists. This is the only way to check a condition \
about something NOT existing.
      delete_resource      : {"mode": "captured", "path": ""} — re-request \
the resource's own URL, which the system captures before the deletion.
      session_termination  : {"mode": "path", "path": "/the-protected-path"} \
— the page that should stop being reachable once signed out.
      create_resource      : {"mode": "final", "path": ""} — re-request the \
URL the flow ended on.
      anything else        : null.

  applicability_confidence  0-1, how strongly the map supports "this \
capability is here and ends there" — NOT how likely it is to be broken.

Return only capabilities you actually believe in. An empty list is a valid \
answer for a map with none. Do not return two candidates for the same \
capability and goal state.\
"""


def build_user_prompt(
    graph: AppGraph, readables: dict[str, dict] | None = None
) -> str:
    """Render the map, plus the readable surface of the states we have it for.

    The map alone is not enough to write a binding: it describes what a user
    can *do* in each state, and a flow's outcome is almost always rendered
    text that no control snapshot contains. The readable surface is what
    makes a selector choosable rather than inventable.
    """
    parts = [
        "Here is a map of a web application.\n",
        summary(graph),
        "",
    ]

    if readables:
        parts.append(
            "\nREADABLE VALUES per state — CSS selectors, how many elements "
            "each matches, and sample text. Choose your binding selectors "
            "from these.\n"
        )
        for state_id, readable in readables.items():
            state = graph.states.get(state_id)
            route = state.url_template if state else "?"
            parts.append(f"\n## {state_id}  {route}")
            parts.append(summarize_readable(readable, limit=14))

    parts.append(
        "\nIdentify the capabilities this application exposes, where each "
        "one ends, and how to read its outcome."
    )
    return "\n".join(parts)


def _check_references(candidate: FlowCandidate, graph: AppGraph) -> str | None:
    """Return a rejection reason, or None if every reference holds up.

    Schema validation guarantees the shape. It cannot guarantee the state ids
    exist, that a path to them is walkable, or that a binding names an
    operand anything will read — which is exactly where a plausible-looking
    hallucination lands. Checking here means a bad candidate is reported as
    rejected rather than becoming an inconclusive run twenty seconds into a
    browser session.
    """
    if candidate.goal_state_id not in graph.states:
        return f"goal_state_id {candidate.goal_state_id!r} is not a state in the graph"

    if candidate.setup_state_id is not None:
        if candidate.setup_state_id not in graph.states:
            return (
                f"setup_state_id {candidate.setup_state_id!r} is not a state "
                f"in the graph"
            )
        if graph.path_to(candidate.setup_state_id) is None:
            return (
                f"no walkable path from the entry to setup_state_id "
                f"{candidate.setup_state_id!r}"
            )

    if graph.path_to(candidate.goal_state_id) is None:
        return (
            f"no walkable path from the entry to goal_state_id "
            f"{candidate.goal_state_id!r}"
        )

    names = [b.name for b in candidate.bindings]
    if len(names) != len(set(names)):
        return "two bindings share one observation name"
    for binding in candidate.bindings:
        if not binding.selector.strip():
            return f"binding {binding.name!r} has an empty selector"

    probe = candidate.probe
    if probe is not None and probe.mode.value == "path" and not probe.path.strip():
        return "probe mode is 'path' but no path was given"
    if probe is not None and probe.mode.value != "path" and probe.path.strip():
        return (
            f"probe mode is {probe.mode.value!r}, which takes no path, but "
            f"{probe.path!r} was given"
        )

    return None


def validate_flow_candidates(
    candidates: Iterable[FlowCandidate], graph: AppGraph
) -> tuple[list[FlowCandidate], list[RejectedFlowCandidate]]:
    """Split candidates into (kept, rejected) by checking every reference.

    Applied wherever candidates enter the system — from a model or loaded
    from a file. A candidate is only meaningful relative to a graph, so the
    check belongs to the boundary rather than to the LLM call.
    """
    kept: list[FlowCandidate] = []
    rejected: list[RejectedFlowCandidate] = []
    seen: set[tuple[str, str]] = set()

    for candidate in candidates:
        reason = _check_references(candidate, graph)
        if reason is None:
            key = (candidate.capability.value, candidate.goal_state_id)
            if key in seen:
                reason = (
                    f"duplicate: another candidate already claims "
                    f"{candidate.capability.value} ending at "
                    f"{candidate.goal_state_id}"
                )
            else:
                seen.add(key)
                kept.append(candidate)
                continue
        rejected.append(RejectedFlowCandidate(candidate=candidate, reason=reason))

    kept.sort(key=lambda c: c.applicability_confidence, reverse=True)
    return kept, rejected


def classify_flows(
    graph: AppGraph,
    *,
    readables: dict[str, dict] | None = None,
    backend: LLMBackend | None = None,
) -> FlowClassificationResult:
    """Map an application graph to flow-testing candidates."""
    backend = backend or backend_for(default_provider=DEFAULT_PROVIDER)

    raw = backend.complete_json(
        system=SYSTEM_PROMPT,
        user=build_user_prompt(graph, readables),
        schema=flow_candidate_set_schema(),
    )

    parsed = FlowCandidateSet.model_validate_json(raw)
    kept, rejected = validate_flow_candidates(parsed.candidates, graph)

    return FlowClassificationResult(
        base_url=graph.base_url,
        model=backend.model,
        candidates=kept,
        rejected=rejected,
    )


if __name__ == "__main__":
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(prog="blindspot-flow-classify")
    parser.add_argument("graph", nargs="?", default="runs/graphs/graph_sound.json")
    parser.add_argument("--provider", help=f"default: {DEFAULT_PROVIDER}")
    parser.add_argument("--model", help="provider-specific model id")
    parser.add_argument("--readables", type=Path,
                        help="JSON of {state_id: readable} to include")
    parser.add_argument("--prompt-only", action="store_true",
                        help="print the prompt instead of calling the model")
    args = parser.parse_args()

    loaded = AppGraph.load(args.graph)
    found = json.loads(args.readables.read_text()) if args.readables else None

    if args.prompt_only:
        print(build_user_prompt(loaded, found))
        raise SystemExit(0)

    result = classify_flows(
        loaded,
        readables=found,
        backend=backend_for(args.provider, args.model,
                            default_provider=DEFAULT_PROVIDER),
    )
    print(result.model_dump_json(indent=2))
