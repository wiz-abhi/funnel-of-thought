"""The observed agent: a 4-node LangGraph implementing a reasoning contract.

    plan -> tool -> validate -> respond

A run takes one of three behaviour modes (see :class:`RunMode`), which is what
makes the funnel demo work:

    healthy       plan -> tool -> validate -> respond      (contract honoured)
    skipped       plan -> tool -> respond                  (no validate span at all)
    out_of_order  plan -> validate -> tool -> respond      (validate span exists,
                                                            but fires BEFORE the
                                                            tool result exists)

`out_of_order` is the mode that powers the money screenshot. If validate were
only ever *skipped*, a naive `GROUP BY span_name COUNT` would report the same
64% the funnel does, and the demo would prove nothing. When validate instead
runs *early*, the counter says validate is present in 100% of traces while the
ordered funnel says only 64% have it in the correct position -- which is the
point: a counter is structurally blind to per-trace ordering.

The 64% is not discovered, it is INJECTED: `--validate-rate 0.64` decides how
many runs honour the contract, and the funnel's job is to recover that rate from
the traces alone. Treat it as a calibration harness -- the known injection rate
is what makes the counter's 100% demonstrably wrong rather than merely
suspicious.

Span shape of one run (all in a SINGLE trace):

    fot.agent.run                          <- root, stable name
      agent.plan                           <- funnel step 1
        chat {model}                       <- GenAI semconv LLM span
      agent.tool                           <- funnel step 2
        search.query
      agent.validate                       <- funnel step 3 (absent when skipped,
        chat {model}                          emitted before agent.tool when
      agent.respond                           out_of_order)
        chat {model}                       <- funnel step 4

The ``agent.*`` names are namespaced on purpose. Stock LangGraph
instrumentation names node spans after the bare node id ("plan", "tool", ...) --
stable, but generic enough to collide with unrelated spans. See README.
"""

from __future__ import annotations

import logging
import random
from enum import Enum
from typing import Any, TypedDict

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from .llm import LLMClient
from .otel_setup import get_tracer
from .timing import Timing
from .tools import pick_question, search

log = logging.getLogger("fot.agent")


class RunMode(str, Enum):
    """How a single run treats the validate step."""

    HEALTHY = "healthy"
    SKIPPED = "skipped"
    OUT_OF_ORDER = "out_of_order"


class AgentState(TypedDict, total=False):
    question: str
    plan: str
    sources: list[str]
    draft: str
    answer: str
    validated: bool
    validate_done: bool
    #: One of :class:`RunMode`, decided up front by the batch runner so the
    #: funnel's conversion rate lands on an exact, intended number.
    mode: str
    #: Explicit OTel parent context. LangGraph may execute nodes on a worker
    #: thread, and contextvar propagation across executors is not guaranteed --
    #: passing the root context through state makes "one run == one trace" a
    #: structural guarantee rather than a hope.
    otel_ctx: Any
    _deps: Any


class Deps:
    """Non-serializable per-run dependencies handed to the node functions."""

    def __init__(self, llm: LLMClient, rng: random.Random, timing: Timing):
        self.llm = llm
        self.rng = rng
        self.timing = timing


def _node_span(state: AgentState, name: str):
    """Start a node span parented at the run root."""
    return get_tracer().start_as_current_span(
        name, context=state.get("otel_ctx"), kind=SpanKind.INTERNAL
    )


# --------------------------------------------------------------------------
# Nodes
# --------------------------------------------------------------------------


def plan_node(state: AgentState) -> AgentState:
    deps: Deps = state["_deps"]
    with _node_span(state, "agent.plan") as span:
        span.set_attribute("fot.step", "plan")
        prompt = (
            "You are a research agent. In one short sentence, outline how you would "
            f"answer this question using a search tool: {state['question']}"
        )
        # Load-bearing: the `chat {model}` span created inside llm.chat() is a
        # CHILD of this one, and without a gap it inherits this span's exact start
        # timestamp -- so a funnel keyed `agent.plan -> chat {model}` fails its
        # strict ordering test and reads ~6% instead of the model's real share of
        # traffic. See Timing.separate().
        deps.timing.separate()
        result = deps.llm.chat(prompt, step="plan")
        # Load-bearing: guarantees a distinct, ordered timestamp. See timing.py.
        deps.timing.sleep_for("plan", deps.rng)
        span.set_attribute("fot.plan.length", len(result.text))
        log.info("planned run for question=%r", state["question"])
        return {"plan": result.text}


def tool_node(state: AgentState) -> AgentState:
    deps: Deps = state["_deps"]
    with _node_span(state, "agent.tool") as span:
        span.set_attribute("fot.step", "tool")
        sources = search(state["question"], rng=deps.rng, timing=deps.timing)
        span.set_attribute("fot.tool.source_count", len(sources))
        # The draft answer is assembled from what the tool returned. `validate`
        # is supposed to check the draft against exactly these sources.
        draft = " ".join(sources)
        log.info("retrieved %d sources", len(sources))
        return {"sources": sources, "draft": draft}


def validate_node(state: AgentState) -> AgentState:
    """Check the draft against the retrieved sources.

    Never runs in SKIPPED mode, so no span is produced at all. In OUT_OF_ORDER
    mode it runs before `tool`, meaning there is no draft and no sources yet --
    the span exists and looks fine to a counter, but the check is vacuous.
    """
    deps: Deps = state["_deps"]
    premature = not state.get("sources")
    with _node_span(state, "agent.validate") as span:
        span.set_attribute("fot.step", "validate")
        span.set_attribute("fot.validate.premature", premature)
        prompt = (
            "Answer SUPPORTED or UNSUPPORTED. Is this draft fully supported by the "
            f"sources?\nSOURCES: {state.get('sources', [])}\n"
            f"DRAFT: {state.get('draft', '(not drafted yet)')}"
        )
        result = deps.llm.chat(prompt, step="validate")
        deps.timing.sleep_for("validate", deps.rng)

        if premature:
            # Validating before the evidence exists cannot conclude anything.
            supported = False
            span.set_status(
                Status(StatusCode.ERROR, "validate ran before tool result was available")
            )
            log.warning("premature validation for question=%r", state["question"])
        else:
            supported = "UNSUPPORTED" not in result.text.upper()
            if not supported:
                span.set_status(Status(StatusCode.ERROR, "draft not supported by sources"))
                log.warning("validation failed for question=%r", state["question"])

        span.set_attribute("fot.validate.supported", supported)
        return {"validated": supported, "validate_done": True}


def respond_node(state: AgentState) -> AgentState:
    deps: Deps = state["_deps"]
    with _node_span(state, "agent.respond") as span:
        span.set_attribute("fot.step", "respond")
        # Recorded so a dashboard can contrast genuinely validated answers
        # against ones that reached the user unchecked.
        span.set_attribute("fot.response.validated", bool(state.get("validated", False)))
        prompt = (
            f"Question: {state['question']}\nNotes: {state['draft']}\n"
            "Write a one-sentence answer."
        )
        result = deps.llm.chat(prompt, step="respond")
        deps.timing.sleep_for("respond", deps.rng)
        log.info("responded (validated=%s)", state.get("validated", False))
        return {"answer": result.text}


# --------------------------------------------------------------------------
# Routing -- this is where the three behaviour modes are realised
# --------------------------------------------------------------------------


def _route_after_plan(state: AgentState) -> str:
    """OUT_OF_ORDER runs validate here, before the tool has produced anything."""
    if state.get("mode") == RunMode.OUT_OF_ORDER.value:
        return "validate"
    return "tool"


def _route_after_validate(state: AgentState) -> str:
    """After a premature validate we still have to actually call the tool."""
    if state.get("mode") == RunMode.OUT_OF_ORDER.value:
        return "tool"
    return "respond"


def _route_after_tool(state: AgentState) -> str:
    """Structural skip: routing straight to respond means validate never runs,
    so the ``agent.validate`` span is absent from the trace entirely."""
    if state.get("mode") == RunMode.HEALTHY.value and not state.get("validate_done"):
        return "validate"
    return "respond"


# --------------------------------------------------------------------------
# Graph
# --------------------------------------------------------------------------


def build_graph():
    """Compile the 4-node graph. Cheap enough to build once and reuse."""
    from langgraph.graph import END, START, StateGraph

    g = StateGraph(AgentState)
    g.add_node("plan", plan_node)
    g.add_node("tool", tool_node)
    g.add_node("validate", validate_node)
    g.add_node("respond", respond_node)

    g.add_edge(START, "plan")
    g.add_conditional_edges(
        "plan", _route_after_plan, {"validate": "validate", "tool": "tool"}
    )
    g.add_conditional_edges(
        "validate", _route_after_validate, {"tool": "tool", "respond": "respond"}
    )
    g.add_conditional_edges(
        "tool", _route_after_tool, {"validate": "validate", "respond": "respond"}
    )
    g.add_edge("respond", END)
    return g.compile()


def run_once(
    app,
    *,
    llm: LLMClient,
    rng: random.Random,
    mode: RunMode,
    timing: Timing,
    question: str | None = None,
    run_index: int = 0,
) -> dict:
    """Execute one full plan->tool->[validate]->respond loop inside ONE trace."""
    question = question or pick_question(rng)
    tracer = get_tracer()

    with tracer.start_as_current_span("fot.agent.run", kind=SpanKind.SERVER) as root:
        root.set_attribute("fot.run.index", run_index)
        root.set_attribute("fot.run.model", llm.model)
        root.set_attribute("fot.run.stub", llm.stub)
        root.set_attribute("fot.run.mode", mode.value)
        root.set_attribute("fot.question", question)

        ctx = trace.set_span_in_context(root)
        state: AgentState = {
            "question": question,
            "mode": mode.value,
            "otel_ctx": ctx,
            "_deps": Deps(llm, rng, timing),
        }
        # Attach the root context to this thread too, so anything we did not
        # parent explicitly (e.g. optional auto-instrumentation) still lands in
        # the same trace.
        token = otel_context.attach(ctx)
        try:
            final = app.invoke(state)
        except Exception as exc:  # pragma: no cover - surfaced to the runner
            root.set_status(Status(StatusCode.ERROR, str(exc)))
            root.record_exception(exc)
            raise
        finally:
            otel_context.detach(token)

        root.set_attribute("fot.run.validated", bool(final.get("validated", False)))
        return {
            "question": question,
            "answer": final.get("answer", ""),
            "validated": final.get("validated", False),
            "mode": mode.value,
            "trace_id": format(root.get_span_context().trace_id, "032x"),
        }
