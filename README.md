# Dashboard Agent

Ask a question about ad performance in plain English. Get an answer, a chart, and
the SQL that produced the number.

```
$ python -m dashboard_agent "What did we spend by channel over the last eight weeks?"

  FINDING
  Spend was $51,039.90 on Meta, $35,679.09 on Google Search, $22,587.25 on YouTube,
  and $18,403.39 on LinkedIn over the last eight weeks.

  RESULT
  channel        spend_usd
  ------------------------
  meta           51,039.90  ######################
  google_search  35,679.09  ###############
  youtube        22,587.25  #########
  linkedin       18,403.39  #######
  unattributed      119.79

  DATA NOTES
  - 18 duplicate rows were removed before aggregating.
  - Figures are USD. 642 rows fall on days with no published FX rate (weekends and
    holidays); the last known rate was carried forward.
  - 9 rows belong to a campaign missing from campaigns.csv and appear as 'unattributed'.

  QUERY
    SELECT
        channel,
        ROUND(SUM(spend_usd), 2) AS spend_usd
    FROM fact_performance
    WHERE date BETWEEN DATE '2026-07-10' AND DATE '2026-09-04'
    GROUP BY channel
    ORDER BY spend_usd DESC NULLS LAST

  CHART  out/2026-09-04_aggregate_spend.png
```

The query is printed with every answer on purpose. A number you cannot check is
a number you have to trust, and this exercise is about not requiring that.

## Run it

```bash
python -m venv .venv && .venv/Scripts/activate     # Windows
# python3 -m venv .venv && source .venv/bin/activate   # macOS / Linux
pip install -r requirements.txt

python -m dashboard_agent --offline "Which campaign should we turn off?"
```

`--offline` replays recorded model responses, so **no API key is needed** to see
the system work. The recordings in `tests/fixtures/` are real `gpt-5.6-luna`
responses, not hand-written ones.

To run against a live model:

```bash
cp .env.example .env      # then fill in your Azure OpenAI details
python -m dashboard_agent "Which campaign should we turn off?"
```

Useful flags: `--open` writes an HTML report and opens it, `--no-chart` skips the
PNG, `--data DIR` points at a different extract.

## Web UI

```bash
python -m dashboard_agent.web --offline     # no API key needed
python -m dashboard_agent.web               # live model
```

Opens at http://localhost:8000. Shows:
- **Dashboard** — KPI cards (spend, revenue, ROAS, conversions), spend by channel,
  campaign ROAS ranking, and data quality summary
- **Chat** — type a question, get a finding with a chart, the data table, the SQL
  that produced it, and data notes. Click the quick-question chips to try the five
  from the brief.

One HTML file, no build step, no npm.

## Check the numbers

```bash
python -m dashboard_agent.verify    # every cleaning decision, and all five answers
python -m pytest                    # 21 tests, no network, no key
```

`verify` involves no model at all. It prints what the cleaning layer did, then
re-derives each of the five answers directly in SQL. Start there — it is the
fastest way to confirm the figures below are real.

## The five questions

| Question | Answer |
|---|---|
| What did we spend by channel over the last eight weeks? | Meta $51,039.90, Google Search $35,679.09, YouTube $22,587.25, LinkedIn $18,403.39 |
| Which campaign generated the most revenue this quarter? | **Brand Search - US**, $90,848.14 |
| Conversions fell off a cliff on the most recent day. What happened? | Nothing did. 4 Sep is a **partial extract** — 11 rows against a median of 30 |
| Which campaign should we turn off? | **C007 LinkedIn B2B Leads** — ROAS 0.41 on $18,801 across 87 days |
| How does our spend compare to our competitors? | **Refused.** No competitor data exists here, and ad platforms do not publish it |

Two of these are traps.

**Question 2 changes answer depending on whether currency is handled.** Spend and
revenue are stored in each campaign's own currency, INR or USD. Rank on the raw
column and the winner is *Brand Search - India*. Convert first and it is *Brand
Search - US*. Both are real campaigns with nearly the same name, so the wrong
answer does not look wrong.

**Question 4 has four wrong answers ranked above the right one.** Sort campaigns
by ROAS and the worst four are: two `awareness` campaigns that were never trying
to earn revenue, a `traffic` campaign judged on cost per click, and an 11-day-old
test with $410 at stake. The agent applies an eligibility filter first and shows
every excluded campaign with the reason.

## How it works

```
question
   │
   ▼
[ LLM ]   plan          strict JSON. Sees no data. Emits no numbers, not even dates.
   │
   ▼
[ code ]  validate      metric exists? period inside the data? intent coherent?
   │
   ▼
[ code ]  SQL           built from a closed enum, run on DuckDB
   │
   ▼
[ code ]  chart         type chosen from the shape of the result
   │
   ▼
[ LLM ]   narrate       two sentences about a table that is already computed
   │
   ▼
answer + chart + the query that produced it
```

Two model calls, both at the edges. Neither performs arithmetic. Neither sees a
raw row.

**Where the LLM is deliberately absent:** all currency handling happens in the
cleaning view, so every figure is already USD before any generated query runs.
The model is never told what an exchange rate is and cannot get one wrong — not
because it is instructed carefully, but because the concept does not exist at
the layer it works on.

**The rule for what is fixed and what is chosen:** anything that changes a number
is fixed in code. The model picks which question to answer; it never participates
in answering it. `DESIGN.md` argues this at length.

No agent framework. There is no tool selection and no branching the model needs
to reason about, so a framework would add indirection and cost the ability to say
exactly where the model sits. The only loop is a plan retry, bounded at one
attempt.

## The data

`data/` is generated by `tools/generate_data.py`. The brief describes four CSVs
but none were supplied; Zocket confirmed by email on 9 September 2026 to generate
mock data against the appendix schema.

It is **not** clean. The brief says the real extract "has the defects real ad data
has", so the generator plants thirteen of them deliberately — missing weekend FX
rates, duplicate rows from an ETL re-run, dates re-saved by Excel in the wrong
format, a campaign deleted after export, negative spend from platform credits. Each
one records the mechanism that causes it in a real export. Clean data would have
removed the exercise.

`DATA.md` has the full register and the ground truth. `tools/audit_data.py` finds
the defects from the CSVs alone, sharing no code with the generator.

## Layout

```
dashboard_agent/
  sql/curated.sql   all defect handling, once, in one readable place
  data.py           loads the CSVs into DuckDB; reports what cleaning did
  plan.py           the plan schema and the validator
  sql.py            plan -> SQL. No model output reaches this file
  llm.py            the two model calls, and the offline replay client
  agent.py          the request path
  chart.py          chart type chosen from the result shape
  cli.py            terminal output and the HTML report
  web.py            FastAPI server — dashboard + chat UI
  static/index.html the entire web UI in one file
  verify.py         re-derives every answer with no model involved

tools/
  generate_data.py  the fixture, with its deliberate defects
  audit_data.py     finds defects from the CSVs alone
  record_fixtures.py  records live responses for offline mode

tests/test_answers.py   21 tests
DATA.md                 defect register and ground truth
DESIGN.md               the production design
```

## What is not here

No authentication, deployment, CI, multi-tenancy or designed UI — the brief says
none of it scores. No vector database: fourteen campaigns and a fixed schema do
not need retrieval, and adding one would be the "framework you would not
otherwise reach for" that the brief warns about.

`DESIGN.md` covers what a production version would need and what I would build
first.

## How this was built

With AI assistance, as the brief invites. The architecture decisions — SQL over
pandas, no agent framework, the LLM confined to the edges of the request path,
the fixed-versus-decided line — are mine, and `DESIGN.md` argues the alternatives
I rejected.
