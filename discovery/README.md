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

```
 page_inspector.py        fingerprint.py           graph.py              crawler.py
┌──────────────────┐    ┌──────────────────┐    ┌────────────────┐    ┌──────────────────┐
│ DOM -> snapshot   │ -> │ snapshot -> id    │ -> │ AppGraph       │ <- │ drives Playwright,│
│ (elements, roles, │    │ ("is this the     │    │ states + edges │    │ builds the graph  │
│  labels, values)  │    │  same state?")    │    │ + persistence  │    │ by firing every   │
└──────────────────┘    └──────────────────┘    └────────────────┘    │ affordance it finds│
                                                         │              └──────────────────┘
                                                         v
                                      ┌──────────────────────────────────┐
                                      │ projections.py / svg.py           │
                                      │ lossy views: summary / mermaid /  │
                                      │ html for a person or a model      │
                                      └──────────────────────────────────┘
                                                         │
                                                         v
                                              ┌────────────────────┐
                                              │ compare.py          │
                                              │ structural diff of  │
                                              │ two graphs           │
                                              └────────────────────┘
```

Nothing below `graph.py` talks to a browser, and nothing in this package
makes a semantic judgment ("this is a checkout flow", "this diff is a
regression"). That's deliberate: it's what makes a crawl cacheable to disk,
diffable, and re-usable by a layer that *does* judge (the knowledge engine),
without re-paying the cost of re-crawling.

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
  `path_to(target)` is a deterministic BFS shortest-path, the primitive any
  capability check is built on; `is_closed()` is true only when every
  discovered affordance was fired — the condition that licenses a *negative*
  claim ("no path exists") rather than just "none was found."

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

### `readable.py` — the value surface

`page_inspector` answers "what can a user do here" and drops everything
non-interactive. That is right for driving an application and wrong for
checking one: the facts a flow invariant asserts over — a total, a column of
prices, a stated row count — are rendered text that never appears in a
control snapshot.

`readable_with_page(page)` returns value-bearing text *grouped by selector*:
one entry per selector, with how many elements it matches and a sample of
their text. The grouping is the point — a flow invariant reads a collection,
so the useful selector is the one matching the collection rather than one of
its members.

Two ranking rules do most of the work:

- For a table cell, column position outranks class names. `td.num` looks
  specific and is not — a price column and a rating column both carry it, so
  binding to it conflates two series.
- A row-level class enters the cell selector, with a `:not()` form for the
  rows that lack it. Without that, `table.totals td:nth-child(2)` matches the
  line amounts *and* the grand total, and `sum(line_totals) == order_total`
  could never fail.

Run it: `python -m discovery.readable <url> [--text]`.

### `web_app_Inspector.py`

Stub, not yet implemented.

## Core concepts

| term | meaning |
|---|---|
| state | a place in the app with a distinct set of affordances — not a URL, not a snapshot's data |
| affordance | one actionable element in a state: a link, submit, button, or field |
| action / edge | firing one affordance from one state landed in another, with recorded inputs and effects |
| observed path | one concrete walk the crawler took |
| capability | a reachability claim — *some* walk exists from a state satisfying a precondition to one satisfying a goal; not tied to one exact sequence |
| closed graph | every discovered affordance was fired; licenses "no path exists" as a real conclusion instead of "none was found yet" |

The capability/path distinction is why inserting an upsell step between
contact and shipping in checkout doesn't invalidate anything — it's a
longer walk to the same capability, not a different one. A crawl, a graph,
and a diff all operate at the state/edge level for exactly this reason: so
the thing being modeled is what the app can do, not the specific sequence
one run happened to take.

## Running it

```bash
make crawl          # map the sound build -> runs/graphs/graph_sound.json
make crawl-broken    # map the broken build -> runs/graphs/graph_broken.json
make crawl-stable    # crawl the sound build twice, diff — proves fingerprint stability
make map             # print the summary() an LLM would be shown
make mermaid         # print mermaid source
make draw / make graph   # render + open the HTML diagram
make graph-diff       # structural diff of the sound vs. broken graphs
```

## Known gap

Discovery (the graph/crawler/fingerprint layer) is wired; the rest of the
pipeline (`main.py`, the persistence runner) still assumes page-shaped
state. `make edit` — pointing the old pipeline at a project edit form —
comes back inconclusive: the commit navigates to the detail page, so when
the runner reloads looking for the field it was watching, the field isn't
there anymore. Nothing is wrong with the application; the test is shaped
like a page and the behavior is shaped like a flow. Closing that gap —
having the runner and knowledge engine consume `AppGraph`/projections
instead of a single page — is the next piece of work.
