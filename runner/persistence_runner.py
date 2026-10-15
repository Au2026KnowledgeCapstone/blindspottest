"""Executes a TestInstance against a live page. Pure Playwright — no LLM.

By this stage every decision has already been made. The model decided what
looks testable; the rule base decided what "testable" means and what the
procedure is; instantiation decided the concrete field and the exact value.
This module only performs steps and writes down what it saw.

It is a step interpreter, the same way `knowledge.engine` is a rule
interpreter: `STEP_HANDLERS` implements the names in the rule base's
vocabulary, and a procedure is executed by looking each name up. Adding a step
to an invariant's procedure means implementing its name here — which is why
`knowledge.engine.STEP_RECORDS` and this module's handlers are checked against
each other at import time.

The runner never decides pass or fail. It returns observations; the knowledge
engine evaluates them.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from knowledge.engine import STEP_RECORDS
from runner.highlight import caption_for, clear as clear_highlight, highlight

DEFAULT_TIMEOUT_MS = 10_000
DEFAULT_SETTLE_MS = 1_500


# --------------------------------------------------------------------------
# DOM helpers
#
# Value reading mirrors `discovery.page_inspector`'s logic exactly. If the two
# disagree about what a field's value is, `original_value` on the instance and
# `final_value` from the run become incomparable.
# --------------------------------------------------------------------------

_READ_STATE_JS = r"""
(el) => {
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute('type') || 'text').toLowerCase();

  if (tag === 'input' && (type === 'checkbox' || type === 'radio')) {
    return el.indeterminate ? 'mixed' : el.checked;
  }
  if (el.hasAttribute('aria-checked')) {
    const aria = el.getAttribute('aria-checked');
    return aria === 'mixed' ? 'mixed' : aria === 'true';
  }
  if (tag === 'select') {
    const selected = Array.from(el.selectedOptions).map((o) => o.value);
    return el.multiple ? selected : (selected[0] ?? '');
  }
  if (tag === 'input' || tag === 'textarea') return el.value;
  if (el.isContentEditable) return (el.innerText || '').replace(/\s+/g, ' ').trim();
  return null;
}
"""


class StepError(Exception):
    """A step could not be carried out. Aborts the run as inconclusive."""


def locate(page, ref):
    """Resolve an ElementRef to a Playwright locator.

    Semantic first. `get_by_role("textbox", name="Bio")` describes what the
    element *is*, so it survives re-renders, reordered markup, and changed
    class names — all of which break a CSS path. The snapshot's `e2`-style id
    never touches the browser; it exists only to tie the model's answer back
    to the snapshot.

    The CSS selector is kept as a fallback for pages whose accessibility
    information is too thin to locate anything by role.
    """
    strategy = ref.locator_strategy or {}
    kind = strategy.get("type")

    try:
        if kind == "role" and strategy.get("name"):
            candidate = page.get_by_role(
                strategy["role"], name=strategy["name"], exact=True
            )
        elif kind == "label" and strategy.get("name"):
            candidate = page.get_by_label(strategy["name"], exact=True)
        elif kind == "placeholder" and strategy.get("name"):
            candidate = page.get_by_placeholder(strategy["name"], exact=True)
        elif kind == "test_id" and strategy.get("value"):
            candidate = page.get_by_test_id(strategy["value"])
        else:
            candidate = page.locator(ref.selector)

        if candidate.count() > 0:
            return candidate.first
    except Exception:
        pass  # fall through to the CSS fallback

    return page.locator(ref.selector).first


def describe_locator(ref) -> str:
    """How the locator would be written, for the report."""
    s = ref.locator_strategy or {}
    kind = s.get("type")
    if kind == "role":
        return f'get_by_role("{s["role"]}", name="{s["name"]}")'
    if kind == "label":
        return f'get_by_label("{s["name"]}")'
    if kind == "placeholder":
        return f'get_by_placeholder("{s["name"]}")'
    if kind == "test_id":
        return f'get_by_test_id("{s["value"]}")'
    return f'locator("{ref.selector}")'


def _read_state(page, locator, label: str) -> Any:
    """Read a field's current value through a resolved locator."""
    try:
        if locator.count() == 0:
            raise StepError(f"{label} is not on the page")
        return locator.evaluate(_READ_STATE_JS)
    except StepError:
        raise
    except Exception as exc:
        raise StepError(f"could not read {label}: {_brief(exc)}") from None


# --------------------------------------------------------------------------
# Run records
# --------------------------------------------------------------------------


@dataclass
class StepRecord:
    name: str
    ok: bool
    detail: str = ""
    records: str | None = None
    value: Any = None
    teardown: bool = False

    def to_dict(self) -> dict:
        return {
            "step": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "records": self.records,
            "value": self.value,
            "teardown": self.teardown,
        }


@dataclass
class RunResult:
    """What one execution of one instance produced.

    `observations` is the dict `KnowledgeEngine.evaluate()` consumes. A run
    that aborts early simply has fewer keys in it, which the engine already
    reports as inconclusive rather than as a violation.
    """

    test_id: str
    instance: Any
    observations: dict = field(default_factory=dict)
    steps: list[StepRecord] = field(default_factory=list)
    error: str | None = None
    duration_ms: int = 0
    restored: bool | None = None
    notes: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "test_id": self.test_id,
            "instance": self.instance.to_dict(),
            "observations": self.observations,
            "steps": [s.to_dict() for s in self.steps],
            "error": self.error,
            "duration_ms": self.duration_ms,
            "restored": self.restored,
            "notes": self.notes,
        }


@dataclass
class StepContext:
    page: Any
    instance: Any
    observations: dict
    timeout_ms: int
    settle_ms: int
    # Facts worth reporting that aren't observations the rule base consumes.
    scratch: dict = field(default_factory=dict)
    # Box each control for this many ms before touching it; 0 is off.
    highlight_ms: int = 0


# --------------------------------------------------------------------------
# Step handlers — one per name in knowledge.engine.STEP_RECORDS
# --------------------------------------------------------------------------


def _preview(ctx: StepContext, locator, kind: str, label: str,
             value: str | None = None) -> None:
    """Box a control before touching it, when the run asked to be watched."""
    if ctx.highlight_ms:
        highlight(ctx.page, locator, caption_for(kind, label, value),
                  kind=kind, dwell_ms=ctx.highlight_ms)


def _unpreview(ctx: StepContext) -> None:
    """Remove the box before the page is read again.

    The invariant being tested is about what the page holds, and an injected
    overlay is something the page now holds.
    """
    if ctx.highlight_ms:
        clear_highlight(ctx.page)


def _target(ctx: StepContext):
    return locate(ctx.page, ctx.instance.target)


def _target_label(ctx: StepContext) -> str:
    return describe_locator(ctx.instance.target)


def _observe_initial_value(ctx: StepContext) -> Any:
    return _read_state(ctx.page, _target(ctx), _target_label(ctx))


def _generate_mutation(ctx: StepContext) -> Any:
    """The value was materialized at instantiation; this step records it.

    Generating here instead would make the run unreproducible and the instance
    a lie about what was tested.
    """
    return ctx.instance.mutation_value


def _write(ctx: StepContext, value: str) -> Any:
    """Fill the target and read back what it actually holds."""
    locator = _target(ctx)
    label = _target_label(ctx)
    try:
        locator.wait_for(state="visible", timeout=ctx.timeout_ms)
        _preview(ctx, locator, "fill",
                 ctx.instance.target.accessible_name or label, str(value))
        # v1 handles text inputs and textareas only, so `fill` covers every
        # supported field. Checkables and selects are rejected at
        # instantiation, not here.
        locator.fill(str(value), timeout=ctx.timeout_ms)
        # Blur so change/blur-driven handlers fire — many autosave
        # implementations commit on blur, not on keystroke.
        locator.evaluate("el => el.blur && el.blur()")
    except Exception as exc:
        raise StepError(f"could not set {label}: {_brief(exc)}") from None
    finally:
        _unpreview(ctx)
    return _read_state(ctx.page, locator, label)


def _apply_mutation(ctx: StepContext) -> Any:
    """Write the value, then read back what the field actually holds.

    The read-back matters. `maxlength`, input masks, and normalizing handlers
    all mean the field may not hold what we typed, and the invariant compares
    against what was *committed*, not what was attempted. Recording the typed
    value here would report a spurious violation every time a page legitimately
    truncates or reformats input.
    """
    return _write(ctx, ctx.instance.mutation_value)


def _click_commit(ctx: StepContext) -> None:
    commit = ctx.instance.commit_action
    if commit is None:
        raise StepError("procedure has a commit step but the instance has no commit action")
    label = describe_locator(commit)
    try:
        locator = locate(ctx.page, commit)
        locator.wait_for(state="visible", timeout=ctx.timeout_ms)
        _preview(ctx, locator, "click", commit.accessible_name or label)
        locator.click(timeout=ctx.timeout_ms)
    except Exception as exc:
        raise StepError(f"could not click {label}: {_brief(exc)}") from None
    finally:
        _unpreview(ctx)


def _commit(ctx: StepContext) -> None:
    ctx.scratch["url_before_commit"] = ctx.page.url
    writes: list[tuple[str, int, str]] = []

    def on_response(response):
        try:
            method = response.request.method.upper()
            if method in _WRITE_METHODS:
                writes.append((method, response.status, response.url))
        except Exception:
            pass

    ctx.page.on("response", on_response)
    try:
        _click_commit(ctx)
        try:
            ctx.page.wait_for_load_state("domcontentloaded", timeout=ctx.timeout_ms)
        except Exception:
            pass
        # Give an async save (fetch/XHR with no navigation) a moment to land.
        ctx.page.wait_for_timeout(min(ctx.settle_ms, 600))
    finally:
        ctx.page.remove_listener("response", on_response)

    ctx.scratch["writes_during_commit"] = writes


# HTTP methods that change server state. A 2xx/3xx on one of these is the
# strongest available evidence that the application accepted a commit — and
# unlike reading the page, it is the same signal in every language.
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _has_text(ctx: StepContext, needle: str, timeout: int) -> bool:
    try:
        ctx.page.wait_for_function(
            "needle => document.body.innerText.toLowerCase().includes(needle)",
            arg=needle.lower(),
            timeout=timeout,
        )
        return True
    except Exception:
        return False


def _observe_success(ctx: StepContext) -> bool:
    """Decide whether the commit was confirmed.

    The classifier's predicted wording is tried first, but a wrong guess must
    NOT be read as "the save failed". Those are different facts, and conflating
    them is dangerous in one specific direction: a mispredicted banner would
    turn a genuine lost-data regression into `inconclusive` and hide it. The
    model's phrasing is a hint about where to look, not evidence about the
    application.

    So when the predicted phrase is absent we fall back to wording-independent
    evidence that a commit actually occurred. `success_method` records which
    test answered, so the report never conflates a confident confirmation with
    a weak one.
    """
    signal = ctx.instance.success_signal
    predicted = (signal or {}).get("expected_pattern")

    if predicted and _has_text(ctx, predicted, ctx.timeout_ms):
        ctx.scratch["success_method"] = "predicted_text"
        return True

    if predicted:
        ctx.scratch["predicted_text_missing"] = predicted

    return _fallback_success(ctx)


def _fallback_success(ctx: StepContext) -> bool:
    """Wording-independent evidence that the commit went through.

    Three tests, weakest last, and none of them reads page copy — a
    confirmation oracle built on English words would fail on a localized app
    and would smuggle knowledge into the runner, which is meant to hold none.
    If none holds, the commit really was unconfirmed and `inconclusive` is the
    honest answer.
    """
    ctx.page.wait_for_timeout(min(ctx.settle_ms, 600))

    # 1. The commit navigated — a form POST/redirect is a strong signal that
    #    the application accepted the submission.
    before = ctx.scratch.get("url_before_commit")
    if before is not None and ctx.page.url != before:
        ctx.scratch["success_method"] = "url_changed"
        return True

    # 2. A state-changing request completed successfully. This is the only
    #    check here that asks the application rather than the page, so it is
    #    immune to wording, locale, and markup entirely.
    for method, status, url in ctx.scratch.get("writes_during_commit", []):
        if status < 400:
            ctx.scratch["success_method"] = f"http:{method} {status}"
            return True

    # 3. The field still holds what we committed — weak, but it rules out a
    #    form that reset or blew away on submit.
    committed = ctx.observations.get("committed_value")
    try:
        if _read_state(ctx.page, _target(ctx), _target_label(ctx)) == committed:
            ctx.scratch["success_method"] = "value_retained"
            return True
    except StepError:
        pass

    ctx.scratch["success_method"] = "none"
    return False


def _settle(ctx: StepContext) -> None:
    """Give an autosaving field time to persist before we reload out from under it."""
    try:
        ctx.page.wait_for_load_state("networkidle", timeout=ctx.timeout_ms)
    except Exception:
        pass
    ctx.page.wait_for_timeout(ctx.settle_ms)


def _reload(ctx: StepContext) -> None:
    try:
        ctx.page.reload(timeout=ctx.timeout_ms, wait_until="domcontentloaded")
        try:
            ctx.page.wait_for_load_state("networkidle", timeout=ctx.timeout_ms)
        except Exception:
            pass
    except Exception as exc:
        raise StepError(f"reload failed: {_brief(exc)}") from None


def _observe_final_value(ctx: StepContext) -> Any:
    """Re-find the field semantically — snapshot ids do not survive a reload."""
    label = _target_label(ctx)
    locator = _target(ctx)
    try:
        locator.wait_for(state="attached", timeout=ctx.timeout_ms)
    except Exception:
        raise StepError(
            f"{label} did not reappear after reload; cannot compare values"
        ) from None
    return _read_state(ctx.page, locator, label)


def _restore_original_value(ctx: StepContext) -> Any:
    """Put the field back to what it held before the test.

    BlindSpot writes to a real application. Leaving `BlindSpot_a7b231` in
    someone's Bio is not acceptable even on a staging box, so restoration is
    declared in the invariant's `teardown` and run in a finally block — it
    happens whether the test passed, failed, or blew up half-way.
    """
    original = ctx.observations.get("initial_value")
    if original is None:
        raise StepError("no initial_value was recorded; nothing to restore")
    ctx.page.reload(timeout=ctx.timeout_ms, wait_until="domcontentloaded")
    return _write(ctx, original)


def _commit_restore(ctx: StepContext) -> None:
    _click_commit(ctx)


STEP_HANDLERS: dict[str, Callable[[StepContext], Any]] = {
    "observe_initial_value": _observe_initial_value,
    "generate_mutation": _generate_mutation,
    "apply_mutation": _apply_mutation,
    "commit": _commit,
    "observe_success": _observe_success,
    "settle": _settle,
    "reload": _reload,
    "observe_final_value": _observe_final_value,
    "restore_original_value": _restore_original_value,
    "commit_restore": _commit_restore,
}

# The rule base may only name steps that exist here, and every step this
# module implements must be known to the rule base. A mismatch is a wiring
# bug; catching it at import beats discovering it mid-run.
_missing = set(STEP_RECORDS) - set(STEP_HANDLERS)
_extra = set(STEP_HANDLERS) - set(STEP_RECORDS)
if _missing or _extra:  # pragma: no cover - guards against edits to either side
    raise ImportError(
        f"step vocabulary mismatch with knowledge.engine: "
        f"unimplemented={sorted(_missing)} unknown={sorted(_extra)}"
    )


def _brief(exc: Exception, limit: int = 120) -> str:
    return str(exc).strip().splitlines()[0][:limit]


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


class PersistenceRunner:
    """Executes instances against a Playwright page."""

    def __init__(
        self,
        page,
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        settle_ms: int = DEFAULT_SETTLE_MS,
        highlight_ms: int = 0,
    ):
        self.page = page
        self.timeout_ms = timeout_ms
        self.settle_ms = settle_ms
        self.highlight_ms = highlight_ms

    def run(self, instance) -> RunResult:
        """Execute one instance's procedure, then always run its teardown."""
        result = RunResult(test_id=instance.test_id, instance=instance)
        started = time.monotonic()

        ctx = StepContext(
            page=self.page,
            instance=instance,
            observations=result.observations,
            timeout_ms=self.timeout_ms,
            settle_ms=self.settle_ms,
            highlight_ms=self.highlight_ms,
        )

        try:
            # Each test starts from a fresh load so a previous test's mutation
            # is never mistaken for this field's starting state.
            self.page.goto(
                instance.url, timeout=self.timeout_ms, wait_until="domcontentloaded"
            )
            self._execute(ctx, result, instance.procedure, teardown=False)
        except Exception as exc:
            result.error = f"navigation: {_brief(exc)}"
        finally:
            # Restoration runs whether the procedure passed, failed, or threw.
            # A failed teardown is recorded but never changes the verdict —
            # leaving test data behind is an operational problem, not evidence
            # about the invariant.
            if instance.teardown and "initial_value" in result.observations:
                self._execute(ctx, result, instance.teardown, teardown=True)
                result.restored = all(
                    step.ok for step in result.steps if step.teardown
                )

        result.notes = dict(ctx.scratch)
        result.duration_ms = int((time.monotonic() - started) * 1000)
        return result

    def _execute(self, ctx, result, steps, *, teardown: bool) -> None:
        """Run a sequence of named steps, recording what each produced."""
        for name in steps:
            handler = STEP_HANDLERS[name]
            records = STEP_RECORDS[name]
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

            if records:
                result.observations[records] = value
            result.steps.append(
                StepRecord(
                    name=name,
                    ok=True,
                    records=records,
                    value=value,
                    teardown=teardown,
                )
            )

    def run_all(self, instances) -> list[RunResult]:
        return [self.run(instance) for instance in instances]


def run_instances(
    instances,
    *,
    headless: bool = True,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    settle_ms: int = DEFAULT_SETTLE_MS,
) -> list[RunResult]:
    """Run instances in a throwaway browser. Convenience wrapper."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            page = browser.new_page()
            runner = PersistenceRunner(
                page, timeout_ms=timeout_ms, settle_ms=settle_ms
            )
            return runner.run_all(instances)
        finally:
            browser.close()
