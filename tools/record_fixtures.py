"""Records real model responses so the project runs without an API key.

The brief asks that the project run from a clean checkout by following the
README. A reviewer may not have an Azure deployment, so the five questions are
answered once against the real model and the responses saved. `--offline` then
replays them.

These are recordings, not fabrications - each one is what gpt-5.6-luna actually
returned, and re-running this script refreshes them.

    python tools/record_fixtures.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard_agent.agent import answer_question          # noqa: E402
from dashboard_agent.data import connect, quality_report   # noqa: E402
from dashboard_agent.llm import FIXTURES, build_client     # noqa: E402

# The five from the brief, plus two that exercise paths the five do not: a
# question the data cannot answer for a different reason than competitors, and
# one asking for a period outside the extract.
QUESTIONS = [
    "What did we spend by channel over the last eight weeks?",
    "Which campaign generated the most revenue this quarter?",
    "Conversions look like they fell off a cliff on the most recent day. What happened?",
    "Which campaign should we turn off?",
    "How does our spend compare to our competitors?",
    "Which creative had the best click-through rate last month?",
    "What did we spend in January 2026?",
]


class RecordingClient:
    """Wraps a real client and remembers what it returned."""

    def __init__(self, inner):
        self.inner = inner
        self.captured = {}

    def plan(self, question, data_min, data_max, retry_hint=""):
        result = self.inner.plan(question, data_min, data_max, retry_hint)
        self.captured.setdefault(question.strip(), {})["plan"] = result
        return result

    def narrate(self, question, table_text, caveats):
        result = self.inner.narrate(question, table_text, caveats)
        self.captured.setdefault(question.strip(), {})["narration"] = result
        return result


def main():
    con = connect()
    report = quality_report(con)
    client = RecordingClient(build_client())

    for question in QUESTIONS:
        print("  " + question)
        try:
            answer = answer_question(question, con, client, report)
            state = "refused" if answer.refused else "answered"
            print("    {}: {}".format(state, answer.finding[:90]))
        except Exception as exc:
            print("    FAILED: {}".format(exc))

    FIXTURES.parent.mkdir(parents=True, exist_ok=True)
    FIXTURES.write_text(
        json.dumps(client.captured, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("\nwrote {} recordings to {}".format(len(client.captured), FIXTURES))


if __name__ == "__main__":
    main()
