"""Executes a FlowTestInstance against a live page. Pure Playwright — no LLM.

The same contract as `runner.persistence_runner`: by this stage every decision
has been made. The model decided which capability lives where; the rule base
decided what has to hold and what the procedure is; instantiation decided the
concrete walk and the concrete selectors. This module performs steps and
writes down what it saw, and it never decides pass or fail.

It is a step interpreter, so `STEP_HANDLERS` implements the names in
`knowledge.flows.FLOW_STEP_RECORDS` and a procedure is executed by looking
each name up. The two are checked against each other at import time.

Three things make a flow run different from a page run:

  verified arrival   Replaying a path is not the same as arriving. Every walk
                     ends with a fingerprint check against the state the graph
                     said it would reach, recorded as `setup_reached` /
                     `goal_reached`. That is what turns the old pipeline's
                     silent confusion — reload, go looking for a field, find
                     the page navigated away — into an explicit observation,
                     and it is why a flow that could not be completed comes
                     back inconclusive instead of looking like a defect.

  bindings           A flow's outcome is rendered text, not a form value. The
                     invariant names what it needs (`line_totals`), the
                     instance carries the selector that supplies it, and this
                     module only does the reading.

  probes             A goal condition about something *not existing* cannot be
                     checked by looking at the page you landed on. The probe
                     goes back and asks for the thing that should be gone.

WARNING: a flow run drives an application through real mutations — it places
orders and deletes resources — and unlike a field edit most of those have no
inverse. Teardown is a session reset, so the run is only as undoable as the
application's own session scoping. Point this at a development environment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

from discovery.fingerprint import fingerprint
from discovery.page_inspector import inspect_with_page
from knowledge.flows import FLOW_STEP_RECORDS
from runner.persistence_runner import StepError, StepRecord, describe_locator, locate

DEFAULT_TIMEOUT_MS = 10_000
DEFAULT_SETTLE_MS = 0


# --------------------------------------------------------------------------
# Reading values off a page
# --------------------------------------------------------------------------

# The first numeric run in a string, tolerating a currency prefix, thousands
# separators and a trailing unit. `$1,299.00` and `1299` are the same fact
# rendered differently, and an ordering or sum assertion needs the number.
_NUMBER_RE = re.compile(r"-?\d[\d,\s]*(?:\.\d+)?")


def to_number(text: str) -> float | None:
    """Parse rendered text as a number, or None when it holds none.

    None rather than 0.0 on failure, deliberately. A row whose price could
    not be parsed must make the relation unevaluable — `sum` over a list
    holding None raises, which the engine reports as inconclusive. Treating
    it as zero would quietly change the sum and could turn a correct
    application into a reported defect.
    """
    if text is None:
        return None
    match = _NUMBER_RE.search(text)
    if not match:
        return None
    try:
        value = float(match.group(0).replace(",", "").replace(" ", ""))
    except ValueError:
        return None
    # An accounting negative is written as a prefix or in brackets, neither
    # of which the digit run itself captures.
    before = text[: match.start()]
    if "-" in before or "(" in before:
        value = -value
    return value


def _texts(page, selector: str, timeout: int) -> list[str]:
    locator = page.locator(selector)
    try:
        count = locator.count()
    except Exception as exc:
        raise StepError(f"selector {selector!r} is not valid: {_brief(exc)}") from None
    return [
        (locator.nth(i).inner_text(timeout=timeout) or "").strip()
        for i in range(count)
    ]


def read_binding(page, binding, timeout: int) -> Any:
    """Read one binding into its observation value.

    A binding that matches nothing is an error rather than an empty value.
    `sorted_asc([])` is vacuously true and `sum([])` is zero, so a selector
    that silently stopped matching would turn into a confident pass — the
    worst possible failure mode for a regression check, because it reports
    health precisely when something changed.
    """
    kind = binding.extract
    selector = binding.selector

    if kind == "count":
        try:
            return page.locator(selector).count()
        except Exception as exc:
            raise StepError(
                f"selector {selector!r} is not valid: {_brief(exc)}"
            ) from None

    if kind == "visible":
        try:
            locator = page.locator(selector)
            return locator.count() > 0 and locator.first.is_visible()
        except Exception:
            return False

    if kind == "value":
        locator = page.locator(selector)
        if locator.count() == 0:
            raise StepError(f"{binding.name}: {selector!r} matched nothing")
        try:
            return locator.first.input_value(timeout=timeout)
        except Exception as exc:
            raise StepError(
                f"{binding.name}: could not read a value from {selector!r}: "
                f"{_brief(exc)}"
            ) from None

    found = _texts(page, selector, timeout)
    if not found:
        raise StepError(f"{binding.name}: {selector!r} matched nothing")

    if kind == "text":
        return found[0]
    if kind == "texts":
        return found
    if kind == "number":
        number = to_number(found[0])
        if number is None:
            raise StepError(
                f"{binding.name}: {found[0]!r} from {selector!r} is not a number"
            )
        return number
    if kind == "numbers":
        numbers = [to_number(t) for t in found]
        unparsed = [t for t, n in zip(found, numbers) if n is None]
        if unparsed:
            raise StepError(
                f"{binding.name}: {len(unparsed)} of {len(found)} values from "
                f"{selector!r} are not numbers (e.g. {unparsed[0]!r})"
            )
        return numbers

    raise StepError(f"{binding.name}: unsupported extract kind {kind!r}")


# --------------------------------------------------------------------------
# Run records
# --------------------------------------------------------------------------


@dataclass
class FlowRunResult:
    """What one execution of one flow instance produced.

    `observations` is the dict the flow engine consumes. A run that aborted
    early simply has fewer keys in it, which the engine already reports as
    inconclusive rather than as a violation.

    `states` records the fingerprint of every page the walk passed through.
    It is not used to decide anything — it is what makes a regression diff
    able to say *where* a flow started behaving differently, rather than only
    that its outcome changed.
    """

    test_id: str
    instance: Any
    observations: dict = field(default_factory=dict)
    steps: list[StepRecord] = field(default_factory=list)
    states: list[dict] = field(default_factory=list)
    error: str | None = None
    duration_ms: int = 0
    reset: bool | None = None
    notes: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "test_id": self.test_id,
            "instance": self.instance.to_dict(),
            "observations": self.observations,
            "steps": [s.to_dict() for s in self.steps],
            "states": self.states,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "reset": self.reset,
            "notes": self.notes,
        }


@dataclass
class FlowStepContext:
    page: Any
    instance: Any
    observations: dict
    timeout_ms: int
    settle_ms: int
    states: list[dict] = field(default_factory=list)
    scratch: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# Step handlers — one per name in knowledge.flows.FLOW_STEP_RECORDS
# --------------------------------------------------------------------------


def _brief(exc: Exception, limit: int = 120) -> str:
    return str(exc).strip().splitlines()[0][:limit]


def _settle(ctx: FlowStepContext) -> None:
    try:
        ctx.page.wait_for_load_state("domcontentloaded", timeout=ctx.timeout_ms)
    except Exception:
        # A click that triggers no navigation leaves the page already loaded,
        # which is the common case for a button. Not an error.
        pass
    if ctx.settle_ms:
        ctx.page.wait_for_timeout(ctx.settle_ms)


def _act(ctx: FlowStepContext, step) -> None:
    """Perform one replayed step."""
    label = step.label or describe_locator(step)

    if not (step.locator or {}).get("type") and not step.selector:
        raise StepError(f"{label!r} has no usable locator to replay")

    try:
        locator = locate(ctx.page, step)
        locator.wait_for(state="visible", timeout=ctx.timeout_ms)
    except StepError:
        raise
    except Exception as exc:
        raise StepError(f"could not find {label!r}: {_brief(exc)}") from None

    try:
        if step.kind == "fill":
            locator.fill(str(step.value or ""), timeout=ctx.timeout_ms)
        elif step.kind == "click":
            locator.click(timeout=ctx.timeout_ms)
        else:
            raise StepError(f"unsupported step kind {step.kind!r}")
    except StepError:
        raise
    except Exception as exc:
        raise StepError(f"could not {step.kind} {label!r}: {_brief(exc)}") from None

    if step.kind == "click":
        _settle(ctx)


def _walk(ctx: FlowStepContext, steps, expected_state: str, phase: str) -> bool:
    """Replay a sequence of steps and report whether it arrived.

    Arrival is verified against the fingerprint the graph predicted rather
    than assumed from the steps having executed. A path that runs cleanly and
    lands somewhere else is the single most misleading thing a flow test can
    do: every subsequent observation reads a real page, parses fine, and
    describes the wrong one.
    """
    for index, step in enumerate(steps, start=1):
        try:
            _act(ctx, step)
        except StepError as exc:
            ctx.scratch[f"{phase}_failed_at"] = f"step {index}/{len(steps)}: {exc}"
            return False

    snapshot = inspect_with_page(ctx.page)
    print_ = fingerprint(
        snapshot, strip_prefix=getattr(ctx.instance, "route_prefix", "")
    )
    ctx.states.append({
        "phase": phase,
        "state_id": print_.id,
        "expected_state_id": expected_state,
        "url": snapshot.get("url", ""),
        "url_template": print_.url_template,
        "title": snapshot.get("title", ""),
    })

    if print_.id != expected_state:
        ctx.scratch[f"{phase}_arrived_at"] = print_.url_template
        return False
    return True


def _walk_setup(ctx: FlowStepContext) -> bool:
    ctx.page.goto(
        ctx.instance.entry_url,
        timeout=ctx.timeout_ms,
        wait_until="domcontentloaded",
    )
    _settle(ctx)
    return _walk(ctx, ctx.instance.setup, ctx.instance.setup_state_id, "setup")


def _walk_goal(ctx: FlowStepContext) -> bool:
    if not ctx.observations.get("setup_reached"):
        raise StepError("setup did not reach its state, so the goal walk cannot start")
    return _walk(ctx, ctx.instance.goal, ctx.instance.goal_state_id, "goal")


def _capture_probe_url(ctx: FlowStepContext) -> str:
    """Record the URL the flow stands on, for a probe to re-request later.

    A deleted resource's own URL is only knowable before it is deleted, so
    this has to run between the setup walk and the goal actions.
    """
    return ctx.page.url


def _observe_bindings(ctx: FlowStepContext) -> None:
    """Read every binding into the observations, by its declared name."""
    if not ctx.observations.get("goal_reached"):
        raise StepError("the flow did not reach its goal state; nothing to observe")
    for binding in ctx.instance.bindings:
        ctx.observations[binding.name] = read_binding(
            ctx.page, binding, ctx.timeout_ms
        )


def _probe_target(ctx: FlowStepContext) -> str:
    mode = ctx.instance.probe_mode
    if mode == "path":
        return urljoin(ctx.instance.base_url + "/", ctx.instance.probe_path.lstrip("/"))
    if mode == "captured":
        captured = ctx.observations.get("captured_url")
        if not captured:
            raise StepError(
                "probe mode is 'captured' but no URL was captured; the "
                "procedure must run capture_probe_url before the goal walk"
            )
        return captured
    if mode == "final":
        return ctx.page.url
    raise StepError(f"unsupported probe mode {mode!r}")


def _probe(ctx: FlowStepContext) -> tuple[bool, int | None, str]:
    """Request a URL and report whether the thing it names is still there.

    Reachable means both that the request succeeded and that it was answered
    *at the URL asked for*. The second half matters as much as the first: an
    application that redirects an unauthenticated visitor to a sign-in page
    answers 200, and reading only the status would call a correctly-protected
    resource reachable.
    """
    target = _probe_target(ctx)
    wanted = urlsplit(target).path or "/"

    try:
        response = ctx.page.goto(
            target, timeout=ctx.timeout_ms, wait_until="domcontentloaded"
        )
    except Exception as exc:
        # A navigation that cannot complete is evidence of absence, not an
        # error in the test. Recorded as unreachable with the reason kept.
        ctx.scratch["probe_error"] = _brief(exc)
        return False, None, target

    status = response.status if response is not None else None
    landed = ctx.page.url
    landed_path = urlsplit(landed).path or "/"

    ok = status is not None and status < 400
    same_place = landed_path == wanted
    if ok and not same_place:
        ctx.scratch["probe_redirected_to"] = landed_path

    return bool(ok and same_place), status, landed


def _reset_session(ctx: FlowStepContext) -> None:
    """Drop the session the flow mutated.

    Most flow mutations have no inverse — there is no un-place-an-order — so
    teardown cannot be the goal walk run backwards. Clearing cookies is a
    complete reset exactly when the application keys its state to a session,
    and no reset at all when it does not. An application storing state
    server-side needs its own hook here, and a flow run is only as repeatable
    as that hook.
    """
    try:
        ctx.page.context.clear_cookies()
    except Exception as exc:
        raise StepError(f"could not clear cookies: {_brief(exc)}") from None


STEP_HANDLERS: dict[str, Callable[[FlowStepContext], Any]] = {
    "walk_setup": _walk_setup,
    "capture_probe_url": _capture_probe_url,
    "walk_goal": _walk_goal,
    "observe_bindings": _observe_bindings,
    "probe": _probe,
    "reset_session": _reset_session,
}

# The rule base may only name steps implemented here, and every step
# implemented here must be known to the rule base. A mismatch is a wiring
# bug; catching it at import beats discovering it mid-run.
_missing = set(FLOW_STEP_RECORDS) - set(STEP_HANDLERS)
_extra = set(STEP_HANDLERS) - set(FLOW_STEP_RECORDS)
if _missing or _extra:  # pragma: no cover - guards against edits to either side
    raise ImportError(
        f"flow step vocabulary mismatch with knowledge.flows: "
        f"unimplemented={sorted(_missing)} unknown={sorted(_extra)}"
    )


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


class FlowRunner:
    """Executes flow instances against a Playwright page."""

    def __init__(
        self,
        page,
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        settle_ms: int = DEFAULT_SETTLE_MS,
    ):
        self.page = page
        self.timeout_ms = timeout_ms
        self.settle_ms = settle_ms

    def run(self, instance) -> FlowRunResult:
        """Execute one instance's procedure, then always run its teardown."""
        result = FlowRunResult(test_id=instance.test_id, instance=instance)
        started = _now()

        ctx = FlowStepContext(
            page=self.page,
            instance=instance,
            observations=result.observations,
            timeout_ms=self.timeout_ms,
            settle_ms=self.settle_ms,
            states=result.states,
        )

        try:
            # Every flow starts from a clean session. A previous flow's order
            # or deletion left in place would be indistinguishable from this
            # one's effect, and the first thing a flow invariant asserts
            # about is an effect.
            _reset_session(ctx)
            self._execute(ctx, result, instance.procedure, teardown=False)
        except Exception as exc:
            result.error = f"setup: {_brief(exc)}"
        finally:
            if instance.teardown:
                self._execute(ctx, result, instance.teardown, teardown=True)
                result.reset = all(s.ok for s in result.steps if s.teardown)

        result.notes = dict(ctx.scratch)
        result.duration_ms = int((_now() - started) * 1000)
        return result

    def _execute(self, ctx, result, steps, *, teardown: bool) -> None:
        """Run a sequence of named steps, recording what each produced."""
        for name in steps:
            handler = STEP_HANDLERS[name]
            records = FLOW_STEP_RECORDS[name]
            try:
                value = handler(ctx)
            except Exception as exc:
                detail = str(exc) if isinstance(exc, StepError) else _brief(exc)
                result.steps.append(
                    StepRecord(name=name, ok=False, detail=detail, teardown=teardown)
                )
                if not teardown:
                    result.error = f"{name}: {detail}"
                break

            # A step recording several names answers with a tuple in the same
            # order the vocabulary declares them.
            if len(records) == 1:
                result.observations[records[0]] = value
            elif len(records) > 1:
                for key, item in zip(records, value or ()):
                    result.observations[key] = item

            result.steps.append(
                StepRecord(
                    name=name,
                    ok=True,
                    records=", ".join(records) or None,
                    value=value if len(records) != 1 else result.observations[records[0]],
                    teardown=teardown,
                )
            )

            # A walk that did not arrive makes everything after it read the
            # wrong page, so the procedure stops here. The observation is
            # already recorded, which is what lets the engine call this
            # inconclusive rather than a violation.
            if name in ("walk_setup", "walk_goal") and value is False:
                break

    def run_all(self, instances) -> list[FlowRunResult]:
        return [self.run(instance) for instance in instances]


def _now() -> float:
    import time

    return time.monotonic()


def run_flow_instances(
    instances,
    *,
    headless: bool = True,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    settle_ms: int = DEFAULT_SETTLE_MS,
) -> list[FlowRunResult]:
    """Run flow instances in a throwaway browser. Convenience wrapper."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            page = browser.new_page()
            runner = FlowRunner(page, timeout_ms=timeout_ms, settle_ms=settle_ms)
            return runner.run_all(instances)
        finally:
            browser.close()
