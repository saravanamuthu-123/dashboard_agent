-- Curated view over the raw CSV extract.
--
-- Every defect in the extract is handled here, once, in one readable place.
-- Nothing downstream - no generated query, no chart, no LLM - ever touches a raw
-- CSV. By the time anything else runs, money is in USD, dates are real dates, and
-- duplicates are gone.
--
-- Defect codes (D01..D13) refer to the register in DATA.md.
--
-- ${DATA} is replaced with the data directory path before execution.
-- Views are created in dependency order.


-- ---------------------------------------------------------------------------
-- 1. Raw files, read as text.
--
-- all_varchar=true on purpose. Letting the CSV reader infer types means it
-- silently turns unparseable values into NULL before we ever see them. We want
-- to decide what happens to every bad value ourselves, so everything arrives as
-- text and is cast deliberately below.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW raw_performance AS
    SELECT * FROM read_csv('${DATA}/ad_performance_daily.csv', all_varchar = true);

CREATE OR REPLACE VIEW raw_campaigns AS
    SELECT * FROM read_csv('${DATA}/campaigns.csv', all_varchar = true);

CREATE OR REPLACE VIEW raw_creatives AS
    SELECT * FROM read_csv('${DATA}/creatives.csv', all_varchar = true);

CREATE OR REPLACE VIEW raw_fx AS
    SELECT * FROM read_csv('${DATA}/fx_rates.csv', all_varchar = true);


-- ---------------------------------------------------------------------------
-- 2. Campaigns.
--
-- D10: channel arrives as "Meta " and "YouTube" in two rows, against a documented
-- vocabulary of lower_snake_case. Left alone, GROUP BY channel reports "meta" and
-- "Meta " as two separate channels and halves the spend attributed to each.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW campaigns_clean AS
SELECT
    campaign_id,
    campaign_name,
    -- trim, lowercase, and collapse spaces to underscores
    replace(lower(trim(channel)), ' ', '_')                     AS channel,
    lower(trim(objective))                                      AS objective,
    upper(trim(currency))                                       AS currency,
    try_cast(daily_budget AS DOUBLE)                            AS daily_budget,
    try_strptime(start_date, '%Y-%m-%d')::DATE                  AS start_date,
    -- blank end_date means the campaign is still running
    try_strptime(end_date, '%Y-%m-%d')::DATE                    AS end_date,
    end_date IS NULL                                            AS is_running
FROM raw_campaigns;


-- ---------------------------------------------------------------------------
-- 3. Performance rows: deduplicated and dated.
--
-- D04: 18 rows are exact duplicates, left behind by an ETL re-run that appended
--      instead of replacing. SELECT DISTINCT removes them. This is safe precisely
--      because the grain is one row per campaign/creative/day - two byte-identical
--      rows cannot both be legitimate.
--
-- D09: 7 dates are written DD/MM/YYYY instead of ISO, all with day <= 12.
--      Those are the dangerous ones: "11/07/2026" is 11 July but reads perfectly
--      as 7 November under a MM/DD parser, with no error raised.
--
--      We do not guess. We try each format we know about, in a fixed order, and
--      anything matching neither becomes NULL and is reported rather than kept.
--      Being explicit is the entire defence here - a generic date parser gets
--      these wrong silently.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW performance_dated AS
SELECT
    *,
    COALESCE(
        try_strptime(trim(date), '%Y-%m-%d'),     -- 2026-08-14
        try_strptime(trim(date), '%d/%m/%Y')      -- 14/08/2026  (D09)
    )::DATE                                               AS day,
    -- flag rather than fix, so the audit can report how many needed the fallback
    NOT regexp_matches(trim(date), '^\d{4}-\d{2}-\d{2}$') AS date_was_non_iso
FROM (SELECT DISTINCT * FROM raw_performance);


-- ---------------------------------------------------------------------------
-- 4. Exchange rates, made dense.
--
-- D12: the feed delivered (2026-08-03, INR) twice. Joining against a table with
--      duplicate keys fans out - every matching performance row would appear
--      twice and that day's INR spend would double. SELECT DISTINCT first.
--
-- D01: there are no rows at all for Saturdays and Sundays, because currency
--      markets are closed. Performance data has all seven days. An inner join
--      would silently drop two days in seven of spend.
--
-- The fix is a dense calendar (every day x every currency) left-joined to the
-- rates we have, then forward-filled: each gap inherits the most recent known
-- rate, which is what a bank would use to settle a weekend transaction.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW fx_deduped AS
    SELECT DISTINCT
        try_strptime(date, '%Y-%m-%d')::DATE  AS day,
        upper(trim(currency))                 AS currency,
        try_cast(rate_to_usd AS DOUBLE)       AS rate_to_usd
    FROM raw_fx;

CREATE OR REPLACE VIEW fx_dense AS
WITH bounds AS (
    -- span the whole period the data covers, from either source
    SELECT
        least(
            (SELECT min(day) FROM performance_dated),
            (SELECT min(day) FROM fx_deduped)
        ) AS lo,
        greatest(
            (SELECT max(day) FROM performance_dated),
            (SELECT max(day) FROM fx_deduped)
        ) AS hi
),
calendar AS (
    SELECT UNNEST(generate_series(lo, hi, INTERVAL 1 DAY))::DATE AS day
    FROM bounds
),
currencies AS (
    SELECT DISTINCT currency FROM fx_deduped WHERE currency IS NOT NULL
),
grid AS (
    -- one row per day per currency, whether or not a rate was published
    SELECT c.day, u.currency, f.rate_to_usd
    FROM calendar c
    CROSS JOIN currencies u
    LEFT JOIN fx_deduped f ON f.day = c.day AND f.currency = u.currency
)
SELECT
    day,
    currency,
    -- carry the last published rate forward across weekends and holidays
    last_value(rate_to_usd IGNORE NULLS) OVER (
        PARTITION BY currency ORDER BY day
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS rate_to_usd,
    rate_to_usd IS NULL AS rate_was_filled
FROM grid;


-- ---------------------------------------------------------------------------
-- 5. The curated fact table. This is the only view the agent queries.
--
-- D08: 9 rows reference campaign C099, which is not in campaigns.csv - the
--      campaign was deleted in the ad platform after performance was exported.
--      A plain JOIN would delete those rows and their spend without a word.
--      LEFT JOIN keeps them; channel becomes 'unattributed' so they still show
--      up in a channel breakdown instead of quietly vanishing from the total.
--
-- D11: 12 rows of C012 (an INR campaign) are tagged USD, because the campaign
--      currency was changed in the platform and historic rows were not restated.
--      campaigns.csv is treated as authoritative: a campaign has one currency,
--      and a per-row tag that disagrees with it is the thing that is wrong.
--      Converting on the row tag would inflate C012 by roughly 84x.
--
-- D05, D06 and D13 are deliberately NOT corrected here:
--      D05 negative spend is a real refund for invalid traffic.
--      D06 clicks > impressions is impossible, but dropping the row loses real
--          spend; it is flagged so ratio metrics can exclude it.
--      D13 a 6.5x spend spike is a real flash sale, not an error.
--      Silently deleting any of these would misreport the period. They are
--      flagged and surfaced, and the user decides.
--
-- D07: null revenue stays null. It is not zero - the order system had nothing to
--      report, which is different from a day that earned nothing. SUM ignores
--      nulls, so ROAS is computed over the revenue we actually have, and the
--      count of null rows is reported alongside it.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW fact_performance AS
SELECT
    p.day                                                   AS date,
    p.campaign_id,
    p.creative_id,
    COALESCE(c.campaign_name, '(deleted campaign ' || p.campaign_id || ')')
                                                            AS campaign_name,
    COALESCE(c.channel, 'unattributed')                     AS channel,
    COALESCE(c.objective, 'unknown')                        AS objective,
    c.is_running,

    try_cast(p.impressions AS BIGINT)                       AS impressions,
    try_cast(p.clicks AS BIGINT)                            AS clicks,
    try_cast(p.conversions AS BIGINT)                       AS conversions,

    -- money in the campaign currency, kept so any figure can be traced back
    try_cast(p.spend AS DOUBLE)                             AS spend_local,
    try_cast(p.revenue AS DOUBLE)                           AS revenue_local,
    COALESCE(c.currency, upper(trim(p.currency)))           AS currency,
    f.rate_to_usd,

    -- money in USD. Everything downstream uses these two columns only.
    try_cast(p.spend AS DOUBLE)   * f.rate_to_usd           AS spend_usd,
    try_cast(p.revenue AS DOUBLE) * f.rate_to_usd           AS revenue_usd,

    -- quality flags, carried so queries can exclude or report on them
    c.campaign_id IS NULL                                   AS is_orphan_campaign,
    p.date_was_non_iso,
    f.rate_was_filled,
    -- only a mismatch when there is a campaign record to disagree with; an orphan
    -- row has no authoritative currency and is reported under is_orphan_campaign
    (c.campaign_id IS NOT NULL
     AND upper(trim(p.currency)) IS DISTINCT FROM c.currency)
                                                            AS currency_tag_mismatch,
    try_cast(p.spend AS DOUBLE) < 0                         AS is_negative_spend,
    try_cast(p.clicks AS BIGINT) > try_cast(p.impressions AS BIGINT)
                                                            AS is_impossible_funnel,
    p.revenue IS NULL                                       AS revenue_missing

FROM performance_dated p
LEFT JOIN campaigns_clean c
       ON c.campaign_id = p.campaign_id                  -- D08: LEFT, never INNER
LEFT JOIN fx_dense f
       ON f.day = p.day
      -- D11: campaigns.csv wins over the per-row currency tag
      AND f.currency = COALESCE(c.currency, upper(trim(p.currency)))
WHERE p.day IS NOT NULL;                                 -- unparseable dates excluded
