### How it works 

The page inspector simplifies the DOM, sends it to the classifier, in which AI will decide which elements to test, which sends it to the knowledge engine that will extract the property type and find the invarient (preloaded), it will then send the property type and invarient to the test generator, which will generate the test and send it to the test runner, which will run the test and send the result back to the knowledge engine, which will go to the reporter.


I am trying to see, instead of specific varients if we can do this with entire flows..


What that architecture looks like is a little blurry so here's my best try at it.

Current MVP architecture is centered on local invarients, more specifically for this test being persistance.

so an example of an invariant would be:

```bash
edit field
 -> save
 -> reload
 -> compare
```

Works well for distinct properties, but not for flows.

A purchase flow may well be 

```bash
Cart
 -> Checkout
 -> Payment
 -> Confirmation
```

but could later become 

```bash
Cart
→ Checkout
→ Upsell
→ Shipping
→ Payment
→ Confirmation
```

the second flow may be completely valid, so a requirement would be for blindspot to not *treat the exact path as the specification*.


The architecture needs to be able to distingush between:

```bash
Capability
Goal Conditions
Invariants
Observed Path
```





My next piece of work is to find a way to discover entirety of a flow.

1. Discovery
   What does the application currently expose?

With this we can start to build a model of the application, in graph form.

So maybe something like this:

```bash
Projects page
→ "New Project"
→ form with Name field
→ "Create"
→ Project Details page
```

The AI can ask, which of the preloaded behavior patterns does this match? 

and we can have a list of possible labels, like:

```bash
CREATE_RESOURCE
UPDATE_RESOURCE
DELETE_RESOURCE
PERSISTENT_MUTATION
SORT_COLLECTION
FILTER_COLLECTION
AUTHENTICATE
SEARCH
UNKNOWN
```

We can have the system learn new behaviour patterns and add them to invarients if needed.


and from there, we can have the knowledge engine verify that it's the correct invariant for the flow, and then generate the test instances to test on the runner.


## Where this landed

Three pipelines, chosen by flag on `main.py`.

```bash
blindspot <url>                                   # ad-hoc scan of one page (the original MVP)
blindspot --baseline <url>                        # record a baseline of a whole application
blindspot --regression --baseline-dir <dir> <url> # replay it against a later build
```

Against a real site, start with `--read-only`: it follows links and fires
nothing, so it cannot write whatever the buttons are called. A writing crawl
against a non-loopback host is refused unless `--allow-writes` is passed,
because a crawl fires every button it finds. `make remote-map TARGET=...`
wraps the safe form; `make help` has the rest.

### Pipeline 1 — record a baseline

```bash
discovery   crawl the app              -> AppGraph + per-state value surface
semantic    read the map               -> capability candidates          (LLM)
knowledge   capability -> invariants   -> flow test instances (rule base)
runner      walk the paths             -> observations                   (no LLM)
knowledge   observations -> verdicts   (rule base)
baseline    graph + surface + instances + verdicts -> disk
```

### Pipeline 2 — detect regressions

```bash
baseline    load the recorded instances
runner      replay them verbatim against the new build   (no LLM, no re-classification)
regression  diff verdicts and observations; structure/UI with --recrawl
regression  explain what the diff means                  (LLM)
reporting   regressions first, then fixes, then the rest
```

The replay is the load-bearing part. An instance has every value it needs
fixed before it executes — the mutation string, the walk, the selectors — so
both runs assert exactly the same thing and a difference in the answers is a
difference in the application. Re-classifying on each run would compare two
different test suites and blame the code. It is also why a regression run
calls no LLM for classification and is cheap enough to run per build.

### Regression replay and discovery

`--regression` now replays the saved suite without crawling. Use `--recrawl`
to also compare discovered structure and UI, or `--discover-only` to record
a discovery baseline without running flow tests. `--replay-only` explicitly
selects the default regression behavior. Reports state which comparisons ran;
an empty replay suite exits with an execution/coverage error.

Results separate execution health, expectation correctness, and comparison
status. Missing selectors, ambiguous controls, failed setup, and unverified
goals remain inconclusive. Rule, contract, fixture, evaluator, and identity
policy changes make tests noncomparable rather than product regressions.
Baseline schema 1.2 stores this evidence while older schemas remain readable.

Contracts support versioned independent `checks` (an ID, relation, and typed
bindings), per-action readiness, cleanup assertions, frame selectors, and
checked/selected/enabled observations. Numeric predicates distinguish finite
values, nonnegative amounts, and positive integer quantities from arithmetic
consistency. These checks need applicable, scoped bindings; discovery alone
cannot prove the intended business policy. Initial-load, setup, goal, and reload-verification JavaScript errors fail
the default runtime check; set `runtime_errors: record` to retain evidence
without asserting that policy. An explicit `goal_path` requires readiness
assertions; otherwise replay verifies the recorded state fingerprint.

Discovery prioritizes form and capability actions and delays repeated carousel
navigation. `--max-attempts` (default 2000) includes failed actions and
`--crawl-seconds` (default 120) bounds scheduling time. Zero disables each
budget; an in-flight browser operation can extend the deadline. Repeated
restoration failures suppress further attempts and remain unexplored coverage.
Graph metrics include attempts, restoration/action time, and stop reason.
Use repeated `--id-segment-pattern REGEX` options only for known interchangeable
route IDs; real navigation URLs remain intact and the policy is frozen for replay.

### Crawl checkpoints and resume

Discovery now saves an atomic crawl journal automatically and prints its path.
The journal includes the graph, pending and interrupted task, actual action
recipes, browser cookies/local storage/IndexedDB, session storage, observations,
input choices, and failure history. Journal files are private (`0600`) because
form inputs and sessions can contain credentials. Immutable snapshots live in a
private adjacent `<journal>.data/` directory and are written once per state.
Keep that directory with the journal when copying or resuming a run; published
baseline graph files remain standalone.

```bash
# Start a bounded discovery run with a named journal.
.venv/bin/python main.py http://localhost:3000 --discover-only \
  --baseline-dir runs/discovery-partial --crawl-checkpoint runs/crawl.json \
  --max-attempts 100

# Resume into a fresh baseline directory; existing baselines remain immutable.
.venv/bin/python main.py http://localhost:3000 --discover-only \
  --baseline-dir runs/discovery-resumed --resume-crawl runs/crawl.json \
  --max-attempts 500 --retry-crawl-failures
```

Resume requires the same target, entry path, credentials, and exploration policy.
Attempt, action, and time budgets apply to each resumed session; the state limit
applies to the accumulated map. Ctrl-C and CLI termination preserve the active
task for retry. `--retry-crawl-failures` additionally requeues failed actions and
resets restoration suppression. It does not retry controls excluded by policy.

Restoration first verifies the current state, then tries saved browser data at
the recorded URL. If navigation changes that data or the controls, it replays
the actual action recipe from the entry, including removal actions. Recipes
use observed transitions rather than inferred navigation. Browser checkpoints
do not roll back server databases or external side effects; those applications
still need a reproducible server fixture. Contexts restored by the built-in
checkpoint mechanism use default browser options.

Discovery tries individual offered dropdown values, radio choices, and both
checkbox states. Selected values contribute to state identity, allowing a
following Continue action to explore the corresponding branch. Repeated choices
are grouped by structural state and control; arbitrary text values and the full
Cartesian product of controls are not exhaustively generated. `--no-input-choices`
disables these branches and reports their coverage gap. Custom widgets,
multi-select combinations, and uninspected same-origin frames remain explicit
gaps. An exhausted queue alone does not imply a closed graph.

Progress messages show states, queued tasks, attempts, and time spent restoring
versus acting. Saved metrics distinguish walked transitions, inferred navigation,
failed restoration, suppressed actions, and unsupported controls. To combine
maps without counting shared states/edges twice:

```bash
.venv/bin/python -m discovery.coverage runs/admin/graph.json \
  runs/customer/graph.json --out runs/combined-graph.json
```

Maps must share a target and identity policy. Pending counts remain per-source
data; shared queues cannot be deduplicated from graph files alone.

### Outcome and readiness configuration

Select/check actions now honor the runner's settle interval. Saved path steps
can also carry `ready` assertions; `--crawl-readiness readiness.json` applies a
JSON array of UI assertions after discovery transitions. Use an application
readiness signal when asynchronous updates can outlast a fixed delay.

The additional `STOCK_LIMIT` and `LINE_TOTAL_CONSISTENCY` rules require explicit
bindings. `numeric_value` reads a single input/select value or displayed amount;
`numeric_values` reads a set of these, enabling `product(line_factors) == line_total`.
This avoids parsing all the option labels as a selected quantity. Numeric label
parsing now distinguishes `Subtotal (1 items)` from accounting amounts such as
`($600)`.

Contracts can provide `reload_checks` and `reload_ready` to verify persistence
after reload. For removal, a check such as `remaining_items == 0` bound to the
cart rows is recorded separately as `RELOAD/<check-id>`; missing evidence remains
inconclusive. Reload changes the page used by subsequent cleanup, so cleanup
selectors must also work there.

Runtime evidence retains the phase, URL, and message. A reviewed
`runtime_error_rules` entry can mark a specific pattern as record-only with
explicit `phases` and a required `reason`. No PayPal/SDK errors are automatically
ignored. Cleanup errors are recorded separately from the default runtime check.
Evaluator version `relations-3` prevents comparisons with results produced under
the previous runtime policy from being called product regressions.

Run the deterministic browser benchmark without an LLM:

```bash
make test
make regression-benchmark BENCHMARK_OUTPUT=runs/my-regression-check
```

Choose a fresh output directory because baselines are immutable. The benchmark
records a reviewed local fixture suite, reloads it from disk, and replays five
cases against working and deliberately broken builds: quantities, NaN amounts,
payment selection, anonymous runtime errors, and rendered error messages.
Reports group findings by test and include execution health, observations,
screenshots, and Playwright traces. CI runs the suite and uploads benchmark
evidence. Fixture checks validate the engine; application coverage still depends
on the flows and expectations recorded for that application.

### What made flows work

Four things the page-shaped MVP did not have:

- **Capability, not path.** The model names the start and end states; the
  graph computes the walk breadth-first. A model asked to invent a six-step
  checkout produces something that reads well and cannot be walked.
- **Verified arrival.** Every walk ends with a fingerprint check against the
  state the graph predicted, recorded as an observation. A flow that could
  not be completed comes back *inconclusive* rather than looking like a
  defect — which is exactly the `make edit` failure, now explicit.
- **Relations over collections.** `committed == final` cannot express "the
  total equals the sum of the lines" or "the rows are in order", so the
  relation language grew named functions (`sum`, `count`, `sorted_asc`,
  `unique`) and comparison operators.
- **Probes.** A goal condition about something *not existing* is unreadable
  from the page you land on. The probe goes back and asks for the thing that
  should be gone, and counts a redirect away as absence.

### Verified against the two builds

`make baseline` then `make regression` records the sound build and replays it
against the broken one. All four flow-level defects are caught, the fifth
flow (which has no planted defect) holds, and replaying against the *sound*
build reports no regressions:

| invariant | evidence on the broken build |
|---|---|
| TOTAL_CONSISTENCY | `line_totals=[249, 29, 0]` sums to 278, `order_total=249` — the protection plan is listed and not charged |
| DELETION_COMPLETENESS | probe of the deleted resource returned 200, not 404 |
| COLLECTION_COUNT_AGREEMENT | `row_count=2`, `stated_count=3` — the page disagrees with itself |
| SESSION_TERMINATION | `/broken/account` still served after signing out |
| CREATION_VISIBILITY | holds — correctly reports no defect |


## The demo application

Flow discovery needs flows to discover, so the demo is now a small but whole app
rather than three forms. `make run` serves it; `make run OPEN=1` also opens it.

```bash
/                    overview
/login  /account     sign in, contact details, sign out
/projects            list -> new -> detail -> edit -> delete
/catalog             search, category filter, sort
/cart                cart -> contact -> [upsell] -> shipping -> payment -> confirmation
/profile             the original persistence page
/project-settings    the same invariant in different words
```

The checkout is the one that makes the argument. The upsell step only appears
when the subtotal clears $150, so two runs of the *correct* application take two
different paths:

```bash
Cart -> Contact -> Shipping -> Payment -> Confirmation
Cart -> Contact -> Upsell -> Shipping -> Payment -> Confirmation
```

Both are valid. Nothing about the path is the specification — what has to hold is
that the confirmation lists what was in the cart and the total equals the sum of
the lines it shows.

### Two builds

The whole app is mounted twice from the same code:

```bash
/            sound
/broken/...  same app, one behaviour changed per flow
```

Same templates, same wording, same markup — so finding the defect is a testing
problem and not a reading-comprehension one. What is planted where:

| flow | defect |
|---|---|
| profile | Bio is never written; the page still says "Saved successfully" |
| account | sign out shows the confirmation but never clears the session |
| projects | delete is a soft delete: row disappears, resource still reachable, still counted |
| catalog | the price sort orders the rendered string, so $1,299.00 sorts before $89.00 |
| checkout | the protection plan is listed on the confirmation but left out of the total |

Only the first is a persistence bug. The other four are the kind the current MVP
structurally cannot see: they are conditions about a *flow's outcome* — something
no longer existing, an order between rows, a sum across lines — not about one
field surviving a reload.

### What the page pipeline does with it

`make demo` still works exactly as before — it finds the dropped Bio on a
single page. `make edit` points the same pipeline at the project edit form and
comes back **inconclusive**: the commit navigates to the detail page, so when the
runner reloads and goes looking for the field it was watching, the field isn't
there. Nothing is wrong with the application. The test is shaped like a page and
the behaviour is shaped like a flow.

That was the gap. `make baseline` and `make regression` close it — the flow
runner verifies arrival against the state the graph predicted, so a walk that
ends somewhere unexpected says so instead of quietly reading the wrong page.
`make edit` is kept as the live demonstration of the original problem.

### Ground truth

`make truth` (or `/__truth`) prints, per flow, the capability, the goal
conditions, the invariants, the paths a run may take, and the planted defect.
It is there so flow discovery can be scored against something instead of
eyeballed. Nothing in the app reads it, and no page links to it.

State is per browser session (a cookie) and the two builds keep separate copies,
so two scans never collide, and `POST /reset` puts a session back to the seed.
## Model budgets and resumable generation

Every CLI model stage shares a durable budget: classification, flow expectation
generation, optional page classification, and report interpretation. Defaults:

| Limit | Default |
| --- | --- |
| Attempted requests | 5 |
| Total token reservations | 100,000 |
| Input reservation per request | 30,000 |
| Maximum output tokens per request | 6,000 |
| SDK automatic retries | Disabled |

Use `--max-model-calls`, `--max-model-tokens`, `--max-model-input-tokens`,
`--max-model-output-tokens`, and `--model-journal` to configure these limits.
Input reservations use UTF-8 bytes of the request text/schema plus framing
allowance, rather than an exact provider tokenizer. Each attempt reserves that
input allowance plus its maximum output tokens. Reservations remain charged,
including failures and interrupted requests. They are conservative spending
controls, not dollar quotes or exact reported token usage. Provider-reported
usage is retained in the journal when available. Oversized inputs are refused
before a request is sent.

The default journal is `RUNS_DIR/model-budget.sqlite3`. Responses are saved
immediately and reused for exact matching requests on resume. SQLite transactions
prevent two concurrent processes sharing a journal from exceeding its request
limit. Unknown outcomes after interruptions are blocked instead of retried.
Even invalid model output is cached; it does not trigger another paid attempt.
Built-in OpenAI and Anthropic adapters disable SDK retries and pass output-token
caps to their APIs. Custom registered adapters must honor their `max_tokens`
setting and disable any internal retries to provide the same guarantees.

For a bounded baseline batch:

```sh
.venv/bin/python main.py --baseline http://localhost:3000 \
  --baseline-dir runs/baselines/batch-1 --runs-dir runs/amazon-batches \
  --model-journal runs/amazon-batches/calls.sqlite3 \
  --max-model-calls 5 --max-model-tokens 100000 \
  --save-flow-candidates runs/amazon-batches/flows.json
```

Flow classification consumes one request unless supplied from a file. The
remaining allowance is shared by expectation generation and other enabled model
stages. Each completed flow is written to `RUNS_DIR/expectation-progress.json`.
Budget-limited flows appear as explicit coverage gaps; an optional interpretation
skipped for budget reasons leaves the deterministic regression report intact.
A summary is written to `RUNS_DIR/model-budget-summary.json`.

To continue, reuse the **same journal**, provider/model, prompts, output cap, and
discovery evidence, and explicitly raise cumulative limits (for example,
`--max-model-calls 10 --max-model-tokens 200000`). Record into a new baseline
directory if the previous batch already published a baseline. Saved
classifications can be supplied with `--flow-candidates` or `--identified-flows`.
Completed requests come from the journal, and only missing requests use the new
allowance. Evidence or prompt changes create different requests and count again.
Changing journals starts a separate budget; it is not a way to resume the old
allowance. A failed/interrupted exact request requires deliberate investigation
and a new journal before it can be attempted again.

`--max-model-calls 0` prevents new requests while permitting cached ones. Saved
baseline replay with `--no-interpret` requires neither a model request nor a
model journal. Discovery-only runs also make no model calls.

## Behavior contracts and evidence-based regression

Flow baselines now generate intended behaviors with an AI model **after** flow
classification. Classification itself is unchanged. The model receives each
identified flow, its observed paths, controls, readable values, and observation
timeline. It proposes evidence-backed expected outcomes and declarative checks,
including comparisons across steps, rather than treating the observed behavior
as the expected result. For example, a quantity selected before adding an item
should match the quantity displayed in the cart.

Selectors, states, observation timing, operand names, and expressions are
validated before execution. Readiness values are checked without coercion:
`visible`, `checked`, and `enabled` require booleans; `count` requires a
nonnegative integer; `text` and `value` require strings. Generated relations
must compare compatible extractor representations. Numeric reductions require
compatible inputs, and ordered comparisons require numbers. Malformed checks
in a saved expectation suite become coverage gaps while valid checks remain
usable; their values are never silently corrected. Unsupported or uncertain expectations become explicit
coverage gaps. Executed expectations remain **inferred** until reviewed; an AI
confidence score does not establish the application's business requirements.
The browser runner collects values at the specified steps, evaluates the frozen
relation, and records observation evidence and errors.

Use `--save-expectations runs/expectations.json` when creating a baseline to
export the generated suite. The baseline also stores `expectations.json` and
freezes each test's expectation, evidence, model, prompt version, observation
schedule, and discovery hash. Replay uses these saved expectations without
another generation call. Use `--expectations runs/expectations.json` to reuse a
suite with matching discovery evidence, or create a new baseline to regenerate.
Changed evidence requires explicit regeneration. `--expectation-confidence`
sets the execution threshold (default `0.7`). Generation failures stop the run;
there is no silent fallback to fixed rules. `--expectation-mode rules` explicitly
selects the previous rule-based implementation.

An upstream classifier can instead supply `--identified-flows flows.json`:

```json
{"flows": [{"id": "checkout", "name": "Checkout", "setup_state_id": "s_start",
  "goal_state_id": "s_confirmation", "evidence": "Observed checkout transitions"}]}
```

Use real discovery state IDs; paths must consist of observed actions. This input
adapter does not change classification. Existing `--flow-candidates` files also
work with AI expectation generation.

Export these proposals while recording an exploratory baseline:

```sh
.venv/bin/python main.py --baseline http://localhost:3000 \
  --exclude /broken --baseline-dir runs/baselines/exploratory \
  --save-flow-candidates runs/candidates.json \
  --save-expectations runs/expectations.json \
  --export-contracts runs/contracts.json
```

Review `runs/contracts.json`. Each key identifies a generated flow test. Confirm
its capability, relation, selectors, assumptions, and resource semantics. For a
confirmed contract, set `review_status` to `confirmed`, supply `reviewed_by`,
`requirement_source`, and `fixture_version`, and define observable fixture
`preconditions`. Confirming a contract is an assertion by the reviewer that its
expectations and fixture checks match the application's requirements; a model
confidence score cannot confirm it. Then record a separate reviewed baseline:

```sh
.venv/bin/python main.py --baseline http://localhost:3000 \
  --exclude /broken --baseline-dir runs/baselines/reviewed-v1 \
  --flow-candidates runs/candidates.json --expectations runs/expectations.json \
  --contracts runs/contracts.json

.venv/bin/python main.py --regression http://localhost:3000/broken \
  --baseline-dir runs/baselines/reviewed-v1 --no-interpret
```

Contracts bind to generated test IDs and verify capability/invariant identity.
AI test IDs derive from the flow and expectation IDs. Recheck exported contracts after rediscovery;
contracts are not automatically transferred across changed suites. Overrides
are baseline-only: regression uses the saved contract and saved relation rather
than the current rule file. Ordinary fields and selectors remain fixed during
replay. A changed path is not silently repaired.

Application-specific fields include:

- `setup`: ordered `click`, `fill`, `select`, or `check` actions at the entry page.
- `preconditions`: fixture assertions after setup and before recorded navigation.
- `ready`: outcome assertions before goal fingerprinting and observation reads.
- `api_assertions`: same-origin, deployment-relative GET checks with bounded
  polling, optional JSON paths, expected values, and expected HTTP statuses.
  Redirects, server failures, invalid JSON, and invalid paths are inconclusive;
  readable outcome mismatches are violations. Requests share the flow's cookies.
  Omitting `expected` and `json_path` creates a status-only check.
- `cleanup` and `cleanup_assertions`: application cleanup followed by reload and
  verification. Non-session-scoped contracts require both. Failed cleanup stops
  subsequent tests. Every flow uses a fresh browser context, but closing it does
  not undo orders or other shared backend mutations; configure real cleanup.
- `ignored_observations`: observation names mapped to reasons for ignoring
  volatile values in comparison. These never bypass invariant evaluation.
- `branches`: explicitly recorded branch evidence, gaps, and recommendations.
  Unexplored branches produce `not_observed` suggestions, not defect claims.

The example in `examples/behavior-contracts.json` illustrates these fields for a
hypothetical checkout app; adapt its selectors and API to the actual target.
Setup and cleanup support declarative UI actions, not arbitrary Python hooks.
Fixture preconditions are assertions of known starting data, not an automatic
fixture generator. Authentication setup must be included for isolated flows.

### Reading the results

| Finding direction | Meaning |
|---|---|
| `regressed` | A reviewed expectation held before and now fails under a matching contract, with fixture checks completed in both runs |
| `suspected` | An inferred/legacy expectation fails, or comparable starting evidence is insufficient for confirmation |
| `existing_violation` | The expectation fails in both builds |
| `execution_failure` | Missing test, failed navigation, unavailable verification, cleanup failure, or an inconclusive outcome |
| `changed` | Different contract, observation, route, or control; review required |
| `coverage_gap` | Incomplete crawl, unsupported discovered checks, or newly discovered routes |

Reports include current test records, step/API evidence, cleanup status, crawl
coverage, and a separate `suggestions` list. Checkout proposals flag untested
payment-failure recovery as a coverage/improvement opportunity. An optional
upsell step is acceptable when a recorded path reaches a valid outcome; replay
still requires a usable saved path. UI comparison is DOM/control/count based,
not pixel comparison, and currently compares one representative state per route.

Regression exits `1` for confirmed or suspected regressions, `2` for execution
failures when no regression was found, and `0` otherwise. Always inspect coverage:
zero confirmed regressions is not a claim of whole-application correctness.
An empty behavioral suite is an execution failure where tests were wanted; a
baseline recorded with `--read-only` or `--no-flows` never asked for them, so
its empty suite is reported as a coverage gap and exits `0`. Schema 1.0
baselines do not record which they were and are read as wanting tests.
Schema 1.0 baselines remain readable but cannot retroactively establish
reviewed expectations.

Baselines are immutable directories, published together by atomic rename. Use a
new `--baseline-dir` for every version. Promotion requires both `--save-baseline`
and `--new-baseline-dir runs/baselines/v2`; the source baseline is preserved.
Storage remains JSON; no PostgreSQL service or migration is required.

Run deterministic unit, browser, and CLI integration tests without LLM credentials:

```sh
.venv/bin/python -m unittest discover -s tests -v
```
