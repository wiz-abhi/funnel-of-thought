"""Funnel of Thought — deployable single-page UI.

Two data modes:

* ``live``     — reads the real SigNoz Trace Funnel analytics through the
                 documented client at ``fot/signoz.py``. No REST logic is
                 reimplemented here.
* ``snapshot`` — serves ``data/snapshot.json``, frozen from a real run. This is
                 what a public deploy uses, because a Space cannot reach a
                 laptop's localhost SigNoz.

Selection is automatic (live if credentials + a reachable SigNoz exist, else
snapshot) and the rendered page always states which one it is. Snapshot data is
never presented as live.

Env:
    FOT_MODE          auto (default) | live | snapshot
    SIGNOZ_URL        e.g. http://localhost:8080
    SIGNOZ_API_KEY | SIGNOZ_JWT | SIGNOZ_EMAIL + SIGNOZ_PASSWORD
    FOT_FUNNEL        funnel name, default "cognition"
    FOT_SERVICE       service name, default "fot-agent"
    FOT_WINDOW_DAYS   analytics lookback, default 30
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
SNAPSHOT_PATH = HERE / "data" / "snapshot.json"

# Make the sibling `fot` package importable without installing the repo.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

FUNNEL_NAME = os.environ.get("FOT_FUNNEL", "cognition")
SERVICE = os.environ.get("FOT_SERVICE", "fot-agent")
WINDOW_DAYS = int(os.environ.get("FOT_WINDOW_DAYS", "30"))
MODE = os.environ.get("FOT_MODE", "auto").strip().lower()
STEP_LABELS = ["plan", "tool", "validate", "respond"]
CACHE_TTL_S = 30.0

app = FastAPI(title="Funnel of Thought", docs_url="/docs")
templates = Jinja2Templates(directory=str(HERE / "templates"))

_cache: dict[str, Any] = {"at": 0.0, "payload": None}


# --------------------------------------------------------------------------------------
# snapshot
# --------------------------------------------------------------------------------------


def load_snapshot(reason: str | None = None) -> dict[str, Any]:
    payload = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    payload["mode"] = "snapshot"
    payload["source"] = f"frozen from a real run on {payload.get('captured_at', 'unknown')}"
    if reason:
        payload["fallback_reason"] = reason
    return payload


# --------------------------------------------------------------------------------------
# live
# --------------------------------------------------------------------------------------


def _has_credentials() -> bool:
    return bool(
        os.environ.get("SIGNOZ_API_KEY")
        or os.environ.get("SIGNOZ_JWT")
        or (os.environ.get("SIGNOZ_EMAIL") and os.environ.get("SIGNOZ_PASSWORD"))
    )


def fetch_live() -> dict[str, Any]:
    """Read the real funnel. Raises on any failure; the caller falls back."""
    from fot.signoz import SigNozClient, ns_now  # imported lazily so snapshot needs no deps

    base = os.environ.get("SIGNOZ_URL", "http://localhost:8080")
    client = SigNozClient(base, timeout=15.0)
    try:
        if not os.environ.get("SIGNOZ_API_KEY") and not os.environ.get("SIGNOZ_JWT"):
            client.login()  # uses SIGNOZ_EMAIL / SIGNOZ_PASSWORD or a cached token

        funnel = client.find_funnel(FUNNEL_NAME)
        if not funnel:
            raise RuntimeError(f"funnel {FUNNEL_NAME!r} not found on {base}")
        funnel_id = funnel.get("funnel_id") or funnel.get("id")

        end_ns = ns_now()
        start_ns = end_ns - WINDOW_DAYS * 86_400 * 1_000_000_000

        counts = client.step_analytics(funnel_id, start_ns, end_ns, STEP_LABELS)
        if counts.degraded or counts.entered <= 0:
            raise RuntimeError("funnel analytics returned no traces (or the NaN 500)")

        pcts = counts.cumulative_pct()
        snap = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        steps = []
        for i, label in enumerate(STEP_LABELS):
            prev = counts.totals[i - 1] if i else counts.totals[i]
            steps.append(
                {
                    "label": label,
                    "span": f"agent.{label}",
                    "n": counts.totals[i],
                    "pct": round(pcts[i], 1),
                    "violation": i > 0 and counts.totals[i] < prev,
                }
            )

        overview = client.overview(funnel_id, start_ns, end_ns) or {}

        # The naive counter, from the same traces. ClickHouse access is optional;
        # without it we degrade to snapshot rather than invent a number.
        try:
            naive_counts = client.naive_span_counts(
                SERVICE, ["agent.validate"], start_ns, end_ns
            )
            total = client.total_traces(SERVICE, start_ns, end_ns) or counts.entered
            present = int(naive_counts.get("agent.validate", 0))
        except Exception:  # noqa: BLE001 - clickhouse container not reachable
            present, total = counts.entered, counts.entered

        naive_pct = round(100.0 * present / total, 1) if total else 0.0
        funnel_pct = round(pcts[-1], 1)

        payload = {
            "mode": "live",
            "source": f"reading {base}",
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "funnel_name": FUNNEL_NAME,
            "service": SERVICE,
            "thesis": snap["thesis"],
            "steps": steps,
            "entered": counts.entered,
            "completed": counts.totals[-1],
            "funnel_pct": funnel_pct,
            "naive": {
                "span": "agent.validate",
                "present": present,
                "total": total,
                "pct": naive_pct,
            },
            "gap_pp": round(naive_pct - funnel_pct, 1),
            "gap_traces": max(counts.entered - counts.totals[-1], 0),
            "metrics": {
                "avg_duration_s": round(float(overview.get("avg_duration", 0) or 0) / 1000, 2)
                or snap["metrics"]["avg_duration_s"],
                "p99_latency_s": round(float(overview.get("latency", 0) or 0) / 1000, 2)
                or snap["metrics"]["p99_latency_s"],
                "errors": int(overview.get("errors", 0) or 0) or max(counts.errors or [0]),
            },
            "violating_trace": snap["violating_trace"],
        }
        return payload
    finally:
        client.close()


def get_payload(force_refresh: bool = False) -> dict[str, Any]:
    now = time.time()
    if not force_refresh and _cache["payload"] and now - _cache["at"] < CACHE_TTL_S:
        return _cache["payload"]

    if MODE == "snapshot":
        payload = load_snapshot()
    elif MODE == "live":
        payload = fetch_live()
    else:  # auto
        if not _has_credentials() and not os.environ.get("SIGNOZ_EMAIL"):
            payload = load_snapshot("no SigNoz credentials in the environment")
        else:
            try:
                payload = fetch_live()
            except Exception as exc:  # noqa: BLE001 - any failure must degrade, not 500
                payload = load_snapshot(f"live read failed: {type(exc).__name__}: {exc}")

    _cache.update(at=now, payload=payload)
    return payload


# --------------------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------------------


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/funnel")
def api_funnel(refresh: bool = False) -> JSONResponse:
    return JSONResponse(get_payload(force_refresh=refresh))


@app.get("/")
def index(request: Request):
    data = get_payload()
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "d": data, "d_json": json.dumps(data)},
    )
