"""Page mapper: hand ONE page snapshot to an LLM, get back testable candidates.

The narrower of the system's two semantic stages, and the original one:

    this module             one page snapshot  -> persistent_mutation candidates
    semantic.flow_classifier  an application map -> capability candidates

Scope is enforced by the schema, not just by the prompt: every candidate's
`interaction_type` is a `Literal[PERSISTENT_MUTATION]`, and that enum has one
member, because `knowledge/invariants/persistence.json` has invariants for
exactly one category. Broadening what this stage looks for means adding an
enum member, a candidate model, and an invariant — editing the prompt alone
would produce candidates that fail validation. Capabilities beyond persistence
(deletion, sorting, checkout, sign-out) belong to the flow classifier, which
can see across states; this one cannot, by construction.

It is reached two ways: `main.py --with-pages` during a baseline, and the
ad-hoc single-page pipeline (`make demo`, `make scan`, `make edit`).

Provider-agnostic by construction — it knows only `LLMBackend` from
`semantic.backends`, which owns model selection, `.env` loading, and the
strict-schema sanitisation.

The boundary this module enforces: the LLM says "persistence testing applies
here", never "this is broken". Nothing downstream of `classify()` treats a
candidate as a defect.
"""

from __future__ import annotations

import json

from semantic.backends import LLMBackend, backend_for
from semantic.page_schemas import (
    CandidateSet,
    ClassificationResult,
    PersistentMutationCandidate,
    RejectedCandidate,
    candidate_set_schema,
)

DEFAULT_PROVIDER = "anthropic"

SYSTEM_PROMPT = """\
You identify which interactions on a web page represent PERSISTENT, \
USER-CONTROLLED STATE — state a user sets, that is expected to survive a page \
reload.

You are given a simplified description of a page: its interactive elements, \
their roles, labels, current values, and nearby helper text. You return \
candidates that persistence testing should be applied to.

SCOPE — read this carefully:
  - Your job is "this LOOKS LIKE something persistence testing applies to."
  - Your job is NOT "this is broken." You have no evidence of behaviour; you \
are looking at a static description of a page that has not been interacted \
with. Nothing you see can tell you whether saving works.
  - Never speculate about bugs, data loss, or failures. `reasoning_summary` \
describes the EVIDENCE for your reading, not a defect.

WHAT QUALIFIES:
  - A FREE-TEXT field whose value the user would expect to persist: profile \
details, bios, display names, notes, descriptions.
  - Only these two shapes are in scope:
      * `"tag": "textarea"`
      * `"tag": "input"` with `"type": "text"` or `"type": "search"`
    Propose nothing else, however plausibly persistent it looks.
  - Pair it with the control that commits the change — usually a nearby \
button with text like Save, Update, Apply, or Submit.
  - If helper text indicates automatic saving ("saves automatically", \
"changes are saved as you type"), set `commit_action_id` to null and say so \
in `reasoning_summary`.

WHAT DOES NOT QUALIFY:
  - Any field outside the two shapes above. Checkboxes, radios, switches, \
selects/dropdowns, date and time pickers, number and range inputs, colour \
pickers, file uploads, and contenteditable rich-text editors are ALL out of \
scope for now — skip them silently, even when they clearly hold persistent \
state.
  - Email, url, and tel inputs. They look like text fields but enforce a \
format, so they are out of scope too.
  - Search boxes, filters, and sort controls — transient view state.
  - Login and signup fields, one-time codes, payment entry.
  - Fields marked `disabled` or `readonly`, or any element without \
`editable: true`.
  - Navigation links and buttons that only move between pages.

REFERENCES:
  - `field_id` and `commit_action_id` MUST be `id` values that appear in the \
snapshot you were given (e.g. "e3"). Never invent one.
  - `field_id` must name an element with `editable: true`.
  - `commit_action_id` must name a button or link, never an input field.

SUCCESS SIGNAL — how the page would confirm a save:
  - The only supported form is visible confirmation text: `type` is "text" \
and `expected_pattern` is a short lowercase substring, e.g. "saved \
successfully".
  - Base it on wording actually present in the snapshot, or clearly implied \
by it. Do not invent a confirmation message.
  - Set `success_signal` to null when the snapshot gives you nothing to go \
on. Null is much better than a guess: a wrong pattern makes the test \
inconclusive, while null lets the runner fall back to a weaker check.

APPLICABILITY_CONFIDENCE is how strongly the snapshot supports "the \
persistence invariant applies here" — NOT how likely the page is to be \
broken. You are not predicting defects. A clearly-labelled Bio field beside a \
Save button is high; an ambiguous unlabelled text input is low. Once the test \
runs, whether the value survived a reload is a deterministic comparison with \
no confidence attached.

Return only candidates you actually believe in. An empty list is a valid and \
correct answer for a page with no persistent state.\
"""


def build_user_prompt(snapshot: dict) -> str:
    """Render the page snapshot as the user turn."""
    return (
        "Here is a simplified description of a web page.\n\n"
        f"{json.dumps(snapshot, indent=2)}\n\n"
        "Identify the interactions that represent persistent, user-controlled "
        "state."
    )


def _check_references(
    candidate: PersistentMutationCandidate, elements: dict[str, dict]
) -> str | None:
    """Return a rejection reason, or None if every reference holds up.

    Schema validation guarantees the shape. It cannot guarantee that the ids
    the model produced exist, or that they point at the right kind of element —
    which is exactly where a plausible-looking hallucination lands.
    """
    field = elements.get(candidate.field_id)
    if field is None:
        return f"field_id {candidate.field_id!r} is not in the snapshot"
    if not field.get("editable"):
        reason = "disabled" if field.get("disabled") else (
            "readonly" if field.get("readonly") else "not editable"
        )
        return f"field_id {candidate.field_id!r} is {reason}"

    if candidate.commit_action_id is not None:
        # Checked before the editable test below, which would otherwise catch
        # this case first and report it less precisely.
        if candidate.commit_action_id == candidate.field_id:
            return "commit_action_id is the same element as field_id"
        action = elements.get(candidate.commit_action_id)
        if action is None:
            return (
                f"commit_action_id {candidate.commit_action_id!r} is not in "
                "the snapshot"
            )
        if action.get("editable"):
            return (
                f"commit_action_id {candidate.commit_action_id!r} is an "
                "editable field, not a commit control"
            )

    signal = candidate.success_signal
    if signal is not None:
        if not signal.is_coherent():
            return f"success_signal of type {signal.type.value!r} is missing its payload"
    return None


def validate_candidates(candidates, snapshot: dict):
    """Split candidates into (kept, rejected) by checking every reference.

    Applied wherever candidates enter the system — whether they came from a
    model or were loaded from a file. A candidate is only trustworthy relative
    to a snapshot, so the check belongs to the boundary, not to the LLM call.
    """
    elements = {e["id"]: e for e in snapshot.get("elements", [])}
    kept: list[PersistentMutationCandidate] = []
    rejected: list[RejectedCandidate] = []

    for candidate in candidates:
        reason = _check_references(candidate, elements)
        if reason is None:
            kept.append(candidate)
        else:
            rejected.append(RejectedCandidate(candidate=candidate, reason=reason))

    kept.sort(key=lambda c: c.applicability_confidence, reverse=True)
    return kept, rejected


def classify(
    snapshot: dict, *, backend: LLMBackend | None = None
) -> ClassificationResult:
    """Map a page snapshot to persistence-testing candidates.

    `snapshot` is the dict produced by `discovery.page_inspector`. Pass a
    `backend` to choose the provider and model, or let it come from the
    environment.
    """
    backend = backend or backend_for(default_provider=DEFAULT_PROVIDER)

    raw = backend.complete_json(
        system=SYSTEM_PROMPT,
        user=build_user_prompt(snapshot),
        schema=candidate_set_schema(),
    )

    # Pydantic is the first gate: anything structurally wrong fails here.
    parsed = CandidateSet.model_validate_json(raw)
    kept, rejected = validate_candidates(parsed.candidates, snapshot)

    return ClassificationResult(
        url=snapshot.get("url", ""),
        model=backend.model,
        candidates=kept,
        rejected=rejected,
    )


if __name__ == "__main__":
    import argparse

    from discovery.page_inspector import inspect_page

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("url")
    parser.add_argument("--provider", help=f"default: {DEFAULT_PROVIDER}")
    parser.add_argument("--model", help="provider-specific model id")
    args = parser.parse_args()

    result = classify(
        inspect_page(args.url),
        backend=backend_for(args.provider, args.model,
                            default_provider=DEFAULT_PROVIDER),
    )
    print(result.model_dump_json(indent=2))
