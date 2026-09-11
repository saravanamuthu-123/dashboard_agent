"""Prints what the cleaning layer did, and re-derives the known answers.

Run this before trusting anything the agent says:

    python -m dashboard_agent.verify               # tables only, no LLM
    python -m dashboard_agent.verify --explain      # adds LLM explanation per question
    python -m dashboard_agent.verify --offline      # explanation from recorded responses

It exists because "the numbers are right" is a claim, and a claim needs
something a reviewer can run.
"""

import argparse
import sys

from dashboard_agent.data import connect, quality_report


QUESTIONS = [
    "What did we spend by channel over the last eight weeks?",
    "Which campaign generated the most revenue this quarter?",
    "Conversions look like they fell off a cliff on the most recent day. What happened?",
    "Which campaign should we turn off?",
    "How does our spend compare to our competitors?",
]


def rule(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def fmt(value):
    """Format a number with commas for readability."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return "{:,.2f}".format(value)
    if isinstance(value, int):
        return "{:,}".format(value)
    return str(value)


def show_table(con, sql, title=None):
    """Run a query and print it as a formatted table with commas."""
    if title:
        print("\n" + title)
    cursor = con.execute(sql)
    columns = [d[0] for d in cursor.description]
    rows = cursor.fetchall()

    # Format all cells
    cells = [[fmt(v) for v in row] for row in rows]
    widths = [len(c) for c in columns]
    for row in cells:
        for i, text in enumerate(row):
            widths[i] = max(widths[i], len(text))

    # Print header
    header = "  ".join(columns[i].ljust(widths[i]) for i in range(len(columns)))
    print("  " + header)
    print("  " + "-" * len(header))

    # Print rows — right-align numbers
    for cell_row, raw_row in zip(cells, rows):
        parts = []
        for i, text in enumerate(cell_row):
            if isinstance(raw_row[i], (int, float)) and not isinstance(raw_row[i], bool):
                parts.append(text.rjust(widths[i]))
            else:
                parts.append(text.ljust(widths[i]))
        print("  " + "  ".join(parts))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="dashboard_agent.verify")
    parser.add_argument("data_dir", nargs="?", default=None)
    parser.add_argument("--explain", action="store_true",
                        help="run each question through the agent for LLM explanations")
    parser.add_argument("--offline", action="store_true",
                        help="use recorded responses for explanations (no API key)")
    parser.add_argument("--out", default="out",
                        help="directory for chart PNGs (default: out)")
    args = parser.parse_args(argv)

    con = connect(args.data_dir)
    report = quality_report(con)

    # ------------------------------------------------------------------
    # CLEANING LAYER
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # FX COMPARISON
    # ------------------------------------------------------------------
    rule("WHY FX NORMALISATION MATTERS")
    show_table(con, """
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

    # ------------------------------------------------------------------
    # Q1
    # ------------------------------------------------------------------
    rule("Q1  SPEND BY CHANNEL, LAST EIGHT WEEKS")
    show_table(con, """
        SELECT
            channel,
            ROUND(SUM(spend_usd), 2) AS spend_usd,
            ROUND(100 * SUM(spend_usd) / SUM(SUM(spend_usd)) OVER (), 1) AS pct
        FROM fact_performance
        WHERE date BETWEEN DATE '2026-07-10' AND DATE '2026-09-04'
        GROUP BY channel
        ORDER BY spend_usd DESC
    """)

    # ------------------------------------------------------------------
    # Q2
    # ------------------------------------------------------------------
    rule("Q2  MOST REVENUE THIS QUARTER - THE ANSWER DEPENDS ON FX")
    show_table(con, """
        SELECT campaign_id, campaign_name,
               ROUND(SUM(revenue_local), 2) AS revenue_raw_local_currency
        FROM fact_performance
        WHERE date BETWEEN DATE '2026-07-01' AND DATE '2026-09-04'
        GROUP BY campaign_id, campaign_name
        ORDER BY revenue_raw_local_currency DESC
        LIMIT 3
    """, "  NAIVE - ranking on raw column, currencies mixed (WRONG):")
    show_table(con, """
        SELECT campaign_id, campaign_name,
               ROUND(SUM(revenue_usd), 2) AS revenue_usd
        FROM fact_performance
        WHERE date BETWEEN DATE '2026-07-01' AND DATE '2026-09-04'
        GROUP BY campaign_id, campaign_name
        ORDER BY revenue_usd DESC
        LIMIT 3
    """, "  CORRECT - FX-normalised (RIGHT):")

    # ------------------------------------------------------------------
    # Q3
    # ------------------------------------------------------------------
    rule("Q3  IS THE LAST DAY REAL, OR A PARTIAL EXTRACT?")
    show_table(con, """
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

    # ------------------------------------------------------------------
    # Q4
    # ------------------------------------------------------------------
    rule("Q4  CAMPAIGN SCORECARD")
    show_table(con, """
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

    # ------------------------------------------------------------------
    # Q5
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # --explain: run the five questions through the agent with LLM
    # ------------------------------------------------------------------
    if args.explain:
        _run_explained(con, report, args)

    print()
    return 0


def _run_explained(con, report, args):
    """Run all five questions through the full agent pipeline."""
    from dashboard_agent.agent import answer_question
    from dashboard_agent import chart as chartmod
    from dashboard_agent.llm import build_client, LLMError

    rule("AGENT ANSWERS (with LLM explanation and charts)")
    try:
        client = build_client(offline=args.offline)
    except LLMError as exc:
        print("\n  Could not connect to LLM: {}".format(exc))
        print("  Use --offline to replay recorded responses.")
        return

    for i, question in enumerate(QUESTIONS, 1):
        print("\n  ---- Q{}: {} ----".format(i, question))
        try:
            answer = answer_question(question, con, client, report)
        except LLMError as exc:
            print("  ERROR: {}".format(exc))
            continue

        # Explanation
        print("\n  FINDING")
        for line in _wrap(answer.finding):
            print("    " + line)

        if answer.refused:
            print("\n  (refused — no data to answer this)")
            if answer.notes:
                for note in answer.notes:
                    print("    " + note)
            continue

        # Result table with formatted numbers
        if answer.rows:
            print("\n  RESULT")
            _print_result(answer.columns, answer.rows)

        # Chart
        chart_path = chartmod.render(answer, args.out)
        if chart_path:
            answer.chart_path = chart_path
            print("\n  CHART  {}".format(chart_path))

        # Data notes
        if answer.notes:
            print("\n  DATA NOTES")
            for note in answer.notes:
                print("    - " + note)

        # SQL
        if answer.sql:
            print("\n  SQL")
            for line in answer.sql.splitlines():
                print("      " + line)


def _print_result(columns, rows, limit=20):
    """Print a formatted result table."""
    shown = rows[:limit]
    cells = [[fmt(v) for v in row] for row in shown]
    widths = [len(c) for c in columns]
    for row in cells:
        for i, text in enumerate(row):
            widths[i] = max(widths[i], len(text))

    header = "  ".join(columns[i].ljust(widths[i]) for i in range(len(columns)))
    print("    " + header)
    print("    " + "-" * len(header))
    for cell_row, raw_row in zip(cells, shown):
        parts = []
        for i, text in enumerate(cell_row):
            if isinstance(raw_row[i], (int, float)) and not isinstance(raw_row[i], bool):
                parts.append(text.rjust(widths[i]))
            else:
                parts.append(text.ljust(widths[i]))
        print("    " + "  ".join(parts))
    if len(rows) > limit:
        print("    ... {} more rows".format(len(rows) - limit))


def _wrap(text, width=76):
    words, lines, current = text.split(), [], ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = (current + " " + word).strip()
    if current:
        lines.append(current)
    return lines or [""]


if __name__ == "__main__":
    raise SystemExit(main())
