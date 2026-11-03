"""
Resolve user checklists against Blindspot's discovered AppGraph.

This module translates natural-language application journey
requirements into references to discovered graph states and actions,
verifies their ordered connectivity, and performs static feasibility
analysis using declared action requirements and established conditions.

The user supplies the specification at runtime. The module does not
contain predefined application journeys such as checkout, project
creation, authentication, or deletion. The same mechanism is intended
to work with any application flow represented in the discovered graph.

The module maintains a distinction between:

- User requirements: What the user wants to accomplish.
- Graph resolution: Which discovered states and actions correspond
  to those requirements.
- Path verification: Whether the required checkpoints are connected
  in the requested order.
- Feasibility analysis: Whether declared action requirements can be
  established along the discovered route.
- Goal conditions: What must be true when the journey completes.
- Runtime verification: Whether the application actually behaves
  correctly when the journey is executed.

These are separate questions. A checklist can describe a valid
journey even when its exact sequence of pages differs from the route
observed by the discovery system. Likewise, finding a connected route
does not prove that the application's behavior is correct.

The pipeline works in five stages.

CHECKLIST RESOLUTION

The resolver accepts a checklist in natural language and passes it,
along with a compact representation of the discovered AppGraph, to
the configured LLMBackend.

It reuses the provider abstraction from semantic.classifier rather
than introducing a separate AI client or coupling the module to one
provider.

The model translates the checklist into structured requirements:

- Preconditions describing the conditions expected before the journey.
- Ordered checkpoints referring to graph states or actions.
- Goal conditions describing the expected outcomes.
- Unresolved requirements that cannot be mapped reliably.

Each checkpoint receives a requirement identifier so that individual
requirements can be traced through subsequent verification.

The model is an interpreter, not the authority on what exists in the
application. It may only reference states and actions supplied in
the discovered graph. Its output is validated against the actual
graph before verification.

GRAPH PATH VERIFICATION

The verifier uses discovery.graph.AppGraph.path_between(), the
graph's existing breadth-first path finder, to connect required
checkpoints.

States are nodes. Actions are directed edges between states.

A user checklist defines required checkpoints and their order; it
does not necessarily define every intermediate state.

For example, a checklist may require:

Cart -> Contact -> Shipping -> Payment -> Confirmation

The discovered application may expose:

Cart -> Contact -> Upsell -> Shipping -> Payment -> Confirmation

The additional Upsell state does not invalidate the requested
journey. The verifier can find a route through it without treating
the checklist as an exact path specification.

For action checkpoints, the verifier first finds a route to the
action's source state and then appends the specific requested action.
This prevents a route that reaches the same destination through
a different action from being mistaken for the requested action.

RESULT CLASSIFICATION

The module returns one of three graph resolution outcomes:

- found: All resolved checkpoints are connected in order.
- not_found: A closed discovered graph contains no qualifying route.
- inconclusive: The graph is incomplete or checklist requirements
  cannot be mapped or verified reliably.

The distinction between not_found and inconclusive is important.
An unexplored action is evidence that discovery may have missed
a route, not evidence that the route is impossible.

AppGraph.is_closed() is used to prevent an incomplete discovery
from producing an unjustified negative result.

The verification result includes the matched checkpoints, their
requirement identifiers, the route assembled from discovered actions,
the first unreachable requirement, and an explanation.

STATIC FEASIBILITY ANALYSIS

After graph verification, the resolver performs an additional
feasibility analysis.

It examines the requires and establishes conditions declared on
each action along the discovered route.

Conditions established by preceding actions are tracked and compared
against the requirements of subsequent actions.

The analysis identifies declared action requirements that cannot
be established by preceding graph actions.

Feasibility outcomes are:

- feasible: No unsatisfied declared action requirements were found
  along the verified route.
- inconclusive: The route could not be established or declared
  action requirements could not be established from available
  graph conditions.

The analysis is intentionally conservative. Checklist preconditions
are preserved as setup requirements but are not independently
verified or assumed to satisfy action requirements.

A feasible result therefore describes static graph-based feasibility.
It does not prove that actions are executable or that the application
will behave correctly at runtime.

HANDOFF TO TEST GENERATION AND EXECUTION

The result is intended to become an input to Blindspot's existing
knowledge and runner pipelines.

Once a journey has been resolved, the knowledge engine can select
appropriate invariants and determine how its goal conditions should
be tested. The runner can subsequently execute a materialized test
instance and record observations and verdicts.

This module does not instantiate or execute those tests. It does
not interact with a browser, evaluate actual application state,
verify goal conditions, or establish that mutations persist.

Those responsibilities require downstream test generation and
runtime execution.

WHY THIS IS SEPARATE FROM semantic.classifier

semantic.classifier classifies elements in an individual page
snapshot to identify candidates for persistence testing. Its unit
of analysis is a page and its interactive elements.

This module interprets a user-specified journey across multiple
states. Its unit of analysis is an ordered set of requirements
over the AppGraph.

Reusing the same LLMBackend keeps model configuration consistent,
while separating the modules avoids turning the page-level
persistence classifier into a general-purpose flow planner.

WHY THE GRAPH IS THE SOURCE OF TRUTH

The LLM may interpret the user's wording, but it must not invent
graph states, transitions, or identifiers.

AppGraph owns the observed topology, and its path finder determines
which routes are connected.

The checklist expresses the requested behavior; the graph describes
what discovery found; the feasibility analysis examines declared
action conditions; the knowledge engine supplies behavioral
invariants; and the runner gathers runtime evidence.

Keeping those responsibilities separate is necessary for meaningful
regression testing.

Replaying a fixed test instance against a later build must test the
same requirement rather than ask an LLM to invent a new journey
each time.

The intended role of this module is therefore to bridge
natural-language requirements and Blindspot's graph-based flow
architecture while preserving the distinction between graph
connectivity, static feasibility, and verified application behavior.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from discovery.graph import AppGraph, Action
from semantic.classifier import LLMBackend, backend_for


SYSTEM_PROMPT = """
You map user checklist requirements to an existing discovered application graph.

The graph is the sole authority on application states and actions.

Rules:
1. Preserve every required journey step and its original order.
2. Assign every journey step a stable requirement_id.
3. Map every journey requirement to a graph object or unresolved.
4. State checkpoints must reference existing state IDs.
5. Action checkpoints must reference exact action_ref values.
6. Never invent graph identifiers or action references.
7. Never replace a requested action with another action.
8. Intermediate states may be omitted.
9. Preconditions are setup requirements, not navigation steps.
10. Goal conditions are expected outcomes, not navigation steps.
11. Preserve preconditions and goal conditions separately.
12. Never silently omit a requirement.
13. Do not decide whether a route exists.
14. Do not claim that an action is executable.
15. Return JSON matching the supplied schema.

IMPORTANT INPUT HANDLING:
- Fields are affordances, not necessarily separate graph actions.
- Form inputs are often recorded in Action.inputs.
- If the checklist says to enter credentials and submit a login form,
  map that requirement to the recorded login submission action when
  its inputs contain the required credentials.
- Do not report that entering credentials is an unresolved action
  merely because no independent field-entry transition exists.
- Inspect action labels, affordance keys, and recorded inputs.
- Do not assume inputs were supplied unless they appear in the graph.
- A submit action with recorded inputs represents the combined
  interaction of supplying those inputs and submitting the form.
- Do not confuse recorded input values with proof of authentication.

A requirement that cannot be mapped to a graph object must be
included in unresolved.

Return only JSON matching the supplied schema.
"""


SCHEMA = {
    "type": "object",
    "properties": {
        "preconditions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"}
                },
                "required": ["description"],
                "additionalProperties": False,
            },
        },
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "requirement_id": {"type": "string"},
                    "description": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["state", "action"],
                    },
                    "target_id": {
                        "type": ["string", "null"]
                    },
                    "action_ref": {
                        "type": ["string", "null"]
                    },
                },
                "required": [
                    "requirement_id",
                    "description",
                    "kind",
                    "target_id",
                    "action_ref",
                ],
                "additionalProperties": False,
            },
        },
        "goal_conditions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"}
                },
                "required": ["description"],
                "additionalProperties": False,
            },
        },
        "unresolved": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "requirement_id": {"type": "string"},
                    "description": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": [
                    "requirement_id",
                    "description",
                    "reason",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "preconditions",
        "steps",
        "goal_conditions",
        "unresolved",
    ],
    "additionalProperties": False,
}


def graph_for_prompt(
    graph: AppGraph,
) -> tuple[dict[str, Any], dict[str, Action]]:

    states = [
        {
            "id": state.id,
            "url": state.url_template,
            "title": state.title,
            "affordances": [
                {
                    "key": affordance.key,
                    "kind": affordance.kind,
                    "label": affordance.label,
                    "locator": affordance.locator,
                }
                for affordance in state.affordances.values()
            ],
        }
        for state in graph.states.values()
    ]

    action_refs = {
        f"a{i}": action
        for i, action in enumerate(graph.actions)
    }

    actions = [
        {
            "action_ref": ref,
            "source": action.source,
            "target": action.target,
            "affordance_key": action.affordance_key,
            "label": action.label,
            "inputs": [
                {"field": key, "value": value}
                for key, value in action.inputs
            ],
            "requires": list(action.requires),
            "establishes": list(action.establishes),
            "effects": list(action.effects),
            "inferred": action.inferred,
        }
        for ref, action in action_refs.items()
    ]

    return {
        "entry": graph.entry,
        "states": states,
        "actions": actions,
        "unexplored": graph.unexplored,
        "out_of_region": graph.out_of_region,
    }, action_refs


def _validate_model_output(
    result: Any,
) -> dict[str, Any]:

    if not isinstance(result, dict):
        raise ValueError("LLM output must be a JSON object.")

    expected = {
        "preconditions",
        "steps",
        "goal_conditions",
        "unresolved",
    }

    if set(result) != expected:
        raise ValueError(
            "LLM output contains missing or unexpected fields."
        )

    for name in expected:
        if not isinstance(result[name], list):
            raise ValueError(
                f"LLM field {name!r} must be a list."
            )

    return result


def _validate_references(
    result: dict[str, Any],
    graph: AppGraph,
    action_refs: dict[str, Action],
) -> dict[str, Any]:

    unresolved = result["unresolved"]
    seen_ids: set[str] = set()

    for item in unresolved:
        if not isinstance(item, dict):
            raise ValueError("Invalid unresolved requirement.")

        if not all(
            isinstance(item.get(key), str) and item[key].strip()
            for key in (
                "requirement_id",
                "description",
                "reason",
            )
        ):
            raise ValueError("Malformed unresolved requirement.")

        requirement_id = item["requirement_id"]

        if requirement_id in seen_ids:
            raise ValueError(
                f"Duplicate requirement ID: {requirement_id}"
            )

        seen_ids.add(requirement_id)

    for step in result["steps"]:

        if not isinstance(step, dict):
            raise ValueError("Invalid checklist step.")

        for key in (
            "requirement_id",
            "description",
            "kind",
        ):
            if (
                not isinstance(step.get(key), str)
                or not step[key].strip()
            ):
                raise ValueError(
                    f"Invalid checklist step field: {key}"
                )

        requirement_id = step["requirement_id"]
        description = step["description"]
        kind = step["kind"]

        if requirement_id in seen_ids:
            raise ValueError(
                f"Duplicate requirement ID: {requirement_id}"
            )

        seen_ids.add(requirement_id)

        if kind == "state":

            target = step.get("target_id")
            action_ref = step.get("action_ref")

            if (
                action_ref is not None
                or not isinstance(target, str)
                or target not in graph.states
            ):
                unresolved.append({
                    "requirement_id": requirement_id,
                    "description": description,
                    "reason": f"Invalid state reference: {target!r}",
                })

                step["target_id"] = None
                step["action_ref"] = None

        elif kind == "action":

            ref = step.get("action_ref")
            target = step.get("target_id")

            if (
                target is not None
                or not isinstance(ref, str)
                or ref not in action_refs
            ):
                unresolved.append({
                    "requirement_id": requirement_id,
                    "description": description,
                    "reason": f"Invalid action reference: {ref!r}",
                })

                step["target_id"] = None
                step["action_ref"] = None

        else:

            unresolved.append({
                "requirement_id": requirement_id,
                "description": description,
                "reason": f"Invalid checkpoint kind: {kind!r}",
            })

            step["target_id"] = None
            step["action_ref"] = None

    for field_name in (
        "preconditions",
        "goal_conditions",
    ):
        for item in result[field_name]:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("description"), str)
                or not item["description"].strip()
            ):
                raise ValueError(
                    f"Malformed {field_name} entry."
                )

    return result

def resolve_checklist(
    checklist: str,
    graph: AppGraph,
    *,
    backend: LLMBackend | None = None,
) -> dict[str, Any]:

    if not isinstance(checklist, str) or not checklist.strip():
        raise ValueError("Please provide a flow checklist.")

    if not graph.states:
        raise ValueError("Cannot resolve a checklist against an empty graph.")

    graph_data, action_refs = graph_for_prompt(graph)

    backend = backend or backend_for(
        default_provider="openai"
    )

    raw = backend.complete_json(
        system=SYSTEM_PROMPT,
        user=(
            f"USER CHECKLIST:\n{checklist}\n\n"
            "DISCOVERED GRAPH:\n"
            f"{json.dumps(graph_data, ensure_ascii=False)}"
        ),
        schema=SCHEMA,
    )

    if isinstance(raw, dict):
        result = raw
    else:
        try:
            result = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError(
                "LLM returned invalid JSON for checklist resolution."
            ) from exc

    result = _validate_model_output(result)

    result = _validate_references(
        result,
        graph,
        action_refs,
    )

    result["_action_refs"] = action_refs

    return result


@dataclass
class Verification:

    status: str
    exists: bool | None
    path: list[dict[str, Any]]
    matched_steps: list[str]
    missing_step: str | None = None
    explanation: str = ""
    matched_requirement_ids: list[str] = field(
        default_factory=list
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "exists": self.exists,
            "path": self.path,
            "matched_steps": self.matched_steps,
            "matched_requirement_ids": self.matched_requirement_ids,
            "missing_step": self.missing_step,
            "explanation": self.explanation,
        }


@dataclass
class Feasibility:

    status: str
    satisfied_conditions: list[str] = field(default_factory=list)
    unmet_conditions: list[str] = field(default_factory=list)
    explanation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "satisfied_conditions": self.satisfied_conditions,
            "unmet_conditions": self.unmet_conditions,
            "explanation": self.explanation,
        }


def _action_to_dict(action: Action) -> dict[str, Any]:
    return {
        "source": action.source,
        "action": action.label,
        "target": action.target,
        "affordance_key": action.affordance_key,
        "inputs": [
            {"field": key, "value": value}
            for key, value in action.inputs
        ],
        "inferred": action.inferred,
        "requires": list(action.requires),
        "establishes": list(action.establishes),
        "effects": list(action.effects),
    }


def verify_checklist(
    graph: AppGraph,
    resolved: dict[str, Any],
    *,
    start_state: str | None = None,
) -> Verification:

    if resolved["unresolved"]:
        return Verification(
            status="inconclusive",
            exists=None,
            path=[],
            matched_steps=[],
            explanation=(
                "Some checklist requirements could not be "
                "mapped reliably to discovered graph objects."
            ),
        )

    steps = resolved["steps"]

    if not steps:
        return Verification(
            status="inconclusive",
            exists=None,
            path=[],
            matched_steps=[],
            explanation=(
                "No navigation or action checkpoints were resolved."
            ),
        )

    current = start_state if start_state is not None else graph.entry

    if current not in graph.states:
        return Verification(
            status="inconclusive",
            exists=None,
            path=[],
            matched_steps=[],
            explanation="The graph has no valid starting state.",
        )

    path: list[dict[str, Any]] = []
    matched: list[str] = []
    matched_ids: list[str] = []

    def append_walk(walk: list[Action]) -> None:
        for action in walk:
            path.append(_action_to_dict(action))

    def missing(
        description: str,
        message: str,
    ) -> Verification:

        closed = graph.is_closed()

        return Verification(
            status="not_found" if closed else "inconclusive",
            exists=False if closed else None,
            path=path,
            matched_steps=matched,
            matched_requirement_ids=matched_ids,
            missing_step=description,
            explanation=(
                message
                if closed
                else (
                    message
                    + " The discovered graph contains unexplored "
                    "actions, so an alternative route may exist."
                )
            ),
        )

    action_refs = resolved.get("_action_refs", {})

    for step in steps:

        description = step["description"]
        requirement_id = step["requirement_id"]

        if step["kind"] == "state":

            target = step["target_id"]

            if target is None:
                return missing(
                    description,
                    "The requested state was not resolved.",
                )

            walk = graph.path_between(current, target)

            if walk is None:
                return missing(
                    description,
                    f"No discovered path from {current!r} to {target!r}.",
                )

            append_walk(walk)
            current = target

        elif step["kind"] == "action":

            action = action_refs.get(step["action_ref"])

            if action is None:
                return missing(
                    description,
                    "The requested action was not resolved.",
                )

            walk = graph.path_between(
                current,
                action.source,
            )

            if walk is None:
                return missing(
                    description,
                    f"Cannot reach the source of action {action.label!r}.",
                )

            append_walk(walk)

            path.append(_action_to_dict(action))

            current = action.target

        else:
            return missing(
                description,
                f"Unsupported checkpoint kind: {step['kind']!r}.",
            )

        matched.append(description)
        matched_ids.append(requirement_id)

    return Verification(
        status="found",
        exists=True,
        path=path,
        matched_steps=matched,
        matched_requirement_ids=matched_ids,
        explanation=(
            "All required checkpoints are connected in order "
            "in the discovered graph."
        ),
    )

def _normalize_condition(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def analyze_feasibility(
    verification: Verification,
    resolved: dict[str, Any],
) -> Feasibility:

    if verification.status != "found":
        return Feasibility(
            status="inconclusive",
            explanation=(
                "Feasibility cannot be assessed because the "
                "required graph route was not established."
            ),
        )

    if resolved["unresolved"]:
        return Feasibility(
            status="inconclusive",
            explanation=(
                "Unresolved checklist requirements prevent "
                "reliable feasibility analysis."
            ),
        )

    known_conditions: set[str] = set()
    assumed_conditions = {
        _normalize_condition(item["description"])
        for item in resolved.get("preconditions", [])
    }

    satisfied: list[str] = []
    unmet: list[str] = []
    inferred_actions: list[str] = []

    for condition in assumed_conditions:
        satisfied.append(
            f"User-provided precondition assumed: {condition}"
        )

    for edge in verification.path:

        required = {
            _normalize_condition(condition)
            for condition in edge["requires"]
        }

        established = {
            _normalize_condition(condition)
            for condition in edge["establishes"]
        }

        missing = required - known_conditions
        assumed = missing & assumed_conditions
        unestablished = missing - assumed

        action_name = edge["action"]

        if assumed:
            for condition in sorted(assumed):
                unmet.append(
                    f"Action '{action_name}' depends on "
                    f"'{condition}', which is only a user-provided "
                    "precondition and has not been independently verified."
                )

        if unestablished:
            for condition in sorted(unestablished):
                unmet.append(
                    f"Action '{action_name}' requires "
                    f"'{condition}', which has not been established "
                    "by preceding graph actions."
                )

        if not missing:
            satisfied.append(
                f"Action '{action_name}' has its declared "
                "requirements established."
            )

        if edge["inferred"]:
            inferred_actions.append(action_name)

        if not missing:
            known_conditions.update(established)

    if inferred_actions:
        for action_name in inferred_actions:
            unmet.append(
                f"Action '{action_name}' is inferred rather than "
                "directly observed during discovery."
            )

    if unmet:
        return Feasibility(
            status="inconclusive",
            satisfied_conditions=satisfied,
            unmet_conditions=unmet,
            explanation=(
                "A connected graph route exists, but its "
                "executability cannot be established with the "
                "available evidence. Some conditions are "
                "unverified or some transitions were inferred."
            ),
        )

    return Feasibility(
        status="feasible",
        satisfied_conditions=satisfied,
        unmet_conditions=[],
        explanation=(
            "The discovered route has no unsatisfied declared "
            "action requirements. All route transitions are "
            "observed and their recorded requirements are "
            "established by preceding graph actions. This is "
            "static feasibility analysis, not proof of runtime "
            "correctness or goal-condition satisfaction."
        ),
    )


def check_flow(
    checklist: str,
    graph: AppGraph,
    *,
    backend: LLMBackend | None = None,
    start_state: str | None = None,
) -> dict[str, Any]:

    resolved = resolve_checklist(
        checklist,
        graph,
        backend=backend,
    )

    verification = verify_checklist(
        graph,
        resolved,
        start_state=start_state,
    )

    feasibility = analyze_feasibility(
        verification,
        resolved,
    )

    return {
        "checklist": checklist,
        "status": verification.status,
        "exists": verification.exists,
        "feasibility_status": feasibility.status,
        "feasibility": feasibility.to_dict(),
        "path": verification.path,
        "matched_steps": verification.matched_steps,
        "matched_requirement_ids": (
            verification.matched_requirement_ids
        ),
        "missing_step": verification.missing_step,
        "explanation": verification.explanation,
        "preconditions": resolved["preconditions"],
        "goal_conditions": resolved["goal_conditions"],
        "unresolved": resolved["unresolved"],
    }


if __name__ == "__main__":

    import argparse

    parser = argparse.ArgumentParser(
        description="Check a user-provided flow against an AppGraph."
    )

    parser.add_argument(
        "--graph",
        required=True,
        help="Baseline graph.json",
    )

    parser.add_argument(
        "--checklist",
        help="Flow checklist in plain English",
    )

    args = parser.parse_args()

    graph = AppGraph.load(args.graph)

    checklist = args.checklist

    if checklist is None:
        print("Enter your checklist; finish with END.")

        lines = []

        while True:
            try:
                line = input("> ")
            except EOFError:
                break

            if line.strip().upper() == "END":
                break

            lines.append(line)

        checklist = "\n".join(lines)

    if not checklist.strip():
        parser.error("A non-empty checklist is required.")

    print(
        json.dumps(
            check_flow(checklist, graph),
            indent=2,
            ensure_ascii=False,
        )
    )
