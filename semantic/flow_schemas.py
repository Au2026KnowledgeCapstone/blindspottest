"""Pydantic contracts for the flow mapper's output.

The page-level mapper in `semantic.schemas` points at one element and says
"persistence testing applies here". This one reads an application *map* — the
summary projection of a crawled `AppGraph` — and points at a path through it:
"the states between here and there are a DELETE_RESOURCE, and this is where
its outcome is readable."

Two things the model supplies, and one it deliberately does not:

    supplies    which capability a region of the graph represents, and which
                state the flow ends in
    supplies    observation bindings — the selectors that make the flow's
                outcome readable as named values
    does NOT    the path itself. `AppGraph.path_to` computes that, breadth-first
                and deterministically, so a plan is correct by construction
                against the graph rather than merely plausible-looking.

That split is the whole reason discovery builds a graph. A model asked to
invent a six-step checkout path will produce something that reads well and
cannot be walked; a model asked only to name the destination cannot.

The bindings are the bridge between a generic invariant and a specific
application. `TOTAL_CONSISTENCY` says `sum(line_totals) == order_total`; it
has no idea that this application renders line totals in `.line .amount`.
Naming that is application knowledge, which is exactly what the model is for,
and `validate_flow_candidates` checks every selector actually resolves before
any of it reaches a browser.

Provider-neutral: nothing here imports an LLM SDK.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from semantic.schemas import strict_schema


class FlowCapability(str, Enum):
    """What a region of the graph lets a user *do*.

    A capability is a reachability claim, not a fixed sequence: inserting an
    upsell step into a checkout leaves the capability intact and lengthens
    the walk. Invariants attach to these names through their `applies_to`,
    the same way page-level ones attach to `persistent_mutation`.
    """

    CREATE_RESOURCE = "create_resource"
    UPDATE_RESOURCE = "update_resource"
    DELETE_RESOURCE = "delete_resource"
    AUTHENTICATE = "authenticate"
    SESSION_TERMINATION = "session_termination"
    SORT_COLLECTION = "sort_collection"
    FILTER_COLLECTION = "filter_collection"
    SEARCH = "search"
    MULTI_STEP_TRANSACTION = "multi_step_transaction"


class ExtractKind(str, Enum):
    """How to turn matching elements into one observation value.

    A closed vocabulary on purpose. The invariant's relation decides what to
    do with a list of numbers; this only decides how a list of numbers is
    read off a page, and a model that could name an arbitrary extraction
    would be writing the test rather than describing the application.
    """

    TEXT = "text"        # the first match's visible text, as a string
    TEXTS = "texts"      # every match's visible text, as a list of strings
    NUMBER = "number"    # the first match's text, parsed as a number
    NUMBERS = "numbers"  # every match's text, parsed as a list of numbers
    COUNT = "count"      # how many elements match, as an integer
    VALUE = "value"      # the first match's form value
    VISIBLE = "visible"  # whether at least one match is visible, as a bool


class ObservationBinding(BaseModel):
    """One named value the invariant's relation reads, and where to find it.

    `name` must be an operand the invariant's relation actually refers to.
    A binding nothing reads is harmless but wasted; a relation operand with
    no binding makes the test inconclusive, which the engine reports rather
    than guessing.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        description="The observation name, e.g. 'line_totals' or "
        "'order_total'. Must match an operand in the invariant's relation."
    )
    selector: str = Field(
        description="A CSS selector resolving to the element(s) holding this "
        "value. Prefer ids and stable class names over positional paths."
    )
    extract: ExtractKind = Field(
        description="How to read the matching elements into one value."
    )


class ProbeMode(str, Enum):
    """Which URL a reachability probe should request.

    Reachability is how a goal condition about something *not existing* gets
    checked. A deleted resource whose row vanished from the collection but is
    still served at its own URL is the canonical case, and no amount of
    looking at the page you landed on will reveal it — you have to go and ask
    for the thing that should be gone.
    """

    PATH = "path"          # a fixed path named here, e.g. "/account"
    CAPTURED = "captured"  # the URL the flow stood on just before the goal ran
    FINAL = "final"        # the URL the flow ended on


class ProbeSpec(BaseModel):
    """A request the runner makes after the flow, to test existence.

    The probe runs in the *same* session as the flow. A fresh session would
    be a stronger check for creation, but it is the wrong check for deletion
    and sign-out, where the session carrying the change is precisely what is
    under test.
    """

    model_config = ConfigDict(extra="forbid")

    mode: ProbeMode
    path: str = Field(
        description="Path to request when mode is 'path', e.g. '/account'. "
        "Empty string for the 'captured' and 'final' modes."
    )


class FlowCandidate(BaseModel):
    """One capability the map appears to expose, and how to read its outcome."""

    model_config = ConfigDict(extra="forbid")

    capability: FlowCapability

    goal_state_id: str = Field(
        description="Fingerprint id of the state the flow ends in, e.g. "
        "'s_1a2b3c4d5e'. Must be a state id present in the map."
    )

    setup_state_id: str | None = Field(
        description="Fingerprint id of the state the capability starts from — "
        "the last state before the actions that perform it. Null means the "
        "flow starts at the application's entry state."
    )

    bindings: list[ObservationBinding] = Field(
        description="How to read the flow's outcome as named values. Empty "
        "when the invariant needs only a reachability probe."
    )

    probe: ProbeSpec | None = Field(
        description="A reachability check to make after the flow, or null "
        "when the outcome is fully readable from the page."
    )

    applicability_confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="0-1. How strongly the map supports 'this capability is "
        "here and this is where it ends' — NOT how likely it is to be "
        "broken. Once the flow runs, whether its relation held is a "
        "deterministic comparison with no confidence attached.",
    )

    reasoning_summary: str = Field(
        max_length=400,
        description="One or two sentences on what in the map supports this "
        "reading. Describe evidence, not defects.",
    )


class FlowCandidateSet(BaseModel):
    """Top-level object the model returns."""

    model_config = ConfigDict(extra="forbid")

    candidates: list[FlowCandidate]


class RejectedFlowCandidate(BaseModel):
    """A candidate dropped during reference checking, kept for reporting.

    Schema validation proves the shape. It cannot prove `goal_state_id` names
    a state that exists, or that a path to it is walkable — which is exactly
    where a plausible-looking hallucination lands.
    """

    candidate: FlowCandidate
    reason: str


class FlowClassificationResult(BaseModel):
    """What `semantic.flow_classifier.classify_flows()` hands back."""

    base_url: str
    model: str
    candidates: list[FlowCandidate]
    rejected: list[RejectedFlowCandidate]


def flow_candidate_set_schema() -> dict:
    """JSON Schema for `FlowCandidateSet`, safe for strict structured outputs."""
    return strict_schema(FlowCandidateSet)
