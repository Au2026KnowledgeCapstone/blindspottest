"""The shape of a saved baseline, and what each part of it is for.

A baseline is everything needed to ask the same questions of a later build:
the application's structure, the tests that were instantiated against it, and
the answers they got. Saving all three is the point — the pieces are only
useful together:

    graph        what the application exposed. Diffing two of these finds
                 structural change: a route that disappeared, a transition
                 that no longer exists.
    readables    the value surface of each state. Diffing two of these finds
                 presentational change a control snapshot cannot see.
    tests        the materialized instances, their observations, and their
                 verdicts. Re-running the *instances* is what makes a
                 comparison meaningful; re-deriving them would produce
                 different tests and compare nothing.

That last point is the one that constrains the format. An instance has every
value it needs fixed before it executes — the mutation string, the walk, the
selectors — so it can be written down and replayed verbatim. A baseline that
stored only "test the Bio field for persistence" would generate a fresh
mutation value on the regression run, and a changed observation would then be
indistinguishable from a changed input.

Stored as a directory rather than one file. The graph is by far the largest
part and is independently useful — `discovery.projections` will render a
baseline's `graph.json` directly — and keeping it separate means reading the
verdicts does not mean parsing megabytes of snapshots.

    <baseline>/
      manifest.json    metadata: when, against what, with which model
      graph.json       discovery.graph.AppGraph.to_dict()
      readables.json   {state_id: discovery.readable surface}
      tests.json       the instances, observations and verdicts
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = "1.0"

MANIFEST_FILE = "manifest.json"
GRAPH_FILE = "graph.json"
READABLES_FILE = "readables.json"
TESTS_FILE = "tests.json"

# Which runner and engine a record belongs to. Kept on every test rather than
# split across two files so the differ can walk one list and stay agnostic
# about where a test came from.
PAGE = "page"
FLOW = "flow"


@dataclass
class TestRecord:
    """One test: what it asserted, what it saw, and what that meant.

    `instance` is the serialized instance, and it is the re-runnable part.
    `observations` and `verdict` are what the baseline is compared against.
    `states` is only populated for flow tests, where it records the walk —
    useful for saying *where* a flow diverged rather than only that its
    outcome did.
    """

    test_id: str
    kind: str  # PAGE | FLOW
    invariant: str
    instance: dict
    observations: dict = field(default_factory=dict)
    verdict: dict = field(default_factory=dict)
    states: list[dict] = field(default_factory=list)
    error: str | None = None
    duration_ms: int = 0

    @property
    def status(self) -> str:
        return self.verdict.get("status", "inconclusive")

    def to_dict(self) -> dict:
        return {
            "test_id": self.test_id,
            "kind": self.kind,
            "invariant": self.invariant,
            "instance": self.instance,
            "observations": self.observations,
            "verdict": self.verdict,
            "states": self.states,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TestRecord:
        return cls(
            test_id=data["test_id"],
            kind=data["kind"],
            invariant=data["invariant"],
            instance=data.get("instance", {}),
            observations=data.get("observations", {}),
            verdict=data.get("verdict", {}),
            states=data.get("states", []),
            error=data.get("error"),
            duration_ms=data.get("duration_ms", 0),
        )


@dataclass
class BaselineRecord:
    """A complete baseline: structure, surface, and answers."""

    base_url: str
    entry: str
    created_at: str
    model: str = ""
    schema_version: str = SCHEMA_VERSION
    blindspot_version: str = "0.2"
    graph: dict = field(default_factory=dict)
    readables: dict[str, dict] = field(default_factory=dict)
    tests: list[TestRecord] = field(default_factory=list)

    def manifest(self) -> dict:
        """The metadata half, written to `manifest.json`.

        Counts are included so `blindspot --list-baselines`-style tooling and
        a human reading the directory can tell what is in it without loading
        the graph.
        """
        return {
            "schema_version": self.schema_version,
            "blindspot_version": self.blindspot_version,
            "created_at": self.created_at,
            "base_url": self.base_url,
            "entry": self.entry,
            "model": self.model,
            "counts": {
                "states": len(self.graph.get("states", [])),
                "actions": len(self.graph.get("actions", [])),
                "readables": len(self.readables),
                "tests": len(self.tests),
                "page_tests": sum(1 for t in self.tests if t.kind == PAGE),
                "flow_tests": sum(1 for t in self.tests if t.kind == FLOW),
            },
        }

    def by_test_id(self) -> dict[str, TestRecord]:
        return {t.test_id: t for t in self.tests}

    def of_kind(self, kind: str) -> list[TestRecord]:
        return [t for t in self.tests if t.kind == kind]


class BaselineError(Exception):
    """A baseline directory is missing, incomplete, or of an unknown version."""


def check_version(manifest: dict) -> None:
    """Refuse a baseline this build cannot read.

    A silently misread baseline is worse than a refused one: every field it
    fails to find becomes an apparent change, and the regression report fills
    with defects that are really format drift.
    """
    found = manifest.get("schema_version")
    if found != SCHEMA_VERSION:
        raise BaselineError(
            f"baseline schema version {found!r} is not {SCHEMA_VERSION!r}; "
            f"re-record the baseline with this build"
        )
