"""Write a baseline to disk.

Assembling the record is the interesting part; writing it is four files. The
assembly is here rather than in `main.py` so that what counts as a baseline is
defined in one place — if a later build starts recording something new, the
regression side learns about it by reading this module rather than by being
kept in sync by hand.

Everything is written atomically. A baseline half-written by an interrupted
run would be read back as an application that lost half its routes, and the
regression report would be entirely false rather than merely incomplete.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from baseline.record_schema import (
    FLOW,
    GRAPH_FILE,
    MANIFEST_FILE,
    PAGE,
    READABLES_FILE,
    TESTS_FILE,
    BaselineRecord,
    TestRecord,
)


def _write_json(path: Path, payload) -> None:
    """Write JSON through a temporary file and one rename.

    `os.replace` is atomic on every platform this runs on, so a reader either
    sees the previous content or the complete new content, never a prefix.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    os.replace(temporary, path)


def page_test_record(instance, result, verdict) -> TestRecord:
    """One page-level test, as a baseline entry."""
    return TestRecord(
        test_id=instance.test_id,
        kind=PAGE,
        invariant=instance.invariant,
        instance=instance.to_dict(),
        observations=dict(result.observations),
        verdict=verdict.to_dict(),
        states=[],
        error=result.error,
        duration_ms=result.duration_ms,
    )


def flow_test_record(instance, result, verdict) -> TestRecord:
    """One flow-level test, as a baseline entry."""
    return TestRecord(
        test_id=instance.test_id,
        kind=FLOW,
        invariant=instance.invariant,
        instance=instance.to_dict(),
        observations=dict(result.observations),
        verdict=verdict.to_dict(),
        states=list(result.states),
        error=result.error,
        duration_ms=result.duration_ms,
    )


def build(
    *,
    graph,
    tests: Iterable[TestRecord],
    readables: dict[str, dict] | None = None,
    model: str = "",
) -> BaselineRecord:
    """Assemble a baseline record from a crawl and its test results."""
    return BaselineRecord(
        base_url=graph.base_url,
        entry=graph.entry,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        model=model,
        graph=graph.to_dict(),
        readables=readables or {},
        tests=list(tests),
    )


def save(record: BaselineRecord, directory: Path | str) -> Path:
    """Write a baseline into `directory`, creating it if needed."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    # The graph goes first and the manifest last: the manifest is what a
    # loader checks for, so writing it only once the parts it describes are
    # all on disk means an interrupted save leaves a directory that is
    # recognisably incomplete rather than one that looks whole and is not.
    _write_json(directory / GRAPH_FILE, record.graph)
    _write_json(directory / READABLES_FILE, record.readables)
    _write_json(
        directory / TESTS_FILE,
        {"tests": [t.to_dict() for t in record.tests]},
    )
    _write_json(directory / MANIFEST_FILE, record.manifest())

    return directory


def summarize(record: BaselineRecord) -> str:
    """A short human description of what was recorded."""
    counts = record.manifest()["counts"]
    return (
        f"{counts['states']} states, {counts['actions']} actions, "
        f"{counts['page_tests']} page test(s), "
        f"{counts['flow_tests']} flow test(s)"
    )
