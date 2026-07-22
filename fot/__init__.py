"""Funnel of Thought -- an agent's reasoning contract, measured as a SigNoz funnel.

Public surface:
    :class:`fot.funnels.Funnel` / :func:`fot.funnels.load_suite` -- contracts as code
    :class:`fot.signoz.SigNozClient`                            -- REST + ClickHouse
    :class:`fot.gauges.GaugeEmitter`                            -- OTLP re-emission
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
