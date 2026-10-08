"""Rule base of testable invariants, and the interpreter that applies them.

The deliberate architectural choice here is that no invariant is expressed in
Python. There is no `if interaction_type == "persistent_mutation":` anywhere in
this file. Instead:

    interaction_type  ->  which invariants apply       (rule base: applies_to)
    candidate         ->  whether one is applicable    (rule base: requirements)
    invariant         ->  what the runner does         (rule base: procedure)
    observations      ->  whether it held              (rule base: expected_relation)

What Python supplies is only a *vocabulary* — named predicates and named steps
that the JSON composes. Adding an invariant is a data change. Adding a new kind
of primitive is a code change. That split is the point: the behaviour under
test is declared, auditable, and diffable, rather than buried in control flow,
and it does not come from the language model. The model only says "this
interaction looks like persistent state"; everything about what that means and
how it is checked lives in `invariants/`.
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

INVARIANTS_DIR = Path(__file__).parent / "invariants"


# --------------------------------------------------------------------------
# Vocabulary: requirements
#
# A requirement asks "does this candidate supply what the procedure needs?".
# The rule base names them and chooses the combination; these are the only
# names it may use.
# --------------------------------------------------------------------------

Predicate = Callable[[Any, dict], bool]


def _element(candidate, snapshot: dict, element_id: str | None) -> dict | None:
    if element_id is None:
        return None
    return next(
        (e for e in snapshot.get("elements", []) if e.get("id") == element_id), None
    )


def _editable_target(candidate, snapshot: dict) -> bool:
    """The field named by the candidate exists and accepts user input."""
    el = _element(candidate, snapshot, candidate.field_id)
    return bool(el and el.get("editable"))


def _commit_action(candidate, snapshot: dict) -> bool:
    """A distinct control exists that commits the change."""
    el = _element(candidate, snapshot, candidate.commit_action_id)
    return bool(el and not el.get("editable"))


def _no_commit_action(candidate, snapshot: dict) -> bool:
    """No commit control — the field is expected to save on its own."""
    return candidate.commit_action_id is None


def _observable_post_commit_state(candidate, snapshot: dict) -> bool:
    """The field's value can be read back after a reload.

    Needs a value to compare and a selector to re-find the element by, since
    snapshot ids are only stable within a single snapshot.
    """
    el = _element(candidate, snapshot, candidate.field_id)
    if not el:
        return False
    return ("value" in el or "checked" in el) and bool(el.get("selector"))


REQUIREMENT_PREDICATES: dict[str, Predicate] = {
    "editable_target": _editable_target,
    "commit_action": _commit_action,
    "no_commit_action": _no_commit_action,
    "observable_post_commit_state": _observable_post_commit_state,
}


# --------------------------------------------------------------------------
# Vocabulary: procedure steps
#
# Each name maps to the observation it records, or None for a pure side
# effect. The runner implements the behaviour; the engine only needs to know
# what each step contributes, so it can verify a procedure actually produces
# the values its relation refers to.
# --------------------------------------------------------------------------

STEP_RECORDS: dict[str, str | None] = {
    "observe_initial_value": "initial_value",
    "generate_mutation": "mutation_value",
    "apply_mutation": "committed_value",
    "commit": None,
    "observe_success": "success_observed",
    "settle": None,
    "reload": None,
    "observe_final_value": "final_value",
    # Teardown. BlindSpot mutates real application state, so an invariant
    # declares how to put it back. These run in a finally block, so they
    # execute even when the procedure above failed part-way.
    "restore_original_value": "restored_value",
    "commit_restore": None,
}


# --------------------------------------------------------------------------
# Relations
# --------------------------------------------------------------------------

# `<=` and `>=` precede `<` and `>` in the alternation, so the two-character
# operators are never split by a greedy single-character match.
_RELATION_RE = re.compile(r"^\s*(\S+)\s*(==|!=|<=|>=|<|>)\s*(\S+)\s*$")
_LITERALS = {"true": True, "false": False, "null": None}

# A single-argument call over one observation, e.g. `sum(line_totals)`. One
# argument and no spaces, deliberately: this is a relation language, not an
# expression language, and anything needing more belongs in a named step that
# records its answer as an observation.
_CALL_RE = re.compile(r"^([a-z_]+)\(([A-Za-z_][A-Za-z0-9_]*)\)$")


# --------------------------------------------------------------------------
# Vocabulary: relation functions
#
# Page-level invariants only ever needed to compare two scalars. Flow-level
# ones assert over collections — the total equals the sum of the lines, the
# rows are in the order that was asked for — and none of those is expressible
# as `a == b`. These are the named reductions the rule base may apply to an
# observation, and the same split holds as everywhere else: Python supplies
# the names, the JSON chooses which to use.
# --------------------------------------------------------------------------


class RelationError(Exception):
    """A relation could not be evaluated against the observations it was given.

    Distinct from `InvariantError`, which is a malformed rule base caught at
    load time. This is a well-formed relation meeting data it cannot reduce —
    `sum` over a list holding `None` because one row failed to parse. The
    honest verdict is inconclusive, never a violation: the application may be
    perfectly correct and the observation simply unreadable.
    """


def _as_list(value: Any, fn: str) -> list:
    if not isinstance(value, (list, tuple)):
        raise RelationError(f"{fn}() needs a list, got {type(value).__name__}")
    return list(value)


def _as_numbers(value: Any, fn: str) -> list[float]:
    items = _as_list(value, fn)
    numbers = []
    for item in items:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise RelationError(
                f"{fn}() needs numbers, got {item!r} in the list"
            )
        numbers.append(float(item))
    return numbers


def _fn_sum(value: Any) -> float:
    return sum(_as_numbers(value, "sum"))


def _fn_count(value: Any) -> int:
    return len(_as_list(value, "count"))


def _fn_sorted_asc(value: Any) -> bool:
    """True when the list never decreases.

    Non-strict on purpose: two products at the same price are not a sort
    defect, and requiring strictness would report one every time a catalog
    holds a duplicate value.
    """
    numbers = _as_numbers(value, "sorted_asc")
    return all(a <= b for a, b in zip(numbers, numbers[1:]))


def _fn_sorted_desc(value: Any) -> bool:
    numbers = _as_numbers(value, "sorted_desc")
    return all(a >= b for a, b in zip(numbers, numbers[1:]))


def _fn_unique(value: Any) -> bool:
    items = _as_list(value, "unique")
    return len(items) == len({json.dumps(i, sort_keys=True) for i in items})


def _fn_len(value: Any) -> int:
    """Length of a list or string. `count` is the list-only spelling."""
    if isinstance(value, str):
        return len(value)
    return len(_as_list(value, "len"))


RELATION_FUNCTIONS: dict[str, Callable[[Any], Any]] = {
    "sum": _fn_sum,
    "count": _fn_count,
    "len": _fn_len,
    "sorted_asc": _fn_sorted_asc,
    "sorted_desc": _fn_sorted_desc,
    "unique": _fn_unique,
}

# Money arrives as a float parsed out of rendered text, so `12.80 + 7.20` is
# not reliably `20.00`. A sum over line items compared for exact equality
# against a rendered total would report a violation on representation error
# alone, which is a false defect in the one invariant most likely to matter.
_FLOAT_TOLERANCE = 1e-6


def _compare(left: Any, right: Any, operator: str) -> bool:
    numeric = all(
        isinstance(v, (int, float)) and not isinstance(v, bool) for v in (left, right)
    )
    if operator in ("==", "!="):
        if numeric:
            equal = abs(float(left) - float(right)) <= _FLOAT_TOLERANCE
        else:
            equal = left == right
        return equal if operator == "==" else not equal

    if not numeric:
        raise RelationError(
            f"{operator} needs numbers, got {type(left).__name__} and "
            f"{type(right).__name__}"
        )
    if operator == "<":
        return left < right
    if operator == ">":
        return left > right
    if operator == "<=":
        return left <= right
    return left >= right


@dataclass(frozen=True)
class Relation:
    """A comparison between two operands.

    An operand is a literal, the name of an observation, or a named function
    applied to one observation:

        committed_value == final_value       scalar equality
        sum(line_totals) == order_total      a reduction over a collection
        sorted_asc(row_prices) == true       an order predicate
        resource_reachable == false          a probe's answer
        count(rows) == stated_count          two observations, one reduced
    """

    source: str
    left: str
    operator: str
    right: str

    @classmethod
    def parse(cls, text: str) -> Relation:
        match = _RELATION_RE.match(text)
        if not match:
            raise InvariantError(
                f"cannot parse relation {text!r}; expected "
                f"'<operand> <op> <operand>' where <op> is one of "
                f"==, !=, <, >, <=, >="
            )
        left, operator, right = match.groups()

        # A call to a function the vocabulary does not have is a rule-base
        # typo, and catching it here means it surfaces at load time rather
        # than as an inconclusive verdict halfway through a browser run.
        for token in (left, right):
            call = _CALL_RE.match(token)
            if call and call.group(1) not in RELATION_FUNCTIONS:
                raise InvariantError(
                    f"unknown relation function {call.group(1)!r} in {text!r}. "
                    f"Known: {', '.join(sorted(RELATION_FUNCTIONS))}"
                )
            if not call and "(" in token:
                raise InvariantError(
                    f"cannot parse operand {token!r} in {text!r}; a function "
                    f"call takes exactly one observation name and no spaces, "
                    f"e.g. sum(line_totals)"
                )

        return cls(source=text, left=left, operator=operator, right=right)

    @property
    def operands(self) -> set[str]:
        """Observation names the relation reads, excluding literals.

        A call contributes the name it wraps: `sum(line_totals)` needs
        `line_totals` to have been recorded, so a procedure that never
        records it is a rule-base bug the loader can catch.
        """
        names = set()
        for token in (self.left, self.right):
            if self._is_literal(token):
                continue
            call = _CALL_RE.match(token)
            names.add(call.group(2) if call else token)
        return names

    @staticmethod
    def _is_literal(token: str) -> bool:
        if token.lower() in _LITERALS or token.startswith(('"', "'")):
            return True
        try:
            float(token)
        except ValueError:
            return False
        return True

    @staticmethod
    def _resolve(token: str, observations: dict) -> Any:
        low = token.lower()
        if low in _LITERALS:
            return _LITERALS[low]
        if token.startswith(('"', "'")):
            return token[1:-1]
        try:
            return float(token) if "." in token else int(token)
        except ValueError:
            pass

        call = _CALL_RE.match(token)
        if call:
            name, argument = call.group(1), call.group(2)
            return RELATION_FUNCTIONS[name](observations[argument])
        return observations[token]

    def evaluate(self, observations: dict) -> bool:
        """Whether the relation holds.

        Raises `RelationError` when the observations cannot support the
        comparison. Callers turn that into an inconclusive verdict rather
        than letting a parse failure in one row read as a defect.
        """
        left = self._resolve(self.left, observations)
        right = self._resolve(self.right, observations)
        return _compare(left, right, self.operator)

    def missing_operands(self, observations: dict) -> set[str]:
        return {name for name in self.operands if name not in observations}


# --------------------------------------------------------------------------
# Invariants
# --------------------------------------------------------------------------


class InvariantError(Exception):
    """The rule base is malformed. Raised at load time, never mid-run."""


@dataclass(frozen=True)
class Invariant:
    id: str
    name: str
    description: str
    applies_to: tuple[str, ...]
    requirements: tuple[str, ...]
    procedure: tuple[str, ...]
    expected_relation: Relation
    precondition: Relation | None
    verification_strategy: str
    teardown: tuple[str, ...]
    on_violation: dict
    source: Path

    @classmethod
    def from_dict(cls, data: dict, source: Path) -> Invariant:
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

        # Validate every name against the vocabulary now, so a typo in the rule
        # base surfaces at load time rather than halfway through a browser run.
        for requirement in raw["requirements"]:
            if requirement not in REQUIREMENT_PREDICATES:
                raise InvariantError(
                    f"{ident}: unknown requirement {requirement!r}. "
                    f"Known: {', '.join(sorted(REQUIREMENT_PREDICATES))}"
                )
        for step in tuple(raw["procedure"]) + tuple(data.get("teardown", ())):
            if step not in STEP_RECORDS:
                raise InvariantError(
                    f"{ident}: unknown procedure step {step!r}. "
                    f"Known: {', '.join(sorted(STEP_RECORDS))}"
                )

        relation = Relation.parse(data["expected_relation"])
        precondition = (
            Relation.parse(data["precondition"]) if data.get("precondition") else None
        )

        # A relation referring to something the procedure never records can
        # only ever be inconclusive, which is a rule-base bug worth catching.
        recorded = {STEP_RECORDS[s] for s in raw["procedure"]} - {None}
        for rel, label in ((relation, "expected_relation"), (precondition, "precondition")):
            if rel is None:
                continue
            unrecorded = rel.operands - recorded
            if unrecorded:
                raise InvariantError(
                    f"{ident}: {label} refers to {', '.join(sorted(unrecorded))}, "
                    f"which no step in its procedure records"
                )

        return cls(
            id=ident,
            expected_relation=relation,
            precondition=precondition,
            verification_strategy=data.get("verification_strategy", "reload_and_compare"),
            teardown=tuple(data.get("teardown", ())),
            on_violation=data.get("on_violation", {}),
            source=source,
            **raw,
        )

    def unmet_requirements(self, candidate, snapshot: dict) -> list[str]:
        """Requirements this candidate fails to satisfy."""
        return [
            name
            for name in self.requirements
            if not REQUIREMENT_PREDICATES[name](candidate, snapshot)
        ]


# --------------------------------------------------------------------------
# Plans and verdicts
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ElementRef:
    """A concrete element, named as the snapshot saw it and as the runner
    will re-find it. `element_id` is stable only within one snapshot;
    `selector` is what survives a reload."""

    element_id: str
    accessible_name: str | None
    locator_strategy: dict
    selector: str

    def to_dict(self) -> dict:
        return {
            "element_id": self.element_id,
            "accessible_name": self.accessible_name,
            "locator_strategy": self.locator_strategy,
            "selector": self.selector,
        }

    @classmethod
    def of(cls, element: dict) -> ElementRef:
        return cls(
            element_id=element["id"],
            accessible_name=(
                element.get("accessible_name")
                or element.get("label")
                or element.get("text")
            ),
            locator_strategy=element.get(
                "locator_strategy", {"type": "css", "selector": element["selector"]}
            ),
            selector=element["selector"],
        )

    @classmethod
    def from_dict(cls, data: dict) -> ElementRef:
        return cls(
            element_id=data["element_id"],
            accessible_name=data.get("accessible_name"),
            locator_strategy=data.get("locator_strategy") or {},
            selector=data.get("selector", ""),
        )


@dataclass(frozen=True)
class TestInstance:
    """A generic invariant fused with specific application elements.

    This is the central artifact. The rule base says:

        persistent state must survive a reload

    A test instance says:

        Bio must contain "BlindSpot Test 8472" after reload

    The first is knowledge; the second is an executable, serializable,
    reproducible assertion about one field on one page. Every value the run
    needs is materialized here *before* execution, so an instance can be
    logged, diffed, re-run verbatim, or handed to someone else — none of which
    works if the mutation value is invented mid-run.
    """

    test_id: str
    invariant: str
    url: str
    target: ElementRef
    commit_action: ElementRef | None
    original_value: Any
    mutation_value: Any
    mutation_strategy: str
    verification_strategy: str
    procedure: tuple[str, ...]
    teardown: tuple[str, ...]
    expected_relation: str
    precondition: str | None
    success_signal: dict | None

    def to_dict(self) -> dict:
        return {
            "test_id": self.test_id,
            "invariant": self.invariant,
            "url": self.url,
            "target": self.target.to_dict(),
            "commit_action": (
                self.commit_action.to_dict() if self.commit_action else None
            ),
            "original_value": self.original_value,
            "mutation_value": self.mutation_value,
            "mutation_strategy": self.mutation_strategy,
            "verification_strategy": self.verification_strategy,
            "procedure": list(self.procedure),
            "teardown": list(self.teardown),
            "expected_relation": self.expected_relation,
            "precondition": self.precondition,
            "success_signal": self.success_signal,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TestInstance:
        """Rebuild an instance from its record.

        This is what makes a baseline re-runnable: every value the run needs
        was materialized before execution, so loading one back reproduces the
        same test rather than a freshly-generated lookalike. A new mutation
        value would mean the regression run and the baseline were never
        asserting the same thing.
        """
        commit = data.get("commit_action")
        return cls(
            test_id=data["test_id"],
            invariant=data["invariant"],
            url=data["url"],
            target=ElementRef.from_dict(data["target"]),
            commit_action=ElementRef.from_dict(commit) if commit else None,
            original_value=data.get("original_value"),
            mutation_value=data.get("mutation_value"),
            mutation_strategy=data.get("mutation_strategy", "text"),
            verification_strategy=data.get(
                "verification_strategy", "reload_and_compare"
            ),
            procedure=tuple(data.get("procedure", ())),
            teardown=tuple(data.get("teardown", ())),
            expected_relation=data["expected_relation"],
            precondition=data.get("precondition"),
            success_signal=data.get("success_signal"),
        )

    def describe(self) -> str:
        """The instance as a human-readable assertion."""
        name = self.target.accessible_name or self.target.selector
        return f"{name} must contain {self.mutation_value!r} after reload"


# --------------------------------------------------------------------------
# Vocabulary: mutation strategies
#
# How to produce a *different* value for a given kind of field. Named so an
# instance records which one it used, and so unsupported field types fail
# loudly at instantiation rather than silently doing nothing at run time.
# --------------------------------------------------------------------------


class InstantiationError(Exception):
    """A candidate cannot be turned into a runnable test."""


def _mutate_text(el: dict, rng) -> str:
    """A recognizable, unique string. Deliberately boring.

    No LLM, no cleverness, no attempt at realistic-looking data. The value
    only has to be different from what was there and identifiable in a page,
    a database, or a log.
    """
    return f"BlindSpot_{rng.randrange(16 ** 6):06x}"


MUTATION_STRATEGIES: dict[str, Callable[[dict, Any], Any]] = {
    "text": _mutate_text,
}

# Scope for v1: free-text fields only. Everything else — checkboxes, selects,
# dates, sliders, file inputs, contenteditable rich text, custom widgets — is
# deliberately out. Each needs its own interaction model in the runner, not
# just its own value generator, and none of them is needed to demonstrate a
# persistence blindspot.
#
# `email`/`url`/`tel` are excluded too: they look like text fields but enforce
# a format, so an arbitrary marker string fails browser validation on submit
# and the test would fail for a reason that has nothing to do with persistence.
_TEXT_INPUT_TYPES = frozenset({"text", "search"})


def strategy_for(element: dict) -> str:
    """Pick a mutation strategy, or refuse the field.

    Refusing is a feature: an unsupported field lands in `skipped` with a
    reason, instead of producing a test that silently does nothing.
    """
    tag = element.get("tag")
    if tag == "textarea":
        return "text"
    if tag == "input" and element.get("type") in _TEXT_INPUT_TYPES:
        return "text"

    raise InstantiationError(
        f"{element.get('id')}: unsupported field type for v1 "
        f"(tag={tag!r} type={element.get('type')!r} role={element.get('role')!r}); "
        f"only text inputs and textareas are supported"
    )


def _current_value(element: dict) -> Any:
    """What the field holds now — `checked` for checkables, else `value`."""
    return element["checked"] if "checked" in element else element.get("value")


@dataclass(frozen=True)
class Skipped:
    """A candidate no invariant could be applied to, and why."""

    candidate: Any
    invariant_id: str
    unmet: tuple[str, ...]


class Status(str, Enum):
    HOLDS = "holds"
    VIOLATED = "violated"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class Verdict:
    """The outcome of evaluating one invariant against one run's observations.

    This is the only place a defect is declared, and it is declared by the
    rule base's relation — not by the language model, which never sees the run.
    """

    invariant_id: str
    status: Status
    detail: str
    observations: dict = field(default_factory=dict)
    # Carried from the invariant's on_violation block, and only when the
    # invariant actually failed — a holding invariant has no severity.
    severity: str | None = None
    summary: str | None = None

    def to_dict(self) -> dict:
        return {
            "invariant_id": self.invariant_id,
            "status": self.status.value,
            "detail": self.detail,
            "observations": self.observations,
            "severity": self.severity,
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Verdict:
        return cls(
            invariant_id=data["invariant_id"],
            status=Status(data["status"]),
            detail=data.get("detail", ""),
            observations=data.get("observations", {}),
            severity=data.get("severity"),
            summary=data.get("summary"),
        )


def _try_evaluate(relation: Relation, observations: dict) -> tuple[bool | None, str]:
    """Evaluate a relation, reporting unevaluable instead of raising.

    A relation that cannot be reduced against its observations is not evidence
    of a defect — a currency string one row failed to parse makes `sum`
    impossible without telling us anything about the application. The honest
    answer is that the test did not run, so `None` here becomes inconclusive.
    """
    try:
        return relation.evaluate(observations), ""
    except RelationError as exc:
        return None, str(exc)
    except Exception as exc:  # a malformed observation, not a verdict
        return None, f"{type(exc).__name__}: {exc}"


def decide(
    invariant_id: str,
    expected: Relation,
    precondition: Relation | None,
    observations: dict,
    *,
    on_violation: dict | None = None,
    violation_detail: Callable[[dict], str] | None = None,
) -> Verdict:
    """Turn observations into a verdict for one invariant.

    Shared by the page-level and flow-level engines so the logic that decides
    holds / violated / inconclusive exists exactly once. The two differ only
    in what they observe and how they phrase a failure, never in how a
    relation becomes a verdict.

    The relation is checked before the precondition, and the order matters. A
    precondition exists to stop us blaming the application for a commit we
    never confirmed — it must not discard a pass. If the expected relation
    held, the invariant held whether or not a confirmation appeared; demoting
    that to inconclusive would report a correctly-working application as
    untested.
    """
    missing = expected.missing_operands(observations)
    if missing:
        return Verdict(
            invariant_id=invariant_id,
            status=Status.INCONCLUSIVE,
            detail=(
                f"run did not record {', '.join(sorted(missing))}; "
                f"cannot evaluate {expected.source}"
            ),
            observations=observations,
        )

    held, problem = _try_evaluate(expected, observations)
    if held is None:
        return Verdict(
            invariant_id=invariant_id,
            status=Status.INCONCLUSIVE,
            detail=f"could not evaluate {expected.source}: {problem}",
            observations=observations,
        )

    if held:
        unconfirmed = False
        if precondition is not None:
            if precondition.missing_operands(observations):
                unconfirmed = True
            else:
                met, _ = _try_evaluate(precondition, observations)
                unconfirmed = met is not True
        detail = f"{expected.source} held"
        if unconfirmed:
            detail += (
                f" (note: {precondition.source} was not observed — the "
                f"relation held, but the commit was never confirmed)"
            )
        return Verdict(
            invariant_id=invariant_id,
            status=Status.HOLDS,
            detail=detail,
            observations=observations,
        )

    # The relation failed. Only now does an unconfirmed commit matter: we
    # never earned the right to expect the outcome, so this is inconclusive
    # rather than a defect.
    if precondition is not None:
        if precondition.missing_operands(observations):
            return Verdict(
                invariant_id=invariant_id,
                status=Status.INCONCLUSIVE,
                detail=f"precondition {precondition.source} was not observed",
                observations=observations,
            )
        met, problem = _try_evaluate(precondition, observations)
        if met is None:
            return Verdict(
                invariant_id=invariant_id,
                status=Status.INCONCLUSIVE,
                detail=(
                    f"could not evaluate precondition {precondition.source}: "
                    f"{problem}"
                ),
                observations=observations,
            )
        if not met:
            return Verdict(
                invariant_id=invariant_id,
                status=Status.INCONCLUSIVE,
                detail=(
                    f"precondition {precondition.source} did not hold; the "
                    f"commit was never confirmed, so the outcome is untested"
                ),
                observations=observations,
            )

    on_violation = on_violation or {}
    detail = (
        violation_detail(observations)
        if violation_detail
        else f"{expected.source} did not hold"
    )
    return Verdict(
        invariant_id=invariant_id,
        status=Status.VIOLATED,
        detail=detail,
        observations=observations,
        severity=on_violation.get("severity"),
        summary=on_violation.get("summary"),
    )


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------


class KnowledgeEngine:
    """Loads the rule base and applies it. Holds no invariant logic itself."""

    def __init__(self, invariants: list[Invariant]):
        self._invariants = invariants
        self._by_interaction: dict[str, list[Invariant]] = {}
        for inv in invariants:
            for interaction in inv.applies_to:
                self._by_interaction.setdefault(interaction, []).append(inv)

    @classmethod
    def load(cls, directory: Path | str = INVARIANTS_DIR) -> KnowledgeEngine:
        """Load every page-level *.json in `directory`.

        A file may hold one invariant object or a {"invariants": [...]} wrapper.

        Files declaring `"kind": "flow"` are skipped: flow invariants name a
        different requirement and step vocabulary, so parsing one here would
        fail on a perfectly valid rule. Selecting by declared kind rather than
        by filename lets both rule bases share this directory without either
        loader knowing the other's files.
        """
        directory = Path(directory)
        if not directory.is_dir():
            raise InvariantError(f"no invariant directory at {directory}")

        invariants: list[Invariant] = []
        seen: dict[str, Path] = {}

        for path in sorted(directory.glob("*.json")):
            data = json.loads(path.read_text())
            if isinstance(data, dict) and data.get("kind", "page") != "page":
                continue
            entries = data.get("invariants", [data]) if isinstance(data, dict) else data
            for entry in entries:
                inv = Invariant.from_dict(entry, path)
                if inv.id in seen:
                    raise InvariantError(
                        f"duplicate invariant id {inv.id!r} in {path.name} "
                        f"and {seen[inv.id].name}"
                    )
                seen[inv.id] = path
                invariants.append(inv)

        if not invariants:
            raise InvariantError(f"no invariants found in {directory}")
        return cls(invariants)

    def __len__(self) -> int:
        return len(self._invariants)

    @property
    def invariants(self) -> list[Invariant]:
        return list(self._invariants)

    def get(self, invariant_id: str) -> Invariant:
        for inv in self._invariants:
            if inv.id == invariant_id:
                return inv
        raise KeyError(invariant_id)

    def invariants_for(self, interaction_type: str) -> list[Invariant]:
        """The mapping the whole design turns on: interaction type -> rules.

        `persistent_mutation` resolves to PERSISTENCE_001 and PERSISTENCE_002
        because those invariants declare it in `applies_to`, not because this
        method knows anything about persistence.
        """
        return list(self._by_interaction.get(interaction_type, []))

    def _build_instance(
        self, inv: Invariant, candidate, snapshot: dict, test_id: str, rng
    ) -> TestInstance:
        """Fuse one invariant with one candidate's concrete elements."""
        field_el = _element(candidate, snapshot, candidate.field_id)
        commit_el = _element(candidate, snapshot, candidate.commit_action_id)

        original = _current_value(field_el)
        strategy_name = strategy_for(field_el)
        mutation = MUTATION_STRATEGIES[strategy_name](field_el, rng)

        # A mutation equal to the original makes the test vacuous: a page that
        # silently discards every edit would still satisfy
        # `committed_value == final_value`. Retry, then give up loudly.
        for _ in range(5):
            if mutation != original:
                break
            mutation = MUTATION_STRATEGIES[strategy_name](field_el, rng)
        else:
            raise InstantiationError(
                f"{candidate.field_id}: could not generate a value different "
                f"from the current one ({original!r}); the test would pass "
                f"even if the page discarded the edit"
            )

        return TestInstance(
            test_id=test_id,
            invariant=inv.id,
            url=snapshot.get("url", ""),
            target=ElementRef.of(field_el),
            commit_action=ElementRef.of(commit_el) if commit_el else None,
            original_value=original,
            mutation_value=mutation,
            mutation_strategy=strategy_name,
            verification_strategy=inv.verification_strategy,
            procedure=inv.procedure,
            expected_relation=inv.expected_relation.source,
            precondition=inv.precondition.source if inv.precondition else None,
            success_signal=(
                candidate.success_signal.model_dump(mode="json")
                if candidate.success_signal
                else None
            ),
            teardown=inv.teardown,
        )

    def instantiate(
        self,
        candidates,
        snapshot: dict,
        *,
        seed: int | None = None,
        start: int = 1,
    ) -> tuple[list[TestInstance], list[Skipped]]:
        """Turn candidates into executable test instances.

        For each candidate, every invariant whose interaction type matches and
        whose requirements are met becomes one instance. Invariants that
        matched but whose requirements went unmet are returned as `Skipped`, so
        "nothing was tested here" is never silent.

        `seed` makes mutation values reproducible — useful when re-running a
        report's instances verbatim.
        """
        if not isinstance(candidates, (list, tuple)):
            candidates = [candidates]

        rng = random.Random(seed)
        instances: list[TestInstance] = []
        skipped: list[Skipped] = []
        counter = start

        for candidate in candidates:
            for inv in self.invariants_for(candidate.interaction_type.value):
                unmet = inv.unmet_requirements(candidate, snapshot)
                if unmet:
                    skipped.append(
                        Skipped(
                            candidate=candidate,
                            invariant_id=inv.id,
                            unmet=tuple(unmet),
                        )
                    )
                    continue
                try:
                    instances.append(
                        self._build_instance(
                            inv, candidate, snapshot, f"test-{counter:03d}", rng
                        )
                    )
                except InstantiationError as exc:
                    skipped.append(
                        Skipped(
                            candidate=candidate,
                            invariant_id=inv.id,
                            unmet=(str(exc),),
                        )
                    )
                    continue
                counter += 1

        return instances, skipped

    def evaluate(self, invariant: Invariant, observations: dict) -> Verdict:
        """Decide whether the invariant held, given what the runner observed."""

        def detail(obs: dict) -> str:
            return (
                f"{invariant.expected_relation.source} did not hold: "
                f"committed {obs.get('committed_value')!r}, "
                f"found {obs.get('final_value')!r} after reload"
            )

        return decide(
            invariant.id,
            invariant.expected_relation,
            invariant.precondition,
            observations,
            on_violation=invariant.on_violation,
            violation_detail=detail,
        )


if __name__ == "__main__":
    engine = KnowledgeEngine.load()
    print(f"{len(engine)} invariant(s) loaded from {INVARIANTS_DIR}\n")
    for inv in engine.invariants:
        print(f"  {inv.id}  {inv.name}")
        print(f"    applies to  : {', '.join(inv.applies_to)}")
        print(f"    requires    : {', '.join(inv.requirements)}")
        print(f"    procedure   : {' -> '.join(inv.procedure)}")
        print(f"    expects     : {inv.expected_relation.source}")
        if inv.precondition:
            print(f"    conditioned : {inv.precondition.source}")
        print()
