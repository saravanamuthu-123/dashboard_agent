"""Web UI — a dashboard with an agentic chat interface.

    python -m dashboard_agent.web                  # live model
    python -m dashboard_agent.web --offline         # recorded responses, no key

One file, no build step. Serves a single HTML page with:
  - KPI cards and charts showing the dataset at a glance
  - A chat interface that runs questions through the agent
"""

import argparse
import base64
import io
import json
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import uvicorn

from dashboard_agent.agent import answer_question
from dashboard_agent.data import connect, quality_report
from dashboard_agent import chart as chartmod
from dashboard_agent import sql as sqlmod
from dashboard_agent.llm import LLMError, build_client
from dashboard_agent.plan import validate
from dashboard_agent.cli import _fmt

# Globals set at startup
CON = None
REPORT = None
CLIENT = None
APP = FastAPI(title="Dashboard Agent")

STATIC_DIR = Path(__file__).parent / "static"


# ---- API models ----

class QuestionRequest(BaseModel):
    question: str


class AnswerResponse(BaseModel):
    question: str
    finding: str
    columns: list = []
    rows: list = []
    sql: str = ""
    notes: list = []
    chart_base64: str = ""
    refused: bool = False


# ---- Dashboard data endpoint ----

@APP.get("/api/dashboard")
def get_dashboard():
    """KPI cards and chart data for the overview dashboard."""
    # KPIs
    kpis = CON.execute("""
        SELECT
            ROUND(SUM(spend_usd), 2) AS total_spend,
            ROUND(SUM(revenue_usd), 2) AS total_revenue,
            SUM(conversions) AS total_conversions,
            SUM(clicks) AS total_clicks,
            SUM(impressions) AS total_impressions,
            ROUND(SUM(revenue_usd) / NULLIF(SUM(spend_usd), 0), 2) AS overall_roas,
            COUNT(DISTINCT campaign_id) AS campaigns,
            COUNT(DISTINCT channel) AS channels,
            min(date) AS date_from,
            max(date) AS date_to
        FROM fact_performance
    """).fetchone()

    # Spend by channel
    channel_spend = CON.execute("""
        SELECT channel, ROUND(SUM(spend_usd), 2) AS spend
        FROM fact_performance
        GROUP BY channel ORDER BY spend DESC
    """).fetchall()

    # Campaign ROAS
    campaign_roas = CON.execute("""
        SELECT campaign_name, objective,
               ROUND(SUM(revenue_usd) / NULLIF(SUM(spend_usd), 0), 2) AS roas,
               ROUND(SUM(spend_usd), 0) AS spend
        FROM fact_performance
        WHERE NOT is_orphan_campaign
        GROUP BY campaign_name, objective
        ORDER BY roas DESC
    """).fetchall()

    # Daily spend trend
    daily_spend = CON.execute("""
        SELECT date, ROUND(SUM(spend_usd), 2) AS spend
        FROM fact_performance
        GROUP BY date ORDER BY date
    """).fetchall()

    return {
        "kpis": {
            "total_spend": kpis[0],
            "total_revenue": kpis[1],
            "total_conversions": int(kpis[2] or 0),
            "total_clicks": int(kpis[3] or 0),
            "total_impressions": int(kpis[4] or 0),
            "overall_roas": kpis[5],
            "campaigns": kpis[6],
            "channels": kpis[7],
            "date_from": str(kpis[8]),
            "date_to": str(kpis[9]),
        },
        "channel_spend": [{"channel": r[0], "spend": r[1]} for r in channel_spend],
        "campaign_roas": [
            {"name": r[0], "objective": r[1], "roas": r[2], "spend": r[3]}
            for r in campaign_roas
        ],
        "daily_spend": [{"date": str(r[0]), "spend": r[1]} for r in daily_spend],
        "cleaning": {
            "raw_rows": REPORT.raw_rows,
            "clean_rows": REPORT.clean_rows,
            "duplicates_removed": REPORT.duplicates_removed,
            "caveats": REPORT.caveats(),
        },
    }


# ---- Chat endpoint ----

@APP.post("/api/ask", response_model=AnswerResponse)
def ask_question(req: QuestionRequest):
    """Run a question through the agent and return the answer."""
    try:
        answer = answer_question(req.question, CON, CLIENT, REPORT)
    except LLMError as exc:
        return AnswerResponse(
            question=req.question,
            finding="Error: {}".format(str(exc)),
            refused=True,
        )

    # Render chart to base64 PNG
    chart_b64 = ""
    if not answer.refused and answer.rows:
        chart_path = chartmod.render(answer, "out")
        if chart_path:
            answer.chart_path = chart_path
            with open(chart_path, "rb") as f:
                chart_b64 = base64.b64encode(f.read()).decode("ascii")

    # Format rows for JSON
    formatted_rows = []
    for row in answer.rows:
        formatted_rows.append([
            _fmt(v, answer.columns[i] if i < len(answer.columns) else "")
            for i, v in enumerate(row)
        ])

    return AnswerResponse(
        question=req.question,
        finding=answer.finding,
        columns=answer.columns,
        rows=formatted_rows,
        sql=answer.sql,
        notes=answer.notes,
        chart_base64=chart_b64,
        refused=answer.refused,
    )


# ---- Serve the HTML page ----

@APP.get("/", response_class=HTMLResponse)
def index():
    html_path = STATIC_DIR / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


def main():
    global CON, REPORT, CLIENT

    parser = argparse.ArgumentParser(prog="dashboard_agent.web")
    parser.add_argument("--offline", action="store_true",
                        help="use recorded responses, no API key needed")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data", default=None)
    args = parser.parse_args()

    CON = connect(args.data)
    REPORT = quality_report(CON)

    try:
        CLIENT = build_client(offline=args.offline)
        mode = "offline (recorded responses)" if args.offline else "live (Azure OpenAI)"
    except LLMError as exc:
        print("  LLM: {}".format(exc))
        print("  Use --offline to run without a key.")
        return 1

    print()
    print("  Dashboard Agent")
    print("  mode:  {}".format(mode))
    print("  data:  {} rows, {} .. {}".format(
        REPORT.clean_rows, REPORT.date_min, REPORT.date_max))
    print("  open:  http://localhost:{}".format(args.port))
    print()

    uvicorn.run(APP, host="0.0.0.0", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
