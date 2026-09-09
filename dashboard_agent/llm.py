"""The only two places a language model is involved.

    plan()      question -> a structured plan. Sees no data.
    narrate()   a computed result table -> two sentences. Computes nothing.

Neither call performs arithmetic and neither sees a raw row. Everything between
them is deterministic code.

Provider and model come from the environment, so switching between deployments
is a config change. The interface is small on purpose: two methods, both taking
and returning plain data, which is what makes the offline client below a genuine
substitute rather than a mock.
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

from dashboard_agent.plan import (
    GROUPINGS, INTENTS, METRICS, PERIOD_KINDS, PLAN_SCHEMA,
)

load_dotenv()

PROJECT_DIR = Path(__file__).resolve().parent.parent
FIXTURES = PROJECT_DIR / "tests" / "fixtures" / "llm_responses.json"


# ---------------------------------------------------------------------------
# Prompts.
#
# The planning prompt describes what the system can serve, not how to answer.
# It deliberately does not mention competitors, or any other specific thing the
# data lacks - it lists what exists and instructs the model to refuse anything
# outside that. A rule generalises; a hardcoded exception only covers the case
# someone thought of.
# ---------------------------------------------------------------------------

PLAN_SYSTEM_PROMPT = """You turn questions about an advertising dataset into a \
structured plan. You do not answer the question and you never state a number - \
a separate system computes the answer from your plan.

THE DATA
One row per campaign, creative and day. It contains only this advertiser's own \
campaigns.

  metrics available   {metrics}
  break down by       {groupings}
  channels            google_search, meta, youtube, linkedin
  objectives          conversions, traffic, awareness
  covers              {date_min} to {date_max}

CHOOSING AN INTENT
  aggregate         a total, optionally broken down
                    "what did we spend by channel"
  rank              order groups by a metric, best or worst first
                    "which campaign made the most revenue"
  anomaly           explain a movement over time
                    "conversions dropped yesterday, what happened"
  recommend_pause   which campaign to stop running
                    "which campaign should we turn off"
  refuse            the dataset cannot answer it

TIME PERIODS
Never compute or write a date. Describe the period with period_kind and \
period_n and it will be resolved for you. "The last eight weeks" is \
period_kind=last_n_weeks with period_n=8. Use period_kind=explicit only when \
the question names actual calendar dates.
Available: {periods}

WHEN TO REFUSE
Use intent=refuse whenever answering would need something not listed under THE \
DATA above - another company's figures, a metric that is not there, a channel \
that is not there, or anything outside the period covered. Do not approximate, \
and do not substitute a related question you can answer.
In refusal_reason, name what the question asked for and what the dataset holds \
instead. Be specific and brief.

Refusing correctly is a right answer, not a failure."""


NARRATE_SYSTEM_PROMPT = """You write the finding that accompanies a chart on a \
marketing dashboard.

You are given a question and the result table that has already been computed \
from it. Write two or three short sentences stating what the result shows.

RULES
Every number you write must appear in the result table. Do not add, average, \
convert, project or otherwise derive any figure - if it is not in the table, \
it does not go in the sentence.
All money is USD, already converted from the original currencies.
Lead with the answer to the question. Add one observation only if the table \
supports it.
No preamble, no bullet points, no restating the question, no offers of further \
analysis. Plain prose."""


class LLMError(Exception):
    """The provider could not be reached or returned something unusable."""


# ---------------------------------------------------------------------------
# Azure OpenAI / OpenAI
# ---------------------------------------------------------------------------

class OpenAIClient:
    """Talks to Azure OpenAI, or to OpenAI directly - the call shape is the same.

    Strict Structured Outputs constrain decoding against PLAN_SCHEMA, so the
    returned plan cannot be malformed JSON, cannot contain a field we do not
    know, and cannot hold an enum value outside the allowed set. That removes
    the syntactic failures. The validator still runs afterwards, because a
    perfectly well-formed plan can still describe the wrong analysis.

    Not every deployment supports strict schemas. If one rejects the request,
    we fall back to plain JSON mode with the schema in the prompt and let the
    validator do the work it was going to do anyway.
    """

    def __init__(self):
        provider = os.getenv("LLM_PROVIDER", "azure_openai").strip().lower()
        self.model = None
        self.supports_strict_schema = True

        if provider == "azure_openai":
            from openai import AzureOpenAI

            endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "").strip()
            key = os.getenv("AZURE_OPENAI_API_KEY", "").strip()
            deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "").strip()
            if not (endpoint and key and deployment):
                raise LLMError(
                    "Azure OpenAI needs AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY "
                    "and AZURE_OPENAI_DEPLOYMENT. Copy .env.example to .env and fill "
                    "them in, or run with --offline."
                )
            self.client = AzureOpenAI(
                azure_endpoint=endpoint,
                api_key=key,
                api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21").strip(),
            )
            # On Azure the deployment name stands in for the model name.
            self.model = deployment

        elif provider == "openai":
            from openai import OpenAI

            key = os.getenv("OPENAI_API_KEY", "").strip()
            if not key:
                raise LLMError("LLM_PROVIDER=openai needs OPENAI_API_KEY")
            self.client = OpenAI(api_key=key)
            self.model = os.getenv("OPENAI_MODEL", "gpt-5.6-luna").strip()

        else:
            raise LLMError(
                "Unknown LLM_PROVIDER {!r}; expected azure_openai or openai".format(
                    provider
                )
            )

    def plan(self, question, data_min, data_max, retry_hint=""):
        system = PLAN_SYSTEM_PROMPT.format(
            metrics=", ".join(METRICS),
            groupings=", ".join(GROUPINGS),
            periods=", ".join(PERIOD_KINDS),
            date_min=data_min,
            date_max=data_max,
        )
        user = question
        if retry_hint:
            # The validator rejected the previous attempt. Telling the model what
            # was wrong is far more effective than asking it to try again.
            user += (
                "\n\nA previous attempt was rejected: {}\n"
                "Return a corrected plan.".format(retry_hint)
            )

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        if self.supports_strict_schema:
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": "query_plan",
                            "strict": True,
                            "schema": PLAN_SCHEMA,
                        },
                    },
                )
                return self._parse(response)
            except Exception as exc:
                if not _looks_like_unsupported_schema(exc):
                    raise LLMError("planning call failed: {}".format(exc)) from exc
                # Remember, so the fallback is taken directly next time.
                self.supports_strict_schema = False

        # Fallback: JSON mode with the schema described in the prompt.
        messages[0]["content"] += "\n\nReturn JSON matching exactly this schema:\n"
        messages[0]["content"] += json.dumps(PLAN_SCHEMA, indent=2)
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            raise LLMError("planning call failed: {}".format(exc)) from exc
        return self._parse(response)

    def narrate(self, question, table_text, caveats):
        user = "Question: {}\n\nResult:\n{}".format(question, table_text)
        if caveats:
            user += "\n\nData notes (mention only if they affect the answer):\n"
            user += "\n".join("- " + c for c in caveats)

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": NARRATE_SYSTEM_PROMPT},
                    {"role": "user", "content": user},
                ],
                # Capped rather than requested politely in the prompt. Output
                # tokens dominate the cost of this workload, so the limit is
                # enforced where it cannot be ignored.
                max_completion_tokens=int(os.getenv("LLM_MAX_NARRATION_TOKENS", "220")),
            )
        except Exception as exc:
            raise LLMError("narration call failed: {}".format(exc)) from exc

        return (response.choices[0].message.content or "").strip()

    @staticmethod
    def _parse(response):
        content = response.choices[0].message.content
        if not content:
            raise LLMError("the model returned an empty plan")
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMError("the model returned unparseable JSON: {}".format(exc))


def _looks_like_unsupported_schema(exc):
    """Did the deployment reject strict schemas, as opposed to failing for real?"""
    text = str(exc).lower()
    return any(
        marker in text
        for marker in ("json_schema", "response_format", "unsupported", "not supported")
    )


# ---------------------------------------------------------------------------
# Offline
# ---------------------------------------------------------------------------

class OfflineClient:
    """Replays recorded responses. No network, no key.

    Two uses. It keeps the tests hermetic, and it means the whole pipeline can
    be demonstrated from a clean checkout by someone who has not been given an
    API key - which matters when the brief asks that the project run from a
    clean checkout by following the README.

    Lookup is by exact question text. A question with no recording raises rather
    than guessing, so a silent fallback can never be mistaken for a real answer.
    """

    def __init__(self, fixtures_path=None):
        path = Path(fixtures_path or FIXTURES)
        if not path.exists():
            raise LLMError(
                "No recorded responses at {}. Run with a configured provider "
                "first, or use tools/record_fixtures.py".format(path)
            )
        self.recordings = json.loads(path.read_text(encoding="utf-8"))

    def plan(self, question, data_min, data_max, retry_hint=""):
        record = self.recordings.get(question.strip())
        if record is None:
            raise LLMError(
                "No recorded plan for {!r}. Offline mode only answers the "
                "questions that were recorded.".format(question)
            )
        return record["plan"]

    def narrate(self, question, table_text, caveats):
        record = self.recordings.get(question.strip())
        if record is None or "narration" not in record:
            raise LLMError("No recorded narration for {!r}".format(question))
        return record["narration"]


def build_client(offline=False):
    """Pick a client. Falls back to offline only when explicitly asked."""
    if offline:
        return OfflineClient()
    return OpenAIClient()
