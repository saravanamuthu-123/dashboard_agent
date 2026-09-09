"""Prints what the cleaning layer did, and re-derives the known answers.

Run this before trusting anything the agent says:

    python -m dashboard_agent.verify

It exists because "the numbers are right" is a claim, and a claim needs
something a reviewer can run. Nothing here involves an LLM.
"""

import sys

from dashboard_agent.data import connect, quality_report


def rule(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show(con, sql, title=None):
    """Run a query and print it as a table."""
    if title:
        print("\n" + title)
    print(con.sql(sql))


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    data_dir = argv[0] if argv else None

    con = connect(data_dir)
    report = quality_report(con)

    rule("CLEANING LAYER")
    print("  raw rows in ad_performance_daily.csv : {:>8,}".format(report.raw_rows))
    print("  rows after cleaning                  : {:>8,}".format(report.clean_rows))
    print("  covering                             : {} .. {}".format(
        report.date_min, report.date_max))
    print("  total spend (USD, FX-normalised)     : {:>12,.2f}".format(
        report.total_spend_usd))
    print("  total revenue (USD, FX-normalised)   : {:>12,.2f}".format(
        report.total_revenue_usd))

    print("\n  what the cleaning layer did:")
    for note in report.caveats():
        print("    - " + note)

    # The single most important comparison in this project: what the same
    # question returns with and without currency handling.
    rule("WHY FX NORMALISATION MATTERS")
    show(con, """
        SELECT
            'naive  SUM(spend)'          AS method,
            ROUND(SUM(spend_local), 2)   AS total,
            'adds rupees to dollars'     AS note
        FROM fact_performance
        UNION ALL
        SELECT
            'correct SUM(spend_usd)',
            ROUND(SUM(spend_usd), 2),
            'every row converted at that day rate'
        FROM fact_performance
    """)

    rule("Q1  SPEND BY CHANNEL, LAST EIGHT WEEKS")
    show(con, """
        SELECT
            channel,
            ROUND(SUM(spend_usd), 2) AS spend_usd,
            ROUND(100 * SUM(spend_usd) / SUM(SUM(spend_usd)) OVER (), 1) AS pct
        FROM fact_performance
        WHERE date BETWEEN DATE '2026-07-10' AND DATE '2026-09-04'
        GROUP BY channel
        ORDER BY spend_usd DESC
    """)

    rule("Q2  MOST REVENUE THIS QUARTER - THE ANSWER DEPENDS ON FX")
    show(con, """
        SELECT campaign_id, campaign_name,
               ROUND(SUM(revenue_local), 2) AS revenue_raw_local_currency
        FROM fact_performance
        WHERE date BETWEEN DATE '2026-07-01' AND DATE '2026-09-04'
        GROUP BY campaign_id, campaign_name
        ORDER BY revenue_raw_local_currency DESC
        LIMIT 3
    """, "  NAIVE - ranking on the raw column, currencies mixed (WRONG):")
    show(con, """
        SELECT campaign_id, campaign_name,
               ROUND(SUM(revenue_usd), 2) AS revenue_usd
        FROM fact_performance
        WHERE date BETWEEN DATE '2026-07-01' AND DATE '2026-09-04'
        GROUP BY campaign_id, campaign_name
        ORDER BY revenue_usd DESC
        LIMIT 3
    """, "  CORRECT - FX-normalised:")

    rule("Q3  IS THE LAST DAY REAL, OR A PARTIAL EXTRACT?")
    show(con, """
        WITH daily AS (
            SELECT date, count(*) AS rows, SUM(conversions) AS conversions,
                   ROUND(SUM(spend_usd), 2) AS spend_usd
            FROM fact_performance GROUP BY date
        ),
        stats AS (
            SELECT
                (SELECT max(date) FROM daily) AS last_day,
                median(rows)        AS med_rows,
                median(conversions) AS med_conv
            FROM daily
            WHERE date >= (SELECT max(date) FROM daily) - INTERVAL 15 DAY
              AND date <  (SELECT max(date) FROM daily)
        )
        SELECT
            d.date AS last_day,
            d.rows, s.med_rows AS median_rows,
            ROUND(100.0 * d.rows / s.med_rows, 0) AS rows_pct_of_median,
            d.conversions, s.med_conv AS median_conversions,
            ROUND(100.0 * d.conversions / s.med_conv, 0) AS conv_pct_of_median
        FROM daily d, stats s
        WHERE d.date = s.last_day
    """)
    print("  A day at a fraction of the trailing median is an incomplete extract,")
    print("  not a collapse in performance. Reporting it as a drop would be wrong.")

    rule("Q4  CAMPAIGN SCORECARD")
    show(con, """
        SELECT
            campaign_id, campaign_name, objective, is_running,
            count(DISTINCT date)             AS days,
            ROUND(SUM(spend_usd), 0)         AS spend,
            ROUND(SUM(revenue_usd), 0)       AS revenue,
            SUM(conversions)                 AS conversions,
            CASE WHEN SUM(spend_usd) > 0
                 THEN ROUND(SUM(revenue_usd) / SUM(spend_usd), 2) END AS roas
        FROM fact_performance
        WHERE NOT is_orphan_campaign
        GROUP BY campaign_id, campaign_name, objective, is_running
        ORDER BY roas
    """)
    print("  A ROAS-only ranking would recommend turning off an awareness campaign,")
    print("  which was never trying to convert. Objective has to be checked first.")

    rule("Q5  COMPETITOR SPEND")
    columns = con.execute("""
        SELECT count(*) FROM (
            SELECT column_name FROM information_schema.columns
            WHERE lower(column_name) SIMILAR TO '%(compet|benchmark|industry|peer|market)%'
        )
    """).fetchone()[0]
    print("  columns matching competitor/benchmark/industry/peer/market: {}".format(columns))
    print("  No competitor data exists in this extract, and ad platforms do not")
    print("  publish it. The agent must refuse and say what would be needed.")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
