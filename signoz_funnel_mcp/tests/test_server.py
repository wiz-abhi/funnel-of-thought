"""Tests that drive the MCP server, not just its payload builders.

The existing suite covers payload shapes and arithmetic thoroughly but executed
0 of server.py and none of the network paths, so the whole protocol layer and
every error branch were unverified. Four real bugs lived in that gap:

* all five tools ran on the event loop, freezing the server for the full HTTP
  timeout (up to 90s for get_funnel_analytics, which makes three calls)
* step labels were read in list order while counts are read positionally, so an
  unordered definition silently mislabelled every conversion number
* ``funnel_id`` was interpolated into the URL path unvalidated, letting
  ``../dashboards/<uuid>`` retarget an irreversible DELETE
* ``NaN`` could reach the JSON-RPC stream through the dict branch of ``_rows``

These use an in-memory MCP session and ``httpx.MockTransport``: no SigNoz, no
sockets, no credentials.
"""

from __future__ import annotations

import json

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session as connect

from signoz_funnel_mcp import client as C
from signoz_funnel_mcp.server import mcp

FUNNEL_ID = "019f8813-4152-7d1c-8fa3-80250b4817a8"
TOOLS = {
    "list_funnels",
    "create_funnel",
    "get_funnel_analytics",
    "get_funnel_slow_traces",
    "delete_funnel",
}


def _result(call) -> dict:
    """Pull the JSON payload out of an MCP tool result."""
    return json.loads(call.content[0].text)


# --------------------------------------------------------------------------
# protocol surface
# --------------------------------------------------------------------------


async def test_handshake_lists_all_five_tools():
    async with connect(mcp._mcp_server) as session:
        await session.initialize()
        names = {t.name for t in (await session.list_tools()).tools}
    assert names == TOOLS


async def test_every_tool_advertises_a_well_formed_input_schema():
    async with connect(mcp._mcp_server) as session:
        await session.initialize()
        tools = (await session.list_tools()).tools
    for tool in tools:
        schema = tool.inputSchema
        assert schema.get("type") == "object", f"{tool.name} schema is not an object"
        assert "properties" in schema, f"{tool.name} schema has no properties"
        assert tool.description, f"{tool.name} has no description for the model"


async def test_offloading_preserved_the_tool_signatures():
    """The @offloaded decorator must not erase the params FastMCP derives."""
    expected = {
        "list_funnels": set(),
        "create_funnel": {"name", "steps"},
        "delete_funnel": {"funnel_id"},
        "get_funnel_analytics": {"funnel_id", "funnel_name", "time_range"},
        "get_funnel_slow_traces": {
            "funnel_id", "funnel_name", "time_range", "step_start", "step_end",
        },
    }
    async with connect(mcp._mcp_server) as session:
        await session.initialize()
        tools = {t.name: t for t in (await session.list_tools()).tools}
    for name, params in expected.items():
        assert set(tools[name].inputSchema.get("properties", {})) == params, name


async def test_a_failing_tool_returns_data_not_a_protocol_error(monkeypatch):
    """An agent can reason about a structured failure; it cannot about a crash."""
    monkeypatch.setenv("SIGNOZ_URL", "http://127.0.0.1:1")  # nothing listens
    async with connect(mcp._mcp_server) as session:
        await session.initialize()
        call = await session.call_tool("list_funnels", {})
    payload = _result(call)
    assert payload["ok"] is False
    assert payload["error_type"]
    assert payload["hint"]


async def test_tool_output_is_always_json_serialisable(monkeypatch):
    """Whatever happens, what goes on the wire must be valid JSON."""
    monkeypatch.setenv("SIGNOZ_URL", "http://127.0.0.1:1")
    async with connect(mcp._mcp_server) as session:
        await session.initialize()
        for name in ("list_funnels", "get_funnel_analytics"):
            call = await session.call_tool(name, {})
            text = call.content[0].text
            json.loads(text)  # raises if NaN/Infinity leaked through
            assert "NaN" not in text and "Infinity" not in text


# --------------------------------------------------------------------------
# funnel_id validation -- path traversal on an irreversible DELETE
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    [
        "../dashboards/019f8813-4152-7d1c-8fa3-80250b4817a8",
        "abc/../../../v2/rules",
        "019f8813-4152-7d1c-8fa3-80250b4817a8?foo=bar",
        "not-a-uuid",
        "",
    ],
)
def test_safe_id_rejects_anything_that_is_not_a_uuid(bad_id):
    with pytest.raises(C.SigNozError, match="valid funnel id"):
        C.SigNozFunnelClient._safe_id(bad_id)


def test_safe_id_accepts_a_real_uuid():
    assert C.SigNozFunnelClient._safe_id(FUNNEL_ID) == FUNNEL_ID
    assert C.SigNozFunnelClient._safe_id(f"  {FUNNEL_ID}  ") == FUNNEL_ID


async def test_delete_funnel_refuses_a_traversal_id_without_issuing_a_request(monkeypatch):
    """The dangerous case: DELETE is documented as irreversible.

    Credentials are set because the client checks those first; without them the
    call fails for the wrong reason and the test proves nothing.
    """
    monkeypatch.setenv("SIGNOZ_JWT", "test-token")
    monkeypatch.setenv("SIGNOZ_URL", "http://127.0.0.1:1")
    async with connect(mcp._mcp_server) as session:
        await session.initialize()
        call = await session.call_tool(
            "delete_funnel", {"funnel_id": "../dashboards/" + FUNNEL_ID}
        )
    payload = _result(call)
    assert payload["ok"] is False
    assert "valid funnel id" in payload["error"]


# --------------------------------------------------------------------------
# response handling, via MockTransport
# --------------------------------------------------------------------------


def _client_with(handler) -> C.SigNozFunnelClient:
    cl = C.SigNozFunnelClient(base_url="http://signoz.test", jwt="t")
    cl._http = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers=cl._auth_headers(),
        base_url="http://signoz.test",
    )
    return cl


def test_an_html_200_is_reported_as_a_bad_url_not_an_attribute_error():
    """A wrong SIGNOZ_URL points at the SPA, which answers anything with 200."""

    def handler(request):
        return httpx.Response(
            200, text="<!doctype html><title>SigNoz</title>",
            headers={"content-type": "text/html"},
        )

    with _client_with(handler) as cl, pytest.raises(C.SigNozError, match="not JSON"):
        cl.list_funnels()


def test_nan_in_a_dict_response_is_cleaned_before_it_reaches_json():
    """json.dumps would otherwise emit bare NaN, which is not valid JSON."""
    rows = C.SigNozFunnelClient._rows({"latency": float("nan"), "rate": float("inf")})
    assert rows == [{"latency": None, "rate": None}]
    json.loads(json.dumps(rows))  # would raise on NaN


def test_nan_in_a_list_response_is_cleaned_too():
    rows = C.SigNozFunnelClient._rows([{"data": {"latency": float("nan")}}])
    assert rows == [{"latency": None}]


def test_a_500_with_nan_is_classified_as_the_known_signoz_bug():
    def handler(request):
        return httpx.Response(
            500, text="app.ApiResponse.Data: []*v3.Row: v3.Row.Data: unsupported value: NaN"
        )

    with _client_with(handler) as cl, pytest.raises(C.ZeroTraceNaNError):
        cl.list_funnels()


def test_a_401_names_the_expired_token_as_the_likely_cause():
    def handler(request):
        return httpx.Response(401, json={"error": "unauthorized"})

    with _client_with(handler) as cl, pytest.raises(C.SigNozAuthError, match="expired"):
        cl.list_funnels()


def test_a_403_explains_that_writes_need_an_editor_identity():
    def handler(request):
        return httpx.Response(403, json={"error": "forbidden"})

    with _client_with(handler) as cl, pytest.raises(C.SigNozPermissionError, match="EDITOR"):
        cl.list_funnels()


def test_a_huge_error_body_is_truncated_before_entering_the_agent_context():
    def handler(request):
        return httpx.Response(502, text="<html>" + "x" * 8000 + "</html>")

    with _client_with(handler) as cl:
        with pytest.raises(C.SigNozError) as caught:
            cl.list_funnels()
    assert len(str(caught.value)) < 1000, "a proxy error page would flood the context"


# --------------------------------------------------------------------------
# step ordering
# --------------------------------------------------------------------------


def test_analytics_labels_follow_step_order_not_list_order():
    """Counts are read positionally as s1..sN, so labels must be sorted to match."""
    definition = {
        "funnel_id": FUNNEL_ID,
        "steps": [
            {"step_order": 3, "name": "third", "service_name": "s", "span_name": "c"},
            {"step_order": 1, "name": "first", "service_name": "s", "span_name": "a"},
            {"step_order": 2, "name": "second", "service_name": "s", "span_name": "b"},
        ],
    }

    def handler(request):
        path = request.url.path
        if path.endswith("/analytics/steps"):
            return httpx.Response(200, json={"status": "success", "data": [{"data": {
                "total_s1_spans": 100, "total_s2_spans": 50, "total_s3_spans": 10,
                "total_s1_errored_spans": 0, "total_s2_errored_spans": 0,
                "total_s3_errored_spans": 0,
            }}]})
        if path.endswith("/analytics/overview"):
            return httpx.Response(200, json={"status": "success", "data": [{"data": {
                "avg_duration": 1.0, "p99_latency": 2.0, "conversion_rate": 10.0, "errors": 0,
            }}]})
        return httpx.Response(200, json={"status": "success", "data": definition})

    with _client_with(handler) as cl:
        report = cl.funnel_analytics(funnel_id=FUNNEL_ID, time_range="1h")

    got = [(r["step"], r["label"], r["n"]) for r in report["steps"]]
    assert got == [(1, "first", 100), (2, "second", 50), (3, "third", 10)]


def test_steps_update_always_assigns_contiguous_one_based_order():
    """An LLM-supplied step_order must not create gaps or duplicates."""
    steps = [
        C.FunnelStep.from_dict({"service": "s", "span": "a", "step_order": 5}),
        C.FunnelStep.from_dict({"service": "s", "span": "b"}),
        C.FunnelStep.from_dict({"service": "s", "span": "c", "step_order": 0}),
    ]
    payload = C.build_steps_update_payload(FUNNEL_ID, steps)
    assert [s["step_order"] for s in payload["steps"]] == [1, 2, 3]


def test_only_one_credential_header_is_sent():
    """Sending both makes a stale JWT's 401 impossible to diagnose."""
    cl = C.SigNozFunnelClient(base_url="http://signoz.test", api_key="K", jwt="J")
    headers = cl._auth_headers()
    assert headers.get("SIGNOZ-API-KEY") == "K"
    assert "Authorization" not in headers
