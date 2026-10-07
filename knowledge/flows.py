"""Rule base of flow-level invariants, and the interpreter that applies them.

The same architectural choice as `knowledge.engine`: no invariant is expressed
in Python. There is no `if capability == "delete_resource":` anywhere in this
file. The JSON composes a named vocabulary, and what Python supplies is only
the vocabulary itself:

    capability     ->  which invariants apply       (rule base: applies_to)
    candidate      ->  whether one is applicable    (rule base: requirements)
    invariant      ->  what the runner does         (rule base: procedure)
    observations   ->  whether it held              (rule base: expected_relation)

What makes a flow invariant different from a page-level one is not the
machinery, it is what the observations are *about*. A page-level invariant
compares two readings of one field. A flow-level one asserts over a
collection, across several states, or about whether something still exists:

    sum(line_totals) == order_total      a sum across the lines of one page
    sorted_asc(row_values) == true       an order over a collection
    probe_reachable == false             a resource that should be gone
    count(row_count) == stated_count     a page agreeing with itself

None of those is `a == b`, which is why `Relation` grew functions and
comparison operators. None of them is readable from one snapshot either,
which is why an invariant here declares the *names* it needs and a candidate
supplies the selectors that fill them. That two-sided contract is checked at
load time against the rule base and at instantiation against the candidate,
so a mismatch is reported as a skip with a reason rather than discovered as
an inconclusive verdict partway through a browser session.

Nothing in this module talks to a browser. It turns candidates into
executable, serializable instances and turns observations into verdicts;
`runner.flow_runner` performs them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from discovery.graph import Action, AppGraph
from knowledge.engine import (
    InvariantError,
    Relation,
    Status,
    Verdict,
    decide,
)

INVARIANTS_DIR = Path(__file__).parent / "invariants"


# --------------------------------------------------------------------------
# Vocabulary: requirements
#
# A flow requirement asks "does this candidate supply what the procedure
# needs?", the same question the page-level ones ask, but of a graph rather
# than a snapshot. The signature differs for that reason, which is also why
# the two dictionaries are kept apart instead of merged.
# --------------------------------------------------------------------------

FlowPredicate = Callable[[Any, AppGraph], bool]


def _goal_reachable(candidate, graph: AppGraph) -> bool:
    """A walkable path exists from the entry to the state the flow ends in."""
    return graph.path_to(candidate.goal_state_id) is not None


def _goal_differs_from_setup(candidate, graph: AppGraph) -> bool:
    """The capability actually has actions to perform.

    A candidate whose setup and goal are the same state describes no
    transition, so the goal walk would be empty and the invariant would
    assert about a flow that never ran. Catching it here beats reporting a
    confident pass on nothing.
    """
    setup = candidate.setup_state_id or graph.entry
    return setup != candidate.goal_state_id


def _probe_declared(candidate, graph: AppGraph) -> bool:
    """The candidate supplies a reachability probe.

    Required by the invariants whose goal condition is about something *not*
    existing. Without a probe those cannot be checked at all — the page you
    land on after a deletion looks identical whether or not the resource is
    still served — so running them anyway would manufacture an inconclusive
    result instead of saying what is missing.
    """
    return candidate.probe is not None


FLOW_REQUIREMENT_PREDICATES: dict[str, FlowPredicate] = {
    "goal_reachable": _goal_reachable,
    "goal_differs_from_setup": _goal_differs_from_setup,
    "probe_declared": _probe_declared,
}


# --------------------------------------------------------------------------
# Vocabulary: procedure steps
#
# Each name maps to the observations it records. A step may record several —
# a probe answers with a status and a landing URL as well as a verdict on
# reachability — so unlike the page-level table this one holds tuples.
#
# `observe_bindings` records nothing fixed. Its observations are named by the
# invariant's `bindings` declaration and filled from the candidate's
# selectors, which is the whole mechanism by which a generic invariant reads
# a specific application.
# --------------------------------------------------------------------------

FLOW_STEP_RECORDS: dict[str, tuple[str, ...]] = {
    "walk_setup": ("setup_reached",),
    "capture_probe_url": ("captured_url",),
    "walk_goal": ("goal_reached",),
    "observe_bindings": (),
    "probe": ("probe_reachable", "probe_status", "probe_url"),
    # Teardown. A flow mutates real application state and often cannot undo
    # itself by reversing its own steps — there is no un-place-an-order. The
    # reset is therefore session-shaped rather than action-shaped, and only
    # as complete as the application's own session scoping.
    "reset_session": (),
}


# --------------------------------------------------------------------------
# Invariants
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FlowInvariant:
    id: str
    name: str
    description: str
    applies_to: tuple[str, ...]
    requirements: tuple[str, ...]
    procedure: tuple[str, ...]
    bindings: tuple[str, ...]
    expected_relation: Relation
    precondition: Relation | None
    teardown: tuple[str, ...]
    on_violation: dict
    source: Path

    @classmethod
    def from_dict(cls, data: dict, source: Path) -> FlowInvariant:
        try:
            ident = data["id"]
            raw = {
                "name": data["name"],
                "description": data["description"],
                "applies_to": tuple(data["applies_to"]),
                "requirements": tuple(data["requirements"]),
                "procedure": tuple(data["procedure"]),
            }
        except KeyError as exc:
            raise InvariantError(f"{source.name}: missing field {exc}") from None

        bindings = tuple(data.get("bindings", ()))

        for requirement in raw["requirements"]:
            if requirement not in FLOW_REQUIREMENT_PREDICATES:
                raise InvariantError(
                    f"{ident}: unknown flow requirement {requirement!r}. "
                    f"Known: {', '.join(sorted(FLOW_REQUIREMENT_PREDICATES))}"
                )
        for step in tuple(raw["procedure"]) + tuple(data.get("teardown", ())):
            if step not in FLOW_STEP_RECORDS:
                raise InvariantError(
                    f"{ident}: unknown flow step {step!r}. "
                    f"Known: {', '.join(sorted(FLOW_STEP_RECORDS))}"
                )

        relation = Relation.parse(data["expected_relation"])
        precondition = (
            Relation.parse(data["precondition"]) if data.get("precondition") else None
        )

        # A relation operand has to come from somewhere: either a step in
        # this procedure records it, or the invariant declares it as a
        # binding for a candidate to supply. An operand from neither can only
        # ever be inconclusive, which is a rule-base bug worth catching at
        # load time rather than in a browser.
        recorded = {
            name for step in raw["procedure"] for name in FLOW_STEP_RECORDS[step]
        } | set(bindings)
        for rel, label in ((relation, "expected_relation"),
                           (precondition, "precondition")):
            if rel is None:
                continue
            unrecorded = rel.operands - recorded
            if unrecorded:
                raise InvariantError(
                    f"{ident}: {label} refers to {', '.join(sorted(unrecorded))}, "
                    f"which no step in its procedure records and no binding "
                    f"declares"
                )

        # A declared binding nothing reads is dead weight in the contract and
        # usually means a renamed operand, so it is worth refusing too.
        read = relation.operands | (precondition.operands if precondition else set())
        unread = set(bindings) - read
        if unread:
            raise InvariantError(
                f"{ident}: declares binding(s) {', '.join(sorted(unread))} that "
                f"no relation reads"
            )
        if bindings and "observe_bindings" not in raw["procedure"]:
            raise InvariantError(
                f"{ident}: declares bindings but its procedure never calls "
                f"observe_bindings"
            )

        # A probe navigates away from the page the flow ended on, so reading
        # bindings afterwards would read them off the probe's response
        # instead of off the outcome. The ordering is a correctness
        # requirement, not a style preference, and it is invisible in the
        # results when it is wrong — the values parse, they are simply the
        # wrong page's.
        procedure = list(raw["procedure"])
        if "probe" in procedure and "observe_bindings" in procedure:
            if procedure.index("probe") < procedure.index("observe_bindings"):
                raise InvariantError(
                    f"{ident}: probe runs before observe_bindings, so the "
                    f"bindings would read the probe's page rather than the "
                    f"flow's outcome"
                )

        return cls(
            id=ident,
            bindings=bindings,
            expected_relation=relation,
            precondition=precondition,
            teardown=tuple(data.get("teardown", ())),
            on_violation=data.get("on_violation", {}),
            source=source,
            **raw,
        )

    def unmet_requirements(self, candidate, graph: AppGraph) -> list[str]:
        return [
            name
            for name in self.requirements
            if not FLOW_REQUIREMENT_PREDICATES[name](candidate, graph)
        ]

    def missing_bindings(self, candidate) -> list[str]:
        """Binding names the invariant needs that the candidate did not supply."""
        supplied = {b.name for b in candidate.bindings}
        return sorted(set(self.bindings) - supplied)


# --------------------------------------------------------------------------
# Instances
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PathStep:
    """One replayable action in a flow's walk.

    Exposes `locator_strategy` and `selector` so the same `locate()` the
    page-level runner uses resolves these too. If the crawler and the flow
    runner found elements through different code, a path the crawler walked
    would not be replayable, which is the one guarantee discovery has to
    provide.
    """

    kind: str  # "click" | "fill"
    label: str
    locator: dict
    value: str | None = None

    @property
    def locator_strategy(self) -> dict:
        return self.locator

    @property
    def selector(self) -> str:
        return self.locator.get("selector", "")

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "label": self.label,
            "locator": self.locator,
            "value": self.value,
        }

    @classmethod
    def from_dict(cls, data: dict) -> PathStep:
        return cls(
            kind=data["kind"],
            label=data.get("label", ""),
            locator=data.get("locator") or {},
            value=data.get("value"),
        )


@dataclass(frozen=True)
class BindingSpec:
    """How one named observation is read off a page."""

    name: str
    selector: str
    extract: str

    def to_dict(self) -> dict:
        return {"name": self.name, "selector": self.selector,
                "extract": self.extract}

    @classmethod
    def from_dict(cls, data: dict) -> BindingSpec:
        return cls(name=data["name"], selector=data["selector"],
                   extract=data["extract"])


@dataclass(frozen=True)
class FlowTestInstance:
    """A generic flow invariant fused with one application's concrete walk.

    The rule base says:

        the total charged must equal the sum of the lines shown

    An instance says:

        after clicking Continue to checkout, Continue, Add to order,
        Continue to payment, Place order — the number in
        `table.totals tr.grand td:nth-child(2)` must equal the sum of the
        numbers in `table.totals tr:not(.grand) td:nth-child(2)`

    Every value the run needs is materialized here *before* execution, for
    the same reason the page-level instance is: an instance that re-derived
    its path at run time could not be logged, diffed, re-run verbatim, or
    compared against a baseline — and comparing against a baseline is the
    entire point of recording one.
    """

    test_id: str
    invariant: str
    capability: str
    base_url: str
    entry_url: str
    setup_state_id: str
    goal_state_id: str
    setup: tuple[PathStep, ...]
    goal: tuple[PathStep, ...]
    bindings: tuple[BindingSpec, ...]
    probe_mode: str | None
    probe_path: str
    procedure: tuple[str, ...]
    teardown: tuple[str, ...]
    expected_relation: str
    precondition: str | None
    # A mount point to remove from observed URLs before fingerprinting.
    #
    # Empty when an instance runs against the deployment it was recorded
    # against, which is the normal case. Set by `baseline.loader.retarget_instance`
    # when the same application is served somewhere else — staging under a
    # path, or the demo app's second build under `/broken`. Without it the
    # recorded state ids could never match, because a fingerprint includes
    # the route and `/broken/cart` is not `/cart`.
    route_prefix: str = ""

    def to_dict(self) -> dict:
        return {
            "test_id": self.test_id,
            "invariant": self.invariant,
            "capability": self.capability,
            "base_url": self.base_url,
            "entry_url": self.entry_url,
            "setup_state_id": self.setup_state_id,
            "goal_state_id": self.goal_state_id,
            "setup": [s.to_dict() for s in self.setup],
            "goal": [s.to_dict() for s in self.goal],
            "bindings": [b.to_dict() for b in self.bindings],
            "probe_mode": self.probe_mode,
            "probe_path": self.probe_path,
            "procedure": list(self.procedure),
            "teardown": list(self.teardown),
            "expected_relation": self.expected_relation,
            "precondition": self.precondition,
            "route_prefix": self.route_prefix,
        }

    @classmethod
    def from_dict(cls, data: dict) -> FlowTestInstance:
        return cls(
            test_id=data["test_id"],
            invariant=data["invariant"],
            capability=data["capability"],
            base_url=data.get("base_url", ""),
            entry_url=data.get("entry_url", ""),
            setup_state_id=data.get("setup_state_id", ""),
            goal_state_id=data.get("goal_state_id", ""),
            setup=tuple(PathStep.from_dict(s) for s in data.get("setup", ())),
            goal=tuple(PathStep.from_dict(s) for s in data.get("goal", ())),
            bindings=tuple(
                BindingSpec.from_dict(b) for b in data.get("bindings", ())
            ),
            probe_mode=data.get("probe_mode"),
            probe_path=data.get("probe_path", ""),
            procedure=tuple(data.get("procedure", ())),
            teardown=tuple(data.get("teardown", ())),
            expected_relation=data["expected_relation"],
            precondition=data.get("precondition"),
            route_prefix=data.get("route_prefix", ""),
        )

    def describe(self) -> str:
        """The instance as a human-readable assertion."""
        walk = " -> ".join(s.label for s in self.goal) or "(no actions)"
        return f"{self.capability}: {walk} then {self.expected_relation}"


@dataclass(frozen=True)
class FlowSkipped:
    """A flow candidate no invariant could be applied to, and why."""

    candidate: Any
    invariant_id: str
    unmet: tuple[str, ...]


# --------------------------------------------------------------------------
# Turning graph paths into replayable steps
# --------------------------------------------------------------------------


def path_steps(graph: AppGraph, actions: Iterable[Action]) -> tuple[PathStep, ...]:
    """Materialize a graph path into concrete, replayable steps.

    An action's recorded inputs are expanded into fills before its click,
    because the crawler only observed that transition *with* those inputs —
    submitting the same form empty is a different edge that usually lands on
    a validation error. Replaying the click alone would walk somewhere the
    graph never claimed.
    """
    steps: list[PathStep] = []
    for action in actions:
        source = graph.states.get(action.source)
        if source is None:
            raise InstantiationError(
                f"path references state {action.source!r}, which is not in the graph"
            )
        for field_key, value in action.inputs:
            field = source.affordances.get(field_key)
            if field is None:
                continue
            steps.append(
                PathStep(kind="fill", label=field.label, locator=field.locator,
                         value=value)
            )
        affordance = source.affordances.get(action.affordance_key)
        if affordance is None:
            raise InstantiationError(
                f"state {action.source!r} has no affordance "
                f"{action.affordance_key!r} to replay"
            )
        steps.append(
            PathStep(kind="click", label=affordance.label,
                     locator=affordance.locator)
        )
    return tuple(steps)


class InstantiationError(Exception):
    """A flow candidate cannot be turned into a runnable test."""


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------


class FlowEngine:
    """Loads the flow rule base and applies it. Holds no invariant logic."""

    def __init__(self, invariants: list[FlowInvariant]):
        self._invariants = invariants
        self._by_capability: dict[str, list[FlowInvariant]] = {}
        for inv in invariants:
            for capability in inv.applies_to:
                self._by_capability.setdefault(capability, []).append(inv)

    @classmethod
    def load(cls, directory: Path | str = INVARIANTS_DIR) -> FlowEngine:
        """Load every *.json in `directory` that declares flow invariants.

        Files are selected by their `kind`, not their name, so the page-level
        and flow-level rule bases can live side by side in one directory and
        neither loader has to know the other's filenames.
        """
        directory = Path(directory)
        if not directory.is_dir():
            raise InvariantError(f"no invariant directory at {directory}")

        invariants: list[FlowInvariant] = []
        seen: dict[str, Path] = {}

        for path in sorted(directory.glob("*.json")):
            data = json.loads(path.read_text())
            if not isinstance(data, dict) or data.get("kind") != "flow":
                continue
            for entry in data.get("invariants", []):
                inv = FlowInvariant.from_dict(entry, path)
                if inv.id in seen:
                    raise InvariantError(
                        f"duplicate flow invariant id {inv.id!r} in "
                        f"{path.name} and {seen[inv.id].name}"
                    )
                seen[inv.id] = path
                invariants.append(inv)

        return cls(invariants)

    def __len__(self) -> int:
        return len(self._invariants)

    @property
    def invariants(self) -> list[FlowInvariant]:
        return list(self._invariants)

    def get(self, invariant_id: str) -> FlowInvariant:
        for inv in self._invariants:
            if inv.id == invariant_id:
                return inv
        raise KeyError(invariant_id)

    def invariants_for(self, capability: str) -> list[FlowInvariant]:
        """The mapping the design turns on: capability -> rules.

        `delete_resource` resolves to DELETION_COMPLETENESS because that
        invariant declares it in `applies_to`, not because this method knows
        anything about deletion.
        """
        return list(self._by_capability.get(capability, []))

    def _build_instance(
        self,
        inv: FlowInvariant,
        candidate,
        graph: AppGraph,
        test_id: str,
    ) -> FlowTestInstance:
        """Fuse one flow invariant with one candidate's concrete walk."""
        setup_state = candidate.setup_state_id or graph.entry

        setup_actions = graph.path_to(setup_state)
        if setup_actions is None:
            raise InstantiationError(
                f"no walkable path from the entry to {setup_state!r}"
            )
        goal_actions = graph.path_between(setup_state, candidate.goal_state_id)
        if goal_actions is None:
            raise InstantiationError(
                f"no walkable path from {setup_state!r} to "
                f"{candidate.goal_state_id!r}"
            )
        if not goal_actions:
            raise InstantiationError(
                f"{setup_state!r} and {candidate.goal_state_id!r} are the same "
                f"state, so the capability has no actions to perform"
            )

        entry = graph.states.get(graph.entry)
        probe = candidate.probe

        return FlowTestInstance(
            test_id=test_id,
            invariant=inv.id,
            capability=candidate.capability.value,
            base_url=graph.base_url,
            entry_url=(entry.example_url if entry else "") or graph.base_url,
            setup_state_id=setup_state,
            goal_state_id=candidate.goal_state_id,
            setup=path_steps(graph, setup_actions),
            goal=path_steps(graph, goal_actions),
            # Only the bindings this invariant declared. A candidate may
            # supply more — the model does not know which invariant will pick
            # it up — and carrying extras into the instance would put values
            # in the record that nothing asserts about.
            bindings=tuple(
                BindingSpec(b.name, b.selector, b.extract.value)
                for b in candidate.bindings
                if b.name in inv.bindings
            ),
            probe_mode=probe.mode.value if probe else None,
            probe_path=probe.path if probe else "",
            procedure=inv.procedure,
            teardown=inv.teardown,
            expected_relation=inv.expected_relation.source,
            precondition=inv.precondition.source if inv.precondition else None,
        )

    def instantiate(
        self,
        candidates,
        graph: AppGraph,
        *,
        start: int = 1,
    ) -> tuple[list[FlowTestInstance], list[FlowSkipped]]:
        """Turn flow candidates into executable instances.

        For each candidate, every invariant whose capability matches and
        whose requirements and bindings are satisfied becomes one instance.
        Everything else is returned as `FlowSkipped`, so "nothing was tested
        here" is never silent.
        """
        if not isinstance(candidates, (list, tuple)):
            candidates = [candidates]

        instances: list[FlowTestInstance] = []
        skipped: list[FlowSkipped] = []
        counter = start

        for candidate in candidates:
            for inv in self.invariants_for(candidate.capability.value):
                unmet = inv.unmet_requirements(candidate, graph)
                missing = inv.missing_bindings(candidate)
                if missing:
                    unmet = unmet + [
                        f"no binding supplied for {name}" for name in missing
                    ]
                if unmet:
                    skipped.append(
                        FlowSkipped(candidate=candidate, invariant_id=inv.id,
                                    unmet=tuple(unmet))
                    )
                    continue
                try:
                    instances.append(
                        self._build_instance(
                            inv, candidate, graph, f"flow-{counter:03d}"
                        )
                    )
                except InstantiationError as exc:
                    skipped.append(
                        FlowSkipped(candidate=candidate, invariant_id=inv.id,
                                    unmet=(str(exc),))
                    )
                    continue
                counter += 1

        return instances, skipped

    def evaluate(self, invariant: FlowInvariant, observations: dict) -> Verdict:
        """Decide whether the flow invariant held."""

        def detail(obs: dict) -> str:
            shown = ", ".join(
                f"{name}={obs[name]!r}"
                for name in sorted(invariant.expected_relation.operands)
                if name in obs
            )
            return f"{invariant.expected_relation.source} did not hold: {shown}"

        return decide(
            invariant.id,
            invariant.expected_relation,
            invariant.precondition,
            observations,
            on_violation=invariant.on_violation,
            violation_detail=detail,
        )


if __name__ == "__main__":
    engine = FlowEngine.load()
    print(f"{len(engine)} flow invariant(s) loaded from {INVARIANTS_DIR}\n")
    for inv in engine.invariants:
        print(f"  {inv.id}  {inv.name}")
        print(f"    applies to  : {', '.join(inv.applies_to)}")
        print(f"    requires    : {', '.join(inv.requirements) or '-'}")
        print(f"    bindings    : {', '.join(inv.bindings) or '-'}")
        print(f"    procedure   : {' -> '.join(inv.procedure)}")
        print(f"    expects     : {inv.expected_relation.source}")
        if inv.precondition:
            print(f"    conditioned : {inv.precondition.source}")
        print()
