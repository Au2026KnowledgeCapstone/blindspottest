"""Walk an application exhaustively and record what it exposes as a graph.

The crawl is breadth-first over *affordances*, not URLs. A URL crawler follows
links and never learns that submitting the contact form lands on shipping;
that transition only exists if someone fills the form and presses the button.
Since the flows worth testing are exactly the ones behind buttons, the
frontier is a queue of `(state, affordance)` pairs and the crawl is finished
only when every pair has been fired.

Exhaustiveness is the point. A partial crawl cannot distinguish "the
application has no path to confirmation" from "I did not look", so a planner
built on it reports failures it cannot justify. `AppGraph.is_closed()` is the
claim this module exists to earn, and anything left in `unexplored` is a
retraction of it.

Getting back to a state
-----------------------
Firing an affordance leaves the state, so the next affordance of the same
state needs the application put back. Because the demo app keys all state to
a session cookie, clearing cookies is a complete reset — much cheaper than
restarting the process — and the state is then re-reached by replaying the
recorded path from the entry. Replay is verified rather than assumed: if the
page does not fingerprint as the state we meant to reach, the affordance is
recorded as unexplored instead of being fired somewhere unintended.

What this module does not do
----------------------------
No LLM, and no judgement about what anything means. It records that pressing
"Continue" in one state lands in another; whether that constitutes
CREATE_RESOURCE is a question for the semantic layer, working from the
projections in `discovery.projections`. Keeping labelling out of the crawl is
what makes a crawl cacheable and re-runnable for free.

WARNING: crawling fires every button it finds, including destructive ones.
Point this at a development application you can reset, never at anything
real. `CrawlConfig.deny_destructive` provides a text-matching guard, which is
a seatbelt and not a safety net.
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from discovery.fingerprint import fingerprint
from discovery.graph import Affordance, Action, AppGraph, State
from discovery.page_inspector import inspect_with_page
from runner.persistence_runner import locate

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

# Labels that usually mean "this cannot be undone". Matched case-insensitively
# as whole words against an affordance's visible text.
DESTRUCTIVE_WORDS = frozenset({
    "delete", "remove", "destroy", "deactivate", "close account",
    "cancel subscription", "unsubscribe", "revoke", "purge", "wipe",
})

# Values used to fill a form before submitting it. Keyed by input type, then
# by a substring of the field's label. Deliberately boring: the crawl is
# mapping structure, and inventing interesting values is the test generator's
# job, not discovery's.
DEFAULT_VALUES: dict[str, str] = {
    "email": "blindspot@example.com",
    "password": "demo123",
    "tel": "5551234567",
    "number": "1",
    "date": "2026-01-01",
    "url": "https://example.com",
    "search": "a",
    "text": "Blindspot",
}


@dataclass
class CrawlConfig:
    """Budgets and safety rails.

    The budgets exist because crawl cost is O(edges x replay depth), not
    O(states): every affordance needs the application put back before it can
    be fired, so a graph twice as deep costs far more than twice as much.
    """

    max_states: int = 60
    max_actions: int = 400
    max_depth: int = 10
    timeout_ms: int = 8_000
    # How long to wait for a page to be ready after navigating.
    #
    # `networkidle` costs ~520ms per navigation because Playwright defines it
    # as 500ms of network silence — an unavoidable floor paid on every single
    # transition. `domcontentloaded` costs ~17ms. Across a crawl that is the
    # difference between two minutes and ten seconds, and it is the dominant
    # term in crawl time by a wide margin.
    #
    # `domcontentloaded` is correct for a server-rendered application. A
    # client-rendered one paints after its first XHR, so it needs either
    # `networkidle` or a non-zero `settle_ms` — otherwise the crawl snapshots
    # a skeleton and fingerprints every page identically.
    wait_until: str = "domcontentloaded"
    # Extra fixed grace after each transition, for async work that completes
    # without a navigation (an autosave fetch, a client-side re-render).
    settle_ms: int = 0
    deny_destructive: bool = False
    # Credentials to use when a password field is present. Without these an
    # authenticated region of the app is simply unreachable, and the crawl
    # will honestly report it as such rather than pretend it is not there.
    credentials: dict[str, str] = field(default_factory=dict)
    # Paths the crawl must not leave. Anything off-site is recorded as a link
    # and never followed.
    same_origin_only: bool = True
    # Route prefixes the crawl must not enter. The demo app serves a second,
    # deliberately broken build under /broken, and following the link to it
    # merges two applications into one graph — which both doubles the crawl
    # and destroys the comparison the two builds exist to support. They are
    # two crawls whose graphs get diffed, not one crawl.
    exclude_prefixes: tuple[str, ...] = ()
    # How many distinct states an affordance must behave identically in
    # before it is treated as site-wide navigation and stops being fired.
    # Two is too eager (a coincidence), and much above three wastes most of
    # the saving on a large app.
    global_after: int = 3
    on_event: Callable[[str], None] | None = None


# --------------------------------------------------------------------------
# Crawler
# --------------------------------------------------------------------------


@dataclass
class _FrontierItem:
    state_id: str
    affordance_key: str
    depth: int


class Crawler:
    """Drives a Playwright page to build an `AppGraph`."""

    def __init__(self, page, base_url: str, config: CrawlConfig | None = None):
        self.page = page
        self.base_url = base_url.rstrip("/")
        self.config = config or CrawlConfig()
        self.graph = AppGraph(base_url=self.base_url)
        self._frontier: deque[_FrontierItem] = deque()
        self._queued: set[tuple[str, str]] = set()
        self._actions_fired = 0
        # Where each affordance key has been fired from, and where it landed.
        # An affordance that lands in the same place from many different
        # states is navigation chrome, and re-firing it teaches nothing.
        self._observed_targets: dict[str, set[str]] = {}
        self._observed_sources: dict[str, set[str]] = {}
        self._global: dict[str, str] = {}

    # -- logging -----------------------------------------------------------

    def _say(self, message: str) -> None:
        if self.config.on_event:
            self.config.on_event(message)

    # -- entry point -------------------------------------------------------

    def crawl(self, entry_path: str = "/") -> AppGraph:
        entry_url = self.base_url + entry_path

        self._reset()
        self._goto(entry_url)
        entry_state = self._observe()
        self.graph.entry = entry_state.id
        self._say(f"entry {entry_state.id} {entry_state.url_template}")
        self._enqueue(entry_state, depth=0)

        while self._frontier:
            if len(self.graph.states) >= self.config.max_states:
                self._say(f"stopping: max_states ({self.config.max_states}) reached")
                break
            if self._actions_fired >= self.config.max_actions:
                self._say(f"stopping: max_actions ({self.config.max_actions}) reached")
                break
            self._explore(self._frontier.popleft())

        # Anything still queued when a budget ran out is unexplored, and has
        # to be recorded as such — a graph that silently drops frontier items
        # would claim closure it has not earned.
        for item in self._frontier:
            self._mark_unexplored(item.state_id, item.affordance_key)

        self._say(
            f"done: {len(self.graph.states)} states, {len(self.graph.actions)} actions, "
            f"{'closed' if self.graph.is_closed() else 'OPEN (frontier not exhausted)'}"
        )
        return self.graph

    # -- the loop ----------------------------------------------------------

    def _explore(self, item: _FrontierItem) -> None:
        state = self.graph.states.get(item.state_id)
        if state is None:
            return
        affordance = state.affordances.get(item.affordance_key)
        if affordance is None:
            return

        if self._is_destructive(affordance):
            self._say(f"  skip (destructive): {affordance.label}")
            self._mark_unexplored(state.id, affordance.key)
            return

        # Site-wide navigation: already proven to behave identically from
        # enough states that firing it again is a browser round-trip spent
        # re-learning a known fact. Record the edge and move on.
        known_target = self._global.get(affordance.key)
        if known_target is not None and known_target != state.id:
            self.graph.connect(Action(
                source=state.id,
                target=known_target,
                affordance_key=affordance.key,
                label=affordance.label,
                effects=("site-wide navigation",),
                inferred=True,
            ))
            return

        if not self._establish(state):
            self._say(f"  could not return to {state.id}; leaving {affordance.label} unexplored")
            self._mark_unexplored(state.id, affordance.key)
            return

        inputs = self._fill_form(state, affordance)

        before = fingerprint(inspect_with_page(self.page))
        if not self._fire(affordance):
            self._mark_unexplored(state.id, affordance.key)
            return
        self._actions_fired += 1

        after_snapshot = self._snapshot()

        # A link's href can be checked before clicking; a button's destination
        # cannot. If firing one left the region under test, record where it
        # went but do not explore onward from there.
        if self._is_excluded(after_snapshot.get("url")):
            self._say(f"  {affordance.label!r} -> excluded region, not followed")
            self._mark_unexplored(state.id, affordance.key)
            return

        target = self.graph.observe(after_snapshot)
        after = fingerprint(after_snapshot)

        effects = self._effects(before, after)
        self.graph.connect(Action(
            source=state.id,
            target=target.id,
            affordance_key=affordance.key,
            label=affordance.label,
            inputs=tuple(sorted(inputs.items())),
            effects=effects,
        ))

        arrow = "self" if target.id == state.id else target.id
        self._say(f"  {affordance.label!r} -> {arrow} {target.url_template}")

        self._note_outcome(affordance.key, state.id, target.id)

        if target.visits == 1 and item.depth + 1 <= self.config.max_depth:
            self._enqueue(target, depth=item.depth + 1)

    def _note_outcome(self, key: str, source: str, target: str) -> None:
        """Promote an affordance to site-wide navigation once it has earned it.

        The test is deliberately strict: the same affordance must have been
        fired from `global_after` distinct states and landed in exactly one
        place every time. "Continue" moves through checkout to a different
        step each time and so never qualifies, which is the point — only
        genuinely position-independent controls are skipped.
        """
        if key in self._global:
            return
        self._observed_sources.setdefault(key, set()).add(source)
        self._observed_targets.setdefault(key, set()).add(target)

        sources = self._observed_sources[key]
        targets = self._observed_targets[key]
        if len(sources) >= self.config.global_after and len(targets) == 1:
            destination = next(iter(targets))
            # A control that always lands where it was pressed is a no-op, not
            # navigation; leave it alone so genuine self-loops stay visible.
            if destination in sources and len(sources) == 1:
                return
            self._global[key] = destination
            self._say(f"  [chrome] {key.split('|')[-1]!r} is site-wide -> {destination}")

    def _enqueue(self, state: State, depth: int) -> None:
        """Queue every affordance of a newly discovered state.

        Fields are not queued: filling one is part of firing a submit, not a
        transition in its own right. Queuing them would double the frontier
        and record edges that loop back to the same state.
        """
        for key, affordance in state.affordances.items():
            if affordance.kind == "field":
                continue
            if affordance.kind == "link" and not self._is_same_origin(affordance.href):
                continue
            if self._is_excluded(affordance.href):
                continue
            pair = (state.id, key)
            if pair in self._queued:
                continue
            self._queued.add(pair)
            self._frontier.append(_FrontierItem(state.id, key, depth))

    # -- getting into a state ---------------------------------------------

    def _establish(self, state: State) -> bool:
        """Put the application in `state`, verifying that it worked.

        Cheap path first: if the page already fingerprints as the state we
        want, nothing needs doing. That happens whenever an action lands
        somewhere with several affordances left to try.
        """
        try:
            if fingerprint(inspect_with_page(self.page)).id == state.id:
                return True
        except Exception:
            pass

        path = self.graph.path_to(state.id)
        if path is None:
            return False

        self._reset()
        self._goto(self.graph.states[self.graph.entry].example_url or self.base_url)

        for action in path:
            current = fingerprint(inspect_with_page(self.page))
            source = self.graph.states.get(current.id)
            if source is None:
                return False
            affordance = source.affordances.get(action.affordance_key)
            if affordance is None:
                return False
            for name, value in action.inputs:
                self._fill_one(name, value)
            if not self._fire(affordance):
                return False

        return fingerprint(self._snapshot()).id == state.id

    def _reset(self) -> None:
        """Return the application to its seed state.

        The demo app keys every mutation to a session cookie, so dropping
        cookies is a full reset with no server round-trip. An application
        without that property needs its own hook here — a fixture reload, a
        database snapshot — and the crawl is only as repeatable as that hook.
        """
        try:
            self.page.context.clear_cookies()
        except Exception:
            pass

    def _goto(self, url: str) -> None:
        self.page.goto(url, timeout=self.config.timeout_ms,
                       wait_until=self.config.wait_until)
        self._settle()

    def _settle(self) -> None:
        """Wait for the page to be ready to read.

        Cheap by default and deliberately so — see `CrawlConfig.wait_until`.
        A failed wait is not an error: a click that triggers no navigation
        leaves the page already loaded, which is the common case for buttons.
        """
        try:
            self.page.wait_for_load_state(
                self.config.wait_until, timeout=self.config.timeout_ms
            )
        except Exception:
            pass
        if self.config.settle_ms:
            self.page.wait_for_timeout(self.config.settle_ms)

    def _snapshot(self) -> dict:
        self._settle()
        return inspect_with_page(self.page)

    def _observe(self) -> State:
        return self.graph.observe(self._snapshot())

    # -- doing things ------------------------------------------------------

    def _fire(self, affordance: Affordance) -> bool:
        """Click an affordance. False when it could not be actioned at all."""
        try:
            locator = locate(self.page, affordance)
            locator.wait_for(state="visible", timeout=self.config.timeout_ms)
            locator.click(timeout=self.config.timeout_ms)
        except Exception as exc:
            self._say(f"  could not click {affordance.label!r}: {type(exc).__name__}")
            return False
        self._settle()
        return True

    def _fill_form(self, state: State, affordance: Affordance) -> dict[str, str]:
        """Fill every field in the state before firing a submit.

        Submitting an empty required form lands on a validation error, which
        is a real state and worth recording — but if that is the *only* thing
        the crawl ever does with a form, everything behind it stays invisible.
        Filling first means the happy path gets discovered; the validation
        path is reachable later by an explicit empty-input variant.
        """
        if affordance.kind not in ("submit", "button"):
            return {}

        used: dict[str, str] = {}
        for key, field_affordance in state.affordances.items():
            if field_affordance.kind != "field":
                continue
            value = self._value_for(field_affordance)
            if value is None:
                continue
            if self._fill_one(key, value, affordance=field_affordance):
                used[key] = value
        return used

    def _fill_one(self, key: str, value: str, affordance: Affordance | None = None) -> bool:
        if affordance is None:
            state_id = fingerprint(inspect_with_page(self.page)).id
            state = self.graph.states.get(state_id)
            affordance = state.affordances.get(key) if state else None
        if affordance is None:
            return False
        try:
            locator = locate(self.page, affordance)
            if locator.count() == 0:
                return False
            locator.fill(value, timeout=self.config.timeout_ms)
            return True
        except Exception:
            return False

    def _value_for(self, affordance: Affordance) -> str | None:
        """Pick a value for a field from its type and label."""
        strategy = affordance.locator or {}
        label = (affordance.label or "").lower()
        role = strategy.get("role", "")

        # Non-text controls are left alone. Selects already carry a default,
        # and toggling a checkbox is a state change worth its own edge rather
        # than a side effect of filling a form.
        if role in ("checkbox", "radio", "switch", "combobox", "listbox"):
            return None

        for name, value in self.config.credentials.items():
            if name in label:
                return value

        for hint, value in DEFAULT_VALUES.items():
            if hint in label:
                return value

        if "email" in label:
            return DEFAULT_VALUES["email"]
        if "post" in label or "zip" in label:
            return "SW1A 1AA"
        return DEFAULT_VALUES["text"]

    # -- guards ------------------------------------------------------------

    def _is_destructive(self, affordance: Affordance) -> bool:
        if not self.config.deny_destructive:
            return False
        text = (affordance.label or "").lower()
        return any(re.search(rf"\b{re.escape(word)}\b", text) for word in DESTRUCTIVE_WORDS)

    def _is_same_origin(self, href: str | None) -> bool:
        if not href or not self.config.same_origin_only:
            return True
        if href.startswith("/") or href.startswith("#"):
            return True
        return href.startswith(self.base_url)

    def _is_excluded(self, url: str | None) -> bool:
        """Whether a destination is outside the region being crawled."""
        if not url or not self.config.exclude_prefixes:
            return False
        path = url[len(self.base_url):] if url.startswith(self.base_url) else url
        if not path.startswith("/"):
            path = "/" + path
        return any(
            path == prefix or path.startswith(prefix.rstrip("/") + "/")
            for prefix in self.config.exclude_prefixes
        )

    def _mark_unexplored(self, state_id: str, key: str) -> None:
        self.graph.unexplored.setdefault(state_id, [])
        if key not in self.graph.unexplored[state_id]:
            self.graph.unexplored[state_id].append(key)

    @staticmethod
    def _effects(before, after) -> tuple[str, ...]:
        """The transition's effect, as a short diff.

        This is what an LLM is shown for an edge instead of two full
        snapshots — roughly fifty tokens against four thousand — and it is
        also the raw material for the `establishes` annotation a planner
        needs.
        """
        effects: list[str] = []
        if before.url_template != after.url_template:
            effects.append(f"url:{before.url_template}->{after.url_template}")

        before_set, after_set = set(before.signature), set(after.signature)
        for added in sorted(after_set - before_set)[:6]:
            effects.append(f"+{added}")
        for removed in sorted(before_set - after_set)[:6]:
            effects.append(f"-{removed}")
        if not effects:
            effects.append("no observable change")
        return tuple(effects)


# --------------------------------------------------------------------------
# Convenience entry point
# --------------------------------------------------------------------------


def crawl_app(
    base_url: str,
    entry_path: str = "/",
    *,
    headless: bool = True,
    config: CrawlConfig | None = None,
) -> AppGraph:
    """Open a browser, crawl, and return the graph."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            page = browser.new_page()
            return Crawler(page, base_url, config).crawl(entry_path)
        finally:
            browser.close()


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(prog="blindspot-crawl")
    parser.add_argument("base_url", nargs="?", default="http://127.0.0.1:3000")
    parser.add_argument("--entry", default="/")
    # Not `runs/`: `reporting.dashboard` globs `runs/*.json` and reads every
    # hit as a persistence run record. A graph in there is picked up as a
    # malformed run.
    parser.add_argument("--out", default="runs/graphs/graph.json")
    parser.add_argument("--max-states", type=int, default=60)
    parser.add_argument("--max-actions", type=int, default=400)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--safe", action="store_true",
                        help="skip affordances whose text looks destructive")
    parser.add_argument("--exclude", action="append", default=[],
                        metavar="PREFIX",
                        help="route prefix the crawl must not enter "
                             "(repeatable, e.g. --exclude /broken)")
    args = parser.parse_args()

    started = time.time()
    graph = crawl_app(
        args.base_url,
        args.entry,
        headless=not args.headed,
        config=CrawlConfig(
            max_states=args.max_states,
            max_actions=args.max_actions,
            deny_destructive=args.safe,
            exclude_prefixes=tuple(args.exclude),
            credentials={"username": "demo", "password": "demo123"},
            on_event=lambda m: print(m, flush=True),
        ),
    )
    path = graph.save(args.out)
    print(f"\n{len(graph.states)} states, {len(graph.actions)} actions "
          f"in {time.time() - started:.1f}s -> {path}")
