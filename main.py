"""BlindSpot CLI.

Three pipelines, chosen by flag.

    python main.py http://localhost:3000/profile
        Ad-hoc scan of one page. The original MVP, unchanged: inspect,
        classify, instantiate, run, evaluate, report.

    python main.py --baseline http://localhost:3000
        Record a baseline of a whole application. Crawl it, identify the
        capabilities it exposes, instantiate flow invariants against them,
        run them, and write the graph, the value surface, the instances and
        the verdicts to disk.

    python main.py --regression --baseline-dir runs/baseline http://localhost:3000
        Replay a recorded baseline against a later build, diff the results,
        and explain what changed.

Why the regression pipeline replays instances rather than re-deriving them:
an instance has every value it needs fixed before it executes — the mutation
string, the walk, the selectors — so the two runs assert exactly the same
things and a difference in the answers is a difference in the application. A
pipeline that re-classified on each run would compare two different test
suites and attribute the difference to the code. It is also why a regression
run calls no LLM for classification at all, and is therefore cheap enough to
run on every build.

Stage ownership is unchanged from the MVP; there is simply more of it:

    discovery   what the application exposes               (no LLM)
    semantic    which capabilities those are               (LLM)
    knowledge   which invariant applies, and how           (rule base)
    knowledge   rule + concrete walk -> executable instance
    runner      execute it against the browser             (no LLM)
    knowledge   did the relation hold                      (rule base)
    regression  what changed against the baseline          (deterministic)
    regression  what that probably means                   (LLM)
    reporting   terminal output + runs/*.json

WARNING: BlindSpot writes to the application it tests, and a flow run drives
real mutations — it places orders and deletes resources. Unlike a field edit,
most of those have no inverse: teardown resets the session, so a run is only
as undoable as the application's own session scoping. Point this at a
development or staging environment only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from baseline import loader as baseline_loader
from baseline import saver as baseline_saver
from baseline.record_schema import FLOW, PAGE, BaselineError, BaselineRecord
from discovery.crawler import CrawlConfig, Crawler
from discovery.graph import AppGraph
from discovery.page_inspector import inspect_with_page
from discovery.readable import readable_with_page
from knowledge.engine import KnowledgeEngine, Status
from knowledge.flows import FlowEngine
from regression.differ import diff
from regression.interpreter import interpret
from regression.interpreter import DEFAULT_PROVIDER as INTERPRET_PROVIDER
from regression.interpreter import fallback as interpret_fallback
from reporting.regression_report import (
    RegressionReporter,
    build_regression_record,
    exit_code as regression_exit_code,
    write_regression_record,
)
from reporting.reporter import (
    ConsoleReporter,
    build_entry,
    build_flow_entry,
    build_mixed_record,
    build_record,
    write_record,
)
from runner.flow_runner import FlowRunner
from runner.persistence_runner import PersistenceRunner
from semantic.classifier import backend_for, classify, validate_candidates
from semantic.flow_classifier import DEFAULT_PROVIDER as FLOW_PROVIDER
from semantic.flow_classifier import classify_flows, validate_flow_candidates
from semantic.flow_schemas import FlowCandidateSet
from semantic.schemas import CandidateSet

DEFAULT_BASELINE_DIR = Path("runs/baseline")

# Markers for the two kinds of credential failure, matched on the message
# because each SDK signals it with its own exception type.
_CREDENTIAL_MARKERS = ("credential", "api_key", "api key", "authentication")


def _is_credential_error(exc: Exception) -> bool:
    return any(marker in str(exc).lower() for marker in _CREDENTIAL_MARKERS)


def _credential_help(backend, *, extra: str = "") -> str:
    key = (
        "OPENAI_API_KEY"
        if type(backend).__name__.startswith("OpenAI")
        else "ANTHROPIC_API_KEY"
    )
    return (
        f"\nNo API credentials found for provider "
        f"'{type(backend).__name__}' (model {backend.model}).\n\n"
        f"  Put it in .env:   {key}=...\n"
        f"  Or export it:     export {key}=...\n"
        f"{extra}"
        f"\n  Copy .env.example to .env to get started.\n"
    )


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="blindspot",
        description="Find persistence and flow blindspots in a web application.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  blindspot http://localhost:3000/profile\n"
            "      scan one page\n"
            "  blindspot --baseline http://localhost:3000\n"
            "      record a baseline of the whole application\n"
            "  blindspot --regression --baseline-dir runs/baseline "
            "http://localhost:3000/broken\n"
            "      replay that baseline against another build\n"
        ),
    )
    p.add_argument("url", help="page or application to test (dev/staging only)")

    mode = p.add_argument_group("mode")
    mode.add_argument("--baseline", action="store_true",
                      help="crawl the whole application and record a baseline")
    mode.add_argument("--regression", action="store_true",
                      help="replay a recorded baseline and report what changed")
    mode.add_argument("--baseline-dir", type=Path, default=DEFAULT_BASELINE_DIR,
                      help=f"where a baseline lives (default: {DEFAULT_BASELINE_DIR})")

    llm = p.add_argument_group("model")
    llm.add_argument("--provider", help="LLM provider")
    llm.add_argument("--model", help="model id, e.g. gpt-5 or claude-sonnet-5")
    llm.add_argument("--no-interpret", action="store_true",
                     help="skip the LLM reading of a regression diff")

    crawl = p.add_argument_group("crawl (baseline and regression)")
    crawl.add_argument("--entry", default="/", help="path to start the crawl at")
    crawl.add_argument("--max-states", type=int, default=60)
    crawl.add_argument("--max-actions", type=int, default=400)
    crawl.add_argument("--exclude", action="append", default=[], metavar="PREFIX",
                       help="route prefix the crawl must not enter (repeatable)")
    crawl.add_argument("--stay-under", default="", metavar="PREFIX",
                       help="confine the crawl to this route subtree")
    crawl.add_argument("--safe", action="store_true",
                       help="skip affordances whose text looks destructive "
                            "(a text match on an English word list — a "
                            "seatbelt, not a safety net)")
    crawl.add_argument("--read-only", action="store_true",
                       help="follow links only: never fire a button, submit a "
                            "form, or type into a field. Cannot sign in and "
                            "cannot run flow tests, and the map will be "
                            "incomplete and will say so")
    crawl.add_argument("--credential", action="append", default=[],
                       metavar="LABEL=VALUE",
                       help="value to type into fields whose label contains "
                            "LABEL, e.g. --credential password=hunter2 "
                            "(repeatable). No credentials are used unless given")
    crawl.add_argument("--settle", type=int, default=0, metavar="MS",
                       help="wait this long after each transition; also "
                            "throttles the crawl, which a real host may need")
    crawl.add_argument("--allow-writes", action="store_true",
                       help="permit a writing crawl against a non-local host. "
                            "Required because a crawl fires every button it "
                            "finds, which on a real site means real deletions "
                            "and real submissions")

    scope = p.add_argument_group("what to test")
    scope.add_argument("--no-flows", action="store_true",
                       help="skip flow-level tests on a baseline run")
    scope.add_argument("--with-pages", action="store_true",
                       help="also classify and run page-level persistence tests "
                            "on every state holding an editable text field "
                            "(one LLM call per such state)")

    cache = p.add_argument_group("caching")
    cache.add_argument("--candidates", type=Path,
                       help="load page candidates from a file instead of "
                            "calling the LLM")
    cache.add_argument("--save-candidates", type=Path,
                       help="write the page classifier's candidates for later reuse")
    cache.add_argument("--flow-candidates", type=Path,
                       help="load flow candidates from a file instead of "
                            "calling the LLM")
    cache.add_argument("--save-flow-candidates", type=Path,
                       help="write the flow classifier's candidates for later reuse")
    cache.add_argument("--save-baseline", action="store_true",
                       help="on a regression run, also record this build as a "
                            "new baseline")

    run = p.add_argument_group("run")
    run.add_argument("--headed", action="store_true", help="show the browser")
    run.add_argument("--slow-mo", type=int, default=0, metavar="MS",
                     help="pause this many ms before each browser action. "
                          "Pair with --headed to actually watch a crawl or a "
                          "flow walk; 300-500 is readable")
    run.add_argument("-v", "--verbose", action="store_true",
                     help="print the crawler's running trace of what it "
                          "clicks and where it lands")
    run.add_argument("--seed", type=int,
                     help="fix mutation values for reproducibility")
    run.add_argument("--runs-dir", type=Path, default=Path("runs"))
    run.add_argument("--no-restore", action="store_true",
                     help="skip page-test teardown (leaves marker values behind)")
    run.add_argument("--timeout", type=int, default=10_000, help="per-step ms")

    args = p.parse_args(argv)
    if args.baseline and args.regression:
        p.error("--baseline and --regression are different modes; pick one")

    args.credentials = _parse_credentials(args.credential, p.error)

    # A crawl fires every button it finds. Against localhost that is a demo
    # resetting itself; against a real host it is real deletions, real form
    # submissions, and real outbound email. The gate is deliberately a refusal
    # rather than a warning, because a warning scrolls past and the damage
    # does not undo.
    if (args.baseline or args.regression) and not args.read_only:
        if not _is_local(args.url) and not args.allow_writes:
            p.error(
                f"{_host(args.url)!r} is not a local host, and a writing crawl "
                f"fires every button it finds — including deletions and form "
                f"submissions.\n"
                f"  Map it without writing:  --read-only\n"
                f"  Or accept the writes:    --allow-writes"
            )
    return args


def _parse_credentials(pairs, fail) -> dict[str, str]:
    """`LABEL=VALUE` pairs into the form `CrawlConfig.credentials` wants.

    Keyed by a lowercased substring of a field's label, because that is how
    the crawler matches a value to a field — `password=hunter2` fills
    anything labelled "Password" or "Confirm password".
    """
    out: dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            fail(f"--credential expects LABEL=VALUE, got {pair!r}")
        label, _, value = pair.partition("=")
        out[label.strip().lower()] = value
    return out


_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]"})


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(url).hostname or "").lower()


def _is_local(url: str) -> bool:
    """Whether a URL points at this machine.

    Only loopback counts. A private-range address is somebody's staging box
    and quite possibly somebody's production box, and guessing wrong in that
    direction writes to it.
    """
    return _host(url) in _LOCAL_HOSTS


# --------------------------------------------------------------------------
# Shared stages
# --------------------------------------------------------------------------


def _launch(playwright, args):
    """Open a browser, honouring `--headed` and `--slow-mo`.

    `slow_mo` is what makes a headed run worth watching. Playwright drives a
    page far faster than anyone can follow, so `--headed` on its own gives
    you a visible browser flickering through a hundred actions — technically
    what was asked for and useless in practice. The pause is applied by
    Playwright before each action, so it slows the run down without changing
    what the run does.
    """
    return playwright.chromium.launch(
        headless=not args.headed,
        slow_mo=args.slow_mo,
    )


def _crawl(page, base_url: str, args, console) -> tuple[AppGraph, dict[str, dict]]:
    """Map the application, capturing each state's value surface on the way.

    The readable surface is collected through the crawler's `on_state` hook
    rather than by revisiting each route afterwards. Some states are only
    reachable by POST — a checkout confirmation is the obvious one — and
    re-requesting their URL with a GET renders something else entirely, so
    the single visit the crawl makes is the only chance to read them.
    """
    readables: dict[str, dict] = {}

    def observe(live_page, state) -> None:
        readables[state.id] = readable_with_page(live_page)

    console.crawling(base_url)
    crawler = Crawler(
        page,
        base_url,
        CrawlConfig(
            max_states=args.max_states,
            max_actions=args.max_actions,
            deny_destructive=args.safe,
            exclude_prefixes=tuple(args.exclude),
            stay_under=args.stay_under,
            timeout_ms=args.timeout,
            settle_ms=args.settle,
            read_only=args.read_only,
            # Empty unless `--credential` was given. A default of demo/demo123
            # meant pointing this at a real login form typed those into it.
            credentials=args.credentials,
            on_state=observe,
            # The crawler narrates every affordance it fires and where it
            # landed. `discovery.crawler`'s own CLI prints that by default,
            # but a baseline run went silent for the whole crawl — which on
            # a large application is indistinguishable from a hang. Under
            # `-v` the same trace is available here.
            on_event=console.crawl_event if args.verbose else None,
        ),
    )
    graph = crawler.crawl(args.entry)
    console.crawled(graph)
    console.reading_values(len(readables))
    return graph, readables


def _flow_candidates(graph, readables, args, console):
    """Flow candidates, from a file or from the model."""
    if args.flow_candidates:
        loaded = FlowCandidateSet.model_validate_json(
            args.flow_candidates.read_text()
        ).candidates
        # Loaded candidates get the same reference check as generated ones.
        # A stale file naming states that no longer exist should be caught
        # here, not halfway through a browser session.
        candidates, rejected = validate_flow_candidates(loaded, graph)
        model_name = f"file:{args.flow_candidates}"
        console.analyzing(model_name)
        console.flow_classified(
            type("R", (), {"candidates": candidates, "rejected": rejected})()
        )
        return candidates, rejected, model_name

    backend = backend_for(args.provider, args.model,
                          default_provider=FLOW_PROVIDER)
    console.analyzing(backend.model)
    try:
        result = classify_flows(graph, readables=readables, backend=backend)
    except Exception as exc:
        if not _is_credential_error(exc):
            raise
        print(
            _credential_help(
                backend,
                extra="  Or skip the LLM:  --flow-candidates <file.json>\n",
            ),
            file=sys.stderr,
        )
        raise SystemExit(2) from None

    console.flow_classified(result)
    if args.save_flow_candidates:
        args.save_flow_candidates.write_text(
            FlowCandidateSet(candidates=result.candidates).model_dump_json(indent=2)
        )
    return result.candidates, result.rejected, result.model


def _run_flows(page, graph, candidates, args, console):
    """Instantiate and run every applicable flow invariant."""
    engine = FlowEngine.load()
    instances, skipped = engine.instantiate(candidates, graph)

    for skip in skipped:
        console.flow_skipped(skip)
    if skipped:
        print()

    by_goal = {
        (c.capability.value, c.goal_state_id): c for c in candidates
    }
    runner = FlowRunner(page, timeout_ms=args.timeout)

    entries, records, verdicts = [], [], []
    for index, instance in enumerate(instances, start=1):
        candidate = by_goal.get((instance.capability, instance.goal_state_id))
        console.flow_candidate(index, instance, candidate)

        result = runner.run(instance)
        console.flow_testing(result)

        verdict = engine.evaluate(engine.get(instance.invariant), result.observations)
        console.flow_verdict(verdict, result)

        verdicts.append(verdict)
        entries.append(build_flow_entry(instance, candidate, result, verdict))
        records.append(baseline_saver.flow_test_record(instance, result, verdict))

    return entries, records, verdicts, skipped


def _editable_states(graph) -> list:
    """States holding a text field a persistence test could apply to.

    Narrowed with the same shape rule the page classifier enforces, so a
    baseline run does not pay for an LLM call on a state whose only editable
    control the classifier is required to reject anyway.
    """
    out = []
    for state in graph.states.values():
        for element in (state.snapshot or {}).get("elements", []):
            if not element.get("editable"):
                continue
            tag, type_ = element.get("tag"), element.get("type")
            if tag == "textarea" or (tag == "input" and type_ in ("text", "search")):
                out.append(state)
                break
    return out


def _run_pages(page, graph, args, console):
    """Classify and run page-level persistence tests across the application."""
    engine = KnowledgeEngine.load()
    backend = backend_for(args.provider, args.model)

    entries, records, verdicts, skipped_all = [], [], [], []
    counter = 1

    for state in _editable_states(graph):
        url = state.example_url or (graph.base_url + state.url_template)
        try:
            page.goto(url, timeout=args.timeout, wait_until="domcontentloaded")
        except Exception as exc:
            console.skipped(type("S", (), {
                "invariant_id": "PERSISTENCE",
                "unmet": (f"could not open {url}: {exc}",),
            })())
            continue

        snapshot = inspect_with_page(page)
        console.analyzing(f"{backend.model} @ {state.url_template}")
        try:
            classification = classify(snapshot, backend=backend)
        except Exception as exc:
            if not _is_credential_error(exc):
                raise
            print(_credential_help(backend), file=sys.stderr)
            raise SystemExit(2) from None

        console.classified(classification)
        if not classification.candidates:
            continue

        instances, skipped = engine.instantiate(
            classification.candidates, snapshot, seed=args.seed, start=counter
        )
        for skip in skipped:
            console.skipped(skip)
        skipped_all.extend(skipped)

        if args.no_restore:
            instances = [
                type(i)(**{**i.__dict__, "teardown": ()}) for i in instances
            ]

        by_field = {c.field_id: c for c in classification.candidates}
        runner = PersistenceRunner(page, timeout_ms=args.timeout)

        for instance in instances:
            candidate = by_field[instance.target.element_id]
            console.candidate(counter, instance, candidate)
            result = runner.run(instance)
            console.testing(result)
            verdict = engine.evaluate(engine.get(instance.invariant),
                                      result.observations)
            console.verdict(verdict, result)

            verdicts.append(verdict)
            entries.append(build_entry(instance, candidate, result, verdict))
            records.append(
                baseline_saver.page_test_record(instance, result, verdict)
            )
            counter += 1

    return entries, records, verdicts, skipped_all


# --------------------------------------------------------------------------
# Pipeline 1 — record a baseline
# --------------------------------------------------------------------------


def run_baseline(args) -> int:
    from playwright.sync_api import sync_playwright

    base_url = args.url.rstrip("/")
    console = ConsoleReporter()
    console.header(base_url)

    entries: list[dict] = []
    records: list = []
    verdicts: list = []
    skipped: list = []
    rejected: list = []
    model_name = "(none)"

    with sync_playwright() as p:
        browser = _launch(p, args)
        try:
            page = browser.new_page()
            graph, readables = _crawl(page, base_url, args, console)

            # A flow test walks a path and fires its actions, so there is no
            # read-only version of one. Saying so beats running zero tests and
            # letting the empty summary imply the application has no flows.
            if args.read_only and not args.no_flows:
                console.read_only_note()

            if not args.no_flows and not args.read_only:
                candidates, rejected, model_name = _flow_candidates(
                    graph, readables, args, console
                )
                if candidates:
                    f_entries, f_records, f_verdicts, f_skipped = _run_flows(
                        page, graph, candidates, args, console
                    )
                    entries += f_entries
                    records += f_records
                    verdicts += f_verdicts
                    skipped += f_skipped

            if args.with_pages:
                p_entries, p_records, p_verdicts, p_skipped = _run_pages(
                    page, graph, args, console
                )
                entries += p_entries
                records += p_records
                verdicts += p_verdicts
                skipped += p_skipped
        finally:
            browser.close()

    record = baseline_saver.build(
        graph=graph, tests=records, readables=readables, model=model_name
    )
    directory = baseline_saver.save(record, args.baseline_dir)

    run_record = build_mixed_record(
        base_url, model_name, entries, skipped, rejected,
        graph_summary={
            "states": len(graph.states),
            "actions": len(graph.actions),
            "closed": graph.is_closed(),
        },
    )
    path = write_record(run_record, args.runs_dir)

    console.summary(verdicts, path)
    console.baselined(directory, baseline_saver.summarize(record))

    return 1 if run_record["summary"]["violations"] else 0


# --------------------------------------------------------------------------
# Pipeline 2 — detect regressions
# --------------------------------------------------------------------------


def run_regression(args) -> int:
    from playwright.sync_api import sync_playwright

    base_url = args.url.rstrip("/")
    reporter = RegressionReporter()

    try:
        recorded = baseline_loader.load(args.baseline_dir)
        replays = baseline_loader.instances(recorded)
    except BaselineError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 2

    reporter.header(args.baseline_dir, base_url, recorded.created_at)

    page_engine = KnowledgeEngine.load()
    flow_engine = FlowEngine.load()

    fresh_records: list = []

    with sync_playwright() as p:
        browser = _launch(p, args)
        try:
            page = browser.new_page()

            # The crawl here is for the structural and UI comparison. The
            # tests themselves come from the baseline, so nothing about the
            # suite depends on what this crawl finds.
            console = ConsoleReporter()
            graph, readables = _crawl(page, base_url, args, console)

            reporter.replaying(len(replays))
            page_runner = PersistenceRunner(page, timeout_ms=args.timeout)
            flow_runner = FlowRunner(page, timeout_ms=args.timeout)

            for test, instance in replays:
                aimed = baseline_loader.retarget_instance(
                    instance, recorded.base_url, base_url
                )
                if test.kind == FLOW:
                    result = flow_runner.run(aimed)
                    verdict = flow_engine.evaluate(
                        flow_engine.get(aimed.invariant), result.observations
                    )
                    fresh_records.append(
                        baseline_saver.flow_test_record(aimed, result, verdict)
                    )
                else:
                    result = page_runner.run(aimed)
                    verdict = page_engine.evaluate(
                        page_engine.get(aimed.invariant), result.observations
                    )
                    fresh_records.append(
                        baseline_saver.page_test_record(aimed, result, verdict)
                    )
                reporter.replayed(test, verdict)
        finally:
            browser.close()

    current = baseline_saver.build(
        graph=graph, tests=fresh_records, readables=readables,
        model=recorded.model,
    )

    reporter.comparing()
    found = diff(recorded, current)
    reporter.findings(found)

    interpretation = None
    if not args.no_interpret and len(found):
        backend = backend_for(args.provider, args.model,
                              default_provider=INTERPRET_PROVIDER)
        try:
            interpretation = interpret(found, backend=backend)
        except Exception as exc:
            if not _is_credential_error(exc):
                raise
            # A missing key degrades the report rather than failing the run:
            # the findings were computed deterministically and are the
            # actual evidence, so they stand on their own.
            print(
                _credential_help(
                    backend,
                    extra="  Or skip the reading: --no-interpret\n",
                ),
                file=sys.stderr,
            )
            interpretation = interpret_fallback(found)
        reporter.interpretation(interpretation)
    elif not len(found):
        interpretation = interpret_fallback(found)

    record = build_regression_record(
        baseline_dir=args.baseline_dir,
        url=base_url,
        baseline=recorded,
        current=current,
        found=found,
        interpretation=interpretation,
    )
    path = write_regression_record(record, args.runs_dir)

    promoted = None
    if args.save_baseline:
        promoted = baseline_saver.save(current, args.baseline_dir)

    reporter.summary(found, path, promoted)
    return regression_exit_code(found)


# --------------------------------------------------------------------------
# Pipeline 3 — ad-hoc single page (the original MVP, unchanged)
# --------------------------------------------------------------------------


def run_adhoc(args) -> int:
    from playwright.sync_api import sync_playwright

    console = ConsoleReporter()
    console.header(args.url)

    engine = KnowledgeEngine.load()

    with sync_playwright() as p:
        browser = _launch(p, args)
        try:
            page = browser.new_page()
            page.goto(args.url, timeout=args.timeout, wait_until="domcontentloaded")
            snapshot = inspect_with_page(page)
            console.inspected(snapshot)

            # --- semantic mapping (the only LLM call in the pipeline) -------
            if args.candidates:
                loaded = CandidateSet.model_validate_json(
                    args.candidates.read_text()
                ).candidates
                # Loaded candidates get the same reference check as generated
                # ones — a stale file referring to elements that have since
                # moved should be caught here, not halfway through a run.
                candidates, rejected = validate_candidates(loaded, snapshot)
                model_name = f"file:{args.candidates}"
                console.analyzing(model_name)
                console.classified(
                    type("R", (), {"candidates": candidates, "rejected": rejected})()
                )
            else:
                backend = backend_for(args.provider, args.model)
                console.analyzing(backend.model)
                try:
                    classification = classify(snapshot, backend=backend)
                except Exception as exc:
                    if not _is_credential_error(exc):
                        raise
                    print(
                        _credential_help(
                            backend,
                            extra="  Or skip the LLM:  --candidates <file.json>\n",
                        ),
                        file=sys.stderr,
                    )
                    return 2
                candidates = classification.candidates
                rejected = classification.rejected
                model_name = classification.model
                console.classified(classification)
                if args.save_candidates:
                    args.save_candidates.write_text(
                        CandidateSet(candidates=candidates).model_dump_json(indent=2)
                    )

            if not candidates:
                console.summary([], None)
                return 0

            # --- rule base: which invariant, and is it applicable -----------
            instances, skipped = engine.instantiate(
                candidates, snapshot, seed=args.seed
            )
            for skip in skipped:
                console.skipped(skip)
            if skipped:
                print()

            if args.no_restore:
                instances = [
                    type(i)(**{**i.__dict__, "teardown": ()}) for i in instances
                ]

            by_field = {c.field_id: c for c in candidates}
            runner = PersistenceRunner(page, timeout_ms=args.timeout)

            entries, verdicts = [], []
            for index, instance in enumerate(instances, start=1):
                candidate = by_field[instance.target.element_id]
                console.candidate(index, instance, candidate)

                result = runner.run(instance)
                console.testing(result)

                verdict = engine.evaluate(engine.get(instance.invariant),
                                          result.observations)
                console.verdict(verdict, result)

                verdicts.append(verdict)
                entries.append(build_entry(instance, candidate, result, verdict))

        finally:
            browser.close()

    record = build_record(args.url, model_name, entries, skipped, rejected)
    path = write_record(record, args.runs_dir)
    console.summary(verdicts, path)

    # Non-zero exit when something needs a human to look at it.
    return 1 if record["summary"]["violations"] else 0


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.regression:
        return run_regression(args)
    if args.baseline:
        return run_baseline(args)
    return run_adhoc(args)


if __name__ == "__main__":
    sys.exit(main())
