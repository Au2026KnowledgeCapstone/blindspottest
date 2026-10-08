"""Semantic mapper: hand a page snapshot to an LLM, get back testable candidates.

Provider-agnostic by construction. The classifier only knows about an
`LLMBackend` — anything that can take a system prompt, a user prompt, and a
JSON Schema, and return JSON text. Claude is the default backend because it
supports strict structured outputs, but nothing in the classification logic
depends on it.

Selecting a model:

    # explicit — the usual way
    classify(snapshot, backend=AnthropicBackend(model="claude-sonnet-5"))

    # environment, or a .env file in the working directory
    BLINDSPOT_LLM_PROVIDER=openai BLINDSPOT_LLM_MODEL=gpt-5

    # your own provider
    register_backend("acme", lambda model: AcmeBackend(model or "acme-1"))

The boundary this module enforces: the LLM says "persistence testing applies
here", never "this is broken". Nothing downstream of `classify()` treats a
candidate as a defect.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

from semantic.schemas import (
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


@runtime_checkable
class LLMBackend(Protocol):
    """Minimum surface the classifier needs from any provider.

    `complete_json` must return the model's raw response text, which should be
    a JSON object conforming to `schema`. Enforcing that server-side is
    preferred where the provider supports it; prompt-level instruction is an
    acceptable fallback, since the result is validated here regardless.
    """

    model: str

    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        ...


class AnthropicBackend:
    """Default backend. Uses Claude's strict structured outputs.

    The model is a constructor argument, not a constant — pass any Claude
    model id, or set BLINDSPOT_LLM_MODEL.
    """

    DEFAULT_MODEL = "claude-opus-5"

    def __init__(
        self,
        model: str | None = None,
        *,
        client=None,
        max_tokens: int = 16_000,
        effort: str | None = None,
    ):
        self.model = model or self.DEFAULT_MODEL
        self.max_tokens = max_tokens
        self.effort = effort
        self._client = client

    @property
    def client(self):
        # Imported lazily so the rest of the package works without the
        # anthropic SDK installed — relevant when another provider is in use.
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        output_config: dict = {"format": {"type": "json_schema", "schema": schema}}
        if self.effort:
            output_config["effort"] = self.effort

        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config=output_config,
        )

        # Safety classifiers can decline a request; content is then empty or
        # partial, so check before indexing into it.
        if response.stop_reason == "refusal":
            raise RuntimeError(
                f"{self.model} declined to classify this page "
                f"(stop_reason=refusal)."
            )

        return next(block.text for block in response.content if block.type == "text")


class OpenAIBackend:
    """OpenAI backend, using strict JSON-schema structured outputs.

    The schema `candidate_set_schema()` produces already satisfies OpenAI's
    strict mode: every object carries `additionalProperties: false` and lists
    every property in `required`. That is not a coincidence — the Pydantic
    models were written with no field defaults for exactly this reason.
    """

    DEFAULT_MODEL = "gpt-5"

    def __init__(self, model: str | None = None, *, client=None, max_tokens: int = 16_000):
        self.model = model or self.DEFAULT_MODEL
        self.max_tokens = max_tokens
        self._client = client

    @property
    def client(self):
        if self._client is None:
            try:
                import openai
            except ImportError:
                raise RuntimeError(
                    "the openai package is not installed — "
                    "run: .venv/bin/pip install openai"
                ) from None
            self._client = openai.OpenAI()  # reads OPENAI_API_KEY
        return self._client

    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "candidate_set",
                    "schema": schema,
                    "strict": True,
                },
            },
        )
        choice = response.choices[0]
        # Strict mode can still refuse; the refusal arrives in its own field
        # rather than as an exception.
        refusal = getattr(choice.message, "refusal", None)
        if refusal:
            raise RuntimeError(f"{self.model} declined to classify this page: {refusal}")
        return choice.message.content


_BACKENDS: dict[str, Callable[[str | None], LLMBackend]] = {
    "anthropic": lambda model: AnthropicBackend(model),
    "openai": lambda model: OpenAIBackend(model),
}


def register_backend(name: str, factory: Callable[[str | None], LLMBackend]) -> None:
    """Register a provider under `name`.

    The factory receives the configured model id (or None for the provider's
    own default) and returns something satisfying `LLMBackend`.
    """
    _BACKENDS[name.lower()] = factory


def available_backends() -> list[str]:
    return sorted(_BACKENDS)


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
    backend = backend or backend_from_env()

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


def load_dotenv(path: str | os.PathLike = ".env") -> None:
    """Read simple KEY=VALUE lines from a .env file into the environment.

    Deliberately tiny and dependency-free. Real environment variables always
    win, so an exported key is never silently overridden by a stale file.
    """
    file = Path(path)
    if not file.is_file():
        return
    for line in file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


def backend_for(
    provider: str | None = None,
    model: str | None = None,
    *,
    default_provider: str | None = None,
) -> LLMBackend:
    """Build a backend explicitly, falling back to the environment per field.

    `default_provider` lets a caller choose what to use when neither an
    explicit provider nor the environment supplies one. The flow and
    regression layers pass "openai"; the page-level classifier keeps
    Anthropic, so adding those layers does not silently change what the
    existing pipeline calls.
    """
    load_dotenv()
    provider = (
        provider
        or os.environ.get("BLINDSPOT_LLM_PROVIDER")
        or default_provider
        or DEFAULT_PROVIDER
    ).lower()
    model = model or os.environ.get("BLINDSPOT_LLM_MODEL") or None

    try:
        factory = _BACKENDS[provider]
    except KeyError:
        raise ValueError(
            f"Unknown LLM provider {provider!r}. "
            f"Registered: {', '.join(available_backends())}. "
            f"Use register_backend() to add your own."
        ) from None

    return factory(model)


def backend_from_env() -> LLMBackend:
    """Build a backend from BLINDSPOT_LLM_PROVIDER / BLINDSPOT_LLM_MODEL."""
    return backend_for()


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
        backend=backend_for(args.provider, args.model),
    )
    print(result.model_dump_json(indent=2))
