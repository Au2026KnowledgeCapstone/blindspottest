# Discovery

Maps what an application exposes — states, the actions that connect them, and
what each action changes — without judging whether any of it is correct.
Discovery answers "what can this app do"; deciding what it *should* do is the
knowledge engine's job, not this package's.

The motivating gap: the original MVP tested one page at a time (edit field →
save → reload → compare). That catches a dropped field, but it structurally
cannot see a bug that only shows up across a flow — a soft-deleted resource
still being reachable, a line item missing from a checkout total, a sort that
breaks on currency strings. Those are properties of a *path through the app*,
so discovery has to build the graph that path lives on before anything can
reason about it. See the root `README.md` for the full motivation and the
planted defects in `demo_app/` that this exists to catch.

## Pipeline

Two capture modules, because driving an application and checking one need
different things off the same page:

```
  a live browser page
        │
        ├──> page_inspector.py   DOM -> control snapshot ("what you can DO")
        │          │
        │          v
        │    fingerprint.py      snapshot -> state id ("the same state?")
        │          │
        │          v                                       crawler.py
        │    graph.py            states, edges,        <──  BFS over
        │          │             pathfinding                (state, affordance),
        │          │                                        firing everything,
        │          v                                        + on_state hook ─┐
        │    projections.py      summary (model-sized),                      │
        │    svg.py              mermaid / html (human)                      │
        │          ├──> compare.py   structural diff of two graphs           │
        │          │                                                         │
        └──> readable.py         text -> value surface ("what you can READ") ─┘
                   │
  ═════════════════╪══════════════ package boundary ══════════════════════
                   v
    semantic.flow_classifier   map + value surface -> capabilities    (LLM)
    knowledge.flows            capability -> which invariants apply
    runner.flow_runner         walk the path, read the bindings, probe
    baseline / regression      record it, replay it, diff it
```

`readable.py` is reached two ways, and both matter. A one-off look can open a
page directly, but the crawl's `on_state` hook is what captures states only
reachable by POST — a checkout confirmation cannot be re-requested with a
GET, so the single visit the crawl makes is the only chance to read it.

Nothing below `graph.py` talks to a browser, and nothing in this package
makes a semantic judgment ("this is a checkout flow", "this diff is a
regression"). That's deliberate: it's what makes a crawl cacheable to disk,
diffable, and re-usable by a layer that *does* judge, without re-paying the
cost of re-crawling.

The boundary is now load-bearing rather than aspirational — see
[Downstream](#downstream-what-consumes-this) for the three things discovery
hands off and the guarantees each depends on.

## Module by module

### `page_inspector.py` — DOM → snapshot

Runs one JS pass inside the page (not N Playwright round-trips) and returns
a compact snapshot: `{url, title, elements: [...]}`. Each element keeps only
what describes *what it is and what it currently holds* — role, type, label,
value, disabled/readonly/required, a resolved locator — and drops everything
else, so a 3000-node DOM becomes a dozen entries.

Two entry points: `inspect_with_page(page)` snapshots whatever an
already-open page is showing (what the crawler and runner use mid-session),
and `inspect_page(url)` opens a throwaway browser for a one-off look.

Every element also carries a `locator_strategy` (role+name first, falling
back to label/placeholder/test-id/CSS) — the same resolution the runner uses
to re-find an element later, so a path the crawler walked is guaranteed
replayable.

### `readable.py` — page → value surface

The other half of capture. `page_inspector` drops everything
non-interactive, which is right for driving an application and wrong for
checking one: the facts a flow invariant asserts over — a total, a column of
prices, a stated row count — are rendered text that never appears in a
control snapshot. Without this module a model asked to bind `line_totals` to
a selector would have to invent one.

`readable_with_page(page)` returns value-bearing text *grouped by selector*:
one entry per selector, with how many elements it matches, a sample of their
text, and whether they parse as numbers. The grouping is the point — a flow
invariant reads a collection, so the useful selector is the one matching the
collection rather than one of its members, and `count` is what distinguishes
the two.

Two ranking rules do most of the work:

- For a table cell, column position outranks class names. `td.num` looks
  specific and is not — a price column and a rating column both carry it, so
  binding to it conflates two unrelated series into one list.
- A row-level class enters the cell selector, with a `:not()` form for the
  rows that lack it. Without that, `table.totals td:nth-child(2)` matches the
  line amounts *and* the grand total, so `sum(line_totals) == order_total`
  would compare a sum against a number inside it and could never fail.

Numeric and repeated groups are ranked first, so a model choosing bindings
finds totals and row values at the top of the list.

`summarize(readable)` renders it one line per selector, which is the form the
flow classifier is actually shown.

Run it: `python -m discovery.readable <url> [--text]`, or
`make values URL=/catalog`.

### `fingerprint.py` — snapshot → state identity

The load-bearing module. Everything above it is only as trustworthy as the
answer to "are these two snapshots the same state?" Too strict and
`/projects/3` and `/projects/4` become different states, blowing up the
graph; too loose and pages that behave differently collapse into one node,
producing plans that can't be walked.

The rule: a state is identified by **what you can do**, not what it
currently shows.

- kept: URL template (`/projects/<id>`, numeric/UUID/hex segments
  normalized away), control roles/types/names, disabled state
- dropped: field values, row counts (bucketed into `1` / `2-3` / `many`),
  instance text (`"Project 37"` → `"project #"`), transient query params
  (`?notice=`, `?saved=`, …)

`strip_prefix` removes a mount point before templating. Because the route is
part of the identity, a state recorded at `/cart` would otherwise never be
recognised at `/broken/cart` or `/v2/cart` — every arrival check would fail
and every replayed test would come back inconclusive, which reads as a total
regression and is really just a changed mount point. The baseline loader sets
it when retargeting a recorded suite at a different deployment.

`fingerprint(snapshot)` returns a `Fingerprint(id, url_template, signature)`
— `id` is a sha256 of the sorted, deduplicated signature, so DOM order and
instance data never affect identity. `diff(left, right)` explains what
changed between two fingerprints, both for debugging drift and as the raw
material for an edge's recorded `effects`.

### `graph.py` — the data model

- **`Affordance`** — one actionable element (`link` / `submit` / `button` /
  `field`), keyed by its element signature (not its DOM position, which
  renumbers on every insert).
- **`State`** — one node: a URL template, a signature, its affordances, and
  one representative snapshot (kept for the detail projection).
- **`Action`** — one edge: firing an affordance in `source` landed in
  `target`, with the `inputs` used, the `effects` observed, and whether the
  edge was walked or `inferred` (see site-wide navigation, below).
  `requires`/`establishes` are where edge preconditions will live so a
  planner can tell a real path from a merely plausible one.
- **`AppGraph`** — states + actions for one build. `observe(snapshot)`
  records a visit (idempotent — revisits bump a counter, not the stored
  snapshot); `connect(action)` adds an edge, de-duping exact repeats;
  `is_closed()` is true only when every discovered affordance was fired —
  the condition that licenses a *negative* claim ("no path exists") rather
  than just "none was found."

**Pathfinding** is the primitive every capability check is built on, and it
is deliberately model-free — a plan is correct by construction against the
graph rather than plausible-looking:

- `path_between(source, target)` — deterministic BFS shortest path. Walked
  edges are explored before `inferred` ones; both are the same length to
  BFS, so this only decides which of several equally short paths comes back,
  and a path made of edges somebody actually fired is a better bet for
  replay than one assembled out of deductions about site-wide navigation.
- `path_to(target)` — `path_between(entry, target)`.

The flow layer needs both: `path_to` reaches the state a capability starts
from, and `path_between` covers the actions that perform it. Splitting a
flow at that seam is what lets a probe URL be captured *between* the two —
a deleted resource's own URL is only knowable before it is deleted.

Also tracks what a crawl didn't finish: `unexplored` (frontier left when a
budget ran out — a retraction of closure) vs. `out_of_region` (deliberately
not followed because it leaves the region under test — a declared boundary,
not a gap).

`graph_diff(before, after)` is the structural diff two graphs reduce to:
state ids added/removed, edge keys added/removed. Same computation whether
`before`/`after` are two crawls of one build (a stability check) or two
different builds (the regression signal) — see `compare.py`.

### `crawler.py` — drives the graph build

BFS over `(state, affordance)` pairs, not URLs — a URL crawler never learns
that submitting a form lands on shipping, because that transition only
exists behind the click. The frontier is finished only when every
`(state, affordance)` pair has been fired or explicitly marked unreachable.

Key mechanics:

- **Re-establishing state.** Firing an affordance leaves its source state,
  so the next affordance from that same state needs the app put back first.
  Cheap path: if the current page already fingerprints as the target state,
  nothing to do. Otherwise: clear cookies (a full reset, no server
  round-trip, since the demo app keys everything to a session cookie),
  navigate to entry, and replay the recorded path to that state — verified
  against the fingerprint at each step, not assumed, so a broken replay is
  recorded as `unexplored` rather than firing an affordance from the wrong
  place.
- **Cost.** `O(affordances × replay depth)`, not `O(states)` — a graph twice
  as deep costs far more than twice as much, because every affordance needs
  its state re-established. Bounded by `CrawlConfig`: `max_states` (60),
  `max_actions` (400), `max_depth` (10).
- **Site-wide navigation.** An affordance fired from `global_after` (default
  3) distinct states that always lands in the same place is promoted to
  "chrome" — recorded as an inferred edge on sight instead of re-fired from
  every remaining state. Without this, a sidebar of 8 links across 20 states
  is 160 browser round-trips to learn one fact.
- **Forms.** Filled before a submit is fired (boring, deterministic values
  from `DEFAULT_VALUES`/`credentials`, keyed by field type and label
  substring) so the happy path gets discovered; an empty-input variant is a
  separate, explicit crawl concern, not something every submit pays for.
- **Guards.** `same_origin_only`, `exclude_prefixes` / `stay_under` (keep the
  two demo builds, `/` and `/broken`, as two separate crawls rather than one
  merged graph), and `deny_destructive` (a text-match seatbelt on labels
  like "delete"/"cancel subscription" — not a safety net; **never point this
  at a real host**, it fires every button it finds).
- **Observer hook.** `CrawlConfig.on_state(page, state)` is called once per
  newly discovered state, while the browser is still on it. This exists
  because some states are only ever arrived at by POST: a checkout
  confirmation has an `example_url`, but re-requesting it with a GET renders
  something else entirely, so the single visit the crawl makes is the only
  chance to read anything off it. Capturing the value surface through the
  hook is what makes `TOTAL_CONSISTENCY` bindable at all. Deliberately a
  callback rather than a flag — the crawl stays a crawl and has no opinion
  about what anyone wants from a page — and an observer that raises is
  logged rather than allowed to fail the crawl.

Run directly: `python -m discovery.crawler <base_url> [--entry /path]
[--exclude PREFIX] [--stay-under PREFIX] [--safe] [--repeat N] [--out
path.json]`. `--repeat N` crawls N times and diffs the graphs — any
difference is crawler instability, not an application change, and is the
precondition for trusting a diff between two different builds.

### `projections.py` — lossy, consumer-specific views

A full graph (every state, every snapshot) is ~150k tokens — unusable as
model context. The graph is stored once and projected many times, each view
sized for what its consumer actually needs:

- `summary(graph)` — ~15 tokens/state: route, affordance kind-counts, named
  transitions, with site-wide navigation hoisted to one shared block instead
  of repeated under every state. This is the application map a model holds
  while it asks for detail on the two or three states it cares about.
- `state_detail(graph, state_id)` — one state's full affordance list.
- `edge_detail(graph, action)` — one transition's effect (~50 tokens vs. the
  ~4000 of two full snapshots).
- `mermaid(graph)` — flowchart source for a person, navigation omitted by
  default (otherwise every state links to every other and the flows worth
  seeing disappear into a mesh).
- `html(graph)` — a standalone, dependency-free page: stats, an interactive
  SVG diagram (via `svg.py`), and full state/transition tables. Meant to sit
  in `runs/` next to the crawl that produced it and open as a local file.

Run directly: `python -m discovery.projections <graph.json> --view
{summary,mermaid,html,json} [--state <id>] [--out path]`.

### `svg.py`

A from-scratch Sugiyama-style layered graph layout and SVG renderer (no
external library, no CDN) so the `html()` projection stays a self-contained
file. Not part of the conceptual pipeline — a rendering detail `projections`
depends on.

### `compare.py` — what changed between two graphs

Thin CLI around `graph_diff`: prints routes unique to each side (diffed
by *route*, not state id, since the two demo builds live at different
prefixes and their fingerprints never match even when the page is
identical), warns if either graph is `OPEN` (a missing route might just be
one the crawl didn't reach, not one that's gone), and — importantly — says
plainly when it has nothing to report: matching routes mean no *structural*
change, but a behavioral defect (wrong total, broken sort) doesn't change
the graph's shape. Discovery finds *where to look*; it can't find the bug
itself — that's the knowledge engine's invariants, working from the paths
and effects discovery recorded.

`--identical` exits non-zero unless the two graphs match exactly, for use as
a stability gate (`make crawl-stable`).

There are now two graph-diffing paths, and they are not redundant.
`compare.py` is the standalone CLI for looking at two crawls by hand;
`regression.differ` does the same route-based comparison as one input among
several (verdicts, observations, UI) inside a full regression run, and turns
the result into ranked findings rather than printed text. Both lean on
`graph.graph_diff`, and both carry the same caveat about open graphs — the
differ attaches it to each structural finding, since that is the point where
someone would otherwise be misled.

### `web_app_Inspector.py`

Stub, not yet implemented.

## Core concepts

| term | meaning | lives in |
|---|---|---|
| state | a place in the app with a distinct set of affordances — not a URL, not a snapshot's data | `graph.State` |
| affordance | one actionable element in a state: a link, submit, button, or field | `graph.Affordance` |
| action / edge | firing one affordance from one state landed in another, with recorded inputs and effects | `graph.Action` |
| observed path | one concrete walk the crawler took | a list of `Action` |
| capability | a reachability claim — *some* walk exists from a state satisfying a precondition to one satisfying a goal; not tied to one exact sequence | `semantic.flow_schemas.FlowCapability` |
| closed graph | every discovered affordance was fired; licenses "no path exists" as a real conclusion instead of "none was found yet" | `AppGraph.is_closed()` |
| binding | a named value plus the selector that reads it — how a generic invariant reads a specific application | `semantic.flow_schemas.ObservationBinding` |
| probe | a request made after a flow, to test whether something still exists | `semantic.flow_schemas.ProbeSpec` |

The capability/path distinction is why inserting an upsell step between
contact and shipping in checkout doesn't invalidate anything — it's a
longer walk to the same capability, not a different one. A crawl, a graph,
and a diff all operate at the state/edge level for exactly this reason: so
the thing being modeled is what the app can do, not the specific sequence
one run happened to take.

Capability was a term in this document before it was a type anywhere. It is
now an enum the flow rule base attaches invariants to, which is what closed
the loop: a capability name resolves to a set of invariants through their
`applies_to`, exactly as `persistent_mutation` already did for the
page-level ones.

## Downstream: what consumes this

Discovery hands off three artifacts. Each carries a guarantee that something
above it depends on, and the guarantee is the interesting part:

| artifact | consumer | the guarantee it rests on |
|---|---|---|
| `projections.summary(graph)` + the value surface | `semantic.flow_classifier` | the map is cheap enough to reason over whole (~3k tokens, not ~150k) |
| `AppGraph.path_to` / `path_between` | `knowledge.flows` instantiation | a path is walkable by construction, so a materialized instance is replayable |
| `fingerprint(snapshot)` | `runner.flow_runner` arrival checks | a state recorded once is recognisable again |

That last one is why the flow runner can report **inconclusive** honestly.
Every walk ends by fingerprinting the page and comparing it against the
state the graph predicted. A path that executes cleanly and lands somewhere
else is the most misleading thing a flow test can do — every observation
after it reads a real page, parses fine, and describes the wrong one — so
arrival is verified rather than assumed, and a flow that could not be
completed is reported as untested instead of as a defect.

`fingerprint`'s `strip_prefix` is the other half of that: without it a suite
recorded against one mount point could never be replayed against another,
and a changed path would read as a total regression.

## Running it

Discovery on its own:

```bash
make crawl               # map the sound build -> runs/graphs/graph_sound.json
make crawl-broken        # map the broken build -> runs/graphs/graph_broken.json
make crawl-stable        # crawl twice and diff — proves fingerprint stability
make map                 # print the summary() an LLM would be shown
make mermaid             # print mermaid source
make draw / make graph   # render + open the HTML diagram
make graph-diff          # structural diff of the sound vs. broken graphs
make values URL=/catalog # print one page's readable value surface
```

Discovery as part of a whole run:

```bash
make baseline            # crawl + classify + run flows, record to runs/baseline-sound
make baseline-cached     # same, reusing saved flow candidates (no LLM)
make regression          # replay that baseline against /broken — expect regressions
make regression-clean    # replay it against the sound build — expect nothing
make flow-rules          # show the flow invariants the rule base holds
```

## Known gaps

The gap this document used to describe — "the runner and knowledge engine
still assume page-shaped state" — is closed. `knowledge.flows` and
`runner.flow_runner` consume `AppGraph` and its projections directly, and a
flow that cannot be completed now reports inconclusive with the state it
actually reached. What remains:

- **The crawler does not branch on `<select>` options.** `_value_for`
  deliberately leaves non-text controls alone ("selects already carry a
  default"), so a catalog is only ever crawled at its default sort —
  `sort=name-asc` on the demo app. The graph therefore contains no
  price-sorted state, and `ORDERING_CONSISTENCY` has nothing to instantiate
  against: the invariant loads and its `sorted_asc` relation behaves
  correctly on the two builds' price columns when handed them directly, but
  no end-to-end run has ever fired it. Exercising it needs the crawl to
  treat a select's options as distinct affordances, which multiplies the
  frontier and so wants a budget of its own.
- **Nothing here has an automated test.** There is no `tests/` directory on
  this branch; every claim above was checked by running the pipeline against
  the demo app's two builds by hand. `make crawl-stable` and
  `make regression-clean` are the two reproducibility checks that exist, and
  both are manual.
- **`Action.requires` / `Action.establishes` are declared and unused.**
  Pathfinding is still pure topology, so a path is walkable because the
  crawler walked it, not because its preconditions were checked. That holds
  up for replay and would not hold up for planning a walk the crawler never
  took.
- **`is_closed()` is cheap to lose and expensive to earn.** Every structural
  claim about absence depends on it, and any budget being hit retracts it.
  `make crawl-stable` establishes that the crawler reproduces itself; the
  regression differ attaches an explicit caveat to structural findings when
  either side's graph is open.
- **`web_app_Inspector.py` is still a stub.**

One operational note that is not a gap but reads like one: the crawl fires
every button it finds, including destructive ones. `deny_destructive` is a
text-match seatbelt, not a safety net. Point this at a development
environment.
