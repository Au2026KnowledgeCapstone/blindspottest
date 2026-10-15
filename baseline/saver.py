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
from secrets import token_hex
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
    from knowledge.contracts import BehaviorContract, suggestions
    proposed = suggestions(BehaviorContract.model_validate(instance.contract)) if instance.contract else []
    return TestRecord(
        test_id=instance.test_id,
        kind=FLOW,
        invariant=instance.invariant,
        instance=instance.to_dict(),
        observations=dict(result.observations),
        verdict=verdict.to_dict(),
        states=list(result.states),
        evidence={"steps": [s.to_dict() for s in result.steps],
                  "notes": result.notes, "cleanup_verified": result.reset, "suggestions": proposed},
        error=result.error,
        duration_ms=result.duration_ms,
    )


def build(
    *,
    graph,
    tests: Iterable[TestRecord],
    readables: dict[str, dict] | None = None,
    model: str = "",
    coverage: dict | None = None,
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
        coverage=coverage or {},
    )


def save(record: BaselineRecord, directory: Path | str) -> Path:
    """Write a baseline into `directory`, which must not already exist."""
    import shutil
    from baseline.record_schema import BaselineError
    directory = Path(directory)
    if directory.exists():
        raise BaselineError(f"baseline {directory} already exists; choose a new version directory")
    directory.parent.mkdir(parents=True, exist_ok=True)

    # Staged under a dotted name next to the target so an interrupted save
    # leaves something recognisably not a baseline, then renamed into place.
    #
    # `mkdir` rather than `mkdtemp`: the mode mkdtemp picks is 0700, and
    # `os.rename` carries it onto the published baseline, so the baseline
    # would be readable only by the user who recorded it. A CI job uploading
    # it as an artifact, a container running as another UID, or a reviewer on
    # a shared machine would all be locked out of a directory whose whole
    # purpose is to be read later. `mkdir` applies the umask like every other
    # file this writes. The suffix only has to beat a concurrent save; it is
    # not a security boundary, and `mkdir` fails rather than reusing a
    # directory if one somehow collides.
    staging = directory.with_name(f".{directory.name}-{os.getpid()}-{token_hex(4)}")
    staging.mkdir()
    try:
        _write_json(staging / GRAPH_FILE, record.graph)
        _write_json(staging / READABLES_FILE, record.readables)
        _write_json(staging / TESTS_FILE, {"tests": [t.to_dict() for t in record.tests]})
        _write_json(staging / MANIFEST_FILE, record.manifest())
        # Atomic publication: the complete directory appears together.
        os.rename(staging, directory)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return directory


def summarize(record: BaselineRecord) -> str:
    """A short human description of what was recorded."""
    counts = record.manifest()["counts"]
    return (
        f"{counts['states']} states, {counts['actions']} actions, "
        f"{counts['page_tests']} page test(s), "
        f"{counts['flow_tests']} flow test(s)"
    )
