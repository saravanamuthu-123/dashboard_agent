"""Does the agent still answer correctly after a change?

The brief asks for an automated check, and says three cases is enough - the
mechanism matters more than the coverage. These are the cases where being wrong
would matter, and each one pins a number that was derived independently in
DATA.md before this code existed.

No network and no API key. Every test runs the deterministic half of the system:
plan -> validate -> SQL -> DuckDB. The LLM chooses which plan to run; it never
influences what a given plan returns, so the numbers are testable without it.

    python -m pytest -q
"""

from datetime import date

import pytest

from dashboard_agent.data import connect, quality_report
from dashboard_agent.plan import PlanError, validate
from dashboard_agent import sql as sqlmod


@pytest.fixture(scope="module")
def con():
    return connect()


@pytest.fixture(scope="module")
def span(con):
    return con.execute("SELECT min(date), max(date) FROM fact_performance").fetchone()


def run(con, raw, span):
    """Validate a raw plan and execute the SQL it produces."""
    plan = validate(raw, span[0], span[1])
    query, params = sqlmod.build(plan)
    return plan, con.execute(query, params).fetchall()


def make(**kwargs):
    """A raw plan with the sentinel defaults strict Structured Outputs requires."""
    base = dict(
        intent="aggregate", metric="spend", group_by="none",
        period_kind="all_time", period_n=0, date_from="", date_to="",
        limit=0, refusal_reason="",
    )
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# Case 1 - spend by channel over the last eight weeks.
#
# The number here is only right if the FX join is right. Summing the raw spend
# column instead would give roughly 34x this figure by adding rupees to dollars,
# so this single assertion catches any regression in currency handling.
# ---------------------------------------------------------------------------

def test_q1_spend_by_channel_last_eight_weeks(con, span):
    plan, rows = run(con, make(
        intent="aggregate", metric="spend", group_by="channel",
        period_kind="last_n_weeks", period_n=8,
    ), span)

    assert (plan.date_from, plan.date_to) == (date(2026, 7, 10), date(2026, 9, 4))

    spend = {channel: value for channel, value in rows}
    assert spend["meta"] == pytest.approx(51039.90, abs=0.01)
    assert spend["google_search"] == pytest.approx(35679.09, abs=0.01)
    assert spend["youtube"] == pytest.approx(22587.25, abs=0.01)
    assert spend["linkedin"] == pytest.approx(18403.39, abs=0.01)

    # Rows from a campaign deleted after export still appear, rather than being
    # silently dropped by an inner join and leaving the total short.
    assert "unattributed" in spend


def test_naive_currency_handling_would_be_wrong(con):
    """Guards the premise of the test above rather than the agent.

    If this ever stops failing, the fixture has lost its mixed currencies and
    every other assertion here becomes much weaker.
    """
    naive, correct = con.execute(
        "SELECT SUM(spend_local), SUM(spend_usd) FROM fact_performance"
    ).fetchone()
    assert naive > correct * 30


# ---------------------------------------------------------------------------
# Case 2 - most revenue this quarter.
#
# The answer changes depending on whether currency is handled. Ranking on the
# raw column gives "Brand Search - India"; ranking correctly gives "Brand Search
# - US". Both are plausible, similarly named campaigns, so a wrong answer here
# does not look wrong. Asserting both directions is the point.
# ---------------------------------------------------------------------------

def test_q2_top_revenue_this_quarter(con, span):
    plan, rows = run(con, make(
        intent="rank", metric="revenue", group_by="campaign",
        period_kind="quarter_to_date", limit=3,
    ), span)

    assert (plan.date_from, plan.date_to) == (date(2026, 7, 1), date(2026, 9, 4))
    assert rows[0][0] == "Brand Search - US"
    assert rows[0][1] == pytest.approx(90848.14, abs=0.01)


def test_q2_answer_flips_without_fx(con):
    naive = con.execute(
        """
        SELECT campaign_name FROM fact_performance
        WHERE date BETWEEN DATE '2026-07-01' AND DATE '2026-09-04'
        GROUP BY campaign_name ORDER BY SUM(revenue_local) DESC LIMIT 1
        """
    ).fetchone()[0]
    assert naive == "Brand Search - India", (
        "the fixture no longer distinguishes correct FX handling from naive"
    )


# ---------------------------------------------------------------------------
# Case 3 - the apparent collapse on the most recent day.
#
# The right answer is that the extract is incomplete, not that performance fell.
# The evidence is the row count, not the metric: a day carrying a third of the
# usual rows has not finished arriving.
# ---------------------------------------------------------------------------

def test_q3_last_day_is_partial_not_a_collapse(con, span):
    _, rows = run(con, make(
        intent="anomaly", metric="conversions", group_by="date",
        period_kind="last_n_days", period_n=20,
    ), span)

    last = rows[-1]
    day, conversions, rows_reported, median_metric, median_rows = last

    assert day == date(2026, 9, 4)
    # Conversions collapsed...
    assert conversions < median_metric * 0.1
    # ...but so did the number of rows, which is what identifies it as partial.
    assert rows_reported < median_rows * 0.6


# ---------------------------------------------------------------------------
# Case 4 - which campaign to turn off.
#
# The trap is that the four worst campaigns by ROAS are all wrong answers: two
# are awareness campaigns that were never trying to convert, one is a traffic
# campaign, one is eleven days old. A ROAS-only ranking recommends killing a
# campaign that is doing its job.
# ---------------------------------------------------------------------------

def test_q4_recommends_the_right_campaign(con, span):
    _, rows = run(con, make(intent="recommend_pause", metric="roas"), span)

    by_id = {row[0]: row for row in rows}
    eligible = [row for row in rows if row[-1] == "eligible"]

    worst = min(eligible, key=lambda row: row[8])
    assert worst[0] == "C007"
    assert worst[1] == "LinkedIn B2B Leads"
    assert worst[8] == pytest.approx(0.41, abs=0.01)

    # Every trap is excluded, and each says why.
    assert "awareness" in by_id["C006"][-1]      # never trying to convert
    assert "awareness" in by_id["C011"][-1]
    assert "traffic" in by_id["C008"][-1]        # judged on cost per click
    assert "11 days" in by_id["C010"][-1]        # too new to judge
    assert "already ended" in by_id["C009"][-1]  # cannot pause what is off

    # Three campaigns have a worse raw ROAS than the recommendation. If the
    # eligibility filter were dropped, all three would be wrong answers.
    worse_on_roas = [r for r in rows if r[8] is not None and r[8] < worst[8]]
    assert len(worse_on_roas) == 3
    assert all(r[-1] != "eligible" for r in worse_on_roas)


# ---------------------------------------------------------------------------
# Case 5 - a question the data cannot answer.
# ---------------------------------------------------------------------------

def test_q5_refusal_is_a_first_class_plan(con, span):
    plan = validate(make(
        intent="refuse",
        refusal_reason="This extract contains only our own campaigns; competitor "
                       "spend is not in it and is not published by ad platforms.",
    ), span[0], span[1])

    assert plan.intent == "refuse"
    assert "competitor" in plan.refusal_reason.lower()
    with pytest.raises(ValueError):
        sqlmod.build(plan)


def test_refusal_must_give_a_reason(con, span):
    with pytest.raises(PlanError):
        validate(make(intent="refuse", refusal_reason=""), span[0], span[1])


# ---------------------------------------------------------------------------
# The validator itself. Structured Outputs guarantee the shape of a plan; these
# cover the failures that a well-formed plan can still contain.
# ---------------------------------------------------------------------------

def test_validator_rejects_unknown_metric(con, span):
    with pytest.raises(PlanError):
        validate(make(metric="profit"), span[0], span[1])


def test_validator_rejects_rank_without_a_grouping(con, span):
    with pytest.raises(PlanError):
        validate(make(intent="rank", group_by="none"), span[0], span[1])


def test_period_outside_the_data_is_clamped_and_reported(con, span):
    plan = validate(make(
        period_kind="explicit", date_from="2025-01-01", date_to="2026-12-31",
    ), span[0], span[1])

    assert plan.date_from == span[0] and plan.date_to == span[1]
    assert len(plan.warnings) == 2
    assert any("data begins" in w for w in plan.warnings)


def test_period_entirely_outside_the_data_is_refused(con, span):
    with pytest.raises(PlanError):
        validate(make(
            period_kind="explicit", date_from="2024-01-01", date_to="2024-06-30",
        ), span[0], span[1])


def test_relative_periods_anchor_on_the_data_not_the_clock(con, span):
    """"The last 7 days" means the last 7 days of data, not of the calendar."""
    plan = validate(make(period_kind="last_n_days", period_n=7), span[0], span[1])
    assert plan.date_to == date(2026, 9, 4)
    assert plan.date_from == date(2026, 8, 28)


def test_ratio_metrics_survive_a_zero_denominator(con, span):
    """A campaign with no conversions must yield NULL for CPA, never an error."""
    _, rows = run(con, make(metric="cpa", group_by="campaign"), span)
    assert any(row[1] is None for row in rows)


# ---------------------------------------------------------------------------
# The cleaning layer.
# ---------------------------------------------------------------------------

def test_cleaning_layer_handles_the_known_defects(con):
    report = quality_report(con)

    assert report.raw_rows == 2398
    assert report.duplicates_removed == 18
    assert report.unparseable_dates == 0          # both formats parsed explicitly
    assert report.non_iso_dates == 7
    assert report.orphan_rows == 9
    assert report.currency_mismatches == 12
    assert report.missing_days == [date(2026, 7, 19), date(2026, 7, 20)]
    assert report.total_spend_usd == pytest.approx(184473.10, abs=0.01)


def test_every_row_has_an_exchange_rate(con):
    """The failure this guards is silent: an unmatched row contributes nothing
    to a SUM, so the total comes out low with no error anywhere."""
    unrated = con.execute(
        "SELECT count(*) FROM fact_performance WHERE rate_to_usd IS NULL"
    ).fetchone()[0]
    assert unrated == 0


# ---------------------------------------------------------------------------
# End to end, through the whole pipeline, using recorded model responses.
#
# The tests above exercise the deterministic half by building plans directly.
# These run the real path - plan, validate, SQL, execute, narrate - with the
# model responses replayed from tests/fixtures/llm_responses.json. That covers
# the part the others cannot: that a plan a real model actually produced still
# resolves to the right answer.
# ---------------------------------------------------------------------------

from dashboard_agent.agent import answer_question   # noqa: E402
from dashboard_agent.llm import OfflineClient       # noqa: E402


@pytest.fixture(scope="module")
def offline():
    return OfflineClient()


def test_end_to_end_spend_by_channel(con, offline):
    answer = answer_question(
        "What did we spend by channel over the last eight weeks?", con, offline)

    assert not answer.refused
    spend = dict(answer.rows)
    assert spend["meta"] == pytest.approx(51039.90, abs=0.01)
    # The finding has to carry the figure, not just gesture at it.
    assert "51,039.90" in answer.finding
    # And the query that produced it has to be shown.
    assert "fact_performance" in answer.sql


def test_end_to_end_names_the_right_campaign_to_pause(con, offline):
    answer = answer_question("Which campaign should we turn off?", con, offline)

    assert "LinkedIn B2B Leads" in answer.finding
    # None of the four traps may be recommended.
    for trap in ("Awareness", "Traffic Blast", "New Product Test", "Summer Sale"):
        assert trap not in answer.finding


def test_end_to_end_refuses_the_competitor_question(con, offline):
    answer = answer_question(
        "How does our spend compare to our competitors?", con, offline)

    assert answer.refused
    assert not answer.sql          # nothing was computed
    assert "competitor" in answer.finding.lower()


def test_end_to_end_refuses_a_period_outside_the_data(con, offline):
    """A different reason for refusing than the competitor question: the metric
    exists, the period does not."""
    answer = answer_question("What did we spend in January 2026?", con, offline)

    assert answer.refused
    assert "january" in answer.finding.lower()


def test_end_to_end_calls_the_last_day_partial(con, offline):
    answer = answer_question(
        "Conversions look like they fell off a cliff on the most recent day. "
        "What happened?", con, offline)

    # The point of this question is that the drop is a reporting artefact.
    assert any(word in answer.finding.lower()
               for word in ("incomplete", "partial"))
    # And the evidence for that claim must be in the result the user sees.
    assert "rows_reported" in answer.columns
