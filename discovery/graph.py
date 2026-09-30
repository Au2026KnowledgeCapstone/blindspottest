"""The application model: states, the actions between them, and what changed.

Nodes are states; edges are actions. A button is not a node — it is the label
on an edge — because the things invariants assert about are states, and
because control text is unstable across releases in a way that state identity
is not.

The distinction the whole flow-discovery exercise turns on lives here:

    observed path       a concrete walk someone took through this graph
    capability          a reachability claim: some walk exists from a state
                        satisfying a precondition to one satisfying a goal
    goal condition      a predicate over a state, not a state's identity
    invariant           a predicate over a state pair, or over a walk

Because a capability is reachability rather than a fixed sequence, inserting
an upsell step between contact and shipping adds a node and lengthens the
walk without invalidating anything. That is the property the demo app's
checkout flow exists to demonstrate.

Edges carry preconditions and effects, not just endpoints. A pure topology
graph produces plans that cannot be walked, because whether `/checkout` is
reachable depends on the cart being non-empty. `requires` and `establishes`
are what let a planner tell a real path from a plausible one — the same role
`REQUIREMENT_PREDICATES` already plays for invariants in `knowledge.engine`.

Nothing in this module talks to a browser. It is a data model plus
serialization, so a crawl can be cached to disk and re-read without paying
for it again — the same reason `main.py` grew `--candidates`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from discovery.fingerprint import Fingerprint, element_signature, fingerprint

SCHEMA_VERSION = "1.0"


# --------------------------------------------------------------------------
# Affordances
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Affordance:
    """Something a user can do in a state.

    `key` is derived from the element's signature rather than its position,
    because `page_inspector` numbers elements `e1, e2, ...` and those numbers
    are explicitly only stable within one snapshot. Inserting a single element
    renumbers everything after it, which would make every graph diff noise.
    """

    key: str
    kind: str  # "link" | "submit" | "button" | "field"
    label: str
    locator: dict
    href: str | None = None
    element_id: str | None = None  # the snapshot-local id, for this crawl only

    # `runner.persistence_runner.locate` resolves anything exposing these two
    # names. Matching them here means the crawler and the runner find elements
    # through exactly the same code — if they disagreed, a path the crawler
    # walked would not be replayable by the runner, which is the one guarantee
    # discovery has to provide.
    @property
    def locator_strategy(self) -> dict:
        return self.locator

    @property
    def selector(self) -> str:
        return self.locator.get("selector", "")

    def to_dict(self) -> dict:
        data = {"key": self.key, "kind": self.kind, "label": self.label,
                "locator": self.locator}
        if self.href:
            data["href"] = self.href
        return data

    @classmethod
    def from_dict(cls, data: dict) -> Affordance:
        return cls(key=data["key"], kind=data["kind"], label=data["label"],
                   locator=data["locator"], href=data.get("href"))


_FIELD_ROLES = {"textbox", "searchbox", "combobox", "listbox", "checkbox",
                "radio", "switch", "slider", "spinbutton"}


def _kind_of(element: dict) -> str | None:
    """Classify a snapshot element, or None if it is not actionable."""
    role = element.get("role") or ""
    tag = element.get("tag") or ""
    type_ = element.get("type") or ""

    if element.get("disabled"):
        return None
    if tag == "a" or role == "link":
        return "link"
    if type_ in ("submit", "image") or (tag == "button" and type_ != "button"):
        return "submit"
    if tag == "button" or role == "button":
        return "button"
    if element.get("editable") or role in _FIELD_ROLES:
        return "field"
    return None


def affordances_of(snapshot: dict) -> dict[str, Affordance]:
    """Extract the actionable elements of a snapshot, keyed stably.

    Duplicate signatures (three identical Delete buttons in a list) are
    disambiguated with an ordinal suffix. That ordinal is positional and so
    only as stable as row order, which is why a crawl should prefer the first
    instance of a repeated affordance and treat the rest as equivalent.
    """
    found: dict[str, Affordance] = {}
    seen: dict[str, int] = {}

    for element in snapshot.get("elements", []):
        kind = _kind_of(element)
        if kind is None:
            continue

        signature = element_signature(element)
        seen[signature] = seen.get(signature, 0) + 1
        ordinal = seen[signature]
        key = signature if ordinal == 1 else f"{signature}#{ordinal}"

        label = (
            element.get("text")
            or element.get("accessible_name")
            or element.get("label")
            or element.get("placeholder")
            or signature
        )

        found[key] = Affordance(
            key=key,
            kind=kind,
            label=label,
            locator=element.get("locator_strategy")
            or {"type": "css", "selector": element.get("selector", "")},
            href=element.get("href"),
            element_id=element.get("id"),
        )

    return found


# --------------------------------------------------------------------------
# States and actions
# --------------------------------------------------------------------------


@dataclass
class State:
    """One node: a place in the application with a distinct set of things to do."""

    id: str
    url_template: str
    title: str
    signature: tuple[str, ...]
    affordances: dict[str, Affordance] = field(default_factory=dict)
    # One representative snapshot, kept for the detail projection and for
    # instantiating tests. Later visits to the same state do not overwrite it.
    snapshot: dict | None = None
    # A concrete URL that reached this state, for replay and for reporting.
    example_url: str = ""
    visits: int = 0

    @property
    def name(self) -> str:
        """A short human label, for the summary projection."""
        slug = self.url_template.strip("/").split("?")[0]
        return slug.replace("/", ".") or "root"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "url_template": self.url_template,
            "title": self.title,
            "signature": list(self.signature),
            "affordances": [a.to_dict() for a in self.affordances.values()],
            "example_url": self.example_url,
            "visits": self.visits,
            "snapshot": self.snapshot,
        }

    @classmethod
    def from_dict(cls, data: dict) -> State:
        return cls(
            id=data["id"],
            url_template=data["url_template"],
            title=data.get("title", ""),
            signature=tuple(data.get("signature", ())),
            affordances={
                a["key"]: Affordance.from_dict(a) for a in data.get("affordances", [])
            },
            snapshot=data.get("snapshot"),
            example_url=data.get("example_url", ""),
            visits=data.get("visits", 0),
        )


@dataclass(frozen=True)
class Action:
    """One edge: firing an affordance in `source` landed in `target`.

    `inputs` records the field values supplied before firing, because the same
    affordance with different inputs can land somewhere different — a login
    form with valid credentials and one with invalid credentials are two
    edges, not one.

    `effects` is the fingerprint diff across the transition. It doubles as the
    edge's semantic content for an LLM (far cheaper than two snapshots) and as
    the effect annotation a planner needs.
    """

    source: str
    target: str
    affordance_key: str
    label: str
    inputs: tuple[tuple[str, str], ...] = ()
    effects: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    establishes: tuple[str, ...] = ()
    # True when the edge was deduced rather than walked. Site-wide navigation
    # is the case that matters: a sidebar link behaves identically from every
    # state, so firing it from all of them costs O(states x links) browser
    # actions to learn nothing. Recording it unwalked keeps the graph complete
    # and the crawl affordable — but a planner relying on one should know it
    # was never actually exercised.
    inferred: bool = False

    @property
    def is_self_loop(self) -> bool:
        return self.source == self.target

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "target": self.target,
            "affordance_key": self.affordance_key,
            "label": self.label,
            "inputs": [list(pair) for pair in self.inputs],
            "effects": list(self.effects),
            "requires": list(self.requires),
            "establishes": list(self.establishes),
            "inferred": self.inferred,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Action:
        return cls(
            source=data["source"],
            target=data["target"],
            affordance_key=data["affordance_key"],
            label=data.get("label", ""),
            inputs=tuple(tuple(p) for p in data.get("inputs", ())),
            effects=tuple(data.get("effects", ())),
            requires=tuple(data.get("requires", ())),
            establishes=tuple(data.get("establishes", ())),
            inferred=data.get("inferred", False),
        )


# --------------------------------------------------------------------------
# The graph
# --------------------------------------------------------------------------


@dataclass
class AppGraph:
    """States and the actions between them, for one build of one application."""

    entry: str = ""
    base_url: str = ""
    states: dict[str, State] = field(default_factory=dict)
    actions: list[Action] = field(default_factory=list)
    # Affordances that were seen but never fired, by state id. A frontier that
    # was not exhausted is the difference between "no path exists" and "I did
    # not look", and a closed-world claim depends on this being empty.
    unexplored: dict[str, list[str]] = field(default_factory=dict)
    # Affordances deliberately not followed because they leave the region
    # under test. Tracked apart from `unexplored` because they are a boundary
    # rather than a gap: the model is complete for the region it claims, and
    # counting a declared edge of the map as a hole in it would retract a
    # closed-world claim that was actually earned.
    out_of_region: dict[str, list[str]] = field(default_factory=dict)

    # -- construction ------------------------------------------------------

    def observe(self, snapshot: dict) -> State:
        """Record a snapshot, returning the state it belongs to.

        Idempotent: revisiting a state increments its visit count and leaves
        the stored representative snapshot alone, so the detail projection is
        stable across a crawl.
        """
        print_id = fingerprint(snapshot)
        state = self.states.get(print_id.id)

        if state is None:
            state = State(
                id=print_id.id,
                url_template=print_id.url_template,
                title=snapshot.get("title", ""),
                signature=print_id.signature,
                affordances=affordances_of(snapshot),
                snapshot=snapshot,
                example_url=snapshot.get("url", ""),
            )
            self.states[state.id] = state

        state.visits += 1
        return state

    def connect(self, action: Action) -> None:
        """Add an edge, ignoring exact duplicates."""
        for existing in self.actions:
            if (
                existing.source == action.source
                and existing.target == action.target
                and existing.affordance_key == action.affordance_key
                and existing.inputs == action.inputs
            ):
                return
        self.actions.append(action)

    # -- queries -----------------------------------------------------------

    def out_edges(self, state_id: str) -> list[Action]:
        return [a for a in self.actions if a.source == state_id]

    def in_edges(self, state_id: str) -> list[Action]:
        return [a for a in self.actions if a.target == state_id]

    def terminals(self) -> list[State]:
        """States with no outgoing edge to anywhere else."""
        return [
            s for s in self.states.values()
            if not [a for a in self.out_edges(s.id) if not a.is_self_loop]
        ]

    def orphans(self) -> list[State]:
        """States nothing reaches. Other than the entry, these signal a bug."""
        reached = {a.target for a in self.actions}
        return [s for s in self.states.values()
                if s.id != self.entry and s.id not in reached]

    def is_closed(self) -> bool:
        """True when every discovered affordance was fired.

        Only a closed graph licenses a negative result — "no path exists to
        this goal" rather than "I did not happen to find one".
        """
        return not any(self.unexplored.values())

    def path_to(self, target: str) -> list[Action] | None:
        """Shortest action sequence from the entry to `target`, if one exists.

        Breadth-first and deterministic: no model is involved in pathfinding,
        so a plan is correct by construction against the graph rather than
        plausible-looking. This is the primitive a capability check is built
        on, and the reason edges need to record their inputs.
        """
        if target == self.entry:
            return []
        if target not in self.states:
            return None

        queue: list[tuple[str, list[Action]]] = [(self.entry, [])]
        seen = {self.entry}

        while queue:
            current, walk = queue.pop(0)
            for action in self.out_edges(current):
                if action.target in seen:
                    continue
                extended = walk + [action]
                if action.target == target:
                    return extended
                seen.add(action.target)
                queue.append((action.target, extended))

        return None

    def reachable(self) -> set[str]:
        """Every state reachable from the entry."""
        seen = {self.entry}
        queue = [self.entry]
        while queue:
            for action in self.out_edges(queue.pop(0)):
                if action.target not in seen:
                    seen.add(action.target)
                    queue.append(action.target)
        return seen

    # -- persistence -------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "base_url": self.base_url,
            "entry": self.entry,
            "states": [s.to_dict() for s in self.states.values()],
            "actions": [a.to_dict() for a in self.actions],
            "unexplored": {k: v for k, v in self.unexplored.items() if v},
            "out_of_region": {k: v for k, v in self.out_of_region.items() if v},
        }

    def save(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2))
        return path

    @classmethod
    def from_dict(cls, data: dict) -> AppGraph:
        return cls(
            entry=data.get("entry", ""),
            base_url=data.get("base_url", ""),
            states={s["id"]: State.from_dict(s) for s in data.get("states", [])},
            actions=[Action.from_dict(a) for a in data.get("actions", [])],
            unexplored=data.get("unexplored", {}),
            out_of_region=data.get("out_of_region", {}),
        )

    @classmethod
    def load(cls, path: Path | str) -> AppGraph:
        return cls.from_dict(json.loads(Path(path).read_text()))


# --------------------------------------------------------------------------
# Graph comparison
# --------------------------------------------------------------------------


def graph_diff(before: AppGraph, after: AppGraph) -> dict:
    """What changed between two crawls.

    Run against two crawls of the same build this measures crawler stability;
    run against two builds it is the regression signal. Same computation, and
    a crawler that cannot pass the first use has nothing to say about the
    second.
    """
    before_states, after_states = set(before.states), set(after.states)

    def edge_key(a: Action) -> tuple:
        return (a.source, a.target, a.affordance_key)

    before_edges = {edge_key(a) for a in before.actions}
    after_edges = {edge_key(a) for a in after.actions}

    return {
        "states_added": sorted(
            f"{sid} ({after.states[sid].url_template})" for sid in after_states - before_states
        ),
        "states_removed": sorted(
            f"{sid} ({before.states[sid].url_template})" for sid in before_states - after_states
        ),
        "edges_added": sorted(map(str, after_edges - before_edges)),
        "edges_removed": sorted(map(str, before_edges - after_edges)),
        "identical": before_states == after_states and before_edges == after_edges,
    }
