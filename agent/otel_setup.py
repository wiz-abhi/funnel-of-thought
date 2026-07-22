"""OpenTelemetry wiring for the Funnel of Thought observed agent.

Exports traces AND logs over OTLP/HTTP to a local SigNoz collector. Logs are
emitted through the stdlib `logging` module so they automatically carry the
active trace/span id, which gives us the trace -> logs jump in the SigNoz UI.

Everything here is env-overridable so the batch runner can point at a different
collector or rename the service without code changes.
"""

from __future__ import annotations

import logging
import os

from opentelemetry import trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)

DEFAULT_ENDPOINT = "http://localhost:4318"
DEFAULT_SERVICE_NAME = "fot-agent"

#: Name reported as ``gen_ai.agent.name`` on every LLM span.
AGENT_NAME = os.environ.get("FOT_AGENT_NAME", "funnel-of-thought")

_TRACER_PROVIDER: TracerProvider | None = None
_LOGGER_PROVIDER: LoggerProvider | None = None


def _endpoint() -> str:
    return os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", DEFAULT_ENDPOINT).rstrip("/")


def _service_name() -> str:
    return os.environ.get("OTEL_SERVICE_NAME", DEFAULT_SERVICE_NAME)


def init_telemetry(
    *,
    console: bool = False,
    otlp: bool = True,
    auto_instrument: bool = False,
) -> TracerProvider:
    """Install global tracer/logger providers. Idempotent.

    Args:
        console: also dump every span to stdout (used for offline verification).
        otlp: export to the OTLP/HTTP collector. Turn off for pure console runs.
        auto_instrument: enable OpenInference LangChain auto-instrumentation.
            Off by default -- see agent/README.md ("Spike result") for why: it
            works, but it duplicates our explicit node spans and adds noise to
            the demo traces.
    """
    global _TRACER_PROVIDER, _LOGGER_PROVIDER
    if _TRACER_PROVIDER is not None:
        return _TRACER_PROVIDER

    resource = Resource.create(
        {
            "service.name": _service_name(),
            "service.version": os.environ.get("FOT_VERSION", "0.1.0"),
            "deployment.environment": os.environ.get("FOT_ENV", "local"),
        }
    )

    provider = TracerProvider(resource=resource)
    if otlp:
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{_endpoint()}/v1/traces"))
        )
    if console:
        # SimpleSpanProcessor so spans print in completion order, immediately.
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    _TRACER_PROVIDER = provider

    if otlp:
        logger_provider = LoggerProvider(resource=resource)
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{_endpoint()}/v1/logs"))
        )
        set_logger_provider(logger_provider)
        _LOGGER_PROVIDER = logger_provider

        handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(handler)

    if auto_instrument:
        # Optional: stock LangGraph instrumentation. Emits bare node-name spans
        # ("plan", "tool", ...) alongside our explicit "agent.*" spans.
        from openinference.instrumentation.langchain import LangChainInstrumentor

        LangChainInstrumentor().instrument(tracer_provider=provider)

    return provider


def get_tracer(name: str = "fot.agent") -> trace.Tracer:
    return trace.get_tracer(name)


def shutdown_telemetry() -> None:
    """Flush and close exporters. Always call this before the process exits or
    the last batch of spans is silently dropped."""
    if _TRACER_PROVIDER is not None:
        _TRACER_PROVIDER.force_flush()
        _TRACER_PROVIDER.shutdown()
    if _LOGGER_PROVIDER is not None:
        _LOGGER_PROVIDER.force_flush()
        _LOGGER_PROVIDER.shutdown()
