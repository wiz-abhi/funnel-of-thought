"""LLM client that emits OTel GenAI-semconv spans.

Why we build the LLM span by hand instead of relying on auto-instrumentation:

1. We need the span NAME to follow the GenAI convention ``{operation} {model}``
   -- e.g. ``chat gemini-3.1-flash-lite``. That naming is the whole point of the
   teaching demo: swap the model and one logical "call the LLM" step fragments
   into N distinct span names, so a SigNoz funnel keyed on it drops to 0%.
   OpenInference names LangChain LLM spans after the class
   (``ChatGoogleGenerativeAI``), which is model-independent -- the opposite of
   what the demo needs.
2. We need a stub mode with the exact same span shape and no network calls.

Nothing here ever prints or logs the API key.
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass

from opentelemetry.trace import SpanKind

from .otel_setup import AGENT_NAME, get_tracer

DEFAULT_MODEL = "gemini-3.1-flash-lite"
GEN_AI_SYSTEM = "gcp.gemini"

# Hard ceiling on a single LLM call. A batch run must fail loudly rather than
# hang: an unbounded call stalls the whole generator with no error and no span.
LLM_TIMEOUT_SECONDS = 45.0


@dataclass
class LLMResult:
    text: str
    input_tokens: int
    output_tokens: int


class LLMError(RuntimeError):
    """Raised when a real LLM call is requested but cannot be made."""


def resolve_model(cli_model: str | None = None) -> str:
    return cli_model or os.environ.get("FOT_MODEL", DEFAULT_MODEL)


def _api_key() -> str | None:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


class LLMClient:
    """Thin wrapper around Gemini (or a deterministic stub).

    Args:
        model: model id, e.g. ``gemini-3.1-flash-lite``.
        stub: skip the network entirely and return canned text. Span shape and
            attributes are identical, so the funnel pipeline is fully testable
            without burning free-tier quota (15 RPM).
        rng: seeded RNG, used only in stub mode so runs are reproducible.
    """

    def __init__(self, model: str, *, stub: bool = False, rng: random.Random | None = None):
        self.model = model
        self.stub = stub
        self._rng = rng or random.Random()
        self._client = None
        if not stub:
            self._client = self._build_client()

    def _build_client(self):
        key = _api_key()
        if not key:
            raise LLMError(
                "No API key found. Set GEMINI_API_KEY (or GOOGLE_API_KEY) in your "
                "environment, or run with --stub for offline mode."
            )
        from langchain_google_genai import ChatGoogleGenerativeAI

        # timeout and max_retries are NOT optional here. Without them the
        # underlying client can block forever on a stalled connection: a real
        # run of this generator hung for 34 minutes mid-batch, having completed
        # 64 of 120 runs, while the API itself was answering in 1.4s. The
        # process looked alive and produced no error -- exactly the silent-hang
        # failure mode this project exists to make visible.
        return ChatGoogleGenerativeAI(
            model=self.model,
            google_api_key=key,
            temperature=0.2,
            timeout=LLM_TIMEOUT_SECONDS,
            max_retries=2,
        )

    def chat(self, prompt: str, *, step: str, operation: str = "chat") -> LLMResult:
        """One LLM call, wrapped in a GenAI-semconv span.

        The span name is deliberately ``f"{operation} {self.model}"`` -- model
        name embedded, per OTel GenAI semconv.
        """
        span_name = f"{operation} {self.model}"
        tracer = get_tracer()
        with tracer.start_as_current_span(span_name, kind=SpanKind.CLIENT) as span:
            span.set_attribute("gen_ai.operation.name", operation)
            span.set_attribute("gen_ai.system", GEN_AI_SYSTEM)
            span.set_attribute("gen_ai.request.model", self.model)
            span.set_attribute("gen_ai.agent.name", AGENT_NAME)
            # Which reasoning step issued this call -- lets us slice LLM spans
            # by funnel step even though the span name only knows the model.
            span.set_attribute("fot.step", step)

            if self.stub:
                result = self._stub_response(prompt, step)
            else:
                result = self._live_response(prompt)

            span.set_attribute("gen_ai.usage.input_tokens", result.input_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", result.output_tokens)
            span.set_attribute("gen_ai.response.model", self.model)
            return result

    def _live_response(self, prompt: str) -> LLMResult:
        response = self._client.invoke(prompt)
        text = response.content if isinstance(response.content, str) else str(response.content)
        usage = getattr(response, "usage_metadata", None) or {}
        return LLMResult(
            text=text,
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
        )

    def _stub_response(self, prompt: str, step: str) -> LLMResult:
        """Deterministic canned output. Token counts are plausible fakes so the
        GenAI attributes are never zero/empty in the demo."""
        canned = {
            "plan": "1) search the knowledge base 2) draft an answer 3) check it against sources",
            "respond": "Based on the retrieved sources, here is the answer.",
            "validate": "SUPPORTED",
        }
        text = canned.get(step, "ok")
        return LLMResult(
            text=text,
            input_tokens=len(prompt.split()) + self._rng.randint(5, 40),
            output_tokens=len(text.split()) + self._rng.randint(2, 20),
        )
