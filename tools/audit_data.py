"""
Data audit for the Zocket dashboard-agent take-home.

The brief says the extract "has the defects real ad data has" and that they were
left in on purpose. This script finds them before any agent code gets written,
so that every number the agent reports can be defended.

Usage:
    python tools/audit_data.py                  # reads ./data
    python tools/audit_data.py --data some/dir
    python tools/audit_data.py > out/audit.txt
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 50)

FINDINGS = []


def note(severity, message):
    """Record a defect so the run can end with a ranked summary."""
    FINDINGS.append((severity, message))


def rule(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def sub(title):
    print("\n--- " + title + " " + "-" * max(0, 72 - len(title)))


EXPECTED = {
    "ad_performance_daily.csv": [
        "date", "campaign_id", "creative_id", "impressions",
        "clicks", "conversions", "spend", "revenue", "currency",
    ],
    "campaigns.csv": [
        "campaign_id", "campaign_name", "channel", "objective",
        "currency", "daily_budget", "start_date", "end_date",
    ],
    "creatives.csv": [
        "creative_id", "campaign_id", "creative_name",
        "format", "headline", "launched_on",
    ],
    "fx_rates.csv": ["date", "currency", "rate_to_usd"],
}


def load(data_dir):
    """Load every expected CSV as raw strings so nothing is silently coerced."""
    frames = {}
    for name in EXPECTED:
        path = data_dir / name
        if not path.exists():
            note("BLOCKER", name + " is missing from " + str(data_dir))
            print("  MISSING: " + str(path))
            continue
        df = pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""])
        frames[name] = df
        print("  loaded {:32s} {:>6,} rows x {} cols".format(name, len(df), len(df.columns)))
        missing_cols = [c for c in EXPECTED[name] if c not in df.columns]
        extra_cols = [c for c in df.columns if c not in EXPECTED[name]]
        if missing_cols:
            note("BLOCKER", name + " is missing expected columns: " + str(missing_cols))
        if extra_cols:
            note("INFO", name + " has undocumented columns: " + str(extra_cols))
    return frames


def to_num(series):
    return pd.to_numeric(series, errors="coerce")


def to_date(series):
    return pd.to_datetime(series, errors="coerce", format="mixed")


def profile(name, df):
    """Null counts, blank values and per-column cardinality."""
    sub(name + " - column profile")
    rows = []
    for col in df.columns:
        s = df[col]
        nulls = int(s.isna().sum())
        blanks = int((s.fillna("").astype(str).str.strip() == "").sum())
        pct = round(100 * nulls / max(len(df), 1), 2)
        rows.append({
            "column": col,
            "nulls": nulls,
            "null_pct": pct,
            "blank": blanks,
            "distinct": int(s.nunique(dropna=True)),
            "sample": str(s.dropna().iloc[0])[:28] if s.notna().any() else "",
        })
        if nulls and col in ("date", "campaign_id", "creative_id", "currency", "rate_to_usd"):
            note("HIGH", "{}.{} has {} nulls in a key/join column".format(name, col, nulls))
        elif nulls:
            note("MEDIUM", "{}.{} has {} nulls ({}%)".format(name, col, nulls, pct))
    print(pd.DataFrame(rows).to_string(index=False))


def check_dupes(name, df, keys):
    """Exact-duplicate rows, plus duplicates on the natural grain."""
    sub(name + " - duplicates")
    exact = int(df.duplicated().sum())
    print("  exact duplicate rows: " + str(exact))
    if exact:
        note("HIGH", "{} has {} fully duplicated rows - these double-count on any SUM".format(name, exact))

    keys = [k for k in keys if k in df.columns]
    if not keys:
        return
    dup_mask = df.duplicated(subset=keys, keep=False)
    n = int(dup_mask.sum())
    print("  rows sharing key {}: {}".format(keys, n))
    if n:
        note("HIGH", "{} grain is not unique on {}: {} rows collide".format(name, keys, n))
        print(df[dup_mask].sort_values(keys).head(12).to_string(index=False))


def check_dates(name, df, col):
    sub("{} - {} range".format(name, col))
    d = to_date(df[col])
    bad = int(d.isna().sum() - df[col].isna().sum())
    print("  min={}  max={}  unparseable={}".format(d.min(), d.max(), bad))
    if bad > 0:
        note("HIGH", "{}.{} has {} values that do not parse as dates".format(name, col, bad))
        print("  examples:", df.loc[d.isna() & df[col].notna(), col].head(5).tolist())

    # Check the raw strings, not the parsed values. A date written DD/MM/YYYY with a
    # day of 12 or less parses cleanly under MM/DD and lands in the wrong month with
    # no error raised, so range checks miss it. Format consistency catches all of them.
    raw = df[col].dropna().astype(str).str.strip()
    iso = raw.str.match(r"^\d{4}-\d{2}-\d{2}$")
    n_non_iso = int((~iso).sum())
    print("  values not in ISO YYYY-MM-DD form: {}".format(n_non_iso))
    if n_non_iso:
        offenders = sorted(raw[~iso].unique())
        note("HIGH", "{}.{} has {} value(s) that are not ISO YYYY-MM-DD: {} - mixed formats parse "
                     "without error and silently land in the wrong month. Normalise on load and "
                     "reject anything that does not match one known format.".format(
                         name, col, n_non_iso, offenders[:6]))
    return d


def check_referential(perf, campaigns, creatives):
    rule("REFERENTIAL INTEGRITY")
    if perf is None:
        return

    if campaigns is not None:
        known = set(campaigns["campaign_id"].dropna())
        used = set(perf["campaign_id"].dropna())
        orphans = used - known
        print("  campaign_ids in perf: {}   in campaigns.csv: {}".format(len(used), len(known)))
        print("  orphan campaign_ids : {} {}".format(len(orphans), sorted(orphans)[:10]))
        if orphans:
            n = int(perf["campaign_id"].isin(orphans).sum())
            note("HIGH", "{} campaign_ids in perf have no campaigns.csv row ({} rows) - "
                         "an INNER JOIN silently drops them".format(len(orphans), n))
        unused = known - used
        if unused:
            note("INFO", "{} campaigns have no performance rows: {}".format(len(unused), sorted(unused)[:5]))

    if creatives is not None:
        known = set(creatives["creative_id"].dropna())
        used = set(perf["creative_id"].dropna())
        orphans = used - known
        print("  orphan creative_ids : {} {}".format(len(orphans), sorted(orphans)[:10]))
        if orphans:
            n = int(perf["creative_id"].isin(orphans).sum())
            note("MEDIUM", "{} creative_ids in perf have no creatives.csv row ({} rows)".format(len(orphans), n))

    if creatives is not None and campaigns is not None:
        known = set(campaigns["campaign_id"].dropna())
        bad = set(creatives["campaign_id"].dropna()) - known
        if bad:
            note("MEDIUM", "creatives.csv points at {} unknown campaign_ids: {}".format(len(bad), sorted(bad)[:5]))


def check_currency(perf, campaigns, fx):
    """The single biggest correctness trap: spend and revenue are in mixed currencies."""
    rule("CURRENCY AND FX COVERAGE  (the main correctness trap)")
    if perf is None:
        return

    sub("currency mix in ad_performance_daily")
    print(perf["currency"].value_counts(dropna=False).to_string())

    if campaigns is not None:
        merged = perf.merge(
            campaigns[["campaign_id", "currency"]],
            on="campaign_id", how="left", suffixes=("_perf", "_camp"),
        )
        mism = merged[
            merged["currency_perf"].notna()
            & merged["currency_camp"].notna()
            & (merged["currency_perf"] != merged["currency_camp"])
        ]
        print("\n  rows where perf.currency != campaigns.currency: " + str(len(mism)))
        if len(mism):
            note("HIGH", "{} rows disagree with campaigns.csv on currency - pick an authoritative "
                         "side and document it".format(len(mism)))
            print(mism[["date", "campaign_id", "currency_perf", "currency_camp"]].head(8).to_string(index=False))

    if fx is None:
        note("BLOCKER", "fx_rates.csv absent - spend and revenue cannot be normalised")
        return

    sub("fx_rates coverage")
    fx_pairs = set(zip(fx["date"], fx["currency"]))
    need = perf[["date", "currency"]].dropna().drop_duplicates()
    need_pairs = set(zip(need["date"], need["currency"]))
    missing = need_pairs - fx_pairs
    print("  distinct (date,currency) needed : " + str(len(need_pairs)))
    print("  present in fx_rates             : " + str(len(need_pairs & fx_pairs)))
    print("  MISSING                         : " + str(len(missing)))
    if missing:
        note("HIGH", "{} (date,currency) pairs have no FX rate - an INNER JOIN silently drops that "
                     "spend. Decide on a fill rule (forward-fill the last known rate) and state "
                     "it in DESIGN.md".format(len(missing)))
        for pair in sorted(missing)[:10]:
            print("    {}  {}".format(pair[0], pair[1]))

    fxd = to_date(fx["date"])
    rates = to_num(fx["rate_to_usd"])
    sub("fx_rates sanity")
    print("  date range: {} .. {}   rows: {}".format(fxd.min(), fxd.max(), len(fx)))
    for cur, grp in fx.assign(_d=fxd, _r=rates).groupby("currency"):
        gaps = pd.date_range(grp["_d"].min(), grp["_d"].max(), freq="D").difference(grp["_d"])
        print("  {:5s} rate min={:.6g} max={:.6g} rows={} calendar_gaps={}".format(
            cur, grp["_r"].min(), grp["_r"].max(), len(grp), len(gaps)))
        if len(gaps):
            note("HIGH", "fx_rates has {} missing calendar days for {} (weekends/holidays) - "
                         "forward-fill required".format(len(gaps), cur))
        if cur == "USD" and not ((grp["_r"] - 1).abs() < 1e-9).all():
            note("MEDIUM", "USD rate_to_usd is not exactly 1.0 on every row")
    dup = int(fx.duplicated(subset=["date", "currency"]).sum())
    if dup:
        note("HIGH", "fx_rates has {} duplicate (date,currency) rows - the join will fan out and "
                     "inflate spend".format(dup))


def check_volume(perf, dates):
    """Question 3 of the brief asks why conversions fell off a cliff on the last day."""
    rule("DAILY VOLUME  (question 3: conversions fell off a cliff)")
    if perf is None:
        return

    df = perf.assign(
        _d=dates,
        _imp=to_num(perf["impressions"]),
        _cnv=to_num(perf["conversions"]),
        _spd=to_num(perf["spend"]),
    )
    daily = df.groupby("_d").agg(
        rows=("_d", "size"),
        campaigns=("campaign_id", "nunique"),
        impressions=("_imp", "sum"),
        conversions=("_cnv", "sum"),
        spend=("_spd", "sum"),
    )

    sub("last 10 days")
    print(daily.tail(10).to_string())

    if len(daily) > 14:
        tail = daily.iloc[-1]
        base = daily.iloc[-15:-1]
        sub("last day vs prior 14-day median")
        for col in ("rows", "campaigns", "impressions", "conversions", "spend"):
            med = base[col].median()
            ratio = (tail[col] / med) if med else float("nan")
            flag = "  <-- LOOKS PARTIAL" if med and ratio < 0.6 else ""
            print("  {:12s} last={:>12,.0f}  median={:>12,.0f}  ratio={:5.2f}{}".format(
                col, tail[col], med, ratio, flag))
            if med and ratio < 0.6:
                note("HIGH", "last day ({}) has {} at {:.0%} of the 14-day median - almost certainly "
                             "a partial extract, not a real drop. This is the answer to question 3.".format(
                                 daily.index[-1].date(), col, ratio))

    # Dates far from the bulk of the data are almost always a parsing casualty
    # (DD/MM read as MM/DD) rather than a genuine record. Flag them before the gap
    # check, or a handful of stray rows invent hundreds of phantom missing days.
    sub("out-of-range dates")
    core_lo, core_hi = daily.index.to_series().quantile([0.02, 0.98])
    span = core_hi - core_lo
    strays = daily.index[(daily.index < core_lo - span * 0.25) | (daily.index > core_hi + span * 0.25)]
    print("  bulk of the data sits in {} .. {}".format(core_lo.date(), core_hi.date()))
    print("  dates outside that span: {}".format(len(strays)))
    if len(strays):
        n_rows = int(daily.loc[strays, "rows"].sum())
        note("HIGH", "{} rows sit on {} date(s) far outside the extract window ({}) - "
                     "these are almost certainly DD/MM dates parsed as MM/DD, which moves a row "
                     "to a different month without erroring".format(
                         n_rows, len(strays), ", ".join(str(d.date()) for d in strays[:6])))
        daily = daily.drop(index=strays)

    gaps = pd.date_range(daily.index.min(), daily.index.max(), freq="D").difference(daily.index)
    sub("missing days within the extract window")
    print("  {} calendar days with zero rows".format(len(gaps)))
    if len(gaps):
        note("MEDIUM", "{} days inside the window have no rows at all: {} - a daily average "
                       "over this period is wrong unless these are handled".format(
                           len(gaps), [str(g.date()) for g in gaps[:8]]))


def check_values(perf):
    """Negative money, funnel violations and other physically impossible rows."""
    rule("VALUE SANITY")
    if perf is None:
        return

    num = pd.DataFrame({
        "impressions": to_num(perf["impressions"]),
        "clicks": to_num(perf["clicks"]),
        "conversions": to_num(perf["conversions"]),
        "spend": to_num(perf["spend"]),
        "revenue": to_num(perf["revenue"]),
    })
    sub("numeric summary")
    print(num.describe().T.to_string())

    for col in num.columns:
        unparsed = int(num[col].isna().sum() - perf[col].isna().sum())
        if unparsed > 0:
            note("HIGH", "{} has {} non-numeric values".format(col, unparsed))
            print("  non-numeric " + col + ": ",
                  perf.loc[num[col].isna() & perf[col].notna(), col].head(5).tolist())

    sub("impossible values")
    checks = {
        "negative spend": num["spend"] < 0,
        "negative revenue": num["revenue"] < 0,
        "negative conversions": num["conversions"] < 0,
        "clicks > impressions": num["clicks"] > num["impressions"],
        "conversions > clicks": num["conversions"] > num["clicks"],
        "spend > 0 with 0 impressions": (num["spend"] > 0) & (num["impressions"] == 0),
        "revenue > 0 with 0 conversions": (num["revenue"] > 0) & (num["conversions"] == 0),
        "non-integer impressions": num["impressions"].mod(1).ne(0) & num["impressions"].notna(),
    }
    for label, mask in checks.items():
        n = int(mask.fillna(False).sum())
        print("  {:32s} {:>6}".format(label, n))
        if n:
            sev = "HIGH" if "negative" in label else "MEDIUM"
            note(sev, "{} rows: {}".format(n, label))

    sub("zero-denominator risk (ROAS / CPA / CTR)")
    print("  rows with spend == 0        : " + str(int((num["spend"] == 0).sum())))
    print("  rows with impressions == 0  : " + str(int((num["impressions"] == 0).sum())))
    print("  rows with conversions == 0  : " + str(int((num["conversions"] == 0).sum())))
    note("INFO", "guard every ratio metric against a zero denominator before reporting it")


def check_categoricals(campaigns, creatives):
    rule("CATEGORICAL VOCABULARY  (typos, casing, stray whitespace)")
    expected = {
        "channel": {"google_search", "meta", "youtube", "linkedin"},
        "objective": {"conversions", "traffic", "awareness"},
        "currency": {"INR", "USD"},
    }
    for name, df in (("campaigns.csv", campaigns), ("creatives.csv", creatives)):
        if df is None:
            continue
        for col in ("channel", "objective", "currency", "format"):
            if col not in df.columns:
                continue
            vc = df[col].value_counts(dropna=False)
            print("\n  {}.{}:".format(name, col))
            print("    " + vc.to_string().replace("\n", "\n    "))
            vals = set(df[col].dropna())
            if col in expected:
                unexpected = vals - expected[col]
                if unexpected:
                    note("MEDIUM", "{}.{} has values outside the documented set: {}".format(
                        name, col, sorted(unexpected)))
            stray = [v for v in vals if v != v.strip()]
            if stray:
                note("MEDIUM", "{}.{} has values with stray whitespace: {}".format(name, col, stray[:5]))


def check_campaign_windows(perf, campaigns, dates):
    rule("CAMPAIGN WINDOWS")
    if perf is None or campaigns is None:
        return
    c = campaigns.assign(_s=to_date(campaigns["start_date"]), _e=to_date(campaigns["end_date"]))
    still_running = int(campaigns["end_date"].isna().sum())
    print("  campaigns: {}   still running (blank end_date): {}".format(len(c), still_running))
    print("  start_date range: {} .. {}".format(c["_s"].min(), c["_s"].max()))
    print("  end_date   range: {} .. {}".format(c["_e"].min(), c["_e"].max()))

    bad_window = c[c["_e"].notna() & c["_s"].notna() & (c["_e"] < c["_s"])]
    if len(bad_window):
        note("HIGH", "{} campaigns have end_date before start_date".format(len(bad_window)))

    m = perf.assign(_d=dates).merge(c[["campaign_id", "_s", "_e"]], on="campaign_id", how="left")
    before = int((m["_d"] < m["_s"]).sum())
    after = int((m["_e"].notna() & (m["_d"] > m["_e"])).sum())
    print("  perf rows dated before campaign start: " + str(before))
    print("  perf rows dated after  campaign end  : " + str(after))
    if before or after:
        note("MEDIUM", "{} performance rows fall outside their campaign declared window".format(before + after))

    if "daily_budget" in c.columns:
        b = to_num(c["daily_budget"])
        print("  daily_budget: nulls={} min={} max={}".format(int(b.isna().sum()), b.min(), b.max()))
        if (b <= 0).any():
            note("MEDIUM", "{} campaigns have a non-positive daily_budget".format(int((b <= 0).sum())))


def check_competitors(frames):
    rule("QUESTION 5 CHECK  (how does our spend compare to competitors)")
    hits = []
    for name, df in frames.items():
        for col in df.columns:
            if any(k in col.lower() for k in ("compet", "benchmark", "market", "industry", "peer")):
                hits.append(name + "." + col)
    if hits:
        print("  possible competitor columns found:", hits)
        note("INFO", "competitor-like columns exist - re-read before assuming question 5 is unanswerable")
    else:
        print("  No competitor, benchmark, market or industry column exists in any file.")
        print("  Question 5 is UNANSWERABLE from this dataset. The agent must refuse and say why.")
        note("INFO", "confirmed: question 5 has no supporting data - the correct output is a "
                     "refusal that names the missing input, not an estimate")


def summary():
    rule("SUMMARY OF FINDINGS")
    order = {"BLOCKER": 0, "HIGH": 1, "MEDIUM": 2, "INFO": 3}
    if not FINDINGS:
        print("  none - which would itself be suspicious given the brief.")
        return
    for sev in sorted({f[0] for f in FINDINGS}, key=lambda s: order.get(s, 9)):
        items = [m for s, m in FINDINGS if s == sev]
        print("\n  [{}] {}".format(sev, len(items)))
        for m in items:
            print("    - " + m)
    print("\n  {} findings total. Each one needs either a documented handling rule in "
          "DESIGN.md or a guard in the code.".format(len(FINDINGS)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data", help="directory holding the four CSVs")
    args = ap.parse_args()
    data_dir = Path(args.data)

    rule("DATA INVENTORY  (" + str(data_dir.resolve()) + ")")
    if not data_dir.exists():
        print("  Directory does not exist: " + str(data_dir.resolve()))
        print("  Drop the four CSVs in there and re-run.")
        return 1

    frames = load(data_dir)
    perf = frames.get("ad_performance_daily.csv")
    campaigns = frames.get("campaigns.csv")
    creatives = frames.get("creatives.csv")
    fx = frames.get("fx_rates.csv")

    rule("COLUMN PROFILES")
    for name, df in frames.items():
        profile(name, df)

    rule("GRAIN AND DUPLICATES")
    if perf is not None:
        check_dupes("ad_performance_daily.csv", perf, ["date", "campaign_id", "creative_id"])
    if campaigns is not None:
        check_dupes("campaigns.csv", campaigns, ["campaign_id"])
    if creatives is not None:
        check_dupes("creatives.csv", creatives, ["creative_id"])
    if fx is not None:
        check_dupes("fx_rates.csv", fx, ["date", "currency"])

    rule("DATE RANGES")
    dates = None
    if perf is not None:
        dates = check_dates("ad_performance_daily.csv", perf, "date")
    if campaigns is not None:
        check_dates("campaigns.csv", campaigns, "start_date")
    if creatives is not None and "launched_on" in creatives.columns:
        check_dates("creatives.csv", creatives, "launched_on")

    check_referential(perf, campaigns, creatives)
    check_currency(perf, campaigns, fx)
    if perf is not None and dates is not None:
        check_volume(perf, dates)
    check_values(perf)
    check_categoricals(campaigns, creatives)
    if dates is not None:
        check_campaign_windows(perf, campaigns, dates)
    check_competitors(frames)
    summary()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
