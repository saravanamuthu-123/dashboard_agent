"""The contract between the model and the rest of the system.

The LLM does not answer questions. It fills in this small, closed structure
describing *what to compute*, and code does the computing.

The line between what the model decides and what the code fixes:

    the model decides    which metric, which grouping, which period, which intent
                         - all of it constrained to values that already exist

    the code decides     how any of that becomes SQL, how a relative period turns
                         into two dates, what makes a campaign eligible to pause,
                         what counts as an anomaly

The rule behind that split: **anything that changes a number is fixed in code.**
The model picks what question to answer; it never participates in answering it.

Note in particular that the model never emits a date. It says "last eight weeks"
in structured form and code resolves that against the data. Date arithmetic is
arithmetic, and the model is kept out of arithmetic everywhere else too.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta

# ---------------------------------------------------------------------------
# The closed vocabularies. Every enum here is a value the system can actually
# serve. A model constrained to these cannot ask for a column that stopped
# existing, which is a whole class of failure removed rather than handled.
# ---------------------------------------------------------------------------

INTENTS = [
    "aggregate",        # totals, optionally grouped        -> question 1
    "rank",             # order groups by a metric          -> question 2
    "anomaly",          # explain a movement over time      -> question 3
    "recommend_pause",  # which campaign to turn off        -> question 4
    "refuse",           # cannot be answered from this data -> question 5
]

METRICS = [
    "spend", "revenue", "conversions", "clicks", "impressions",
    "roas", "cpa", "ctr", "cpc",
]

# Ratio metrics need a guard on the denominator and cannot simply be SUMmed.
RATIO_METRICS = {"roas", "cpa", "ctr", "cpc"}

GROUPINGS = ["channel", "campaign", "creative", "objective", "date", "none"]

PERIOD_KINDS = [
    "last_n_days",
    "last_n_weeks",
    "last_n_months",
    "quarter_to_date",
    "month_to_date",
    "most_recent_day",
    "all_time",
    "explicit",
]

MAX_LIMIT = 50

# A ranking of one row is not a ranking. Asked "which campaign made the most",
# a model sensibly returns limit=1 - but the runner-up is what tells you whether
# the winner won by a mile or a hair, and a bar chart of a single bar says
# nothing at all. Code sets a floor; the narration still names the winner.
MIN_RANK_ROWS = 5

# The shortest history an anomaly can be judged against.
ANOMALY_MIN_WINDOW_DAYS = 21


# ---------------------------------------------------------------------------
# JSON Schema handed to the provider.
#
# Written for OpenAI strict Structured Outputs, which requires every property to
# appear in "required" and forbids additionalProperties. That is why unused
# fields carry sentinels ("" or 0) rather than being optional - under strict
# decoding the model must emit all of them, so we give the unused ones a
# defined empty value instead of pretending they are absent.
# ---------------------------------------------------------------------------

PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "intent", "metric", "group_by", "period_kind", "period_n",
        "date_from", "date_to", "limit", "refusal_reason",
    ],
    "properties": {
        "intent": {
            "type": "string",
            "enum": INTENTS,
            "description": "What kind of answer the question calls for.",
        },
        "metric": {
            "type": "string",
            "enum": METRICS,
            "description": "The quantity being asked about.",
        },
        "group_by": {
            "type": "string",
            "enum": GROUPINGS,
            "description": "How to break the metric down. 'none' for a single total.",
        },
        "period_kind": {
            "type": "string",
            "enum": PERIOD_KINDS,
            "description": (
                "The time window, expressed relatively. Do not compute dates. "
                "'quarter_to_date' means the current calendar quarter. Use "
                "'explicit' only when the question names actual calendar dates."
            ),
        },
        "period_n": {
            "type": "integer",
            "description": "The N in last_n_days / last_n_weeks / last_n_months. 0 otherwise.",
        },
        "date_from": {
            "type": "string",
            "description": "YYYY-MM-DD, only when period_kind is 'explicit'. Empty otherwise.",
        },
        "date_to": {
            "type": "string",
            "description": "YYYY-MM-DD, only when period_kind is 'explicit'. Empty otherwise.",
        },
        "limit": {
            "type": "integer",
            "description": "Rows to return for rank. 0 to use the default.",
        },
        "refusal_reason": {
            "type": "string",
            "description": (
                "Only when intent is 'refuse': what the question asked for that "
                "this dataset does not contain. Empty otherwise."
            ),
        },
    },
}


class PlanError(Exception):
    """The plan is well-formed JSON but does not describe an answerable question."""


@dataclass
class QueryPlan:
    """A validated plan. Only this reaches the SQL builder."""

    intent: str
    metric: str
    group_by: str
    date_from: date
    date_to: date
    limit: int
    period_label: str            # human wording, e.g. "the last 8 weeks"
    refusal_reason: str = ""
    warnings: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Period resolution. Code does this, never the model.
#
# "Today" is the most recent date in the data, not the wall clock. Asking for
# "the last eight weeks" against an extract that ends on 4 September should mean
# the eight weeks up to 4 September, not eight weeks up to now with an empty
# tail. This is a small thing that quietly produces wrong answers otherwise.
# ---------------------------------------------------------------------------

def _quarter_start(d):
    return date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)


def resolve_period(kind, n, raw_from, raw_to, data_min, data_max):
    """Turn a relative period into two dates. Returns (from, to, label)."""
    if kind == "all_time":
        return data_min, data_max, "the full period"

    if kind == "most_recent_day":
        return data_max, data_max, "the most recent day ({})".format(data_max)

    if kind == "quarter_to_date":
        return _quarter_start(data_max), data_max, "this quarter"

    if kind == "month_to_date":
        return date(data_max.year, data_max.month, 1), data_max, "this month"

    if kind in ("last_n_days", "last_n_weeks", "last_n_months"):
        if n <= 0:
            raise PlanError("period_kind {} needs a positive period_n".format(kind))
        if kind == "last_n_days":
            days, label = n, "the last {} days".format(n)
        elif kind == "last_n_weeks":
            days, label = n * 7, "the last {} weeks".format(n)
        else:
            days, label = n * 30, "the last {} months".format(n)
        return data_max - timedelta(days=days), data_max, label

    if kind == "explicit":
        try:
            start = date.fromisoformat(raw_from)
            end = date.fromisoformat(raw_to)
        except (TypeError, ValueError):
            raise PlanError(
                "period_kind 'explicit' needs both date_from and date_to as "
                "YYYY-MM-DD (got {!r} and {!r})".format(raw_from, raw_to)
            )
        return start, end, "{} to {}".format(start, end)

    raise PlanError("unknown period_kind {!r}".format(kind))


# ---------------------------------------------------------------------------
# Validation.
#
# Structured Outputs already guarantee the *shape* - valid JSON, only known
# fields, only allowed enum values. Everything below catches the other failure:
# a perfectly well-formed plan that does not describe an answerable question, or
# describes one the data cannot support.
#
# The enum checks are repeated here anyway. They are redundant when the provider
# enforces the schema, and they are the only defence when it does not - a
# different model, a degraded fallback path, or a provider without strict mode.
# ---------------------------------------------------------------------------

def validate(raw, data_min, data_max):
    """Turn a raw plan dict into a QueryPlan, or raise PlanError."""
    if not isinstance(raw, dict):
        raise PlanError("plan must be an object, got {}".format(type(raw).__name__))

    warnings = []

    intent = str(raw.get("intent", "")).strip()
    if intent not in INTENTS:
        raise PlanError("unknown intent {!r}; expected one of {}".format(intent, INTENTS))

    # A refusal carries no computation, so it skips the rest of the checks.
    if intent == "refuse":
        reason = str(raw.get("refusal_reason", "")).strip()
        if not reason:
            raise PlanError("intent 'refuse' requires a refusal_reason")
        return QueryPlan(
            intent="refuse", metric="spend", group_by="none",
            date_from=data_min, date_to=data_max, limit=0,
            period_label="", refusal_reason=reason, warnings=warnings,
        )

    metric = str(raw.get("metric", "")).strip()
    if metric not in METRICS:
        raise PlanError("unknown metric {!r}; expected one of {}".format(metric, METRICS))

    group_by = str(raw.get("group_by", "none")).strip() or "none"
    if group_by not in GROUPINGS:
        raise PlanError("unknown group_by {!r}; expected one of {}".format(group_by, GROUPINGS))

    period_kind = str(raw.get("period_kind", "all_time")).strip()
    try:
        period_n = int(raw.get("period_n") or 0)
    except (TypeError, ValueError):
        raise PlanError("period_n must be a whole number, got {!r}".format(raw.get("period_n")))

    date_from, date_to, period_label = resolve_period(
        period_kind, period_n,
        raw.get("date_from", ""), raw.get("date_to", ""),
        data_min, data_max,
    )

    if date_from > date_to:
        raise PlanError("date_from {} is after date_to {}".format(date_from, date_to))

    # Coverage. Asking for a window the extract does not cover is common and is
    # not an error - but answering it without saying so would be dishonest, so
    # the range is clamped and the shortfall is reported with the answer.
    if date_from < data_min:
        warnings.append(
            "Requested period starts {} but the data begins {}; "
            "the answer covers {} onward.".format(date_from, data_min, data_min)
        )
        date_from = data_min
    if date_to > data_max:
        warnings.append(
            "Requested period ends {} but the data stops {}; "
            "the answer covers up to {}.".format(date_to, data_max, data_max)
        )
        date_to = data_max
    if date_from > data_max or date_to < data_min:
        raise PlanError(
            "The requested period falls entirely outside the data, which covers "
            "{} to {}.".format(data_min, data_max)
        )

    # Intent-specific coherence.
    if intent == "rank" and group_by == "none":
        raise PlanError("intent 'rank' needs something to rank; group_by cannot be 'none'")

    if intent == "anomaly":
        # An anomaly question is about movement over time. No other grouping can
        # answer it, so correcting this silently is safe.
        group_by = "date"

        # A single day cannot be anomalous on its own. Asked "what happened
        # yesterday", a model reasonably picks the most recent day - but one
        # point has nothing to be compared against, and the answer would rest on
        # an assertion rather than evidence. Code widens the window, because
        # "how much history does it take to see a change" is a property of the
        # method, not of the question.
        earliest = date_to - timedelta(days=ANOMALY_MIN_WINDOW_DAYS)
        if date_from > earliest:
            date_from = earliest
            period_label = "the {} days to {}".format(
                ANOMALY_MIN_WINDOW_DAYS, date_to)

    if intent == "recommend_pause":
        # The metric and grouping are fixed by code for this intent. Whether a
        # campaign should be paused depends on objective, history length and
        # conversion volume - criteria that belong in the system, not in a
        # sentence the model wrote. See sql.py for the eligibility rule.
        if metric != "roas":
            warnings.append(
                "Pause recommendations are assessed on ROAS with eligibility "
                "filters; the requested metric {!r} was not used.".format(metric)
            )
        metric, group_by = "roas", "campaign"

    try:
        limit = int(raw.get("limit") or 0)
    except (TypeError, ValueError):
        raise PlanError("limit must be a whole number, got {!r}".format(raw.get("limit")))
    if limit <= 0:
        limit = 10 if intent == "rank" else MAX_LIMIT
    if intent == "rank" and limit < MIN_RANK_ROWS:
        limit = MIN_RANK_ROWS
    if limit > MAX_LIMIT:
        warnings.append("limit {} reduced to {}".format(limit, MAX_LIMIT))
        limit = MAX_LIMIT

    if metric in RATIO_METRICS and group_by == "none":
        # A single global ROAS is computable but rarely what anyone means.
        warnings.append(
            "{} was requested without a breakdown; reporting it across the "
            "whole account.".format(metric.upper())
        )

    return QueryPlan(
        intent=intent,
        metric=metric,
        group_by=group_by,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        period_label=period_label,
        warnings=warnings,
    )
