"""Turns a validated QueryPlan into SQL.

No model output reaches this file. Every value that lands in a query comes from
a closed enum the validator already checked, and dates arrive as real date
objects, so there is nothing here for a prompt to influence. That is what makes
the generated SQL safe to run and safe to show.

The SQL is also the answer to the brief requiring the agent to show its work:
it is a string, so it can be printed next to the number it produced.
"""

from dashboard_agent.plan import RATIO_METRICS

# How each grouping maps onto a column of the curated view.
GROUP_COLUMN = {
    "channel": "channel",
    "campaign": "campaign_name",
    "creative": "creative_id",
    "objective": "objective",
    "date": "date",
    "none": None,
}

# Plain sums. Ratio metrics are handled separately because they cannot be summed.
SUM_METRIC = {
    "spend": "SUM(spend_usd)",
    "revenue": "SUM(revenue_usd)",
    "conversions": "SUM(conversions)",
    "clicks": "SUM(clicks)",
    "impressions": "SUM(impressions)",
}

# Ratios, each guarded against a zero denominator.
#
# NULLIF turns a zero denominator into NULL, so the row reports "no value"
# instead of raising or producing an infinity that would then be formatted as a
# confident number. A campaign with no conversions has no meaningful CPA, and
# saying so is more useful than printing inf.
RATIO_METRIC = {
    "roas": "SUM(revenue_usd) / NULLIF(SUM(spend_usd), 0)",
    "cpa":  "SUM(spend_usd)   / NULLIF(SUM(conversions), 0)",
    "ctr":  "SUM(clicks)      / NULLIF(SUM(impressions), 0)",
    "cpc":  "SUM(spend_usd)   / NULLIF(SUM(clicks), 0)",
}

METRIC_LABEL = {
    "spend": "spend_usd", "revenue": "revenue_usd", "conversions": "conversions",
    "clicks": "clicks", "impressions": "impressions",
    "roas": "roas", "cpa": "cpa_usd", "ctr": "ctr", "cpc": "cpc_usd",
}

# Decimal places per metric. Money gets 2 - reporting spend to four decimals
# implies a precision the source data does not have. CTR gets 4 because it is a
# small fraction and 2 would round most values to zero.
METRIC_DP = {
    "spend": 2, "revenue": 2, "cpa": 2, "cpc": 2, "roas": 2,
    "conversions": 0, "clicks": 0, "impressions": 0,
    "ctr": 4,
}

# Pause eligibility. These thresholds are the judgement in question 4, and they
# live here rather than in a prompt so that they are inspectable, testable and
# identical on every run.
#
#   objective = 'conversions'  an awareness or traffic campaign was never trying
#                              to earn revenue; ranking it on ROAS is a category
#                              error and would recommend killing a campaign that
#                              is doing its job
#   is_running                 a campaign that already ended cannot be paused
#   >= 30 days of history      a campaign still in its first weeks has not had
#                              time to calibrate
#   >= 50 conversions          below this the ROAS estimate is noise
MIN_DAYS_FOR_PAUSE = 30
MIN_CONVERSIONS_FOR_PAUSE = 50


def _metric_expr(metric):
    if metric in RATIO_METRICS:
        return RATIO_METRIC[metric]
    return SUM_METRIC[metric]


def build(plan):
    """Return (sql, params) for a validated plan.

    Dates are passed as parameters rather than interpolated - not because the
    model could inject anything (it never emits a date), but because the habit
    is the right one and the queries stay copy-pasteable.
    """
    if plan.intent == "refuse":
        raise ValueError("a refusal plan has no query to build")

    if plan.intent == "recommend_pause":
        return _build_pause(plan)

    if plan.intent == "anomaly":
        return _build_anomaly(plan)

    return _build_aggregate(plan)


def _build_aggregate(plan):
    """Totals, optionally grouped, optionally ordered. Covers aggregate and rank."""
    column = GROUP_COLUMN[plan.group_by]
    expr = _metric_expr(plan.metric)
    label = METRIC_LABEL[plan.metric]

    select = []
    group = []
    if column:
        select.append(column)
        group.append(column)
    select.append("ROUND({}, {}) AS {}".format(expr, METRIC_DP[plan.metric], label))

    # Volume alongside a ratio, so a ROAS of 8.0 built on two conversions is
    # visible as such rather than looking like the best campaign in the account.
    if plan.metric in RATIO_METRICS:
        select.append("ROUND(SUM(spend_usd), 2) AS spend_usd")
        select.append("SUM(conversions) AS conversions")

    sql = "SELECT\n    " + ",\n    ".join(select)
    sql += "\nFROM fact_performance"
    sql += "\nWHERE date BETWEEN ? AND ?"
    if group:
        sql += "\nGROUP BY " + ", ".join(group)

    if plan.group_by == "date":
        sql += "\nORDER BY date"
    elif column:
        sql += "\nORDER BY {} DESC NULLS LAST".format(label)
    if column and plan.intent == "rank":
        sql += "\nLIMIT {}".format(plan.limit)

    return sql, [plan.date_from, plan.date_to]


def _build_anomaly(plan):
    """A daily series with a trailing median beside it.

    The comparison is the point. A number is only anomalous relative to what
    came before, and the most common cause of an apparent collapse in ad data is
    an incomplete final day rather than a real drop - so the row count is
    reported next to the metric. A day carrying a third of the usual rows did
    not lose conversions; it has not finished arriving.
    """
    expr = _metric_expr(plan.metric)
    label = METRIC_LABEL[plan.metric]

    sql = """SELECT
    date,
    ROUND({expr}, {dp}) AS {label},
    COUNT(*) AS rows_reported,
    ROUND(MEDIAN({expr}) OVER (
        ORDER BY date ROWS BETWEEN 14 PRECEDING AND 1 PRECEDING
    ), {dp}) AS trailing_14d_median,
    ROUND(MEDIAN(COUNT(*)) OVER (
        ORDER BY date ROWS BETWEEN 14 PRECEDING AND 1 PRECEDING
    ), 1) AS median_rows_reported
FROM fact_performance
WHERE date BETWEEN ? AND ?
GROUP BY date
ORDER BY date""".format(expr=expr, label=label, dp=METRIC_DP[plan.metric])

    return sql, [plan.date_from, plan.date_to]


def _build_pause(plan):
    """Campaigns ranked by ROAS, with the eligibility test applied and shown.

    Every campaign is returned, not only the eligible ones, and each carries the
    reason it is or is not a candidate. Hiding the excluded rows would make the
    recommendation impossible to check - and the excluded rows are exactly where
    a naive answer goes wrong.
    """
    sql = """WITH scored AS (
    SELECT
        campaign_id,
        campaign_name,
        objective,
        is_running,
        COUNT(DISTINCT date)                                   AS days_running,
        ROUND(SUM(spend_usd), 2)                               AS spend_usd,
        ROUND(SUM(revenue_usd), 2)                             AS revenue_usd,
        SUM(conversions)                                       AS conversions,
        ROUND(SUM(revenue_usd) / NULLIF(SUM(spend_usd), 0), 2) AS roas
    FROM fact_performance
    WHERE date BETWEEN ? AND ?
      AND NOT is_orphan_campaign
    GROUP BY campaign_id, campaign_name, objective, is_running
)
SELECT
    *,
    CASE
        WHEN objective <> 'conversions'
            THEN 'excluded: ' || objective || ' campaigns are not judged on ROAS'
        WHEN NOT is_running
            THEN 'excluded: already ended'
        WHEN days_running < {min_days}
            THEN 'excluded: only ' || days_running || ' days of history'
        WHEN conversions < {min_conv}
            THEN 'excluded: ' || conversions || ' conversions is too few to judge'
        ELSE 'eligible'
    END AS eligibility
FROM scored
ORDER BY (eligibility = 'eligible') DESC, roas ASC NULLS LAST""".format(
        min_days=MIN_DAYS_FOR_PAUSE, min_conv=MIN_CONVERSIONS_FOR_PAUSE
    )

    return sql, [plan.date_from, plan.date_to]


def render(sql, params):
    """Inline the parameters so the query can be shown and re-run by hand.

    Display only - the executed query uses real parameter binding.
    """
    out = sql
    for value in params:
        out = out.replace("?", "DATE '{}'".format(value), 1)
    return out
