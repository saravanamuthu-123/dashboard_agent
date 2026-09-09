# Design

The production system: a dashboard agent serving a few hundred teams, each seeing
only their own advertising data.

The MVP in this repo is one slice of it — single tenant, one dataset, a CLI. Where
the two differ, I say so.

---

## 1. The request path

```
                         ┌──────────────── tenant boundary ────────────────┐
  question ──▶ API ──▶   │  resolve tenant → schema catalogue              │
                         │       │                                         │
                         │       ▼                                         │
                         │   plan cache ──hit──────────────┐               │
                         │       │ miss                    │               │
                         │       ▼                         │               │
                         │  ▸ PLAN (LLM)  strict schema    │               │
                         │       │  sees: column names,    │               │
                         │       │  no values, no tenant   │               │
                         │       ▼                         ▼               │
                         │   VALIDATE ──reject──▶ retry once ──▶ refuse    │
                         │       │                                         │
                         │       ▼                                         │
                         │   SQL BUILD  (closed enums only)                │
                         │       │                                         │
                         │       ▼                                         │
                         │   EXECUTE   warehouse, tenant-scoped role       │
                         │       │                                         │
                         │       ├──▶ result cache                         │
                         │       ▼                                         │
                         │   CHART  (type from result shape)               │
                         │       │                                         │
                         │       ▼                                         │
                         │  ▸ NARRATE (LLM)  sees the computed table       │
                         └───────┼─────────────────────────────────────────┘
                                 ▼
                    answer + chart + the SQL + data caveats
```

▸ marks the only two places a model is involved.

**Where the LLM is, and what it sees.** The planner sees the question and a
description of the tenant's schema — column *names*, never values. The narrator
sees a computed result table, typically under twenty rows. Neither sees the
underlying data. Neither does arithmetic.

**Where I deliberately kept it out.** Three places, and the third is the one I
care about most:

- *Aggregation.* A warehouse has been correct at summing numbers for fifty years.
  There is no version of "have the model total the column" that is better.
- *Date resolution.* The model says `last_n_weeks: 8`; code turns that into two
  dates. Date arithmetic is arithmetic. It also lets "today" mean *the last day of
  loaded data*, not the wall clock — otherwise "the last eight weeks" silently
  includes days that have not arrived, and the answer comes back low.
- *Currency.* Every figure is converted to a reporting currency in the curated
  view, before any generated query runs. The model is never told what an exchange
  rate is. This is not a careful instruction that could be ignored; the concept
  does not exist at the layer the model operates on.

That last one generalises into the rule in §2.

**Why no agent framework.** There is no tool selection here, no branching a model
must reason about, no multi-step loop. The shape of the work is known in advance.
A framework would add indirection and take away the ability to say precisely
where the model sits — which is the property the rest of this document depends on.
I write agent loops in production and reach for a DAG engine when the graph is
genuinely dynamic. Here it is not. The only loop is a bounded plan retry.

**Why SQL rather than a dataframe layer.** The brief requires that whatever
produced a number be visible. A SQL string is data — print it beside the result
and a reviewer can re-run it. A dataframe pipeline is code; showing it means
writing a pipeline pretty-printer that can drift from what actually executed. SQL
also transfers: the MVP runs DuckDB over CSVs, production runs the same generated
SQL against Postgres or Snowflake.

---

## 2. Fixed versus decided

**The rule: anything that changes a number is fixed in code. The model chooses
which question to answer; it never participates in answering it.**

| The model decides | Code owns |
|---|---|
| intent (5 values) | what each intent computes |
| metric (9 values) | the SQL expression, and the zero-denominator guard |
| grouping (6 values) | which column that maps to |
| period, relatively | the dates it resolves to |
| whether to refuse | whether a refusal is justified |

Everything the model emits is drawn from a closed enum. It cannot name a column
that was renamed last week, because the only column names it can produce are ones
that exist.

### Why that line and not another

Two properties decide it.

**Does a wrong choice here produce a wrong number, or an unhelpful one?** Picking
`spend` when the user meant `revenue` gives an unhelpful answer — visibly about
the wrong thing, and the user asks again. Getting the currency conversion wrong
gives a *wrong* number that looks right, gets pasted into a deck, and is
discovered a quarter later. The first is safe to delegate. The second is not.

**Is there a defensible fixed answer?** Where a reasonable default exists, hard-code
it and let the user override. Where the answer genuinely depends on wording —
"last quarter", "which campaign is underperforming" — the model is the right tool,
because that is language interpretation, not computation.

### Two examples where the line moved

Both came from running against a live model, not from planning.

*The model chose "most recent day" for an anomaly question.* A fair reading of
"what happened yesterday" — and useless, because one data point has nothing to
compare against. Code now enforces a 21-day minimum window. The reasoning:
**how much history it takes to see a change is a property of the method, not of
the question.**

*The model returned one row for "which campaign made the most revenue".*
Literally correct. But the runner-up is what tells you whether the winner won by
a mile or a hair, and a chart with one bar says nothing. Code sets a floor of
five.

Both are the model making a defensible choice that produces a worse answer, and
both fixes narrow what it is allowed to choose. I expect this line to keep moving
in that direction as more question shapes arrive.

### The alternative I rejected

**Text-to-SQL** — let the model write the query directly. It is more flexible and
it is what most implementations do. I rejected it because of the failure mode, not
the capability. A hallucinated *plan* fails validation and is caught. A
hallucinated *query* is syntactically valid SQL that runs, returns a plausible
number, and is wrong — a mis-scoped date range or a forgotten `WHERE tenant_id`
does not raise. It also removes the isolation guarantee in §4: I cannot promise
tenant scoping if the model writes the `FROM` clause.

The cost of my choice is real: the agent answers a bounded set of question
shapes, and anything outside it is refused rather than attempted. That is the
right trade at this stage, and §8 says when I would revisit it.

---

## 3. Trust

*A user asks for last quarter's ROAS and gets a number. What stands between the
model and that number?*

Five things, in order.

**1. The model never produces the number.** It produces a plan. The number comes
from SQL that code generated. This is the whole architecture, and everything else
is secondary to it.

**2. Strict Structured Outputs.** Decoding is constrained against the plan
schema, so the plan cannot be malformed, cannot contain an unknown field, and
cannot hold an enum value outside the allowed set. Verified working on the Azure
deployment in use. This removes the *syntactic* failures.

**3. The validator.** Structured outputs guarantee shape, not truth. A perfectly
valid plan can still describe the wrong analysis. The validator catches what the
schema cannot: ranking with nothing to rank, a period reaching outside the loaded
data, a ratio metric with no denominator guard. It re-checks the enums too —
redundant while strict mode holds, and the only defence on any fallback path.

**4. The curated view.** Every defect is handled once, before any query runs.
Duplicates removed; both date formats parsed explicitly rather than guessed;
FX deduplicated and forward-filled across weekends; orphan rows kept through a
`LEFT JOIN` rather than silently dropped by an inner one. An inner join is a
silent `DELETE` on anything that fails to match, and it is the most common way a
total comes out quietly low.

**5. Caveats travel with the answer.** Not all of them — only the ones bearing on
this result. Nine notes on every answer trains people to ignore all nine. Ask for
spend and you are told the FX rate was carried forward across 642 weekend rows.
Ask for revenue and you are told 23 rows have no revenue figure and were excluded
rather than counted as zero.

### How would I find out it was wrong anyway?

Being honest: **the hardest failure to detect is a correct query answering a
question the user did not ask.** No amount of validation catches that, because
nothing is wrong with the computation.

Four mechanisms, weakest to strongest:

**Show the query.** Every answer carries the SQL that produced it. This makes a
wrong answer *checkable* by anyone who reads it. It does not make it *caught* —
most users will not read SQL — but it converts a black box into something a
sceptical analyst can audit in ten seconds, and those are the users who report
problems.

**Invariant checks on every result, cheap and automatic.** Do component figures
sum to the total? Is any ROAS above 50 or below zero? Did a period return fewer
rows per day than its neighbours? Each is a few milliseconds of SQL, and each
catches a class of error that is otherwise invisible. In this data the last-day
check is what turns "conversions collapsed" into "the extract is incomplete".

**A golden set with a known answer.** Roughly fifty questions whose correct
answer is fixed and independently derived. Run on every deploy. This is what the
21 tests in this repo are a small version of — and two of them assert that the
*naive* approach gives a different answer, so the fixture cannot lose its ability
to distinguish correct handling from broken.

**Feedback that names the failure.** Not a thumbs-down. "This number looks wrong"
attached to the plan, the SQL, the result and the model version — so a report is
a reproducible case rather than a sentiment. Every confirmed report becomes a
golden-set entry.

---

## 4. Isolation

Every tenant sees only their own data. The obvious mechanism: one schema per
tenant, a database role per tenant, connection acquired from a pool keyed by
tenant, `FROM` clause built by code from the resolved tenant. The model never
influences which schema is read.

That is table stakes. The interesting question is where it breaks anyway.

**The obvious ones**

- A missing `WHERE tenant_id` on a shared table. Mitigation: no shared fact
  tables. Separate schemas, so the isolation is enforced by the grant rather than
  by a predicate someone can forget.
- A connection returned to the pool with tenant context still set. Mitigation:
  reset on release, and assert the expected role on acquire.

**The non-obvious ones — where I would actually expect a leak**

- **Caches keyed on the question, not the tenant.** A plan cache keyed on question
  text alone will serve tenant B a plan built for tenant A's schema. Harmless
  until the schemas differ, then it is a cross-tenant read. Every cache key must
  begin with the tenant id, and I would enforce that in the cache client's type
  signature rather than by convention.

- **The LLM provider itself.** Every question crosses an organisational boundary
  to a third party, and questions are not neutral: *"why did the Acme renewal
  campaign underperform"* leaks a customer name, a business event, and a
  judgement. Result tables sent to the narrator leak actual figures. This is a
  real data-egress path that no amount of row-level security closes. Mitigations:
  send column names but never values to the planner; strip identifiers from
  narration input where the shape allows; use a zero-retention endpoint. For
  tenants with data-residency terms, move the *narration* call — the one that sees
  numbers — onto a self-hosted open-weight model, and leave planning on the hosted
  one, since a plan contains only the shape of a question and not its data.
  Splitting the two calls by what they can see is only possible because the
  architecture keeps them separate.

- **Error messages.** A constraint violation or a query timeout can carry a table
  name, a column, a row count, sometimes a value. Errors must be logged with full
  detail internally and returned to the user as an opaque reference id.

- **Generated artefacts on shared storage.** Chart PNGs and HTML reports are
  files. A predictable path — `/out/2026-09-04_spend.png` — is guessable across
  tenants. Signed URLs, tenant-prefixed keys, short expiry.

- **Timing and cost signals.** Response latency and token counts correlate with
  data volume. A shared multi-tenant rate limiter leaks activity levels between
  tenants. Minor, real, and the sort of thing that ends up in a security review.

- **The golden set and the feedback loop.** The most likely leak in practice.
  Confirmed bug reports become test cases, and a test case built from a real
  question contains real tenant data. Without a deliberate policy, tenant A's
  campaign names end up in a fixture that every engineer can read. Scrub on
  capture, not later.

---

## 5. Not knowing

A question is unanswerable in three distinct ways, and they deserve different
responses.

**The data does not contain it.** Competitor spend is the example in the brief.
No column holds it, and no ad platform publishes it — it is their most
confidential figure. `refuse` is a first-class intent in the plan schema, not an
error path. The planning prompt lists what the dataset holds and instructs the
model to refuse anything outside it. Deliberately no mention of competitors: a
rule generalises, a hardcoded exception covers only the case someone thought of.
The MVP refuses a January 2026 question correctly for the same reason, having
never been told about January.

**The period is not covered.** Distinct from the above: the metric exists, the
window does not. Partial overlap is *clamped and reported* — "you asked for last
year, the data begins 8 June, here is 8 June onward" — because answering a
narrower question silently is the dishonest option. No overlap at all is refused.

**The data is there but cannot support the claim.** The most interesting case,
and the one most systems get wrong. Question 3 looks like a performance collapse
and is an incomplete extract. A campaign with three conversions has a ROAS that is
arithmetically computable and statistically meaningless. Here the answer is not a
refusal but a *qualified* answer: give the number, state what it cannot support.
The pause recommendation does this by construction — it returns every campaign
with the reason it is or is not a candidate, so "this one has too little history
to judge" is visible rather than hidden behind a ranking.

What a refusal must contain: what was asked, what the dataset holds instead, and
what would be needed to answer it. "I don't know" is not a useful answer; "this
would need a third-party estimate like SimilarWeb, which we do not ingest" is.

---

## 6. Cost and latency

500 users × 20 questions/day ≈ **300,000 questions/month**.

Two calls each. Measured against `gpt-5.6-luna` on the Azure deployment in use:

| | input | output (incl. reasoning) |
|---|---:|---:|
| plan | ~900 | ~120 |
| narrate | ~600 | ~180 |
| **per question** | **~1,500** | **~300** |

At Luna's published rate of $0.20 / $1.20 per 1M tokens:

| | volume | cost |
|---|---:|---:|
| input | 450M | $90 |
| output | 90M | $108 |
| **total, uncached** | | **~$198/mo** |
| with prompt caching (90% off cached reads) | | **~$160/mo** |

*Verify rates before relying on these; they are from September 2026.*

**What dominates: output tokens.** $108 of $198, and roughly two-thirds once the
prompt is cached. This is not where I expected the cost to sit, and it inverts
the obvious optimisation — a cheaper model saves less than a shorter answer does.

Note that Luna is a **reasoning model**, so a meaningful share of those output
tokens are reasoning, not prose. That has a second-order consequence covered in
§7: `max_completion_tokens` covers reasoning too, and a cap sized for two
sentences can be consumed entirely by thinking, returning an empty message with
HTTP 200 and no error. This bit us in the MVP.

**Where I would attack it, in order:**

1. **Result caching on the validated plan hash.** "Spend by channel last eight
   weeks" is the same query for every user on a team, and the plan is a small
   canonical structure that hashes cleanly. A cache hit removes *both* model
   calls, not just the input tokens. On dashboard-style traffic I would expect a
   high hit rate, and it is the single largest lever.
2. **Cap narration length in the request.** Enforced by `max_completion_tokens`,
   not requested in the prompt — with the reasoning budget accounted for.
3. **Cache the planning prompt.** The system prompt is ~700 fixed tokens per call.
   Cached reads at 90% off make this nearly free, but it only touches input, which
   is the smaller half.
4. **Skip narration for repeat shapes.** A cached result with a template sentence
   costs nothing at all.

At this volume, engineering time costs more than inference. I would not
micro-optimise the model choice until the traffic is ten times larger.

**Latency.** Measured end to end: plan ~1.4s, query <50ms on this data (low
hundreds of ms on a warehouse), chart ~200ms, narrate ~1.7s. **Roughly 3.5s.**
The two model calls are ~90% of it.

The largest available win is structural rather than technical: **stream the
result before the narration.** The table and chart are ready a second before the
prose, and showing them immediately makes the answer feel roughly twice as fast
without changing the total. Beyond that, the two calls are sequential by necessity
— narration needs the result — so the ceiling is set by the slower of the two.

---

## 7. Change safety

*You improve a prompt on Friday. On Monday someone says answers got worse. How do
you determine whether they are right?*

The premise of the answer: **"worse" has to be decomposed before it can be
measured.** Three failures wear the same complaint.

| What changed | How to tell |
|---|---|
| The plan is different | diff plans on the golden set |
| The plan is the same, the number changed | that is a data or code bug, not the prompt |
| Both identical, the prose changed | judgement — the expensive case |

**Step 1: is the number different?** Re-run the golden set against both prompt
versions and compare the *computed results*, not the prose. Because plans are
small canonical structures, this is an exact diff — not a similarity score. If the
numbers match, the prompt did not break correctness, and the complaint is about
wording. This narrows the question in minutes, and it is the main practical
argument for a structured plan over free text: **a plan is diffable and a
paragraph is not.**

**Step 2: which plans changed?** Where a plan differs, the diff points at the
exact field — the model now picks `last_n_days: 30` where it picked
`month_to_date`. That is a concrete, arguable difference, not a vibe.

**Step 3: is the changed plan worse?** Only now is judgement needed, and only on
the handful of questions that actually changed. Pairwise review of before/after
on those specific cases.

**What makes this work at all:**

- Every prompt is versioned and every answer records which version produced it,
  alongside model, deployment and code commit. Without that, "Monday's answers"
  cannot be tied to Friday's change.
- Prompts ship behind the same flags as code, so a rollback is one flag.
- New prompts run shadow first — planning on both versions, serving the old,
  logging the diff. Most regressions surface before anyone is served them.
- The golden set grows from confirmed reports, so it accumulates exactly the
  cases that have been wrong before.

**What I would not do:** an LLM judge as the primary signal. It is useful for
triage on the prose-only case, but a model scoring another model's output is not
evidence when the question is whether numbers regressed. The numbers are checkable
directly. Check them directly.

---

## 8. Cuts

What is deliberately not in v1, and what I expect it to cost.

**A bounded set of question shapes.** Five intents, nine metrics, six groupings.
Anything outside is refused rather than attempted. *Cost:* a real ceiling on
usefulness — no "compare Meta to Google week over week for creatives launched
after August". *When I would revisit:* when refusals for unsupported shapes exceed
roughly 10% of traffic, and the refusal log will say which shapes to add. I would
add composable operations (compare, trend, segment) before I would open the door
to free-form SQL, because that gives up the guarantees in §3 and §4.

**No conversational memory.** Every question is independent. "What about
LinkedIn?" does not work. *Cost:* the single most obvious usability gap; people
expect follow-ups. *Why cut anyway:* context carries the largest correctness risk
per unit of effort — the wrong period silently inherited from three questions ago
is exactly the "wrong number that looks right" failure this design exists to
prevent. I would add it as an explicit, visible plan diff ("using: last 8 weeks,
LinkedIn only") rather than as hidden state.

**No semantic layer.** Metrics are defined in code, one definition each. Real
organisations disagree about what "conversion" means, and the disagreement is
political rather than technical. *Cost:* the numbers will not match somebody's
spreadsheet, and that argument arrives on week two. *When:* as soon as a second
tenant defines a metric differently — at which point it is a tenant-scoped metric
catalogue, not a code change.

**Batch ingestion only.** No streaming, no intra-day freshness. *Cost:* "why is
today missing?" — mitigated by stating the coverage window on every answer.

**No row-level permissions within a tenant.** A tenant is the isolation unit.
*Cost:* an agency where a client manager should see only their own accounts
cannot use it. That is a real segment, and it is the first thing I would build
after conversational memory.

**No caching in v1.** Deliberate, and worth stating because §6 identifies caching
as the largest cost lever. Cache invalidation on a per-tenant, per-plan key needs
the data-freshness contract settled first, and getting that wrong serves stale
numbers — which is worse than serving expensive ones. Correct and slow before
fast and occasionally wrong.

**The honest summary:** v1 is narrow and defensible rather than broad and
plausible. The bet is that a system trusted on ten question shapes gets used, and
one that answers everything approximately gets checked once and abandoned. If
that bet is wrong, it is wrong in the direction of being too slow to add
features — which is recoverable. The other direction is not.
