"""Command line entry point.

    python -m dashboard_agent "What did we spend by channel over the last eight weeks?"

Prints the finding, the numbers, and the SQL that produced them. The SQL is not
decoration: the brief asks that whatever produced the number be visible, and a
query printed beside its result is checkable in a way that a chart alone is not.
"""

import argparse
import sys
import webbrowser
from html import escape
from pathlib import Path

from dashboard_agent import chart as chartmod
from dashboard_agent.agent import answer_question
from dashboard_agent.data import connect, quality_report
from dashboard_agent.llm import LLMError, build_client

BAR_WIDTH = 22


def _fmt(value, column):
    if value is None:
        return "-"
    if isinstance(value, float):
        if column in chartmod.MONEY_COLUMNS:
            return "{:,.2f}".format(value)
        return "{:,.4f}".format(value).rstrip("0").rstrip(".")
    if isinstance(value, int):
        return "{:,}".format(value)
    return str(value)


def print_table(columns, rows, limit=25):
    """A plain table, with an inline bar on the first numeric column.

    The bars are there so the shape of the answer is visible without opening the
    PNG - which matters when the whole thing is being demonstrated in a terminal.
    """
    shown = rows[:limit]
    cells = [[_fmt(v, columns[i]) for i, v in enumerate(row)] for row in shown]
    widths = [len(c) for c in columns]
    for row in cells:
        for i, text in enumerate(row):
            widths[i] = max(widths[i], len(text))

    # Bar on the first numeric column, if the values are non-negative.
    bar_col = None
    for i, name in enumerate(columns):
        values = [r[i] for r in shown]
        if values and all(isinstance(v, (int, float)) and v >= 0 for v in values):
            bar_col = i
            break
    peak = max((r[bar_col] for r in shown), default=0) if bar_col is not None else 0

    header = "  ".join(name.ljust(widths[i]) for i, name in enumerate(columns))
    print("  " + header)
    print("  " + "-" * len(header))
    for row, raw in zip(cells, shown):
        line = "  ".join(text.ljust(widths[i]) if not _numeric(shown, i)
                         else text.rjust(widths[i])
                         for i, text in enumerate(row))
        if bar_col is not None and peak:
            filled = int(BAR_WIDTH * (raw[bar_col] / peak))
            line += "  " + "#" * filled
        print("  " + line)

    if len(rows) > limit:
        print("  ... {} more rows".format(len(rows) - limit))


def _numeric(rows, index):
    return all(isinstance(r[index], (int, float)) or r[index] is None for r in rows)


def write_html(answer, out_dir="out"):
    """A single self-contained file: finding, chart, numbers, SQL.

    Deliberately plain. The brief says a designed UI scores nothing, so this is
    an output artifact rather than an interface - it exists so the walkthrough
    has something to show full screen.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "answer.html"

    table = ""
    if answer.rows:
        head = "".join("<th>{}</th>".format(escape(c)) for c in answer.columns)
        body = "".join(
            "<tr>" + "".join(
                "<td>{}</td>".format(escape(_fmt(v, answer.columns[i])))
                for i, v in enumerate(row)
            ) + "</tr>"
            for row in answer.rows
        )
        table = "<table><thead><tr>{}</tr></thead><tbody>{}</tbody></table>".format(
            head, body)

    img = ""
    if answer.chart_path:
        img = '<img src="{}" alt="chart">'.format(escape(Path(answer.chart_path).name))

    notes = ""
    if answer.notes:
        notes = "<ul>" + "".join(
            "<li>{}</li>".format(escape(n)) for n in answer.notes) + "</ul>"

    html = """<!doctype html>
<meta charset="utf-8">
<title>{q}</title>
<style>
 body {{ font: 15px/1.6 -apple-system, Segoe UI, system-ui, sans-serif;
        max-width: 900px; margin: 40px auto; padding: 0 20px; color: #1f2933; }}
 h1 {{ font-size: 18px; color: #616e7c; font-weight: 500; }}
 .finding {{ font-size: 17px; line-height: 1.55; margin: 18px 0 26px;
             border-left: 3px solid #2563eb; padding-left: 16px; }}
 img {{ max-width: 100%; margin: 10px 0 26px; }}
 table {{ border-collapse: collapse; font-size: 13px; margin-bottom: 26px; }}
 th, td {{ border-bottom: 1px solid #e4e7eb; padding: 6px 14px 6px 0; text-align: left; }}
 th {{ color: #616e7c; font-weight: 600; }}
 pre {{ background: #f5f7fa; padding: 14px 16px; overflow-x: auto;
        font-size: 12.5px; line-height: 1.5; }}
 h2 {{ font-size: 12px; text-transform: uppercase; letter-spacing: .06em;
       color: #9aa5b1; margin-top: 30px; }}
 li {{ color: #616e7c; font-size: 13.5px; }}
</style>
<h1>{q}</h1>
<div class="finding">{finding}</div>
{img}
{table}
{notes_block}
{sql_block}
""".format(
        q=escape(answer.question),
        finding=escape(answer.finding),
        img=img,
        table=table,
        notes_block=("<h2>Data notes</h2>" + notes) if notes else "",
        sql_block=("<h2>Query</h2><pre>{}</pre>".format(escape(answer.sql))
                   if answer.sql else ""),
    )
    path.write_text(html, encoding="utf-8")
    return str(path)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="dashboard_agent",
        description="Ask a question about the ad performance data.",
    )
    parser.add_argument("question", nargs="?", help="the question, in plain English")
    parser.add_argument("--data", default=None, help="data directory (default: ./data)")
    parser.add_argument("--out", default="out", help="where to write chart and report")
    parser.add_argument("--offline", action="store_true",
                        help="use recorded responses; no API key needed")
    parser.add_argument("--open", action="store_true",
                        help="write an HTML report and open it")
    parser.add_argument("--no-chart", action="store_true", help="skip the chart")
    args = parser.parse_args(argv)

    if not args.question:
        parser.print_help()
        return 2

    con = connect(args.data)
    report = quality_report(con)

    try:
        client = build_client(offline=args.offline)
    except LLMError as exc:
        print("\n  {}\n".format(exc), file=sys.stderr)
        return 1

    try:
        answer = answer_question(args.question, con, client, report)
    except LLMError as exc:
        print("\n  {}\n".format(exc), file=sys.stderr)
        return 1

    print()
    if answer.refused:
        print("  CANNOT ANSWER THIS")
        print()
        for line in _wrap(answer.finding):
            print("  " + line)
        print()
        for note in answer.notes:
            print("  " + note)
        print()
        return 0

    if not args.no_chart:
        answer.chart_path = chartmod.render(answer, args.out)

    print("  FINDING")
    for line in _wrap(answer.finding):
        print("  " + line)

    print("\n  RESULT")
    print_table(answer.columns, answer.rows)

    if answer.notes:
        print("\n  DATA NOTES")
        for note in answer.notes:
            for i, line in enumerate(_wrap(note, 84)):
                print("  " + ("- " if i == 0 else "  ") + line)

    print("\n  QUERY")
    for line in answer.sql.splitlines():
        print("    " + line)

    if answer.chart_path:
        print("\n  CHART  " + answer.chart_path)

    if args.open:
        html = write_html(answer, args.out)
        print("  REPORT " + html)
        webbrowser.open(Path(html).resolve().as_uri())

    print()
    return 0


def _wrap(text, width=86):
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
