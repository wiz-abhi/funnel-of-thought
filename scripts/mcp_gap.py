"""Verify the gap this project exists to fill, against a live SigNoz MCP server.

The central novelty claim is: *SigNoz ships an MCP server with 41 tools and none
of them reach trace funnels*, so an agent can read every SigNoz surface except
the one measuring its own completion rate. That was an assertion a reader had to
take on trust. This makes it a check.

`casting.yaml` provisions `signoz-mcp` (HTTP transport, port 8000), so if you
cast the stack you already have the thing being measured.

    python scripts/mcp_gap.py                    # human summary
    python scripts/mcp_gap.py --json             # machine-readable
    python scripts/mcp_gap.py --list             # print every tool name

Exit codes: 0 = reachable and measured, 3 = could not reach the server (not a
failure of the claim, just an absent measurement), 1 = unexpected error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

DEFAULT_URL = os.environ.get("SIGNOZ_MCP_URL", "http://localhost:8000/mcp")
KEYWORD = "funnel"


def _parse(response: httpx.Response) -> dict:
    """Read a JSON-RPC reply that may arrive as SSE (``data:`` lines)."""
    text = response.text
    if not text.strip():
        return {}
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    try:
        return json.loads(text)
    except ValueError:
        return {}


def list_tools(url: str, token: str) -> list[dict]:
    """Complete an MCP handshake and return the server's tool list."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
        "SIGNOZ-API-KEY": token,
    }
    with httpx.Client(timeout=25) as client:
        opened = client.post(
            url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "funnel-of-thought-gap-check", "version": "1"},
                },
            },
        )
        opened.raise_for_status()
        session = opened.headers.get("mcp-session-id")
        if session:
            headers["Mcp-Session-Id"] = session
        # Required by the spec before any other request is served.
        client.post(
            url, headers=headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        listed = _parse(
            client.post(
                url, headers=headers,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
        )
    return listed.get("result", {}).get("tools", [])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--list", action="store_true", dest="show_list")
    args = ap.parse_args()

    token = os.environ.get("SIGNOZ_JWT") or os.environ.get("SIGNOZ_API_KEY") or ""
    if not token:
        print("SIGNOZ_JWT or SIGNOZ_API_KEY required", file=sys.stderr)
        return 3

    try:
        tools = list_tools(args.url, token)
    except Exception as exc:  # unreachable server is not a failed claim
        print(f"could not reach the SigNoz MCP server at {args.url}: {exc}", file=sys.stderr)
        return 3
    if not tools:
        print(f"no tools returned by {args.url}", file=sys.stderr)
        return 3

    names = sorted(t.get("name", "") for t in tools)
    # Match the keyword in name OR description: a tool could reach funnels
    # without saying so in its name, and that would still refute the claim.
    reaching = sorted(
        t.get("name", "")
        for t in tools
        if KEYWORD in (f"{t.get('name', '')} {t.get('description') or ''}").lower()
    )

    if args.as_json:
        print(json.dumps({
            "url": args.url,
            "tool_count": len(tools),
            "funnel_tool_count": len(reaching),
            "funnel_tools": reaching,
            "tools": names,
        }))
        return 0

    print(f"SigNoz's own MCP server ({args.url})")
    print(f"  tools exposed        : {len(tools)}")
    print(f"  tools reaching funnels: {len(reaching)}  {reaching or ''}")
    if not reaching:
        print("  -> the gap is real: an agent can reach every SigNoz surface")
        print("     except the one measuring its own completion rate.")
    if args.show_list:
        for i, name in enumerate(names, 1):
            print(f"    {i:2}. {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
