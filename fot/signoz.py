"""Self-contained SigNoz REST + ClickHouse helpers for Funnel of Thought.

Deliberately has **no dependency on any other module in this repo** (notably not
``signoz_funnel_mcp``) so that ``fot`` can be built and run in isolation.

Ground truth for this file was probed live against SigNoz **v0.132.2**; where the
published docs and the running server disagree, the server wins. The important,
easy-to-get-wrong facts are called out inline as ``NOTE:`` comments.

Unit discipline (the single biggest footgun in this API):

* funnel *create* / *steps update* take ``timestamp`` in **milliseconds**
* funnel *analytics* take ``start_time`` / ``end_time`` in **nanoseconds**
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import httpx

__all__ = [
    "SigNozError",
    "FunnelAnalyticsNaN",
    "SigNozClient",
    "StepCounts",
    "ns_now",
    "ms_now",
    "parse_duration",
]

DEFAULT_BASE_URL = os.environ.get("SIGNOZ_URL", "http://localhost:8080")
DEFAULT_CLICKHOUSE_CONTAINER = os.environ.get(
    "SIGNOZ_CLICKHOUSE_CONTAINER", "signoz-telemetrystore-clickhouse-0-0"
)
#: Token cache lives outside the repo so a JWT can never be committed by accident.
TOKEN_CACHE = Path(os.environ.get("FOT_TOKEN_CACHE", Path.home() / ".fot" / "token.json"))
#: Repo-local, gitignored plain-text JWT shared between the parallel build agents.
REPO_TOKEN_FILE = Path(__file__).resolve().parent.parent / ".signoz-token"

TRACES_TABLE = "signoz_traces.distributed_signoz_index_v3"
#: ClickHouse mangles OTel resource attributes into ``resource_string_<key>`` with
#: ``.`` replaced by ``$$``. The service name column is therefore not ``service_name``.
SERVICE_COLUMN = "resource_string_service$$name"


class SigNozError(RuntimeError):
    """Any non-2xx response from the SigNoz API."""

    def __init__(self, message: str, *, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class FunnelAnalyticsNaN(SigNozError):
    """Raised for the known server-side NaN crash in funnel analytics.

    When a funnel step matches **zero** traces, SigNoz computes ``avgIf`` /
    ``quantileIf`` over an empty set, gets ``NaN``, and fails to serialise it to
    JSON -- surfacing as ``HTTP 500: unsupported value: NaN``. This is a SigNoz
    bug (reported as issue #12143), not a client error: the correct rendering is
    "0%", never a stack trace. Callers should catch this and degrade gracefully.
    """


# --------------------------------------------------------------------------------------
# time helpers
# --------------------------------------------------------------------------------------


def ns_now() -> int:
    """Current wall-clock time in **nanoseconds** (funnel analytics windows)."""
    return int(time.time() * 1_000_000_000)


def ms_now() -> int:
    """Current wall-clock time in **milliseconds** (funnel create/update timestamps)."""
    return int(time.time() * 1000)


_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: str) -> int:
    """Parse a short duration such as ``30d``, ``6h``, ``90m`` into seconds.

    Raises:
        ValueError: if the string is not ``<int><s|m|h|d|w>``.
    """
    text = text.strip().lower()
    if len(text) < 2 or text[-1] not in _DURATION_UNITS or not text[:-1].isdigit():
        raise ValueError(f"bad duration {text!r}; expected e.g. 30d, 6h, 90m")
    return int(text[:-1]) * _DURATION_UNITS[text[-1]]


# --------------------------------------------------------------------------------------
# analytics result model
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class StepCounts:
    """Per-step trace counts for one funnel, in step order.

    Attributes:
        labels: human-readable step labels, ``len == n_steps``.
        totals: traces that reached each step *in order*, ``len == n_steps``.
        errors: errored spans at each step, ``len == n_steps``.
        degraded: ``True`` when the server returned the NaN 500 and counts were
            forced to zero rather than crashing the CLI.
    """

    labels: list[str]
    totals: list[int]
    errors: list[int] = field(default_factory=list)
    degraded: bool = False

    def __post_init__(self) -> None:
        if not self.errors:
            self.errors = [0] * len(self.totals)

    @property
    def entered(self) -> int:
        """Traces that entered the funnel (reached step 1)."""
        return self.totals[0] if self.totals else 0

    def cumulative_pct(self) -> list[float]:
        """Conversion of each step relative to step 1 (the funnel-wide funnel view)."""
        base = self.entered
        if base <= 0:
            return [0.0] * len(self.totals)
        return [100.0 * t / base for t in self.totals]

    def step_pct(self) -> list[float]:
        """Conversion of each step relative to the *previous* step.

        This is where drop-off cliffs show up; step 1 is 100% by definition.
        """
        out: list[float] = []
        for i, total in enumerate(self.totals):
            if i == 0:
                out.append(100.0 if total else 0.0)
                continue
            prev = self.totals[i - 1]
            out.append(100.0 * total / prev if prev > 0 else 0.0)
        return out

    def biggest_drop(self) -> tuple[int, float, int] | None:
        """Locate the worst drop-off.

        Returns:
            ``(index, pct_lost, traces_lost)`` for the step with the largest
            relative loss versus its predecessor, or ``None`` if there is no
            drop anywhere (or no data).
        """
        if len(self.totals) < 2 or self.entered <= 0:
            return None
        worst: tuple[int, float, int] | None = None
        for i in range(1, len(self.totals)):
            prev, cur = self.totals[i - 1], self.totals[i]
            if prev <= 0 or cur >= prev:
                continue
            lost = prev - cur
            pct_lost = 100.0 * lost / prev
            if worst is None or pct_lost > worst[1]:
                worst = (i, pct_lost, lost)
        return worst


# --------------------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------------------


class SigNozClient:
    """Thin, typed wrapper over the SigNoz REST API.

    Authentication is resolved in this order:

    1. ``SIGNOZ_API_KEY`` -> sent as the ``SIGNOZ-API-KEY`` header.
       NOTE: read-only keys **403 on funnel/dashboard/alert writes**; you need an
       editor or admin key.
    2. ``SIGNOZ_JWT`` -> sent as ``Authorization: Bearer``.
    3. A cached JWT from a previous password login (``~/.fot/token.json``).
    4. ``SIGNOZ_EMAIL`` + ``SIGNOZ_PASSWORD`` -> password login (see :meth:`login`).

    Access tokens expire in ~30 minutes, so any 401 triggers one transparent
    re-login before the request is retried.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 60.0,
        clickhouse_container: str = DEFAULT_CLICKHOUSE_CONTAINER,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.clickhouse_container = clickhouse_container
        self._http = httpx.Client(timeout=timeout)
        self._api_key = os.environ.get("SIGNOZ_API_KEY") or None
        self._jwt = os.environ.get("SIGNOZ_JWT") or None
        if not self._api_key and not self._jwt:
            self._jwt = self._load_cached_token()

    # -- auth ---------------------------------------------------------------------

    @staticmethod
    def _load_cached_token() -> str | None:
        """Read a JWT from the repo-local token file, then the private cache."""
        try:
            token = REPO_TOKEN_FILE.read_text().strip()
            if token:
                return token
        except Exception:
            pass
        try:
            return json.loads(TOKEN_CACHE.read_text()).get("access_token") or None
        except Exception:
            return None

    @staticmethod
    def _store_cached_token(token: str) -> None:
        try:
            TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
            TOKEN_CACHE.write_text(json.dumps({"access_token": token}))
            # Best-effort: keep the cache readable only by the owner.
            os.chmod(TOKEN_CACHE, 0o600)
        except Exception:
            pass  # a non-writable cache is not fatal, just slower

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["SIGNOZ-API-KEY"] = self._api_key
        elif self._jwt:
            headers["Authorization"] = f"Bearer {self._jwt}"
        return headers

    def discover_org_id(self, email: str) -> str:
        """Look up the org id for ``email``.

        NOTE: this endpoint is unauthenticated and is the only way to get the
        ``orgID`` that the password-login endpoint requires.
        """
        resp = self._http.get(
            f"{self.base_url}/api/v2/sessions/context", params={"email": email}
        )
        resp.raise_for_status()
        orgs = resp.json().get("data", {}).get("orgs") or []
        if not orgs:
            raise SigNozError(f"no SigNoz org found for {email}")
        return orgs[0]["id"]

    def login(self, email: str | None = None, password: str | None = None) -> str:
        """Exchange email+password for an access JWT and cache it.

        NOTE: the login route is ``POST /api/v2/sessions/email_password`` and it
        **requires ``orgID``** -- the legacy ``/api/v1/login`` path no longer
        exists and silently falls through to the SPA's ``index.html`` (HTTP 200,
        ``text/html``), which is a very confusing failure mode. Discovering the
        org id first avoids needing a headless browser to obtain a token.
        """
        email = email or os.environ.get("SIGNOZ_EMAIL") or ""
        password = password or os.environ.get("SIGNOZ_PASSWORD") or ""
        if not email or not password:
            raise SigNozError(
                "no credentials: set SIGNOZ_API_KEY, or SIGNOZ_JWT, or "
                "SIGNOZ_EMAIL + SIGNOZ_PASSWORD"
            )
        org_id = self.discover_org_id(email)
        resp = self._http.post(
            f"{self.base_url}/api/v2/sessions/email_password",
            json={"email": email, "password": password, "orgID": org_id},
        )
        if resp.status_code >= 400:
            raise SigNozError("login failed", status=resp.status_code, body=resp.text[:200])
        token = resp.json()["data"]["accessToken"]
        self._jwt = token
        self._api_key = None
        self._store_cached_token(token)
        return token

    # -- transport ----------------------------------------------------------------

    def request(self, method: str, path: str, payload: Any | None = None) -> Any:
        """Issue an API call, refreshing the JWT once on 401.

        Raises:
            FunnelAnalyticsNaN: on the known server NaN serialisation 500.
            SigNozError: on any other non-2xx response.
        """
        url = f"{self.base_url}{path}"

        def _send() -> httpx.Response:
            return self._http.request(method, url, headers=self._headers(), json=payload)

        try:
            resp = _send()
        except httpx.HTTPError as exc:  # network/DNS/timeout
            raise SigNozError(f"{method} {path} failed: {exc}") from exc

        if resp.status_code == 401 and not self._api_key:
            self.login()
            resp = _send()

        if resp.status_code >= 400:
            body = resp.text[:500]
            # The NaN bug is a 500 whose body mentions NaN; classify it precisely so
            # callers can render 0% instead of blowing up.
            if resp.status_code >= 500 and "NaN" in body:
                raise FunnelAnalyticsNaN(
                    "SigNoz returned NaN for a funnel step with zero matching traces",
                    status=resp.status_code,
                    body=body,
                )
            if resp.status_code == 403:
                raise SigNozError(
                    f"{method} {path} -> 403 forbidden (writes need an editor/admin "
                    f"credential; read-only API keys cannot create funnels)",
                    status=403,
                    body=body,
                )
            raise SigNozError(f"{method} {path} -> {resp.status_code}", status=resp.status_code, body=body)

        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    # -- funnels ------------------------------------------------------------------

    def list_funnels(self) -> list[dict[str, Any]]:
        """Return every saved trace funnel."""
        return self.request("GET", "/api/v1/trace-funnels/list").get("data") or []

    def find_funnel(self, name: str) -> dict[str, Any] | None:
        """Find a funnel by exact ``funnel_name``."""
        for funnel in self.list_funnels():
            if funnel.get("funnel_name") == name:
                return funnel
        return None

    def create_funnel(self, name: str) -> str:
        """Create an empty funnel and return its id.

        NOTE: ``timestamp`` here is **milliseconds** (unlike analytics, which is ns).
        """
        data = self.request(
            "POST",
            "/api/v1/trace-funnels/new",
            {"funnel_name": name, "timestamp": ms_now()},
        )
        return data["data"]["funnel_id"]

    def update_steps(self, funnel_id: str, steps: Sequence[dict[str, Any]]) -> None:
        """Replace a funnel's steps.

        NOTE: ``timestamp`` is **required** (milliseconds) and each step must
        **omit ``id``** -- SigNoz assigns a UUID, and passing ``"id": "1"``
        fails with ``invalid UUID length: 1``.
        """
        self.request(
            "PUT",
            "/api/v1/trace-funnels/steps/update",
            {"funnel_id": funnel_id, "timestamp": ms_now(), "steps": list(steps)},
        )

    def delete_funnel(self, funnel_id: str) -> None:
        """Delete a funnel by id."""
        self.request("DELETE", f"/api/v1/trace-funnels/{funnel_id}")

    def step_analytics(
        self, funnel_id: str, start_ns: int, end_ns: int, labels: Sequence[str]
    ) -> StepCounts:
        """Fetch per-step trace counts.

        This is the **only** endpoint that returns per-step counts; the
        ``/analytics/overview`` endpoint gives end-to-end numbers only. The
        response is a flat dict of ``total_s<N>_spans`` / ``total_s<N>_errored_spans``
        keys, which this method reshapes into an ordered :class:`StepCounts`.

        Args:
            funnel_id: funnel uuid.
            start_ns: window start in **nanoseconds**.
            end_ns: window end in **nanoseconds**.
            labels: step labels, used to size and name the result.

        Returns:
            A :class:`StepCounts`; on the known NaN 500 it returns all-zero counts
            with ``degraded=True`` instead of raising.
        """
        try:
            data = self.request(
                "POST",
                f"/api/v1/trace-funnels/{funnel_id}/analytics/steps",
                {"start_time": start_ns, "end_time": end_ns},
            )
        except FunnelAnalyticsNaN:
            return StepCounts(labels=list(labels), totals=[0] * len(labels), degraded=True)

        rows = data.get("data") or []
        payload: dict[str, Any] = rows[0].get("data", {}) if rows else {}
        totals, errors = [], []
        for i in range(1, len(labels) + 1):
            totals.append(int(payload.get(f"total_s{i}_spans", 0) or 0))
            errors.append(int(payload.get(f"total_s{i}_errored_spans", 0) or 0))
        return StepCounts(labels=list(labels), totals=totals, errors=errors)

    def overview(self, funnel_id: str, start_ns: int, end_ns: int) -> dict[str, Any] | None:
        """End-to-end funnel stats, or ``None`` when the NaN bug fires.

        NOTE: ``latency`` is hardcoded p99 server-side regardless of the step's
        ``latency_type``, and ``errors`` is a MAX across steps rather than a sum.
        Treat both as indicative only.
        """
        try:
            data = self.request(
                "POST",
                f"/api/v1/trace-funnels/{funnel_id}/analytics/overview",
                {"start_time": start_ns, "end_time": end_ns},
            )
        except FunnelAnalyticsNaN:
            return None
        rows = data.get("data") or []
        return rows[0].get("data") if rows else None

    # -- dashboards & alerts ------------------------------------------------------

    def list_dashboards(self) -> list[dict[str, Any]]:
        """Return all dashboards."""
        return self.request("GET", "/api/v1/dashboards").get("data") or []

    def create_dashboard(self, body: dict[str, Any]) -> str:
        """Create a dashboard from a v5 payload and return its id."""
        return self.request("POST", "/api/v1/dashboards", body)["data"]["id"]

    def delete_dashboard(self, dashboard_id: str) -> None:
        """Delete a dashboard by id."""
        self.request("DELETE", f"/api/v1/dashboards/{dashboard_id}")

    def list_rules(self) -> list[dict[str, Any]]:
        """Return all alert rules."""
        data = self.request("GET", "/api/v2/rules").get("data")
        if isinstance(data, dict):
            return data.get("rules") or []
        return data or []

    def create_rule(self, body: dict[str, Any]) -> Any:
        """Create an alert rule (``ruleType: threshold_rule``)."""
        return self.request("POST", "/api/v2/rules", body)

    def delete_rule(self, rule_id: str) -> None:
        """Delete an alert rule by id."""
        self.request("DELETE", f"/api/v1/rules/{rule_id}")

    # -- ClickHouse ---------------------------------------------------------------

    def clickhouse(self, sql: str) -> list[list[str]]:
        """Run SQL directly against the traces store via ``docker exec``.

        Used by ``fot counter-proof`` to reproduce the naive ``GROUP BY span name``
        query that the funnel is being contrasted against. Returns tab-separated
        rows already split into columns.

        Raises:
            SigNozError: if docker is unavailable or the query fails.
        """
        if shutil.which("docker") is None:
            raise SigNozError("docker not found on PATH; cannot reach ClickHouse directly")
        cmd = [
            "docker", "exec", self.clickhouse_container,
            "clickhouse-client", "--query", sql,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise SigNozError(f"clickhouse query failed: {proc.stderr.strip()[:300]}")
        return [line.split("\t") for line in proc.stdout.strip().splitlines() if line]

    def naive_span_counts(
        self, service: str, span_names: Iterable[str], start_ns: int, end_ns: int
    ) -> dict[str, int]:
        """The **naive** measurement: distinct traces containing each span name.

        This is the ``GROUP BY span name, COUNT`` that most teams reach for. It is
        the thing ``fot counter-proof`` exists to discredit: it counts a trace for
        a span regardless of *where* that span sits in the trace, so it cannot tell
        "validated, then responded" apart from "responded, then validated" -- or
        from "validated in a completely unrelated code path".

        Returns:
            Mapping of span name -> distinct trace count.
        """
        names = list(span_names)
        if not names:
            return {}
        quoted = ", ".join("'" + n.replace("'", "\\'") + "'" for n in names)
        service_sql = service.replace("'", "\\'")
        sql = f"""
            SELECT name, countDistinct(trace_id)
            FROM {TRACES_TABLE}
            WHERE `{SERVICE_COLUMN}` = '{service_sql}'
              AND name IN ({quoted})
              AND timestamp >= toDateTime64({start_ns / 1e9:.6f}, 9)
              AND timestamp <= toDateTime64({end_ns / 1e9:.6f}, 9)
            GROUP BY name
        """
        return {row[0]: int(row[1]) for row in self.clickhouse(sql) if len(row) >= 2}

    def total_traces(self, service: str, start_ns: int, end_ns: int) -> int:
        """Distinct trace count for ``service`` in the window (the naive denominator)."""
        service_sql = service.replace("'", "\\'")
        sql = f"""
            SELECT countDistinct(trace_id)
            FROM {TRACES_TABLE}
            WHERE `{SERVICE_COLUMN}` = '{service_sql}'
              AND timestamp >= toDateTime64({start_ns / 1e9:.6f}, 9)
              AND timestamp <= toDateTime64({end_ns / 1e9:.6f}, 9)
        """
        rows = self.clickhouse(sql)
        return int(rows[0][0]) if rows and rows[0] else 0

    def ordering_breakdown(
        self, service: str, earlier_span: str, later_span: str, start_ns: int, end_ns: int
    ) -> dict[str, int]:
        """Split traces by whether ``later_span`` actually follows ``earlier_span``.

        This is the evidence that makes ``fot counter-proof`` airtight. A naive
        ``GROUP BY name`` can only report *presence*; this query additionally
        reports *position*, separating traces where the span is present but fires
        too early from traces where it is correctly sequenced.

        NOTE: the funnel's own SQL compares ``minIf(timestamp, ...)`` per step and
        requires the later step to be **strictly** greater. Spans that start within
        the same clock tick therefore do not count as ordered -- an agent whose
        steps are instantaneous no-ops will appear not to convert at all. This
        query uses the same strict ``>`` so its numbers line up with the funnel's.

        Returns:
            ``{"total", "has_earlier", "has_later", "ordered", "out_of_order"}``
            as distinct trace counts.
        """
        service_sql = service.replace("'", "\\'")
        early_sql = earlier_span.replace("'", "\\'")
        late_sql = later_span.replace("'", "\\'")
        sql = f"""
            SELECT
                count() AS total,
                countIf(t_early > 0) AS has_earlier,
                countIf(t_late  > 0) AS has_later,
                countIf(t_early > 0 AND t_late > t_early) AS ordered,
                countIf(t_early > 0 AND t_late > 0 AND t_late <= t_early) AS out_of_order
            FROM (
                SELECT
                    trace_id,
                    minIf(toUnixTimestamp64Nano(timestamp), name = '{early_sql}') AS t_early,
                    minIf(toUnixTimestamp64Nano(timestamp), name = '{late_sql}')  AS t_late
                FROM {TRACES_TABLE}
                WHERE `{SERVICE_COLUMN}` = '{service_sql}'
                  AND timestamp >= toDateTime64({start_ns / 1e9:.6f}, 9)
                  AND timestamp <= toDateTime64({end_ns / 1e9:.6f}, 9)
                GROUP BY trace_id
            )
        """
        rows = self.clickhouse(sql)
        if not rows or len(rows[0]) < 5:
            return {k: 0 for k in ("total", "has_earlier", "has_later", "ordered", "out_of_order")}
        total, has_earlier, has_later, ordered, out_of_order = (int(v) for v in rows[0][:5])
        return {
            "total": total,
            "has_earlier": has_earlier,
            "has_later": has_later,
            "ordered": ordered,
            "out_of_order": out_of_order,
        }

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._http.close()

    def __enter__(self) -> "SigNozClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
