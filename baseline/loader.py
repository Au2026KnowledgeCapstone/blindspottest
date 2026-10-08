"""Read a baseline back, and rehydrate its tests into runnable instances.

The load itself is unremarkable. What matters is `instances()`: it turns the
stored dictionaries back into the exact `TestInstance` and `FlowTestInstance`
objects the baseline run executed, so a regression run asserts the same
things with the same inputs against new code.

That is the whole mechanism. Nothing is re-classified and nothing is
re-instantiated on a regression run — no LLM is called at all, which is also
why a regression run is cheap and repeatable. A pipeline that re-derived its
tests would be comparing two different test suites and attributing the
difference to the application.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

from baseline.record_schema import (
    FLOW,
    GRAPH_FILE,
    MANIFEST_FILE,
    PAGE,
    READABLES_FILE,
    TESTS_FILE,
    BaselineError,
    BaselineRecord,
    TestRecord,
    check_version,
)
from discovery.graph import AppGraph
from knowledge.engine import TestInstance
from knowledge.flows import FlowTestInstance


def _read_json(path: Path, *, required: bool = True, default=None):
    if not path.exists():
        if required:
            raise BaselineError(f"{path} is missing; is this a baseline directory?")
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise BaselineError(f"{path} is not valid JSON: {exc}") from None


def load(directory: Path | str) -> BaselineRecord:
    """Load a baseline from `directory`."""
    directory = Path(directory)
    if not directory.is_dir():
        raise BaselineError(f"no baseline directory at {directory}")

    manifest = _read_json(directory / MANIFEST_FILE)
    check_version(manifest)

    graph = _read_json(directory / GRAPH_FILE)
    readables = _read_json(directory / READABLES_FILE, required=False, default={}) or {}
    tests_blob = _read_json(directory / TESTS_FILE, required=False, default={}) or {}

    return BaselineRecord(
        base_url=manifest.get("base_url", ""),
        entry=manifest.get("entry", ""),
        created_at=manifest.get("created_at", ""),
        model=manifest.get("model", ""),
        schema_version=manifest.get("schema_version", ""),
        blindspot_version=manifest.get("blindspot_version", ""),
        graph=graph,
        readables=readables,
        tests=[TestRecord.from_dict(t) for t in tests_blob.get("tests", [])],
    )


def graph_of(record: BaselineRecord) -> AppGraph:
    """The baseline's application graph, as an `AppGraph`."""
    return AppGraph.from_dict(record.graph)


def instances(record: BaselineRecord) -> list[tuple[TestRecord, object]]:
    """Rehydrate every test into its runnable instance.

    Returned paired with the record it came from, because the regression run
    needs both: the instance to execute, and the baseline's observations and
    verdict to compare the new ones against.

    A record whose instance cannot be rebuilt is reported rather than
    skipped. Silently dropping it would shrink the suite between runs, and a
    test that vanished looks exactly like a test that passed.
    """
    out: list[tuple[TestRecord, object]] = []
    for test in record.tests:
        try:
            if test.kind == PAGE:
                out.append((test, TestInstance.from_dict(test.instance)))
            elif test.kind == FLOW:
                out.append((test, FlowTestInstance.from_dict(test.instance)))
            else:
                raise BaselineError(f"unknown test kind {test.kind!r}")
        except BaselineError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise BaselineError(
                f"{test.test_id}: stored instance cannot be rebuilt "
                f"({type(exc).__name__}: {exc}); re-record the baseline"
            ) from None
    return out


def retarget(url: str, base_url: str, new_base: str) -> str:
    """Point a recorded URL at a different deployment of the same application.

    A baseline recorded against one host is re-runnable against another —
    staging against production, or the demo app's sound build against its
    broken one — and every URL inside it has to move with it. Anything
    already outside the recorded base is left alone, since it was never part
    of the application under test.
    """
    base_url = (base_url or "").rstrip("/")
    new_base = (new_base or "").rstrip("/")
    if not base_url or not new_base or base_url == new_base:
        return url
    if url.startswith(base_url):
        return new_base + url[len(base_url):]
    return url


def route_prefix(base_url: str, new_base: str) -> str:
    """The mount point `new_base` adds relative to `base_url`.

    `http://host` -> `http://host/broken` gives `/broken`. Two deployments on
    different hosts at the same path give `""`, which is the common case and
    needs no stripping at all.
    """
    old_path = urlsplit(base_url or "").path.rstrip("/")
    new_path = urlsplit(new_base or "").path.rstrip("/")
    if new_path == old_path:
        return ""
    if old_path and new_path.startswith(old_path):
        return new_path[len(old_path):]
    return new_path


def retarget_instance(instance, base_url: str, new_base: str):
    """Return a copy of an instance aimed at `new_base`.

    Only the URLs move. The walk, the selectors, and the mutation value are
    left exactly as recorded — changing any of them would mean the two runs
    were not asserting the same thing, which is the one property a regression
    comparison depends on.
    """
    if not new_base or base_url.rstrip("/") == new_base.rstrip("/"):
        return instance

    if isinstance(instance, FlowTestInstance):
        data = instance.to_dict()
        data["base_url"] = new_base.rstrip("/")
        data["entry_url"] = retarget(instance.entry_url, base_url, new_base)
        # The recorded state ids were computed from routes under the old
        # mount point. Carrying the difference lets the runner strip it
        # before fingerprinting, so a state recorded at /cart is still
        # recognised when the application is served at /broken/cart.
        data["route_prefix"] = route_prefix(base_url, new_base)
        return FlowTestInstance.from_dict(data)

    data = instance.to_dict()
    data["url"] = retarget(instance.url, base_url, new_base)
    return TestInstance.from_dict(data)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(prog="blindspot-baseline")
    parser.add_argument("directory")
    args = parser.parse_args()

    loaded = load(args.directory)
    print(json.dumps(loaded.manifest(), indent=2))
    for test in loaded.tests:
        print(f"  {test.test_id}  {test.kind:5}  {test.invariant:28}  {test.status}")
