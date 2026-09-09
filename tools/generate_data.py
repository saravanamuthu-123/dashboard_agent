"""
Generate the synthetic ad-performance extract for the dashboard agent.

The dataset files were not supplied with the brief; the schema was. This script
builds an extract that matches the appendix schema and, deliberately, carries the
defects the brief attributes to the real extract:

    "this is shaped like a real extract, and it has the defects real ad data has.
     We did not clean it up for you. Part of the exercise is what you do about that."

Clean data would remove the only interesting part of the exercise, so every defect
below is planted on purpose, with a note on the real-world mechanism that produces
it. See DATA.md for the full register.

The generator shares no code with the agent. tools/audit_data.py rediscovers the
defects from the CSVs alone, without reference to this file.

Deterministic: same seed, byte-identical output.

Usage:
    python tools/generate_data.py                  # writes ./data
    python tools/generate_data.py --out data --seed 20260908
"""

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

START = pd.Timestamp("2026-06-08")
END = pd.Timestamp("2026-09-04")

# Per-channel economics, all in USD. Ad platforms differ enormously: search
# catches people already shopping, video catches people watching something else.
CHANNEL = {
    "google_search": {"cpc": 1.20, "ctr": 0.045},
    "meta":          {"cpc": 0.55, "ctr": 0.012},
    "youtube":       {"cpc": 0.30, "ctr": 0.006},
    "linkedin":      {"cpc": 4.50, "ctr": 0.006},
}

# Click-to-conversion rate by channel, before the objective modifier.
BASE_CVR = {"google_search": 0.050, "meta": 0.022, "youtube": 0.010, "linkedin": 0.030}

# An awareness campaign is not trying to convert. Judging it on ROAS is the
# classic mistake, and question 4 is built to see whether the agent makes it.
OBJECTIVE_CVR = {"conversions": 1.00, "traffic": 0.30, "awareness": 0.10}

# Campaign roster. budget_usd is the daily budget in USD; it is written out in the
# campaign's own currency. target_roas drives revenue. The `role` field records why
# each campaign exists in the fixture and is not written to the CSVs.
SPECS = [
    dict(id="C001", name="Brand Search - India",     channel="google_search", objective="conversions",
         currency="INR", budget_usd=180, roas=3.2, start=START, end=None, creatives=3,
         role="Q2 NAIVE answer: largest raw INR revenue, third once converted"),
    dict(id="C002", name="Brand Search - US",        channel="google_search", objective="conversions",
         currency="USD", budget_usd=400, roas=3.8, start=START, end=None, creatives=3,
         role="Q2 CORRECT answer: highest revenue this quarter once FX is applied"),
    dict(id="C003", name="Retargeting - Meta IN",    channel="meta",          objective="conversions",
         currency="INR", budget_usd=145, roas=2.4, start=START, end=None, creatives=2,
         role="solid mid performer"),
    dict(id="C004", name="Prospecting - Meta US",    channel="meta",          objective="conversions",
         currency="USD", budget_usd=350, roas=1.6, start=START, end=None, creatives=3,
         role="marginal but profitable"),
    dict(id="C005", name="Festive Push - Meta IN",   channel="meta",          objective="conversions",
         currency="INR", budget_usd=300, roas=1.1, start=pd.Timestamp("2026-07-01"), end=None, creatives=3,
         role="high-spend INR campaign, hovers at break-even"),
    dict(id="C006", name="YouTube Awareness Q3",     channel="youtube",       objective="awareness",
         currency="USD", budget_usd=150, roas=0.08, start=pd.Timestamp("2026-07-01"), end=None, creatives=2,
         role="Q4 TRAP: awareness objective, ROAS near zero by design"),
    dict(id="C007", name="LinkedIn B2B Leads",       channel="linkedin",      objective="conversions",
         currency="USD", budget_usd=250, roas=0.45, start=START, end=None, creatives=2,
         role="Q4 CORRECT answer: conversions objective, long history, high spend, ROAS 0.45"),
    dict(id="C008", name="Traffic Blast - YouTube",  channel="youtube",       objective="traffic",
         currency="INR", budget_usd=95, roas=0.30, start=START, end=None, creatives=2,
         role="Q4 TRAP: traffic objective, ROAS irrelevant"),
    dict(id="C009", name="Summer Sale - Meta IN",    channel="meta",          objective="conversions",
         currency="INR", budget_usd=120, roas=1.9, start=START, end=pd.Timestamp("2026-08-15"), creatives=2,
         role="Q4 TRAP: already ended, cannot be turned off"),
    dict(id="C010", name="New Product Test",         channel="google_search", objective="conversions",
         currency="USD", budget_usd=40, roas=0.60, start=pd.Timestamp("2026-08-25"), end=None, creatives=2,
         role="Q4 TRAP: 11 days old, tiny volume, not yet decidable"),
    dict(id="C011", name="LinkedIn Awareness",       channel="linkedin",      objective="awareness",
         currency="USD", budget_usd=120, roas=0.02, start=START, end=None, creatives=2,
         role="Q4 TRAP: awareness objective"),
    dict(id="C012", name="Search Generic - India",   channel="google_search", objective="conversions",
         currency="INR", budget_usd=110, roas=1.4, start=START, end=None, creatives=2,
         role="mid performer"),
    dict(id="C013", name="YouTube Product Demo",     channel="youtube",       objective="conversions",
         currency="USD", budget_usd=200, roas=1.05, start=pd.Timestamp("2026-06-15"), end=None, creatives=2,
         role="borderline, hovers around break-even"),
    dict(id="C014", name="Meta Lookalike IN",        channel="meta",          objective="conversions",
         currency="INR", budget_usd=85, roas=2.1, start=pd.Timestamp("2026-06-20"), end=None, creatives=2,
         role="good performer, INR"),
]

FORMATS = ["image", "video", "carousel"]
HEADLINES = [
    "Save 30% This Week", "Built For Growing Teams", "Try It Free For 14 Days",
    "The Faster Way To Ship", "Trusted By 10,000 Teams", "Your Ads, Automated",
    "Cut Your Cost Per Lead", "See It In Action",
]

DEFECT_LOG = []


def defect(code, count, what, why):
    DEFECT_LOG.append({"code": code, "rows": count, "defect": what, "real_world_cause": why})


def build_fx(dates, rng):
    """Daily INR/USD rates, with no rows on weekends.

    FX providers do not publish on days the currency markets are shut. Any join
    from a 7-day performance table onto a 5-day rate table therefore loses two
    days in seven unless the consumer forward-fills. This is the most common way
    a spend total comes out quietly low.
    """
    rows = []
    rate = 0.01193
    for d in dates:
        rate = float(np.clip(rate + rng.normal(0, 0.000035), 0.01155, 0.01225))
        if d.weekday() >= 5:
            continue
        rows.append({"date": d.strftime("%Y-%m-%d"), "currency": "USD", "rate_to_usd": 1.0})
        rows.append({"date": d.strftime("%Y-%m-%d"), "currency": "INR", "rate_to_usd": round(rate, 6)})
    fx = pd.DataFrame(rows)
    skipped = sum(1 for d in dates if d.weekday() >= 5)
    defect("D01", skipped * 2,
           "fx_rates has no rows for weekends",
           "FX markets are closed Sat/Sun; rate feeds publish nothing. Consumers must forward-fill.")
    return fx


def rate_lookup(fx):
    """Forward-filled rate per (date, currency), used internally to express USD
    figures in each campaign's own currency. The agent has to rebuild this itself."""
    full = pd.date_range(START, END, freq="D")
    out = {}
    for cur in ("USD", "INR"):
        sub = fx[fx["currency"] == cur].copy()
        sub["date"] = pd.to_datetime(sub["date"])
        s = sub.set_index("date")["rate_to_usd"].reindex(full).ffill().bfill()
        out[cur] = s
    return out


def build_campaigns(rates):
    """campaigns.csv - daily_budget is written in the campaign's own currency."""
    rows = []
    for s in SPECS:
        r = rates[s["currency"]].loc[START]
        rows.append({
            "campaign_id": s["id"],
            "campaign_name": s["name"],
            "channel": s["channel"],
            "objective": s["objective"],
            "currency": s["currency"],
            "daily_budget": round(s["budget_usd"] / r, 0) if s["currency"] == "INR" else round(s["budget_usd"], 2),
            "start_date": s["start"].strftime("%Y-%m-%d"),
            "end_date": s["end"].strftime("%Y-%m-%d") if s["end"] is not None else "",
        })
    return pd.DataFrame(rows)


def build_creatives(rng):
    rows = []
    n = 0
    for s in SPECS:
        for k in range(s["creatives"]):
            n += 1
            launch = s["start"] + pd.Timedelta(days=int(rng.integers(0, 6)) if k else 0)
            rows.append({
                "creative_id": "K{:03d}".format(n),
                "campaign_id": s["id"],
                "creative_name": "{} v{}".format(s["name"].split(" - ")[0], k + 1),
                "format": FORMATS[(n + k) % 3],
                "headline": HEADLINES[n % len(HEADLINES)],
                "launched_on": launch.strftime("%Y-%m-%d"),
            })
    return pd.DataFrame(rows)


def build_performance(creatives, rates, rng):
    """One row per campaign / creative / day, in the campaign's own currency.

    Everything is modelled in USD and then expressed in local currency, so the
    correct FX-normalised answer is exactly recoverable and can be asserted in a test.
    """
    by_campaign = {s["id"]: s for s in SPECS}
    dates = pd.date_range(START, END, freq="D")
    rows = []

    # Per-creative quality multiplier: within one campaign, creatives differ.
    quality = {c: float(rng.uniform(0.75, 1.30)) for c in creatives["creative_id"]}

    for d in dates:
        for _, cr in creatives.iterrows():
            s = by_campaign[cr["campaign_id"]]
            if d < s["start"] or (s["end"] is not None and d > s["end"]):
                continue
            if d < pd.Timestamp(cr["launched_on"]):
                continue

            ch = CHANNEL[s["channel"]]
            # LinkedIn is B2B and dies at the weekend; consumer channels lift slightly.
            weekend = d.weekday() >= 5
            season = (0.55 if weekend else 1.12) if s["channel"] == "linkedin" else (1.06 if weekend else 0.98)
            # Mild upward drift over the period.
            trend = 1.0 + 0.0015 * (d - START).days

            share = quality[cr["creative_id"]]
            total_q = sum(quality[c] for c in creatives.loc[
                creatives["campaign_id"] == s["id"], "creative_id"])
            budget = s["budget_usd"] * (share / total_q)

            spend_usd = budget * season * trend * float(rng.uniform(0.72, 1.02))
            cpc = ch["cpc"] * float(rng.uniform(0.85, 1.18))
            clicks = spend_usd / cpc
            impressions = clicks / (ch["ctr"] * float(rng.uniform(0.8, 1.25)))
            cvr = BASE_CVR[s["channel"]] * OBJECTIVE_CVR[s["objective"]] * float(rng.uniform(0.7, 1.35))
            conversions = clicks * cvr
            revenue_usd = spend_usd * s["roas"] * float(rng.uniform(0.6, 1.45))
            # Not every day produces a sale on low-volume campaigns. Revenue follows
            # conversions, except for a small slice where a conversion attributed to
            # an earlier day settles today - real, and worth not "correcting" away.
            if round(conversions) == 0:
                conversions = 0.0
                if rng.random() < 0.05:
                    revenue_usd = revenue_usd * float(rng.uniform(0.1, 0.4))
                else:
                    revenue_usd = 0.0

            r = rates[s["currency"]].loc[d]
            rows.append({
                "date": d.strftime("%Y-%m-%d"),
                "campaign_id": s["id"],
                "creative_id": cr["creative_id"],
                "impressions": int(round(impressions)),
                "clicks": int(round(clicks)),
                "conversions": int(round(conversions)),
                "spend": round(spend_usd / r, 2),
                "revenue": round(revenue_usd / r, 2),
                "currency": s["currency"],
            })
    return pd.DataFrame(rows)


def inject_defects(perf, campaigns, fx, rng):
    """Every defect below is deliberate. Each carries the mechanism that causes it
    in a real extract, so the handling rule can be argued rather than guessed."""

    # D02 - the final day is a partial extract. The pull ran mid-morning, and
    # conversions lag hardest because attribution settles hours after the click.
    # This is the whole of question 3: the cliff is an artefact, not a collapse.
    last = perf["date"] == END.strftime("%Y-%m-%d")
    idx = perf.index[last]
    keep = rng.choice(idx, size=int(len(idx) * 0.38), replace=False)
    perf = perf.drop(index=[i for i in idx if i not in set(keep)]).reset_index(drop=True)
    last = perf["date"] == END.strftime("%Y-%m-%d")
    perf.loc[last, "conversions"] = (perf.loc[last, "conversions"] * 0.12).round().astype(int)
    perf.loc[last, "revenue"] = (perf.loc[last, "revenue"] * 0.12).round(2)
    defect("D02", int(last.sum()),
           "final day holds ~38% of the usual rows, with conversions and revenue scaled to ~12%",
           "Extract pulled mid-day; conversion attribution settles hours to days after the click.")

    # D03 - a two-day reporting outage. Platform APIs go down and the backfill
    # never happens, so the series simply has a hole in it.
    gap = ["2026-07-19", "2026-07-20"]
    n_gap = int(perf["date"].isin(gap).sum())
    perf = perf[~perf["date"].isin(gap)].reset_index(drop=True)
    defect("D03", n_gap,
           "no rows at all for 19-20 July",
           "Reporting API outage with no backfill. A daily average over the window is wrong unless this is noticed.")

    # D04 - the ETL job was re-run and a slice of rows landed twice.
    dupes = perf.sample(n=18, random_state=11)
    perf = pd.concat([perf, dupes], ignore_index=True)
    defect("D04", len(dupes),
           "18 fully duplicated rows",
           "ETL re-run after a partial failure, appending instead of replacing. Doubles those rows in any SUM.")

    # D05 - platform credits for invalid traffic arrive as negative spend.
    neg = rng.choice(perf.index, size=6, replace=False)
    perf.loc[neg, "spend"] = -perf.loc[neg, "spend"].abs() * 0.25
    defect("D05", len(neg),
           "6 rows carry negative spend",
           "Refunds/credits for bot traffic are posted as negative adjustments against the original day.")

    # D06 - impressions filtered for bots after the fact, clicks not.
    bad = rng.choice(perf.index, size=4, replace=False)
    perf.loc[bad, "clicks"] = perf.loc[bad, "impressions"] + rng.integers(3, 40, size=4)
    defect("D06", len(bad),
           "4 rows where clicks exceed impressions",
           "Bot filtering applied to impressions but not clicks; also happens with cross-device attribution.")

    # D07 - revenue arrives from a separate billing system that sometimes has nothing to say.
    nulls = rng.choice(perf.index, size=23, replace=False)
    perf.loc[nulls, "revenue"] = np.nan
    defect("D07", len(nulls),
           "23 rows with null revenue",
           "Revenue is joined from the order system; unmatched or pending orders come back empty. "
           "Null is not zero, and treating it as zero understates ROAS.")

    # D08 - a campaign deleted in the platform after the fact still has history.
    orph = rng.choice(perf.index, size=9, replace=False)
    perf.loc[orph, "campaign_id"] = "C099"
    defect("D08", len(orph),
           "9 rows reference campaign_id C099, absent from campaigns.csv",
           "Campaign deleted in the ad platform after performance was exported. An INNER JOIN drops the spend silently.")

    # D09 - the file was opened in Excel under a different locale and re-saved.
    # Restricted to days 1-12 on purpose: those are the genuinely dangerous ones.
    # "03/08/2026" is 3 August, but a MM/DD parser reads it as 8 March and moves the
    # row three months without raising anything. Days 13-31 are safe by comparison,
    # because MM/DD is impossible and any parser falls back to the right reading.
    day_le_12 = perf.index[pd.to_datetime(perf["date"], errors="coerce").dt.day <= 12]
    fmt = rng.choice(day_le_12, size=7, replace=False)
    perf.loc[fmt, "date"] = pd.to_datetime(perf.loc[fmt, "date"]).dt.strftime("%d/%m/%Y")
    defect("D09", len(fmt),
           "7 dates written as DD/MM/YYYY, all with day <= 12",
           "CSV opened and re-saved in Excel under a regional setting. These parse without error "
           "under MM/DD and land in the wrong month - corruption with no exception to catch.")

    # D10 - channel written inconsistently by a manual edit.
    campaigns.loc[campaigns["campaign_id"] == "C003", "channel"] = "Meta "
    campaigns.loc[campaigns["campaign_id"] == "C013", "channel"] = "YouTube"
    defect("D10", 2,
           "channel values 'Meta ' and 'YouTube' break the documented lower_snake vocabulary",
           "Hand-edited rows. A GROUP BY channel splits one channel into two buckets and halves the reported spend.")

    # D11 - the performance row and the campaign record disagree on currency.
    mism = perf.index[perf["campaign_id"] == "C012"][:12]
    perf.loc[mism, "currency"] = "USD"
    defect("D11", len(mism),
           "12 rows of C012 (an INR campaign) are tagged USD",
           "Campaign currency changed mid-flight in the platform and old rows were not restated. "
           "Converting on the row tag inflates this campaign ~84x.")

    # D12 - duplicated FX rows fan out the join.
    dup_fx = fx[(fx["date"] == "2026-08-03") & (fx["currency"] == "INR")]
    fx = pd.concat([fx, dup_fx], ignore_index=True).sort_values(["date", "currency"]).reset_index(drop=True)
    defect("D12", len(dup_fx),
           "fx_rates has a duplicate (2026-08-03, INR) row",
           "Rate feed delivered twice. A join on (date,currency) fans out and doubles that day's INR spend.")

    # D13 - a spend spike from a budget cap being lifted, left in as a genuine outlier.
    spike = perf.index[(perf["campaign_id"] == "C005") & (perf["date"] == "2026-08-09")]
    perf.loc[spike, "spend"] = perf.loc[spike, "spend"] * 6.5
    perf.loc[spike, "revenue"] = perf.loc[spike, "revenue"] * 1.4
    defect("D13", len(spike),
           "C005 spend on 2026-08-09 is ~6.5x its normal day",
           "Budget cap lifted for a festival flash sale. A real event, not an error - the agent should not silently drop it.")

    perf = perf.sample(frac=1.0, random_state=5).reset_index(drop=True)
    return perf, campaigns, fx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data")
    ap.add_argument("--seed", type=int, default=20260908)
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    dates = pd.date_range(START, END, freq="D")
    fx = build_fx(dates, rng)
    rates = rate_lookup(fx)
    campaigns = build_campaigns(rates)
    creatives = build_creatives(rng)
    perf = build_performance(creatives, rates, rng)
    perf, campaigns, fx = inject_defects(perf, campaigns, fx, rng)

    perf.to_csv(out / "ad_performance_daily.csv", index=False)
    campaigns.to_csv(out / "campaigns.csv", index=False)
    creatives.to_csv(out / "creatives.csv", index=False)
    fx.to_csv(out / "fx_rates.csv", index=False)

    print("wrote to {}/".format(out))
    print("  ad_performance_daily.csv  {:>6,} rows".format(len(perf)))
    print("  campaigns.csv             {:>6,} rows".format(len(campaigns)))
    print("  creatives.csv             {:>6,} rows".format(len(creatives)))
    print("  fx_rates.csv              {:>6,} rows".format(len(fx)))
    print("\ndeliberate defects planted:")
    print(pd.DataFrame(DEFECT_LOG)[["code", "rows", "defect"]].to_string(index=False))
    print("\nsee DATA.md for the mechanism behind each one")


if __name__ == "__main__":
    main()
