"""The request path, end to end.

    question -> plan (LLM) -> validate (code) -> SQL (code) -> execute (DuckDB)
             -> chart (code) -> narrate (LLM) -> answer

Written as a plain function rather than with an agent framework. There is no
tool selection, no branching a model needs to reason about, and no multi-step
loop - the shape of the work is known in advance, so a framework would add
indirection and take away the ability to say exactly where the model sits.

The one loop here is the plan retry, and it is bounded at one attempt.
"""

from dataclasses import dataclass, field

from dashboard_agent import sql as sqlmod
from dashboard_agent.data import quality_report
from dashboard_agent.llm import LLMError
from dashboard_agent.plan import PlanError, validate


@dataclass
class Answer:
    """Everything needed to display, check and reproduce a result."""

    question: str
    finding: str                       # the prose
    plan: object = None                # the validated QueryPlan
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    sql: str = ""                      # exactly what ran, ready to paste
    notes: list = field(default_factory=list)
    chart_path: str = ""
    refused: bool = False


def answer_question(question, con, client, report=None):
    """Answer one question. Returns an Answer, or raises LLMError."""
    if report is None:
        report = quality_report(con)

    data_min, data_max = report.date_min, report.date_max

    plan = _plan_with_one_retry(question, client, data_min, data_max)

    # A refusal is a designed outcome, not an error path. The model established
    # that the question needs something the data does not hold; we report that
    # plainly instead of answering a nearby question that happens to be
    # answerable.
    if plan.intent == "refuse":
        return Answer(
            question=question,
            finding=plan.refusal_reason,
            plan=plan,
            refused=True,
            notes=["The dataset covers {} to {} and contains only this "
                   "advertiser's own campaigns.".format(data_min, data_max)],
        )

    query, params = sqlmod.build(plan)
    cursor = con.execute(query, params)
    columns = [d[0] for d in cursor.description]
    rows = cursor.fetchall()

    notes = list(plan.warnings)
    notes.extend(_relevant_caveats(plan, report))

    if not rows:
        return Answer(
            question=question, plan=plan, columns=columns, rows=rows,
            sql=sqlmod.render(query, params), notes=notes,
            finding="No rows matched {}. Nothing was recorded for that "
                    "period.".format(plan.period_label or "that period"),
        )

    finding = client.narrate(question, _as_text(columns, rows), notes)

    return Answer(
        question=question,
        finding=finding,
        plan=plan,
        columns=columns,
        rows=rows,
        sql=sqlmod.render(query, params),
        notes=notes,
    )


def _plan_with_one_retry(question, client, data_min, data_max):
    """Ask for a plan; if the validator rejects it, say why and ask once more.

    Retrying with the rejection reason attached is far more effective than
    retrying blind. One retry only - a model that cannot produce a valid plan
    against a closed schema twice is not going to manage it on the third
    attempt, and the user is waiting.
    """
    raw = client.plan(question, data_min, data_max)
    try:
        return validate(raw, data_min, data_max)
    except PlanError as first:
        raw = client.plan(question, data_min, data_max, retry_hint=str(first))
        try:
            return validate(raw, data_min, data_max)
        except PlanError as second:
            raise LLMError(
                "Could not build a valid plan for this question.\n"
                "  first attempt:  {}\n"
                "  second attempt: {}".format(first, second)
            )


def _relevant_caveats(plan, report):
    """Only the data-quality notes that bear on this particular answer.

    Attaching all nine caveats to every answer trains people to ignore them.
    """
    notes = []

    if report.duplicates_removed:
        notes.append("{} duplicate rows were removed before "
                     "aggregating.".format(report.duplicates_removed))

    if plan.metric in ("spend", "revenue", "roas", "cpa", "cpc") and report.fx_filled_rows:
        notes.append(
            "Figures are USD. {} rows fall on days with no published FX rate "
            "(weekends and holidays); the last known rate was carried "
            "forward.".format(report.fx_filled_rows)
        )

    if plan.metric in ("revenue", "roas") and report.missing_revenue_rows:
        notes.append(
            "{} rows have no revenue figure and are excluded rather than "
            "counted as zero.".format(report.missing_revenue_rows)
        )

    if plan.metric in ("ctr", "cpc") and report.impossible_funnel_rows:
        notes.append(
            "{} rows report more clicks than impressions, which is not "
            "possible; treat rate metrics with caution.".format(
                report.impossible_funnel_rows)
        )

    if plan.group_by == "channel" and report.orphan_rows:
        notes.append(
            "{} rows belong to a campaign missing from campaigns.csv and appear "
            "as 'unattributed'.".format(report.orphan_rows)
        )

    # The final day is short in this extract. Any answer whose window includes it
    # needs to say so, or the last day reads as a collapse.
    if plan.date_to == report.date_max:
        notes.append(
            "{} is the last day in the extract and is incomplete - it holds far "
            "fewer rows than a normal day, so treat it as partial rather than as "
            "a drop.".format(report.date_max)
        )

    for day in report.missing_days:
        if plan.date_from <= day <= plan.date_to:
            notes.append(
                "No data was reported at all on {}; averages over this period "
                "are affected.".format(day)
            )

    return notes


def _as_text(columns, rows, max_rows=40):
    """A compact table for the narration prompt.

    Truncated because the model only needs enough to describe the shape of the
    result, and output tokens are the expensive part of this workload.
    """
    lines = [" | ".join(str(c) for c in columns)]
    for row in rows[:max_rows]:
        lines.append(" | ".join("" if v is None else str(v) for v in row))
    if len(rows) > max_rows:
        lines.append("... {} more rows".format(len(rows) - max_rows))
    return "\n".join(lines)
