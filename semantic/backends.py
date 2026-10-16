"""Provider layer: everything the system needs to ask an LLM for JSON.

Shared by all three stages that call a model — `semantic.page_classifier`,
`semantic.flow_classifier`, and `regression.interpreter`. None of them knows
which provider it is talking to; each one knows only `LLMBackend`, a system
prompt, a user prompt, and a JSON Schema.

Selecting a model:

    # explicit — the usual way
    classify(snapshot, backend=AnthropicBackend(model="claude-sonnet-5"))

    # environment, or a .env file in the working directory
    BLINDSPOT_LLM_PROVIDER=openai BLINDSPOT_LLM_MODEL=gpt-5

    # your own provider
    register_backend("acme", lambda model: AcmeBackend(model or "acme-1"))

`strict_schema` lives here rather than beside the models it is applied to
because the set of JSON Schema keywords a provider rejects in strict mode is
a property of the provider, not of any one schema.

Nothing in this module decides what to ask or what to do with the answer. The
stage owns the prompt; this owns the transport.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from pydantic import BaseModel

# Last resort when neither an explicit provider, the environment, nor the
# calling stage supplies one. Every stage does pass its own default
# (`DEFAULT_PROVIDER` in each module), so this is reached only by a caller
# that asked for nothing at all.
FALLBACK_PROVIDER = "anthropic"


@runtime_checkable
class LLMBackend(Protocol):
    """Minimum surface a stage needs from any provider.

    `complete_json` must return the model's raw response text, which should be
    a JSON object conforming to `schema`. Enforcing that server-side is
    preferred where the provider supports it; prompt-level instruction is an
    acceptable fallback, since the result is validated by the caller
    regardless.
    """

    model: str

    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        ...


class AnthropicBackend:
    """Uses Claude's strict structured outputs.

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
                f"{self.model} declined the request (stop_reason=refusal)."
            )

        return next(block.text for block in response.content if block.type == "text")


class OpenAIBackend:
    """OpenAI backend, using strict JSON-schema structured outputs.

    The schemas `strict_schema()` produces already satisfy OpenAI's strict
    mode: every object carries `additionalProperties: false` and lists every
    property in `required`. That is not a coincidence — the Pydantic models
    were written with no field defaults for exactly this reason.
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
            raise RuntimeError(f"{self.model} declined the request: {refusal}")
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

    `default_provider` lets a calling stage choose what to use when neither an
    explicit provider nor the environment supplies one. The flow and
    regression layers pass "openai"; the page classifier passes "anthropic",
    so adding those layers did not silently change what the original pipeline
    calls.
    """
    load_dotenv()
    provider = (
        provider
        or os.environ.get("BLINDSPOT_LLM_PROVIDER")
        or default_provider
        or FALLBACK_PROVIDER
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


# -- strict structured outputs ---------------------------------------------


# JSON Schema keywords the strict structured-output modes of most providers
# reject. We keep them on the Pydantic models — they still validate the parsed
# response client-side — and strip them from the schema we hand to the LLM.
_UNSUPPORTED_SCHEMA_KEYWORDS = frozenset({
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "pattern", "minItems", "maxItems", "uniqueItems",
    "minProperties", "maxProperties",
})


def _strip_unsupported(node: Any) -> Any:
    if isinstance(node, dict):
        # A `$ref` may not carry siblings: OpenAI rejects the whole schema with
        # "$ref cannot have keywords {'description'}". Pydantic emits exactly
        # that shape for any field whose type is a model or enum and which also
        # has a Field(description=...) — so the sibling is dropped rather than
        # the reference. Nothing is lost that the model needs: per-field
        # guidance belongs in the system prompt, which is where every one of
        # these descriptions is already stated at length.
        if "$ref" in node:
            return {"$ref": node["$ref"]}
        return {
            k: _strip_unsupported(v)
            for k, v in node.items()
            if k not in _UNSUPPORTED_SCHEMA_KEYWORDS
        }
    if isinstance(node, list):
        return [_strip_unsupported(item) for item in node]
    return node


def strict_schema(model: type[BaseModel]) -> dict:
    """JSON Schema for a Pydantic model, safe for strict structured outputs.

    Constraints like a 0-1 bound are removed here and enforced when the
    response is parsed instead, so an out-of-range value becomes a validation
    error rather than a schema-compilation error.

    Shared by every stage that asks a model for structured output — page
    candidates, flow candidates, regression interpretations — because the set
    of keywords providers reject is a property of the providers, not of any
    one schema.
    """
    return _strip_unsupported(model.model_json_schema())
