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
    # Follow links and nothing else: never fire a button, never submit a form,
    # never type into a field.
    #
    # This is the mode for an application you cannot restore. `deny_destructive`
    # is a text match on an English word list and misses "Archive", "Void",
    # "Send to all", and every label in another language — it is a seatbelt.
    # Read-only is a different claim: the crawl issues GETs and nothing else,
    # so it cannot write regardless of what any control is called.
    #
    # The cost is honest and unavoidable: everything behind a button is
    # invisible, so the graph is NOT closed and `is_closed()` says so. Every
    # unfired control is recorded in `unexplored`, which is exactly what stops
    # a read-only map being mistaken for a complete one.
    #
    # It also cannot sign in — a login is a form submission — so a read-only
    # crawl sees what an anonymous visitor sees. Reaching an authenticated
    # region without writing needs a pre-authenticated browser session rather
    # than credentials.
    read_only: bool = False
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
    # The inverse, and the one you want when the region under test is a
    # subtree rather than the whole site: everything outside this prefix is
    # out of region. Exclusion cannot express "stay under /broken", because
    # every route on the site starts with the sound build's "/".
    stay_under: str = ""
    # How many distinct states an affordance must behave identically in
    # before it is treated as site-wide navigation and stops being fired.
    # Two is too eager (a coincidence), and much above three wastes most of
    # the saving on a large app.
    global_after: int = 3
    on_event: Callable[[str], None] | None = None
    # Called with (page, state) the first time each state is reached, while
    # the browser is still on it.
    #
    # This exists because some states are only ever arrived at by POST. A
    # checkout confirmation has an `example_url`, but re-requesting it with a
    # GET does not render the confirmation — so anything a later stage needs
    # to read off that page has to be captured now, during the one visit
    # there will be. The flow layer's value bindings are exactly that.
    #
    # Deliberately a callback rather than a flag: the crawl stays a crawl and
    # has no opinion about what anyone wants from a page.
    on_state: Callable[[object, object], None] | None = None


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

    def _note_state(self, state: State) -> None:
        """Offer a newly discovered state to the observer, if there is one.

        Only on the first visit, and never allowed to fail the crawl: an
        observer is a bystander, and a map is still worth having when
        something optional raised on one page.
        """
        if self.config.on_state is None or state.visits != 1:
            return
        try:
            self.config.on_state(self.page, state)
        except Exception as exc:
            self._say(f"  [observer] {state.id}: {type(exc).__name__}: {exc}")

    # -- entry point -------------------------------------------------------

    def crawl(self, entry_path: str = "/") -> AppGraph:
        entry_url = self.base_url + entry_path

        self._reset()
        self._goto(entry_url)
        entry_state = self._observe()
        self._note_state(entry_state)
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

        # Checked here as well as at enqueue time. `_enqueue` keeps buttons out
        # of the frontier, but an edge inferred as site-wide navigation or a
        # frontier item queued before a config change could still arrive here,
        # and a read-only guarantee that depends on one code path holding is
        # not a guarantee.
        if self.config.read_only and affordance.kind != "link":
            self._say(f"  skip (read-only, would submit): {affordance.label}")
            self._mark_unexplored(state.id, affordance.key)
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
            self._say(f"  {affordance.label!r} -> outside region, not followed")
            self._mark_out_of_region(state.id, affordance.key)
            return

        target = self.graph.observe(after_snapshot)
        self._note_state(target)
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
            # Under read-only, anything that is not a link is recorded as
            # unexplored rather than silently dropped. That is what keeps
            # `is_closed()` false and makes the resulting map honest about
            # being partial.
            if self.config.read_only and affordance.kind != "link":
                self._mark_unexplored(state.id, key)
                continue
            if affordance.kind == "link" and not self._is_same_origin(affordance.href):
                continue
            if self._is_excluded(affordance.href):
                self._mark_out_of_region(state.id, key)
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
        if not url:
            return False
        path = url[len(self.base_url):] if url.startswith(self.base_url) else url
        if not path.startswith("/"):
            path = "/" + path
        bare = path.split("?")[0].split("#")[0]

        under = self.config.stay_under.rstrip("/")
        if under and not (bare == under or bare.startswith(under + "/")):
            return True

        return any(
            bare == prefix.rstrip("/") or bare.startswith(prefix.rstrip("/") + "/")
            for prefix in self.config.exclude_prefixes
        )

    def _mark_unexplored(self, state_id: str, key: str) -> None:
        self.graph.unexplored.setdefault(state_id, [])
        if key not in self.graph.unexplored[state_id]:
            self.graph.unexplored[state_id].append(key)

    def _mark_out_of_region(self, state_id: str, key: str) -> None:
        self.graph.out_of_region.setdefault(state_id, [])
        if key not in self.graph.out_of_region[state_id]:
            self.graph.out_of_region[state_id].append(key)

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
    slow_mo: int = 0,
    config: CrawlConfig | None = None,
) -> AppGraph:
    """Open a browser, crawl, and return the graph.

    `slow_mo` pauses before each browser action. It exists for watching a
    crawl with `headless=False`: Playwright fires actions faster than anyone
    can follow, so a visible browser without it shows a blur rather than a
    walk. It changes the pace of a crawl, never its result.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, slow_mo=slow_mo)
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
    parser.add_argument("--slow-mo", type=int, default=0, metavar="MS",
                        help="pause this many ms before each browser action; "
                             "pair with --headed to watch the crawl "
                             "(300-500 is readable)")
    parser.add_argument("--safe", action="store_true",
                        help="skip affordances whose text looks destructive")
    parser.add_argument("--exclude", action="append", default=[],
                        metavar="PREFIX",
                        help="route prefix the crawl must not enter "
                             "(repeatable, e.g. --exclude /broken)")
    parser.add_argument("--stay-under", default="", metavar="PREFIX",
                        help="confine the crawl to this route subtree "
                             "(e.g. --stay-under /broken)")
    parser.add_argument("--repeat", type=int, default=1, metavar="N",
                        help="crawl N times and diff the graphs; any "
                             "difference is crawler instability, not a "
                             "change in the application")
    parser.add_argument("--slow", action="store_true",
                        help="wait for network idle after each transition "
                             "(~30x slower; needed for client-rendered apps)")
    parser.add_argument("--read-only", action="store_true",
                        help="follow links only — never fire a button, submit "
                             "a form, or type into a field. The map will be "
                             "incomplete and will say so")
    parser.add_argument("--credential", action="append", default=[],
                        metavar="LABEL=VALUE",
                        help="value to type into fields whose label contains "
                             "LABEL, e.g. --credential password=hunter2 "
                             "(repeatable)")
    parser.add_argument("--settle", type=int, default=0, metavar="MS",
                        help="wait this long after each transition; also "
                             "throttles the crawl, which a real host may need")
    args = parser.parse_args()

    def parse_credentials(pairs: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for pair in pairs:
            if "=" not in pair:
                parser.error(
                    f"--credential expects LABEL=VALUE, got {pair!r}"
                )
            label, _, value = pair.partition("=")
            out[label.strip().lower()] = value
        return out

    credentials = parse_credentials(args.credential)

    def run_once() -> tuple[AppGraph, float]:
        started = time.time()
        graph = crawl_app(
            args.base_url,
            args.entry,
            headless=not args.headed,
            slow_mo=args.slow_mo,
            config=CrawlConfig(
                max_states=args.max_states,
                max_actions=args.max_actions,
                deny_destructive=args.safe,
                exclude_prefixes=tuple(args.exclude),
                stay_under=args.stay_under,
                wait_until="networkidle" if args.slow else "domcontentloaded",
                settle_ms=args.settle,
                read_only=args.read_only,
                # No default credentials. Shipping demo/demo123 in the library
                # meant pointing the crawler at any real login form typed those
                # into it; the demo's own credentials belong in the demo's
                # invocation, not in the tool.
                credentials=credentials,
                on_event=(lambda m: print(m, flush=True)) if args.repeat == 1 else None,
            ),
        )
        return graph, time.time() - started

    graphs = []
    for attempt in range(max(1, args.repeat)):
        if args.repeat > 1:
            print(f"crawl {attempt + 1} of {args.repeat} ...", flush=True)
        graph, elapsed = run_once()
        graphs.append(graph)
        print(f"  {len(graph.states)} states, {len(graph.actions)} actions, "
              f"{'closed' if graph.is_closed() else 'OPEN'}, {elapsed:.1f}s",
              flush=True)

    path = graphs[0].save(args.out)
    print(f"\nwrote {path}")

    # Whole-graph reproducibility is per-state stability to the Nth power, so
    # instability that looks negligible on one page is fatal across an
    # application. A crawl that cannot reproduce itself cannot be used to
    # detect regressions, because every diff is indistinguishable from noise.
    if len(graphs) > 1:
        from discovery.graph import graph_diff

        print("\n--- stability ---")
        unstable = False
        for i in range(1, len(graphs)):
            report = graph_diff(graphs[0], graphs[i])
            if report["identical"]:
                print(f"crawl 1 vs {i + 1}: identical")
                continue
            unstable = True
            print(f"crawl 1 vs {i + 1}: DIFFERS")
            for field_name in ("states_added", "states_removed",
                               "edges_added", "edges_removed"):
                for entry in report[field_name]:
                    print(f"  {field_name[:-1].replace('_', ' '):14} {entry}")
        if unstable:
            raise SystemExit(1)
