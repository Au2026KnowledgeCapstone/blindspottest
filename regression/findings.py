"""What a regression comparison produces.

A finding is one difference between two runs, stated as a fact with a
direction. The direction is the part that makes a report readable, because
most of what changes between two builds is not a defect:

    regressed   an invariant that held now fails, or a test that ran now
                cannot. This is what someone is looking for.
    fixed       an invariant that failed now holds. Worth saying out loud —
                it is how you confirm a fix landed.
    changed     something is different and neither better nor worse. A route
                was added, a label was reworded, an observed value moved.
                Most findings are these, and burying the regressions in them
                is the main way a regression report becomes useless.

A finding deliberately carries no verdict about intent. A route that
disappeared might be a removed feature or a broken one; a changed total might
be a pricing update. The differ states what moved, the interpreter explains
what it probably means, and a human decides. That is the same split the
page-level pipeline already makes between the runner and the knowledge
engine, applied one level up.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Direction(str, Enum):
    REGRESSED = "regressed"
    FIXED = "fixed"
    CHANGED = "changed"


class Kind(str, Enum):
    """What sort of difference this is.

    Kept coarse. The value of a category is that a reader can skip a whole
    class of findings at a glance, which stops working once there are twenty
    of them.
    """

    VERDICT = "verdict"          # an invariant's status changed
    OBSERVATION = "observation"  # same status, different observed value
    RUN = "run"                  # a test errored, or stopped erroring
    MISSING = "missing"          # a test present in one run and not the other
    STRUCTURE = "structure"      # a route or transition appeared or vanished
    UI = "ui"                    # the control or value surface of a state moved


@dataclass(frozen=True)
class Finding:
    """One difference between a baseline and a later run."""

    kind: Kind
    direction: Direction
    subject: str          # a test id, a route, or a state id
    summary: str          # one line, the finding itself
    detail: str = ""      # supporting specifics
    before: Any = None
    after: Any = None
    invariant: str | None = None
    severity: str | None = None

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "direction": self.direction.value,
            "subject": self.subject,
            "summary": self.summary,
            "detail": self.detail,
            "before": self.before,
            "after": self.after,
            "invariant": self.invariant,
            "severity": self.severity,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Finding:
        return cls(
            kind=Kind(data["kind"]),
            direction=Direction(data["direction"]),
            subject=data["subject"],
            summary=data["summary"],
            detail=data.get("detail", ""),
            before=data.get("before"),
            after=data.get("after"),
            invariant=data.get("invariant"),
            severity=data.get("severity"),
        )


# Severity ranking used only for ordering a report. A regression outranks
# everything, because the question being asked is "did anything break".
_DIRECTION_RANK = {
    Direction.REGRESSED: 0,
    Direction.FIXED: 1,
    Direction.CHANGED: 2,
}
_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2, None: 3}
_KIND_RANK = {
    Kind.VERDICT: 0,
    Kind.MISSING: 1,
    Kind.RUN: 2,
    Kind.OBSERVATION: 3,
    Kind.STRUCTURE: 4,
    Kind.UI: 5,
}


@dataclass
class FindingSet:
    """Findings from one comparison, with the counts a summary needs."""

    findings: list[Finding] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.findings)

    def __iter__(self):
        return iter(self.findings)

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    def extend(self, findings) -> None:
        self.findings.extend(findings)

    def ranked(self) -> list[Finding]:
        """Most important first: regressions, then by severity, then by kind."""
        return sorted(
            self.findings,
            key=lambda f: (
                _DIRECTION_RANK[f.direction],
                _SEVERITY_RANK.get(f.severity, 3),
                _KIND_RANK[f.kind],
                f.subject,
            ),
        )

    def of_direction(self, direction: Direction) -> list[Finding]:
        return [f for f in self.findings if f.direction is direction]

    @property
    def regressions(self) -> list[Finding]:
        return self.of_direction(Direction.REGRESSED)

    def counts(self) -> dict:
        return {
            "total": len(self.findings),
            "regressed": len(self.of_direction(Direction.REGRESSED)),
            "fixed": len(self.of_direction(Direction.FIXED)),
            "changed": len(self.of_direction(Direction.CHANGED)),
            **{
                kind.value: sum(1 for f in self.findings if f.kind is kind)
                for kind in Kind
            },
        }

    def to_dict(self) -> dict:
        return {
            "summary": self.counts(),
            "findings": [f.to_dict() for f in self.ranked()],
        }
