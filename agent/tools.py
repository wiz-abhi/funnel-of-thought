"""Mocked "search" tool.

Deliberately sandboxed: no network, no external service. It is a deterministic
data generator over a tiny in-memory corpus, so batch runs are reproducible and
the only rate-limited dependency in the whole agent is the LLM itself.
"""

from __future__ import annotations

import random

from opentelemetry.trace import SpanKind

from .otel_setup import get_tracer
from .timing import Timing

#: question -> canned snippets the "search index" returns.
CORPUS: dict[str, list[str]] = {
    "What is a trace funnel?": [
        "A trace funnel measures how many traces reach each ordered step of a workflow.",
        "Each funnel step matches spans by service name and exact span name.",
    ],
    "Why does span cardinality matter?": [
        "High-cardinality span names fragment one logical step across many names.",
        "Funnels use exact-match equality, so fragmented names read as 0% conversion.",
    ],
    "What is OpenTelemetry?": [
        "OpenTelemetry is a vendor-neutral standard for traces, metrics and logs.",
        "The GenAI semantic conventions name LLM spans '{operation} {model}'.",
    ],
    "How does SigNoz store traces?": [
        "SigNoz persists spans in ClickHouse, in the signoz_traces database.",
        "The distributed_signoz_index_v3 table backs most trace queries.",
    ],
    "What does a reasoning contract mean?": [
        "A reasoning contract is the sequence an agent is expected to follow.",
        "Plan, call a tool, validate the draft, then respond to the user.",
    ],
}

QUESTIONS: list[str] = list(CORPUS)


def pick_question(rng: random.Random) -> str:
    return rng.choice(QUESTIONS)


def search(query: str, *, rng: random.Random, timing: Timing) -> list[str]:
    """Mocked retrieval, wrapped in its own child span.

    The span sits under ``agent.tool`` so the funnel's tool step stays a single
    stable name while still showing what the tool actually did.
    """
    tracer = get_tracer()
    with tracer.start_as_current_span("search.query", kind=SpanKind.INTERNAL) as span:
        span.set_attribute("tool.name", "search")
        span.set_attribute("tool.query", query)
        # Load-bearing simulated latency, not cosmetic padding: without it this
        # span shares a timestamp with its neighbours and the funnel's strict
        # ordering comparison fails. See timing.py.
        timing.sleep_for("tool", rng)
        hits = CORPUS.get(query, ["No matching document found."])
        span.set_attribute("tool.result_count", len(hits))
        return hits
