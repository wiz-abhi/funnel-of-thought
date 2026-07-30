"""``signoz-funnel-mcp`` -- an MCP stdio server exposing SigNoz Trace Funnels.

SigNoz's official MCP server ships 41 tools and **none of them touch trace
funnels**, so an AI agent currently has no way to create or read one. This
server fills that gap with five tools:

* :func:`list_funnels`           -- enumerate existing funnels
* :func:`create_funnel`          -- create a funnel *and* set its steps in one call
* :func:`get_funnel_analytics`   -- per-step conversion (with n) + end-to-end metrics
* :func:`get_funnel_slow_traces` -- the slowest traces for a step transition
* :func:`delete_funnel`          -- clean up

Run it::

    SIGNOZ_URL=http://localhost:8080 SIGNOZ_JWT=... python -m signoz_funnel_mcp.server

Configuration is entirely by environment variable (never CLI args -- tokens
should not land in a process list):

======================  ====================================================
``SIGNOZ_URL``          Base URL, default ``http://localhost:8080``
``SIGNOZ_JWT``          Bearer token (EDITOR/ADMIN needed for writes)
``SIGNOZ_API_KEY``      Alternative ``SIGNOZ-API-KEY`` credential
``SIGNOZ_TIMEOUT``      Per-request timeout in seconds, default ``30``
======================  ====================================================
"""

from __future__ import annotations

import functools
import os
from collections.abc import Callable
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP

try:  # package import (python -m signoz_funnel_mcp.server)
    from signoz_funnel_mcp.client import (
        SigNozError,
        SigNozFunnelClient,
        resolve_window_ns,
    )
except ImportError:  # direct script import (python server.py)
    from client import (  # type: ignore[no-redef]
        SigNozError,
        SigNozFunnelClient,
        resolve_window_ns,
    )

mcp = FastMCP("signoz-funnel-mcp")


def _client() -> SigNozFunnelClient:
    """Build a client from the environment.

    Constructed per call rather than once at import so the server starts even
    when credentials are absent -- the agent then gets a readable error from
    the first tool call instead of a crash at startup.
    """
    return SigNozFunnelClient(timeout=float(os.environ.get("SIGNOZ_TIMEOUT", "30")))


def offloaded(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Expose a blocking tool function as an async MCP tool.

    FastMCP calls a plain ``def`` tool directly on the event loop -- there is no
    implicit thread offload. Every tool here then makes blocking ``httpx`` calls
    with a 30s default timeout, and ``get_funnel_analytics`` makes three
    sequentially, so an unreachable SigNoz froze the entire server for up to 90s.
    Measured: a ``ping`` sent 0.4s into a 6s call was not answered until +6.29s.

    While the loop is blocked the server cannot read stdin, answer ``ping``, or
    honour ``notifications/cancelled``, and MCP clients conclude it has hung and
    kill it. Running the blocking work in a worker thread keeps the loop free.

    ``functools.wraps`` sets ``__wrapped__``, so ``inspect.signature`` still sees
    the original parameters and FastMCP derives the same input schema.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))

    return wrapper


def _error(exc: Exception) -> dict[str, Any]:
    """Render an exception as a structured tool result.

    MCP tools are more useful to an agent when failures come back as data it
    can reason about than as opaque protocol errors.
    """
    return {
        "ok": False,
        "error": str(exc),
        "error_type": type(exc).__name__,
        "hint": (
            "Check SIGNOZ_URL and that SIGNOZ_JWT / SIGNOZ_API_KEY is set and "
            "unexpired. Creating or modifying funnels additionally requires an "
            "EDITOR or ADMIN identity."
        ),
    }


@mcp.tool()
@offloaded
def list_funnels() -> dict[str, Any]:
    """List every SigNoz trace funnel, with its id, name, and step definitions.

    Start here when you have a funnel name but need its id, or to check whether
    a funnel already exists before creating a duplicate.
    """
    try:
        with _client() as client:
            funnels = client.list_funnels()
        return {
            "ok": True,
            "count": len(funnels),
            "funnels": [
                {
                    "funnel_id": f.get("funnel_id"),
                    "funnel_name": f.get("funnel_name"),
                    "step_count": len(f.get("steps") or []),
                    "steps": [
                        {
                            "step": s.get("step_order"),
                            "service": s.get("service_name"),
                            "span": s.get("span_name"),
                            "name": s.get("name"),
                        }
                        for s in (f.get("steps") or [])
                    ],
                    "created_by": f.get("user_email"),
                }
                for f in funnels
            ],
        }
    except Exception as exc:  # noqa: BLE001 - surfaced as structured data
        return _error(exc)


@mcp.tool()
@offloaded
def create_funnel(name: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Create a SigNoz trace funnel and set its steps in a single call.

    This wraps two REST calls (``POST /new`` then ``PUT /steps/update``) and
    handles the undocumented requirements that make hand-rolling them fail:
    millisecond timestamps on both, the mandatory ``timestamp`` on the update,
    and omitting each step's ``id`` so SigNoz mints a valid UUID. If setting
    steps fails, the empty funnel is deleted rather than left behind.

    Args:
        name: Funnel name, e.g. ``"agent-tool-pipeline"``.
        steps: Ordered list of at least 2 steps. Each is a dict with:

            * ``service`` (required) -- the service name as it appears in traces
            * ``span`` (required) -- the exact span name
            * ``name`` (optional) -- friendly label for charts
            * ``latency_type`` (optional) -- ``"p90"``/``"p95"``/``"p99"``
              (default ``p99``; **``p50`` is not implemented by SigNoz** and
              silently yields p99 -- you will get a warning back)
            * ``has_errors`` (optional) -- only match errored spans

        Example::

            [{"service": "frontend", "span": "GET /cart", "name": "browse"},
             {"service": "payments", "span": "charge", "name": "pay"}]

    Returns:
        ``funnel_id``, the normalized steps, and any ``warnings``.

    Note:
        All steps must occur within the **same trace**. SigNoz matches the
        first occurrence of each step and enforces order, so funnels cannot
        see loops or retries.
    """
    try:
        with _client() as client:
            result = client.create_funnel_with_steps(name, steps)
        result["ok"] = True
        result["next"] = (
            f"Call get_funnel_analytics(funnel_id='{result['funnel_id']}') to measure it."
        )
        return result
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool()
@offloaded
def get_funnel_analytics(
    funnel_id: str | None = None,
    funnel_name: str | None = None,
    time_range: str = "24h",
) -> dict[str, Any]:
    """Measure a funnel: per-step conversion with trace counts, plus end-to-end metrics.

    Identify the funnel by either ``funnel_id`` or ``funnel_name``.

    Every step row carries an explicit ``n`` (number of traces reaching that
    step) alongside its conversion percentages, so any chart built from this
    can label each bar with its sample size.

    Args:
        funnel_id: The funnel's UUID.
        funnel_name: Its name (resolved to an id for you). Ignored if
            ``funnel_id`` is given.
        time_range: Relative window like ``"30m"``, ``"24h"``, ``"7d"``,
            ``"2w"``. Converted to the nanosecond bounds the API needs.

    Returns:
        ``steps`` (one row per step: ``n``, ``errors``,
        ``conversion_from_previous_pct``, ``conversion_from_start_pct``,
        ``dropped_from_previous``), ``end_to_end`` metrics, and ``totals``.

    Note:
        When zero traces complete a step, SigNoz returns HTTP 500
        ``unsupported value: NaN`` instead of a 0% result (SigNoz issue
        #12143). This tool detects that and reports a clean zero, flagged via
        ``end_to_end.zero_trace_fallback``.

        If a step shows 0 traces, read ``diagnostics``. Besides a wrong span
        name, the usual culprit is that SigNoz enforces step ordering
        **strictly** (``t_next > t_prev``): spans that complete within the same
        clock tick -- instantaneous no-ops -- are not counted as ordered and
        will silently under-count even when the sequence is correct.

        ``end_to_end.errors`` is a MAX across steps, not a sum, and its
        ``latency`` is hardcoded to p99 by SigNoz -- both are restated in
        ``end_to_end.caveats``.
    """
    try:
        with _client() as client:
            report = client.funnel_analytics(
                funnel_id=funnel_id, funnel_name=funnel_name, time_range=time_range
            )
        report["ok"] = True
        return report
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool()
@offloaded
def get_funnel_slow_traces(
    funnel_id: str | None = None,
    funnel_name: str | None = None,
    time_range: str = "24h",
    step_start: int = 1,
    step_end: int = 2,
) -> dict[str, Any]:
    """Fetch the slowest traces for one step transition of a funnel.

    Use this after ``get_funnel_analytics`` shows a drop-off or a high latency
    between two steps -- it gives you concrete trace ids to open in SigNoz.

    Args:
        funnel_id: The funnel's UUID.
        funnel_name: Its name, as an alternative to the id.
        time_range: Relative window, e.g. ``"24h"``.
        step_start: 1-based index of the transition's first step.
        step_end: 1-based index of its second step. Must differ from
            ``step_start``.

    Returns:
        Up to 5 rows of ``{trace_id, duration_ms, span_count}``, plus
        ``error_traces`` sampled from the same transition.

    Note:
        **At most 5 rows.** SigNoz hardcodes ``ORDER BY duration_ms DESC LIMIT
        5``; there is no way to page or raise it. The query is also pairwise --
        it measures ``step_start`` to ``step_end`` only, not the whole funnel.
    """
    try:
        if step_start == step_end:
            raise SigNozError(
                "step_start and step_end must differ; SigNoz rejects an "
                "identical pair with 'step start and end cannot be the same'."
            )
        with _client() as client:
            resolved_id = client.resolve_funnel_id(funnel_id, funnel_name)
            start_ns, end_ns = resolve_window_ns(time_range)
            slow = client.slow_traces(resolved_id, start_ns, end_ns, step_start, step_end)
            errors = client.error_traces(resolved_id, start_ns, end_ns, step_start, step_end)
        return {
            "ok": True,
            "funnel_id": resolved_id,
            "transition": f"step {step_start} -> step {step_end}",
            "time_range": time_range,
            "slow_traces": slow,
            "error_traces": errors,
            "limit_note": (
                "SigNoz hardcodes LIMIT 5 on slow-traces -- this is the complete "
                "result set, not a page."
            ),
        }
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool()
@offloaded
def delete_funnel(funnel_id: str) -> dict[str, Any]:
    """Permanently delete a trace funnel by id.

    Irreversible. Use ``list_funnels`` first to confirm you have the right id.
    """
    try:
        with _client() as client:
            client.delete_funnel(funnel_id)
        return {"ok": True, "deleted": funnel_id}
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
