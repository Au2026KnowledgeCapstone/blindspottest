---
layout: default
title: Knowledge
---

# Knowledge

Decides whether what an application did was *correct*. Discovery answers "what
can this app do" and semantic answers "what does this region appear to be";
this package is the only one that declares a defect, and it declares one by
evaluating a relation it owns against values a runner observed.

The architectural commitment, stated in both engines' docstrings and worth
repeating because every design decision below follows from it:

> **No invariant is expressed in Python.** There is no
> `if capability == "delete_resource":` anywhere in this package.

What Python supplies is a *vocabulary* — named predicates, named steps, named
relation functions. The JSON in `invariants/` composes them. Adding an
invariant is a data change; adding a new primitive is a code change. That
split is the point: the behaviour under test is declared, auditable and
diffable rather than buried in control flow, and **it does not come from the
language model**. The model only says "this interaction looks like persistent
state" or "this region looks like a deletion". Everything about what that
means and how it is checked lives here.

## Where it sits

```
  discovery        AppGraph + value surface          (no judgment)
       │
       v
  semantic         capability candidates             (LLM)
       │              capability name
       │              setup / goal state ids
       │              binding selectors, probe
       v
  ═══════════════ package boundary ═══════════════
       │
  knowledge        capability -> which invariants apply
       │           candidate  -> is one applicable       <-- the gate
       │           invariant  -> what the runner must do
       │
       v
  runner           walks it, observes values            (no LLM)
       │
       v
  knowledge        observations -> verdict              <-- the only defect
       │
       v
  baseline / regression / reporting
```

Nothing in this package talks to a browser and nothing in it calls an LLM.
It turns candidates into executable, serializable instances and turns
observations into verdicts. That is what makes an instance recordable to
disk and replayable verbatim against a later build — the property the whole
regression pipeline rests on.

## Files

| file | what it is |
|---|---|
| `engine.py` | page-level rule base interpreter, the `Relation` language, `decide()` |
| `flows.py` | flow-level rule base interpreter, instances, `evaluate_instance` |
| `invariants/persistence.json` | 2 page invariants — `PERSISTENCE_001`, `_002` |
| `invariants/flows.json` | 7 flow invariants |
| `capabilities.json` + `.py` | what each capability *is*, for the prompt and the gap report |
| `capability_models.json` + `contracts.py` | reviewed expectations, assumptions, branch evidence |

Both rule bases live in one directory and are selected by **declared `kind`,
not filename**: `FlowEngine.load` takes only files with `"kind": "flow"`
(`flows.py:580`), the page engine takes only `kind != "flow"`, defaulting
absent to `"page"`. So each loader skips the other's rule base instead of
choking on a perfectly valid rule it does not understand.

## The four columns

Both engines are built on the identical mapping. Read this table first; the
rest of the document is its consequences.

| step | question | declared in JSON as | resolved against |
|---|---|---|---|
| 1 | which invariants does this classification bring? | `applies_to` | nothing — a data lookup |
| 2 | is this invariant applicable to *this* candidate? | `requirements` | `REQUIREMENT_PREDICATES` `engine.py:82` · `FLOW_REQUIREMENT_PREDICATES` `flows.py:174` |
| 3 | what must the runner do? | `procedure`, `teardown` | `STEP_RECORDS` `engine.py:99` · `FLOW_STEP_RECORDS` `flows.py:196` |
| 4 | did it hold? | `expected_relation`, `precondition` | `RELATION_FUNCTIONS` `engine.py:209` |

Step 3 is subtler than it looks. The step tables do not describe behaviour —
the runner implements that. They map each step name to **the observations it
produces** (`"walk_goal": ("goal_reached",)`,
`"probe": ("probe_reachable", "probe_status", "probe_url")`). The engine needs
exactly that much to verify that a procedure actually produces the values its
relation refers to, which it checks at load time.

### The vocabularies, as they stand

| vocabulary | page | flow |
|---|---|---|
| requirement predicates | 4 | 5 |
| procedure steps | 10 | 6 |
| relation functions | 6 (shared) | |
| invariants | 2 | 7 |

The two requirement dictionaries are deliberately **not merged**. A page-level
requirement asks its question of a snapshot, a flow-level one asks it of an
`AppGraph`; the signatures differ, so merging them would mean a predicate that
cannot state what it needs.

## The relation language

Deliberately tiny. `_RELATION_RE` (`engine.py:122`) accepts exactly
`<operand> <op> <operand>`, with `==  !=  <=  >=  <  >`. An operand is a
literal (`true` / `false` / `null`), the name of an observation, or a
**one-argument** call on a single observation — `_CALL_RE` (`engine.py:129`)
permits no spaces and no second argument.

```
committed_value == final_value      scalar equality
sum(line_totals) == order_total     a reduction over a collection
sorted_asc(row_values) == true      an order predicate
probe_reachable == false            a probe's answer
row_count == stated_count           a page agreeing with itself
```

Six functions: `sum`, `count`, `len`, `sorted_asc`, `sorted_desc`, `unique`.

The limit is intentional, in the words of the comment that imposes it: "this
is a relation language, not an expression language, and anything needing more
belongs in a named step that records its answer as an observation." Complexity
is pushed into the step vocabulary, where it is named and auditable, rather
than accumulating as expressions in JSON.

Three behaviours worth knowing before writing a relation:

- **`_FLOAT_TOLERANCE = 1e-6`** (`engine.py:222`). Money arrives as a float
  parsed out of rendered text, so `12.80 + 7.20` is not reliably `20.00`.
  Without the tolerance, `sum(line_totals) == order_total` would report a
  violation on representation error alone — in the one invariant most likely
  to matter.
- **`sorted_asc` is non-strict** (`a <= b`). Two products at the same price
  are not a sort defect, and strictness would report one every time a catalog
  holds a duplicate value.
- **`RelationError` means inconclusive, never violated.** A well-formed
  relation meeting data it cannot reduce — a `sum` over a list holding `None`
  because one row failed to parse — is an unreadable observation, not a broken
  application.

## What a malformed rule base is refused for

This is what makes a data-driven rule base safe to extend. Every one of these
raises `InvariantError` **at load time, never mid-run**:

- a `requirements` name not in the predicate registry (the error lists the
  known names)
- a `procedure` or `teardown` step not in the step table
- a relation function not in `RELATION_FUNCTIONS`, or a call with spaces or a
  second argument
- **an operand nothing supplies** — every relation operand must be recorded by
  some step in *this* procedure or declared as a `binding`. An operand from
  neither can only ever be inconclusive.
- **a declared binding no relation reads** — dead weight in the contract, and
  usually a renamed operand
- `bindings` declared but `observe_bindings` absent from the procedure
- `probe` ordered before `observe_bindings` — a probe navigates away, so
  bindings read afterwards would read the probe's page. The comment flags this
  as "a correctness requirement, not a style preference, and it is invisible
  in the results when it is wrong — the values parse, they are simply the
  wrong page's."

**What this cannot catch** is the one that bit us: *"this relation presumes a
condition that nothing in its procedure establishes."* That is a semantic
relationship between `procedure` and `expected_relation` which the schema has
no way to express. See "Known holes" below.

## How a verdict is reached

Three statuses — `HOLDS`, `VIOLATED`, `INCONCLUSIVE` — and one `decide()`
(`engine.py:721`) shared by both engines, so the logic that turns a relation
into a verdict exists exactly once. The two levels differ in what they observe
and how they phrase a failure, never in how a relation becomes a verdict.

**A defect is declared on exactly one line**, `engine.py:827`, reached only
when the relation evaluates false *and* the precondition is confirmed met.
`Verdict`'s docstring states the claim: "This is the only place a defect is
declared, and it is declared by the rule base's relation — not by the language
model, which never sees the run."

Everything else routes away from "defect":

| situation | verdict |
|---|---|
| relation cannot be evaluated (`RelationError`) | inconclusive |
| an operand missing from the run | inconclusive |
| relation **held**, precondition unconfirmed | **holds**, with a note |
| relation **failed**, precondition unconfirmed or unobserved | inconclusive |
| relation failed, precondition met | **violated** |

The third row is a deliberate ordering decision and easy to get backwards: the
relation is checked *before* the precondition. A precondition exists to stop us
blaming the application for a commit we never confirmed — it must not discard
a **pass**. If the expected relation held, the invariant held whether or not a
confirmation appeared; demoting that to inconclusive would report a correctly
working application as untested.

One exception bypasses `decide()`: `flows.py:804` returns `VIOLATED` directly
when a contract's API assertion fails (`api_outcome is False`), without a
relation.

## The candidate contract — the seam with `semantic/`

An invariant does not merely apply to a capability; it **demands things of the
candidate** the classifier produced. Three kinds:

| demand | declared as | checked by |
|---|---|---|
| named observations, read by selector | `bindings` | `missing_bindings` `flows.py:332` |
| a reachability probe | `probe_declared` in `requirements` | `unmet_requirements` `flows.py:325` |
| conditions on the walk itself | other `requirements` | same |

A candidate failing any of them produces a `FlowSkipped` carrying the unmet
names, so "nothing was tested here" is never silent. That half works well.

**What does not work well is keeping the two sides in agreement.** The
classifier's system prompt (`semantic/flow_classifier.py`) holds a
hand-maintained prose recipe list — which bindings to supply per capability,
which probe mode to use, and `anything else : null`. That list restates
information `flows.json` **already declares**: `bindings` names what must be
supplied, and `probe_declared` says a probe is required.

One source of truth, duplicated into a hand-written string. It drifted the
first time it was tested: `AUTHENTICATION_GRANTS_ACCESS` was added requiring a
probe while the prompt still said `null` for `authenticate`, so the invariant
shipped as a permanent skip and the gap was only visible in a recorded run's
coverage block. Both files were internally consistent. Nothing checks them
against each other.

This is the layer's main open design question, and the precedent for solving
it is already in the tree: `capabilities.json` exists because criteria the
classifier is held to and gap text the report explains itself with must not be
allowed to drift, so there is only one of them. The analogous move here is to
**derive the prompt's binding and probe section from the rule base**, leaving
only genuinely app-shaped guidance ("prefer a `tr:not(.grand)` form for lines")
as prose. The cost is that `flows.json` grows a field describing how each
binding is read, and the prompt becomes less hand-tunable.

## Current coverage

Nine flow invariants' worth of capability vocabulary, seven invariants:

| capability | invariants |
|---|---|
| `authenticate` | `AUTHENTICATION_GRANTS_ACCESS` |
| `session_termination` | `SESSION_TERMINATION` |
| `create_resource` | `CREATION_VISIBILITY` |
| `delete_resource` | `DELETION_COMPLETENESS`, `COLLECTION_COUNT_AGREEMENT` |
| `filter_collection` | `COLLECTION_COUNT_AGREEMENT` |
| `sort_collection` | `ORDERING_CONSISTENCY` |
| `multi_step_transaction` | `TOTAL_CONSISTENCY` |
| `update_resource` | — reported as a named gap |
| `search` | — reported as a named gap |

`capabilities.json`'s `supported` flag must track this table in both
directions; `tests/test_capabilities.py` asserts it, because a capability
marked supported with no invariant silently drops candidates and the reverse
hides a test that would have run.

`update_resource` is a real gap: the page-level persistence invariants cover
the single-page version of the same idea but cannot follow a commit that
navigates. `search` is honestly uncoverable — relevance is not mechanically
checkable the way a count or an ordering is, and its definition redirects the
classifier to `filter_collection` where a stated count exists.

### Coverage is thinner than the table implies

`demo_app/manifest.py` is ground truth for what the demo application's flows
must do. Eight of its seventeen declared goal conditions have no invariant,
**including on capabilities that report as supported**:

| flow | uncovered condition |
|---|---|
| `account.authenticate` | invalid credentials do *not* grant access |
| `projects.create` | it appears in the collection |
| | its fields hold what was submitted |
| `projects.delete` | gone from the collection |
| `catalog.query` | every row matches the active query and filters |
| | clearing the filters returns the full collection |
| `checkout.purchase` | the confirmation lists the cart's items at the cart's quantities |
| | an order number is issued |

Two are worth calling out. `filter_collection` reports as supported while
asserting only that a count agrees with itself — an application returning
entirely wrong rows with an honest count passes. `create_resource` asserts only
that the URL serves *something* — a create that silently drops every submitted
field passes.

Four of the eight need a relation over **two** operands
(`contains_all(row_values, query_term)`), which the single-argument call syntax
cannot express. Two need an observation taken **before** the goal walk, which
the step vocabulary has no way to record: `observe_bindings` runs once, after,
and `bindings` is a flat tuple with no phase.

## Known holes

1. **Relations may presume conditions no gate checks.** The validator proves
   operand *coverage*, not that the procedure *establishes* what the relation
   presumes. `ORDERING_CONSISTENCY` once required only `goal_reachable` and
   `goal_differs_from_setup`, so a candidate whose walk was a search filter
   satisfied both; the invariant asserted `sorted_asc` over a correctly
   name-ascending catalog while the binding read the price column, and a
   correct application was reported as violating an invariant it was never
   given the chance to satisfy. `sort_established` and `credentials_submitted`
   close that shape for two invariants. The same hole remains in
   `COLLECTION_COUNT_AGREEMENT` (an unfiltered table agrees with its own
   count) and probably `TOTAL_CONSISTENCY` (`sum([]) == 0` against an
   empty-cart total) — where it reports as a **vacuous pass** rather than a
   false violation, which is harder to notice and inflates the coverage count.
2. **Requirements are static; establish-conditions may need to be
   observational.** `requirements` answers from the graph before a browser
   opens, which is cheap and skips without spending a run. `precondition` is a
   relation over observations, which is truthful about what actually happened.
   Every invariant currently carries the same `goal_reached == true`
   precondition and nothing more, so that mechanism is effectively unused —
   it reads as having been built for exactly this and never applied. Which of
   the two should carry establish-conditions is undecided.
3. **The prompt duplicates the candidate contract.** See the seam section.
4. **The two predicates added so far make the weaker claim.**
   `sort_established` does not check that the bound column is the sorted one;
   `credentials_submitted` does not check the resource was unreachable before
   the sign-in. Both stronger claims need the before-the-walk observation from
   hole 1's list.

## Adding to this layer

**An invariant — data only.** Add an entry to the right `invariants/*.json`,
composing names that already exist. Load-time validation will refuse it if the
composition is inconsistent. Then update `capabilities.json`'s `supported`
flag if the capability had no invariant before, or `tests/test_capabilities.py`
will fail — by design.

**A primitive — code, then data.** A new requirement predicate, procedure step
or relation function is a function plus one registry entry, after which the
JSON may use its name. Check first whether the thing belongs in the relation
language at all: if it needs two operands or more than one observation, the
house style says it belongs in a named step that records its answer.

**Worth reading before either:** `tests/test_flow_requirements.py` documents
why `sort_established` is not a URL check, which is the kind of decision that
looks like an oversight until someone re-derives the false positive it avoids.
