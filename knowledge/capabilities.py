"""What each capability name means, loaded from `capabilities.json`.

The rule base answers "which invariant applies to a capability". This
answers the question underneath it — "what *is* this capability" — which
until now was answered nowhere. `FlowCapability` supplied the vocabulary and
the system prompt supplied binding recipes; neither said what makes a region
a deletion rather than an archive.

Two consumers, deliberately the same text:

    semantic.flow_classifier   renders these into the system prompt, so the
                               model classifies against written criteria
                               instead of its prior for an English word
    knowledge.flows            reads `supported` to turn a capability with
                               no invariant into a reported gap

Keeping one source for both is the point. A definition the classifier is
held to and a definition the report explains itself with cannot drift apart
if there is only one of them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PATH = Path(__file__).parent / "capabilities.json"


class CapabilityError(Exception):
    """The capability definitions are missing or malformed."""


@dataclass(frozen=True)
class CapabilityDefinition:
    """One capability, as something a reader could apply by hand."""

    name: str
    definition: str
    signals: tuple[str, ...]
    excludes: tuple[str, ...]
    outcome: str
    supported: bool
    gap: str = ""

    @classmethod
    def from_dict(cls, name: str, data: dict) -> CapabilityDefinition:
        try:
            return cls(
                name=name,
                definition=data["definition"],
                signals=tuple(data.get("signals", ())),
                excludes=tuple(data.get("excludes", ())),
                outcome=data.get("outcome", ""),
                supported=bool(data["supported"]),
                gap=data.get("gap", ""),
            )
        except KeyError as exc:
            raise CapabilityError(f"capability {name!r}: missing field {exc}") from None

    def render(self) -> str:
        """The form shown to a model: a definition, then evidence either way."""
        lines = [f"{self.name}", f"  IS: {self.definition}"]
        for signal in self.signals:
            lines.append(f"  evidence for: {signal}")
        for exclude in self.excludes:
            lines.append(f"  NOT: {exclude}")
        if self.outcome:
            lines.append(f"  outcome: {self.outcome}")
        if not self.supported:
            lines.append(
                "  NOTE: no invariant exists for this yet. Propose it anyway "
                "when the application clearly has it — it is reported as a "
                "known coverage gap rather than tested."
            )
        return "\n".join(lines)


def load(path: str | Path = DEFAULT_PATH) -> dict[str, CapabilityDefinition]:
    """Read the definitions, keyed by capability name."""
    file = Path(path)
    if not file.is_file():
        raise CapabilityError(f"no capability definitions at {file}")
    try:
        data = json.loads(file.read_text())
    except json.JSONDecodeError as exc:
        raise CapabilityError(f"{file.name}: {exc}") from None

    raw = data.get("capabilities")
    if not isinstance(raw, dict) or not raw:
        raise CapabilityError(f"{file.name}: no 'capabilities' object")

    return {
        name: CapabilityDefinition.from_dict(name, body)
        for name, body in raw.items()
    }


def render_definitions(names: list[str] | None = None,
                       path: str | Path = DEFAULT_PATH) -> str:
    """The definitions block for a system prompt.

    `names` restricts and orders the output; the default is every definition
    in file order, which groups the supported ones first by convention.
    """
    definitions = load(path)
    chosen = names if names is not None else list(definitions)
    missing = [n for n in chosen if n not in definitions]
    if missing:
        raise CapabilityError(f"no definition for {', '.join(sorted(missing))}")
    return "\n\n".join(definitions[n].render() for n in chosen)


def unsupported(path: str | Path = DEFAULT_PATH) -> dict[str, str]:
    """Capability name -> why it has no invariant, for the ones that don't."""
    return {
        name: d.gap or "no invariant is defined for this capability"
        for name, d in load(path).items()
        if not d.supported
    }


if __name__ == "__main__":
    import sys

    if "--prompt" in sys.argv:
        print(render_definitions())
    else:
        for name, d in load().items():
            mark = " " if d.supported else "!"
            print(f"{mark} {name:24} {d.definition[:72]}")
        gaps = unsupported()
        if gaps:
            print(f"\n{len(gaps)} capability(ies) with no invariant: "
                  f"{', '.join(sorted(gaps))}")
