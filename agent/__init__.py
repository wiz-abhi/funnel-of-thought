"""Funnel of Thought -- the observed agent.

A 4-node LangGraph agent (plan -> tool -> validate -> respond) instrumented with
OpenTelemetry, whose traces the SigNoz Trace Funnel is computed over.
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
