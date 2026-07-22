"""signoz-funnel-mcp -- MCP tools for SigNoz Trace Funnels.

Re-exports the reusable REST client so other components can simply do::

    from signoz_funnel_mcp import SigNozFunnelClient, FunnelStep
"""

from signoz_funnel_mcp.client import (
    FunnelStep,
    SigNozAuthError,
    SigNozError,
    SigNozFunnelClient,
    SigNozPermissionError,
    ZeroTraceNaNError,
    resolve_window_ns,
)

__all__ = [
    "SigNozFunnelClient",
    "FunnelStep",
    "SigNozError",
    "SigNozAuthError",
    "SigNozPermissionError",
    "ZeroTraceNaNError",
    "resolve_window_ns",
]

__version__ = "0.1.0"
