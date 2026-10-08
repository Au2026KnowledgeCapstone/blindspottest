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
regression  diff verdicts, observations, structure, UI   (deterministic)
regression  explain what the diff means                  (LLM)
reporting   regressions first, then fixes, then the rest
```

The replay is the load-bearing part. An instance has every value it needs
fixed before it executes — the mutation string, the walk, the selectors — so
both runs assert exactly the same thing and a difference in the answers is a
difference in the application. Re-classifying on each run would compare two
different test suites and blame the code. It is also why a regression run
calls no LLM for classification and is cheap enough to run per build.

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