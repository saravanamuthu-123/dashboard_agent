"""Draws the result.

The chart type is chosen from the shape of the result, not by the model. A time
series is a line, categories are bars, a single number is a single number. There
is no judgement here worth spending a model call on, and having code decide means
the same query always produces the same picture.
"""

from pathlib import Path

import matplotlib

# Render to a file, never to a window. The CLI may run over SSH or in CI.
matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402  (must follow matplotlib.use)
import matplotlib.ticker as ticker  # noqa: E402

# Enough colours to distinguish categories without becoming a palette.
INK = "#1f2933"
MUTED = "#9aa5b1"
ACCENT = "#2563eb"
WARN = "#b91c1c"
OK = "#047857"

MONEY_COLUMNS = {"spend_usd", "revenue_usd", "cpa_usd", "cpc_usd"}


def _style(ax):
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color(MUTED)
    ax.spines["bottom"].set_color(MUTED)
    ax.tick_params(colors=INK, labelsize=9)
    ax.grid(axis="y", color=MUTED, alpha=0.25, linewidth=0.6)
    ax.set_axisbelow(True)


def _money_axis(ax, column):
    if column in MONEY_COLUMNS:
        ax.yaxis.set_major_formatter(ticker.FuncFormatter(
            lambda v, _: "${:,.0f}".format(v)))


def render(answer, out_dir="out"):
    """Draw the answer and return the path written, or "" if there is nothing to draw."""
    if answer.refused or not answer.rows:
        return ""

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    plan = answer.plan
    if plan.intent == "anomaly":
        fig = _anomaly_chart(answer)
    elif plan.intent == "recommend_pause":
        fig = _pause_chart(answer)
    elif plan.group_by == "date":
        fig = _line_chart(answer)
    elif plan.group_by == "none":
        fig = _single_value(answer)
    else:
        fig = _bar_chart(answer)

    if fig is None:
        return ""

    name = "{}_{}_{}.png".format(plan.date_to, plan.intent, plan.metric)
    path = out_dir / name
    fig.savefig(path, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return str(path)


def _title(answer, extra=""):
    plan = answer.plan
    bits = [plan.metric.upper() if plan.metric in ("roas", "cpa", "ctr", "cpc")
            else plan.metric.replace("_", " ").title()]
    if plan.group_by not in ("none", "date"):
        bits.append("by " + plan.group_by)
    if plan.period_label:
        bits.append("- " + plan.period_label)
    if extra:
        bits.append(extra)
    return " ".join(bits)


def _bar_chart(answer):
    labels = [str(r[0]) for r in answer.rows]
    values = [r[1] if r[1] is not None else 0 for r in answer.rows]
    column = answer.columns[1]

    fig, ax = plt.subplots(figsize=(8, max(3, 0.45 * len(labels) + 1.6)))
    # Horizontal, because campaign names are long and vertical labels are
    # unreadable at any realistic figure width.
    positions = range(len(labels))
    ax.barh(list(positions), values, color=ACCENT, height=0.62)
    ax.set_yticks(list(positions))
    ax.set_yticklabels(labels)
    ax.invert_yaxis()

    span = max(values) if values else 0
    for pos, value in zip(positions, values):
        ax.text(value + span * 0.015, pos,
                "${:,.0f}".format(value) if column in MONEY_COLUMNS
                else "{:,.2f}".format(value).rstrip("0").rstrip("."),
                va="center", fontsize=9, color=INK)

    ax.set_xlim(0, span * 1.16 if span else 1)
    ax.set_xlabel(column.replace("_", " "))
    if column in MONEY_COLUMNS:
        ax.xaxis.set_major_formatter(ticker.FuncFormatter(
            lambda v, _: "${:,.0f}".format(v)))
    _style(ax)
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=MUTED, alpha=0.25, linewidth=0.6)
    ax.set_title(_title(answer), fontsize=11, color=INK, pad=12, loc="left")
    return fig


def _line_chart(answer):
    dates = [r[0] for r in answer.rows]
    values = [r[1] if r[1] is not None else 0 for r in answer.rows]

    fig, ax = plt.subplots(figsize=(9, 3.8))
    ax.plot(dates, values, color=ACCENT, linewidth=1.8)
    ax.fill_between(dates, values, color=ACCENT, alpha=0.10)
    _style(ax)
    _money_axis(ax, answer.columns[1])
    ax.set_title(_title(answer), fontsize=11, color=INK, pad=12, loc="left")
    fig.autofmt_xdate(rotation=45, ha="right")
    return fig


def _anomaly_chart(answer):
    """The metric, its trailing median, and the row count that explains it.

    Two panels rather than one. The row count is not a supporting detail here -
    it is the evidence that separates an incomplete extract from a real drop, so
    it gets its own axis instead of being squeezed onto a second scale.
    """
    dates = [r[0] for r in answer.rows]
    values = [r[1] if r[1] is not None else 0 for r in answer.rows]
    reported = [r[2] for r in answer.rows]
    median = [r[3] for r in answer.rows]
    median_rows = [r[4] for r in answer.rows]

    fig, (top, bottom) = plt.subplots(
        2, 1, figsize=(9, 5.4), sharex=True,
        gridspec_kw={"height_ratios": [2, 1], "hspace": 0.18},
    )

    top.plot(dates, values, color=ACCENT, linewidth=1.8, label=answer.columns[1])
    top.plot(dates, median, color=MUTED, linewidth=1.4, linestyle="--",
             label="14-day trailing median")
    # Mark the final point if it sits well below the trend.
    if median and median[-1] and values[-1] < median[-1] * 0.6:
        top.plot([dates[-1]], [values[-1]], "o", color=WARN, markersize=7, zorder=5)
    top.legend(frameon=False, fontsize=8, loc="upper left")
    _style(top)
    _money_axis(top, answer.columns[1])
    top.set_title(_title(answer), fontsize=11, color=INK, pad=12, loc="left")

    bottom.bar(dates, reported, color=MUTED, width=0.7)
    bottom.plot(dates, median_rows, color=WARN, linewidth=1.2, linestyle="--")
    if reported and median_rows and median_rows[-1] and reported[-1] < median_rows[-1] * 0.6:
        bottom.bar([dates[-1]], [reported[-1]], color=WARN, width=0.7)
    bottom.set_ylabel("rows\nreported", fontsize=8)
    _style(bottom)
    bottom.set_title("A short final bar means the extract is incomplete, not that "
                     "performance fell", fontsize=8.5, color=MUTED, loc="left", pad=6)
    fig.autofmt_xdate(rotation=45, ha="right")
    return fig


def _pause_chart(answer):
    """Campaigns by ROAS, with excluded ones visibly greyed rather than hidden."""
    index = {name: i for i, name in enumerate(answer.columns)}
    rows = [r for r in answer.rows if r[index["roas"]] is not None]
    rows.sort(key=lambda r: r[index["roas"]])

    labels = [r[index["campaign_name"]] for r in rows]
    values = [r[index["roas"]] for r in rows]
    eligible = [r[index["eligibility"]] == "eligible" for r in rows]

    fig, ax = plt.subplots(figsize=(8.5, max(3, 0.42 * len(labels) + 1.8)))
    positions = list(range(len(labels)))
    colours = []
    for i, is_eligible in enumerate(eligible):
        if not is_eligible:
            colours.append(MUTED)
        elif i == next((j for j, e in enumerate(eligible) if e), None):
            colours.append(WARN)      # the recommendation
        else:
            colours.append(ACCENT)
    ax.barh(positions, values, color=colours, height=0.62)

    # Break-even. Below this line a campaign returns less than it costs.
    ax.axvline(1.0, color=OK, linewidth=1.2, linestyle="--")
    ax.text(1.02, len(labels) - 0.4, "break-even", color=OK, fontsize=8)

    ax.set_yticks(positions)
    ax.set_yticklabels(
        ["{}{}".format(name, "" if ok else "  (excluded)")
         for name, ok in zip(labels, eligible)], fontsize=8.5)
    ax.invert_yaxis()
    for pos, value in zip(positions, values):
        ax.text(value + 0.05, pos, "{:.2f}".format(value), va="center", fontsize=8.5)
    ax.set_xlabel("ROAS (revenue / spend, USD)")
    ax.set_xlim(0, max(values) * 1.15 if values else 1)
    _style(ax)
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=MUTED, alpha=0.25, linewidth=0.6)
    ax.set_title("Campaign ROAS - {}".format(answer.plan.period_label),
                 fontsize=11, color=INK, pad=12, loc="left")
    return fig


def _single_value(answer):
    """One number, drawn large. A bar chart of a single bar tells you nothing."""
    value = answer.rows[0][0]
    if value is None:
        return None
    column = answer.columns[0]
    text = ("${:,.2f}".format(value) if column in MONEY_COLUMNS
            else "{:,.2f}".format(value).rstrip("0").rstrip("."))

    fig, ax = plt.subplots(figsize=(6, 2.6))
    ax.axis("off")
    ax.text(0.5, 0.62, text, ha="center", va="center",
            fontsize=40, color=INK, fontweight="bold")
    ax.text(0.5, 0.22, _title(answer), ha="center", va="center",
            fontsize=10, color=MUTED)
    return fig
