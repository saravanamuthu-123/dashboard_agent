"""Loads the CSV extract into DuckDB and exposes the curated view.

DuckDB reads the CSV files in place - there is no import step, no database file
and no server. This module creates the views defined in sql/curated.sql and
provides a quality report describing what the cleaning layer did.

Everything the agent computes runs against fact_performance. Nothing downstream
reads a raw CSV.
"""

from dataclasses import dataclass
from pathlib import Path

import duckdb

# Project layout: this file lives in dashboard_agent/, data/ sits beside it.
PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent
DEFAULT_DATA_DIR = PROJECT_DIR / "data"
CURATED_SQL = PACKAGE_DIR / "sql" / "curated.sql"


def connect(data_dir=None):
    """Open an in-memory DuckDB database with the curated views defined.

    In-memory because the CSVs are the source of truth. Nothing is persisted, so
    a run can never read a stale copy of the data - re-running always re-reads
    the files.
    """
    if data_dir is None:
        data_dir = DEFAULT_DATA_DIR
    data_dir = Path(data_dir).resolve()

    missing = [
        name
        for name in (
            "ad_performance_daily.csv",
            "campaigns.csv",
            "creatives.csv",
            "fx_rates.csv",
        )
        if not (data_dir / name).exists()
    ]
    if missing:
        raise FileNotFoundError(
            "Missing {} in {}. Run: python tools/generate_data.py".format(
                ", ".join(missing), data_dir
            )
        )

    con = duckdb.connect()
    # DuckDB wants forward slashes in paths even on Windows.
    sql = CURATED_SQL.read_text(encoding="utf-8")
    sql = sql.replace("${DATA}", data_dir.as_posix())
    con.execute(sql)
    return con


@dataclass
class QualityReport:
    """What the cleaning layer found and what it did about it.

    Printed by `verify` and attached to every answer, so a number is never shown
    without the caveats that apply to it.
    """

    raw_rows: int
    clean_rows: int
    duplicates_removed: int
    unparseable_dates: int
    non_iso_dates: int
    orphan_rows: int
    currency_mismatches: int
    fx_filled_rows: int
    negative_spend_rows: int
    impossible_funnel_rows: int
    missing_revenue_rows: int
    date_min: object
    date_max: object
    missing_days: list
    total_spend_usd: float
    total_revenue_usd: float

    def caveats(self):
        """Short lines worth showing beside any figure computed from this data."""
        notes = []
        if self.duplicates_removed:
            notes.append(
                "{} duplicate rows removed before aggregation".format(
                    self.duplicates_removed
                )
            )
        if self.non_iso_dates:
            notes.append(
                "{} dates were not ISO format and were parsed as DD/MM/YYYY".format(
                    self.non_iso_dates
                )
            )
        if self.orphan_rows:
            notes.append(
                "{} rows belong to a campaign missing from campaigns.csv and are "
                "grouped as 'unattributed'".format(self.orphan_rows)
            )
        if self.currency_mismatches:
            notes.append(
                "{} rows disagreed with campaigns.csv on currency; the campaign "
                "record was treated as authoritative".format(self.currency_mismatches)
            )
        if self.fx_filled_rows:
            notes.append(
                "{} rows fall on days with no published FX rate; the last known "
                "rate was carried forward".format(self.fx_filled_rows)
            )
        if self.missing_days:
            shown = ", ".join(str(d) for d in self.missing_days[:4])
            notes.append(
                "{} day(s) have no rows at all ({}) - averages over this period "
                "are affected".format(len(self.missing_days), shown)
            )
        if self.missing_revenue_rows:
            notes.append(
                "{} rows have no revenue figure; they are excluded from revenue "
                "totals rather than counted as zero".format(self.missing_revenue_rows)
            )
        if self.impossible_funnel_rows:
            notes.append(
                "{} rows report more clicks than impressions and are excluded "
                "from rate metrics".format(self.impossible_funnel_rows)
            )
        if self.negative_spend_rows:
            notes.append(
                "{} rows carry negative spend (platform credits); these are real "
                "and are kept".format(self.negative_spend_rows)
            )
        return notes


def _scalar(con, sql):
    return con.execute(sql).fetchone()[0]


def quality_report(con):
    """Measure what the cleaning layer did. Used by `verify` and by the tests."""
    raw_rows = _scalar(con, "SELECT count(*) FROM raw_performance")
    deduped = _scalar(con, "SELECT count(*) FROM (SELECT DISTINCT * FROM raw_performance)")
    dated_rows = _scalar(con, "SELECT count(*) FROM performance_dated")
    clean_rows = _scalar(con, "SELECT count(*) FROM fact_performance")

    flags = con.execute(
        """
        SELECT
            count(*) FILTER (WHERE date_was_non_iso)       AS non_iso,
            count(*) FILTER (WHERE is_orphan_campaign)     AS orphans,
            count(*) FILTER (WHERE currency_tag_mismatch)  AS cur_mismatch,
            count(*) FILTER (WHERE rate_was_filled)        AS fx_filled,
            count(*) FILTER (WHERE is_negative_spend)      AS neg_spend,
            count(*) FILTER (WHERE is_impossible_funnel)   AS bad_funnel,
            count(*) FILTER (WHERE revenue_missing)        AS no_revenue,
            min(date), max(date),
            sum(spend_usd), sum(revenue_usd)
        FROM fact_performance
        """
    ).fetchone()

    # Days inside the covered range that carry no rows at all.
    missing_days = [
        row[0]
        for row in con.execute(
            """
            WITH span AS (
                SELECT min(date) AS lo, max(date) AS hi FROM fact_performance
            ),
            all_days AS (
                SELECT UNNEST(generate_series(lo, hi, INTERVAL 1 DAY))::DATE AS day
                FROM span
            )
            SELECT day FROM all_days
            WHERE day NOT IN (SELECT DISTINCT date FROM fact_performance)
            ORDER BY day
            """
        ).fetchall()
    ]

    return QualityReport(
        raw_rows=raw_rows,
        clean_rows=clean_rows,
        duplicates_removed=raw_rows - deduped,
        unparseable_dates=dated_rows - clean_rows,
        non_iso_dates=flags[0],
        orphan_rows=flags[1],
        currency_mismatches=flags[2],
        fx_filled_rows=flags[3],
        negative_spend_rows=flags[4],
        impossible_funnel_rows=flags[5],
        missing_revenue_rows=flags[6],
        date_min=flags[7],
        date_max=flags[8],
        total_spend_usd=flags[9] or 0.0,
        total_revenue_usd=flags[10] or 0.0,
        missing_days=missing_days,
    )
