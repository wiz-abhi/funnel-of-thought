# signoz-funnel-mcp

**MCP tools for SigNoz Trace Funnels — the 0 of 41 that were missing.**

SigNoz's official MCP server ships **41 tools**. Not one of them touches trace
funnels. An AI agent connected to SigNoz today can query logs, metrics and
traces, but it cannot create a funnel, cannot read a conversion rate, and cannot
tell you where a multi-step workflow leaks. This server closes that gap.

It is also a hardening layer. The funnel REST API is undocumented and has a
dozen sharp edges — millisecond timestamps in one place and nanoseconds in
another, a required field that isn't obviously required, an `id` you must *not*
send, and a bug where a perfectly ordinary "nothing converted" answer comes back
as an HTTP 500. Every one of those is handled here so you never meet it.

---

## Quick start

```bash
pip install -r signoz_funnel_mcp/requirements.txt

export SIGNOZ_URL=http://localhost:8080
export SIGNOZ_JWT="<your admin or editor JWT>"

python -m signoz_funnel_mcp.server
```

The server speaks MCP over **stdio**. Point any MCP client at it.

### Client configuration

Claude Code / Cursor style `mcp.json`:

```json
{
  "mcpServers": {
    "signoz-funnels": {
      "command": "python",
      "args": ["-m", "signoz_funnel_mcp.server"],
      "env": {
        "SIGNOZ_URL": "http://localhost:8080",
        "SIGNOZ_JWT": "${SIGNOZ_JWT}"
      }
    }
  }
}
```

### Docker

Build from the **repository root**, not from this directory — the Dockerfile
copies `signoz_funnel_mcp/` as a package, so the build context must be its
parent:

```bash
docker build -f signoz_funnel_mcp/Dockerfile -t signoz-funnel-mcp .
```

Containerized client config (note `-i` — MCP needs stdin):

```json
{
  "mcpServers": {
    "signoz-funnels": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-e", "SIGNOZ_URL=http://host.docker.internal:8080",
        "-e", "SIGNOZ_JWT",
        "signoz-funnel-mcp"
      ]
    }
  }
}
```

### Configuration

| Variable | Required | Default | Notes |
|---|---|---|---|
| `SIGNOZ_URL` | no | `http://localhost:8080` | SigNoz base URL |
| `SIGNOZ_JWT` | one of | — | Bearer token. **Writes need EDITOR or ADMIN.** |
| `SIGNOZ_API_KEY` | one of | — | Sent as `SIGNOZ-API-KEY`. Read-only service-account keys get 403 on create. |
| `SIGNOZ_TIMEOUT` | no | `30` | Per-request timeout, seconds |

Credentials are read from the environment only — never from CLI arguments, so
tokens don't leak into process listings.

> To mint a JWT: `POST /api/v2/sessions/email_password` with
> `{"email": ..., "password": ...}`. (The commonly-cited `/api/v1/login` does
> not exist on current builds — it silently serves the SPA's HTML.)

---

## Tools

### `list_funnels()`
Every funnel with its id, name and step definitions. Start here to turn a name
into an id or to avoid creating a duplicate.

### `create_funnel(name, steps)`
Creates a funnel **and** sets its steps in one call, wrapping two REST calls and
all their undocumented requirements. Steps are simple dicts:

```json
[{"service": "frontend", "span": "GET /cart",  "name": "browse"},
 {"service": "payments", "span": "charge",     "name": "pay"}]
```

Optional per step: `name`, `latency_type` (`p90`/`p95`/`p99`), `latency_pointer`
(`start`/`end`), `has_errors`. If setting steps fails, the orphaned empty funnel
is deleted rather than left behind.

### `get_funnel_analytics(funnel_id | funnel_name, time_range="24h")`
The main event. Merges `/analytics/steps` (the *only* per-step source) with
`/analytics/overview` (end-to-end), and returns:

```jsonc
{
  "steps": [
    {"step": 1, "label": "plan",     "n": 60, "errors": 0,
     "conversion_from_previous_pct": 100.0, "conversion_from_start_pct": 100.0,
     "dropped_from_previous": 0},
    {"step": 3, "label": "validate", "n": 36, "errors": 0,
     "conversion_from_previous_pct": 60.0,  "conversion_from_start_pct": 60.0,
     "dropped_from_previous": 24}
  ],
  "end_to_end": { "conversion_rate": 60, "zero_trace_fallback": false, "caveats": [...] },
  "totals":     { "entered": 60, "completed": 36, "overall_conversion_pct": 60.0 },
  "diagnostics": []
}
```

Every step carries an explicit **`n`** so any chart can label each bar with its
sample size. `time_range` takes a human window (`30m`, `24h`, `7d`, `2w`) and is
converted to the nanoseconds the API actually wants.

### `get_funnel_slow_traces(funnel_id | funnel_name, time_range, step_start, step_end)`
Concrete trace ids for the slowest journeys across one step transition, plus any
error traces. **Returns at most 5 rows** — SigNoz hardcodes `LIMIT 5` and there
is no paging.

### `delete_funnel(funnel_id)`
Removes a funnel. Irreversible.

---

## Gotchas this server handles for you

Each row is a request that fails without this layer. All verified live against
SigNoz **v0.132.2**.

| # | The trap | What happens raw | Handled by |
|---|---|---|---|
| 1 | `POST /new` wants `timestamp` in **milliseconds**, validated to `1e12..1e13` | 400 on seconds or nanoseconds | `build_create_payload` fills it in; a wrong unit fails fast with a message naming the unit |
| 2 | `PUT /steps/update` **requires** `timestamp` | `400 timestamp is required` | Always included |
| 3 | A step must **omit** `id` | `invalid UUID length: 1` | `id` is never serialized; server mints the UUID |
| 4 | Analytics times are **nanoseconds**, not the milliseconds used above | Empty results, silently | You pass `"24h"`; `resolve_window_ns` converts |
| 5 | `/analytics/steps/overview` needs `step_start`/`step_end` | `500 step start and end cannot be the same` | Required and validated as distinct |
| 6 | Per-step counts live **only** in `/analytics/steps`, as a flat `total_s1_spans`, `total_s2_spans`… blob | You reinvent the reshaping | `summarize_steps` returns tidy per-step rows |
| 7 | `/analytics/overview` `errors` is `greatest(...)` — a **MAX across steps, not a sum** — and its latency quantile is **hardcoded p99** | You misreport your own numbers | Restated in `end_to_end.caveats` |
| 8 | `/analytics/slow-traces` is hardcoded `LIMIT 5` and is **pairwise** | You assume you're seeing everything | `limit_note` on every response |
| 9 | **A step matching zero traces returns HTTP 500 `unsupported value: NaN`** — `avgIf`/`quantileIf` over an empty set produce NaN, which Go can't marshal. The body is *plain text*, not JSON. (SigNoz issue **#12143**, filed by us) | Your agent crashes on the most ordinary answer there is: "nothing converted" | Detected and translated into a clean `conversion_rate: 0` with `zero_trace_fallback: true` and an explanation |
| 10 | `latency_type: "p50"` **silently returns p99** — the server switch only implements p90/p95 and defaults to `0.99`. Measured: p50 and p99 came back **byte-identical** (`18.673343`) while p90 (`17.96`) and p95 (`18.01`) differed | You publish a "median" that is really a p99 | `create_funnel` returns a `warnings` entry |
| 11 | Ordering is **strict** (`t_next > t_prev`). Spans completing in the same clock tick aren't "ordered" | 100 correct traces reported 6 conversions | Named in `diagnostics` on any zero-trace step |
| 12 | Steps must share **one trace**; matching is `minIf` (first occurrence) with enforced order | Funnels are blind to loops and retries | Stated in `semantics` on every report |

### On #9 and #11 — the two faces of "zero"

These interact, and it costs people hours. A step reporting zero has **two**
possible causes:

1. the `(service_name, span_name)` pair matches **no spans at all** — a typo, or
   a time window that misses the data; or
2. it matches spans fine, but **no trace has that step strictly after the
   previous one**.

Cause 2 has a subtle form worth stating plainly: because the comparison is
strict, steps that are genuinely sequential but complete **within the same clock
tick** — instantaneous spans with `duration_nano = 0` — do not count as ordered.
We reproduced this with 100 emitted traces, perfectly ordered and correctly
named, of which the funnel counted 6. Adding ~4 ms of real work per span fixed
it entirely.

Raw SigNoz reports both cases as `HTTP 500 unsupported value: NaN`. This server
reports them as a `0` with `diagnostics` naming both candidates.

---

## Using the client directly

The REST client is independently importable, so other components can reuse it
without going through MCP:

```python
from signoz_funnel_mcp import SigNozFunnelClient, FunnelStep

with SigNozFunnelClient() as client:
    result = client.create_funnel_with_steps("checkout", [
        FunnelStep(service_name="frontend", span_name="GET /cart"),
        FunnelStep(service_name="payments", span_name="charge"),
    ])
    report = client.funnel_analytics(result["funnel_id"], time_range="24h")
    for step in report["steps"]:
        print(f"{step['label']}: n={step['n']} ({step['conversion_from_previous_pct']}%)")
```

Errors are typed: `SigNozAuthError` (401, usually an expired JWT),
`SigNozPermissionError` (403, token isn't EDITOR/ADMIN), `ZeroTraceNaNError`
(#12143, already absorbed by the high-level helpers), and `SigNozError` for
everything else. MCP tools never raise — they return `{"ok": false, "error": ...}`
so the agent can reason about the failure instead of hitting a protocol error.

---

## Tests

```bash
python -m pytest signoz_funnel_mcp/tests -q
```

44 unit tests, no network and no SigNoz required — they cover payload
construction, the ms/ns split, and the conversion arithmetic. Each maps to a
gotcha in the table above, including a regression case built from the live
response shape and one asserting the empty-funnel path never produces `NaN`.

**Live verification** — SigNoz v0.132.2, real trace data, all five tools
exercised end to end over genuine MCP stdio (`initialize` → `tools/list` →
`tools/call`), not just in-process:

- Created a 4-step funnel over `fot-spike2` and read `n = [60, 60, 36, 36]`
  → 60% end-to-end conversion.
- Same funnel shape over the project's `fot-agent` service
  (`agent.plan → agent.tool → agent.validate → agent.respond`) gave
  `n = [56, 47, 25, 25]` → 100% / 83.9% / 44.6% / 44.6%, with
  `/analytics/overview` returning 200 and `conversion_rate: 44.64`.
- Pulled slow traces (5 rows — the hardcoded ceiling).
- Deliberately triggered the NaN-500 with a non-existent span name and confirmed
  it surfaced as a clean `conversion_rate: 0` with `zero_trace_fallback: true`
  and a `diagnostics` entry, instead of a 500.
- Confirmed the `p50` warning fires.
- Deleted every funnel created; the instance was left exactly as found.

The Docker image builds (`python:3.11-slim`, non-root `uid 10001`).
