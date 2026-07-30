"""Thin, typed REST client for the SigNoz **Trace Funnels** API.

Why this module exists
----------------------
SigNoz ships 41 MCP tools; *none* of them touch trace funnels. The REST API is
also undocumented and has a handful of sharp edges that each cost a failed
request to discover. This client encapsulates every one of them so callers
never have to think about it:

===  ==========================================================================
#    Gotcha handled here
===  ==========================================================================
1    ``POST /new`` wants ``timestamp`` in **milliseconds** (validated 1e12..1e13).
2    ``PUT /steps/update`` *requires* ``timestamp`` (ms) or it 400s.
3    A step must **omit** ``id`` entirely; passing ``"1"`` fails
     ``invalid UUID length: 1``. The server mints a UUID for you.
4    Analytics ``start_time``/``end_time`` are in **nanoseconds**, not ms.
     Callers pass a human window ("24h") and we convert.
5    ``/analytics/steps/overview`` needs ``step_start``/``step_end`` (1-based)
     or it 500s with "step start and end cannot be the same".
6    ``/analytics/steps`` is the only source of per-step counts.
7    ``/analytics/overview`` is end-to-end only; its ``errors`` is a MAX across
     steps (``greatest(...)``), not a sum, and its latency quantile is
     hardcoded to p99.
8    ``/analytics/slow-traces`` is hardcoded ``LIMIT 5`` and is pairwise.
9    **SigNoz bug #12143**: a step matching zero traces makes ``avgIf``/
     ``quantileIf`` return NaN, which Go cannot marshal -> HTTP 500
     ``unsupported value: NaN`` (a *plain-text* body, not JSON). We detect it
     and return a clean zero-conversion result instead of blowing up.
10   **SigNoz bug (unfiled)**: ``latency_type="p50"`` silently returns p99 --
     the server switch only implements p90/p95 and defaults to 0.99.
     We surface a warning rather than lying to the user.
===  ==========================================================================

Semantics worth knowing
-----------------------
All funnel steps must occur **within the same trace**. The underlying SQL uses
``minIf`` plus a monotonic ordering check, so a funnel sees only the *first*
occurrence of each step and enforces step order. It is therefore structurally
blind to loops and retries: an agent that calls a tool three times looks
identical to one that calls it once.

Ordering is **strict** (``t_next > t_prev``, not ``>=``). This has a nasty
practical consequence: instantaneous spans -- no-ops with ``duration_nano = 0``
that share a clock tick with their neighbour -- fail the comparison even though
they are genuinely sequential, and the funnel silently under-counts. This was
verified live: 100 correctly ordered traces of instantaneous spans reported
only 6 conversions; adding ~4 ms of real work per span fixed it completely.

Public API
----------
``SigNozFunnelClient`` is the entry point. It is a plain synchronous client
(``httpx``) and is safe to import from other components::

    from signoz_funnel_mcp.client import SigNozFunnelClient, FunnelStep

    with SigNozFunnelClient() as c:
        fid = c.create_funnel_with_steps("checkout", [
            FunnelStep(service_name="frontend", span_name="HTTP GET /cart"),
            FunnelStep(service_name="payments", span_name="charge"),
        ])["funnel_id"]
        print(c.funnel_analytics(fid, time_range="24h"))
"""

from __future__ import annotations

import math
import os
import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Literal

import httpx

__all__ = [
    "SigNozFunnelClient",
    "FunnelStep",
    "SigNozError",
    "SigNozAuthError",
    "SigNozPermissionError",
    "ZeroTraceNaNError",
    "STRICT_ORDERING_NOTE",
    "parse_duration_to_seconds",
    "resolve_window_ns",
    "now_ms",
    "to_ns",
    "build_step_payload",
    "build_create_payload",
    "build_steps_update_payload",
    "summarize_steps",
    "NAN_BUG_NOTE",
    "P50_WARNING",
]

DEFAULT_BASE_URL = "http://localhost:8080"
FUNNELS_PREFIX = "/api/v1/trace-funnels"

#: Explanation attached to any result we synthesized because of SigNoz #12143.
#:
#: There are TWO distinct causes and users hit both, so the message names both
#: rather than just reporting 0%.
NAN_BUG_NOTE = (
    "Zero traces completed this funnel in the selected window. Two things cause "
    "this: (a) a step's (service_name, span_name) matches no spans at all -- "
    "check for typos and that the window covers your data; or (b) the steps "
    "match spans but no single trace has each step strictly after the previous "
    "one. SigNoz enforces ordering STRICTLY (minIf per step with t2 > t1), so "
    "steps that are genuinely sequential but complete within the same clock "
    "tick -- instantaneous spans with duration_nano = 0 -- do NOT count as "
    "ordered and will silently under-count. SigNoz returns HTTP 500 "
    "'unsupported value: NaN' instead of a 0% result here (SigNoz issue "
    "#12143); this client translated it into zeros."
)

#: Shorter form of the strict-ordering caveat, attached to every analytics result.
STRICT_ORDERING_NOTE = (
    "Step ordering is enforced STRICTLY (t_next > t_prev). Spans that complete "
    "within the same clock tick do not count as ordered and will under-count."
)

#: Warning attached when a caller asks for an unsupported latency quantile.
P50_WARNING = (
    "latency_type='p50' is NOT implemented by SigNoz: the server's switch only "
    "handles p90/p95 and silently defaults to p99. Reported latency is p99. "
    "Verified live: p50 and p99 returned the byte-identical value 18.673343, "
    "while p90 (17.96) and p95 (18.01) differed."
)

#: Quantiles the SigNoz backend actually implements.
SUPPORTED_LATENCY_TYPES = ("p90", "p95", "p99")

LatencyPointer = Literal["start", "end"]

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw])\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class SigNozError(RuntimeError):
    """Base class for every error raised by this client."""

    def __init__(self, message: str, *, status_code: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class SigNozAuthError(SigNozError):
    """401 -- the JWT/API key is missing, malformed, or expired."""


class SigNozPermissionError(SigNozError):
    """403 -- token authenticated but lacks EDITOR/ADMIN rights.

    Funnel *writes* (``/new``, ``/steps/update``, delete) require an
    EDITOR or ADMIN token. A read-only service-account key gets
    ``only editors/admins can access this resource``.
    """


class ZeroTraceNaNError(SigNozError):
    """SigNoz issue #12143 -- HTTP 500 ``unsupported value: NaN``.

    Raised by the low-level transport when a step matched zero traces. The
    high-level analytics helpers catch this and return a zeroed result, so
    most callers will never see it.
    """


# --------------------------------------------------------------------------- #
# Time helpers (pure functions -- unit tested)
# --------------------------------------------------------------------------- #
def parse_duration_to_seconds(value: str) -> int:
    """Parse a human duration like ``"24h"``, ``"7d"``, ``"30m"`` into seconds.

    Accepted units: ``s`` (seconds), ``m`` (minutes), ``h`` (hours),
    ``d`` (days), ``w`` (weeks). Case-insensitive; surrounding space is fine.

    Raises:
        ValueError: if the string is not a recognized duration.
    """
    match = _DURATION_RE.match(value)
    if not match:
        raise ValueError(
            f"Unrecognized time range {value!r}. Use a number plus a unit, "
            "e.g. '30m', '24h', '7d', '2w'."
        )
    amount, unit = match.group(1), match.group(2).lower()
    seconds = int(float(amount) * _UNIT_SECONDS[unit])
    if seconds <= 0:
        raise ValueError(f"Time range {value!r} must be greater than zero.")
    return seconds


def to_ns(moment: datetime | str | int | float) -> int:
    """Convert a datetime / ISO-8601 string / epoch-seconds number to **nanoseconds**.

    Naive datetimes are interpreted as UTC. ISO strings ending in ``Z`` are
    accepted (Python's ``fromisoformat`` rejects ``Z`` before 3.11).
    """
    if isinstance(moment, (int, float)):
        return int(moment * 1_000_000_000)
    if isinstance(moment, str):
        text = moment.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(moment.timestamp() * 1_000_000_000)


def resolve_window_ns(
    time_range: str | None = "24h",
    start: datetime | str | int | float | None = None,
    end: datetime | str | int | float | None = None,
    *,
    now: float | None = None,
) -> tuple[int, int]:
    """Resolve a human time window into the **nanosecond** pair analytics wants.

    Two mutually exclusive styles are supported:

    * relative -- ``time_range="24h"`` means "now minus 24 hours .. now";
    * absolute -- pass ``start`` and ``end`` as datetimes or ISO-8601 strings.

    Explicit ``start``/``end`` win over ``time_range``. Returns
    ``(start_ns, end_ns)``.

    This exists because the #1 mistake against this API is mixing up the
    millisecond timestamps used by ``/new`` and ``/steps/update`` with the
    nanosecond timestamps used by every ``/analytics/*`` endpoint.
    """
    if start is not None or end is not None:
        if start is None or end is None:
            raise ValueError("Provide both 'start' and 'end', or neither.")
        start_ns, end_ns = to_ns(start), to_ns(end)
    else:
        seconds = parse_duration_to_seconds(time_range or "24h")
        end_seconds = time.time() if now is None else now
        end_ns = int(end_seconds * 1_000_000_000)
        start_ns = end_ns - seconds * 1_000_000_000

    if end_ns <= start_ns:
        raise ValueError("End of the time window must be after its start.")
    return start_ns, end_ns


def now_ms() -> int:
    """Current epoch time in **milliseconds** (what funnel create/update want)."""
    return int(time.time() * 1000)


def _validate_ms(timestamp_ms: int) -> int:
    """Guard the server-side 1e12..1e13 millisecond range check.

    Fails fast with an actionable message instead of letting the server reject
    a value that is almost always seconds or nanoseconds by mistake.
    """
    if not (1_000_000_000_000 <= timestamp_ms < 10_000_000_000_000):
        raise ValueError(
            f"timestamp={timestamp_ms} is outside the millisecond range SigNoz "
            "accepts (1e12..1e13). It looks like seconds or nanoseconds -- "
            "funnel create/update take MILLISECONDS (analytics take nanoseconds)."
        )
    return timestamp_ms


# --------------------------------------------------------------------------- #
# Step model + payload builders (pure -- unit tested)
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class FunnelStep:
    """One step of a trace funnel.

    Only ``service_name`` and ``span_name`` are required; everything else has a
    sane default. ``step_order`` is filled in automatically by
    :func:`build_steps_update_payload` when left as ``None``.

    Note:
        There is deliberately **no** ``id`` field. SigNoz mints a UUID
        server-side, and supplying any non-UUID string (``"1"``, ``"step-1"``)
        fails the request with ``invalid UUID length``.
    """

    service_name: str
    span_name: str
    name: str | None = None
    step_order: int | None = None
    latency_pointer: LatencyPointer = "start"
    latency_type: str = "p99"
    has_errors: bool = False
    filters: dict[str, Any] = field(default_factory=lambda: {"items": [], "op": "AND"})

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> FunnelStep:
        """Build a step from a loose dict, accepting friendly aliases.

        Accepts ``service``/``service_name`` and ``span``/``span_name`` so MCP
        callers can write the short form ``{"service": ..., "span": ...}``.
        """
        service = raw.get("service_name") or raw.get("service")
        span = raw.get("span_name") or raw.get("span")
        if not service or not span:
            raise ValueError(
                f"Step {raw!r} needs both a service and a span name "
                "(keys 'service'/'service_name' and 'span'/'span_name')."
            )
        filters = raw.get("filters") or {"items": [], "op": "AND"}
        return cls(
            service_name=str(service),
            span_name=str(span),
            name=raw.get("name"),
            step_order=raw.get("step_order"),
            latency_pointer=raw.get("latency_pointer", "start"),
            latency_type=raw.get("latency_type", "p99"),
            has_errors=bool(raw.get("has_errors", False)),
            filters=filters,
        )

    def warnings(self) -> list[str]:
        """Return caveats about this step's configuration (see :data:`P50_WARNING`)."""
        if str(self.latency_type).lower() not in SUPPORTED_LATENCY_TYPES:
            return [f"step '{self.label}': {P50_WARNING}"]
        return []

    @property
    def label(self) -> str:
        """Human-facing name: the explicit ``name`` or ``service:span``."""
        return self.name or f"{self.service_name}:{self.span_name}"


def build_step_payload(step: FunnelStep, step_order: int) -> dict[str, Any]:
    """Serialize one step exactly the way SigNoz wants it.

    Critically this **omits the ``id`` key entirely** so the server generates a
    UUID; see gotcha #3 in the module docstring.
    """
    payload: dict[str, Any] = {
        "step_order": step_order,
        "service_name": step.service_name,
        "span_name": step.span_name,
        "filters": step.filters or {"items": [], "op": "AND"},
        "latency_pointer": step.latency_pointer,
        "latency_type": step.latency_type,
        "has_errors": step.has_errors,
    }
    if step.name:
        payload["name"] = step.name
    return payload


def build_create_payload(name: str, timestamp_ms: int | None = None) -> dict[str, Any]:
    """Body for ``POST /api/v1/trace-funnels/new`` (timestamp in milliseconds)."""
    if not name or not name.strip():
        raise ValueError("Funnel name must be a non-empty string.")
    return {
        "funnel_name": name.strip(),
        "timestamp": _validate_ms(now_ms() if timestamp_ms is None else timestamp_ms),
    }


def build_steps_update_payload(
    funnel_id: str,
    steps: Sequence[FunnelStep],
    timestamp_ms: int | None = None,
) -> dict[str, Any]:
    """Body for ``PUT /api/v1/trace-funnels/steps/update``.

    ``timestamp`` is mandatory server-side (gotcha #2) and step ordering is
    assigned here as 1-based, contiguous, in list order -- so callers can just
    pass steps in the order they expect them to happen.
    """
    if not funnel_id:
        raise ValueError("funnel_id is required to update steps.")
    if len(steps) < 2:
        raise ValueError(
            "A funnel needs at least 2 steps to measure a conversion "
            f"(got {len(steps)})."
        )
    return {
        "funnel_id": funnel_id,
        "timestamp": _validate_ms(now_ms() if timestamp_ms is None else timestamp_ms),
        "steps": [
            build_step_payload(step, step.step_order or index)
            for index, step in enumerate(steps, start=1)
        ],
    }


def summarize_steps(
    step_counts: dict[str, Any],
    step_labels: Sequence[str],
) -> list[dict[str, Any]]:
    """Turn the flat ``/analytics/steps`` response into per-step conversion rows.

    ``/analytics/steps`` returns a flat blob keyed by position -- ``total_s1_spans``,
    ``total_s1_errored_spans``, ``total_s2_spans``, ... This reshapes it into one
    row per step, each carrying:

    * ``n`` -- trace count reaching this step (always present; charts must be
      able to print n on every bar);
    * ``errors`` -- errored spans at this step;
    * ``conversion_from_previous_pct`` -- vs. the step before (100 for step 1);
    * ``conversion_from_start_pct`` -- vs. step 1;
    * ``dropped_from_previous`` -- absolute traces lost since the previous step.

    Percentages are rounded to 2 decimals and are ``0.0`` (never NaN) when the
    denominator is zero.
    """
    rows: list[dict[str, Any]] = []
    first_count: int | None = None
    previous_count: int | None = None

    for index, label in enumerate(step_labels, start=1):
        count = int(step_counts.get(f"total_s{index}_spans", 0) or 0)
        errors = int(step_counts.get(f"total_s{index}_errored_spans", 0) or 0)
        if first_count is None:
            first_count = count

        from_previous = 100.0 if previous_count is None else _percent(count, previous_count)
        rows.append(
            {
                "step": index,
                "label": label,
                "n": count,
                "errors": errors,
                "conversion_from_previous_pct": round(from_previous, 2),
                "conversion_from_start_pct": round(_percent(count, first_count or 0), 2)
                if index > 1
                else 100.0,
                "dropped_from_previous": 0
                if previous_count is None
                else max(previous_count - count, 0),
            }
        )
        previous_count = count
    return rows


def _percent(numerator: float, denominator: float) -> float:
    """Safe percentage: returns 0.0 rather than NaN/ZeroDivisionError."""
    if not denominator:
        return 0.0
    return (numerator / denominator) * 100.0


def _clean_floats(value: Any) -> Any:
    """Recursively replace NaN/inf with ``None`` so results are JSON-safe."""
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, dict):
        return {k: _clean_floats(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean_floats(v) for v in value]
    return value


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class SigNozFunnelClient:
    """Synchronous REST client for SigNoz Trace Funnels.

    Args:
        base_url: SigNoz base URL. Defaults to ``$SIGNOZ_URL`` then
            ``http://localhost:8080``.
        jwt: Bearer token; defaults to ``$SIGNOZ_JWT``. Obtainable from a
            browser session or ``POST /api/v2/sessions/email_password``.
        api_key: ``SIGNOZ-API-KEY`` value; defaults to ``$SIGNOZ_API_KEY``.
        timeout: Per-request timeout in seconds.

    Either ``jwt`` or ``api_key`` must be resolvable. Note that **writes**
    (create / update steps / delete) require an EDITOR or ADMIN identity; a
    read-only service-account key raises :class:`SigNozPermissionError`.
    """

    def __init__(
        self,
        base_url: str | None = None,
        jwt: str | None = None,
        api_key: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = (base_url or os.environ.get("SIGNOZ_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._jwt = jwt or os.environ.get("SIGNOZ_JWT") or None
        self._api_key = api_key or os.environ.get("SIGNOZ_API_KEY") or None
        if not self._jwt and not self._api_key:
            raise SigNozError(
                "No SigNoz credentials. Set SIGNOZ_JWT (a Bearer token) or "
                "SIGNOZ_API_KEY, or pass jwt=/api_key= explicitly. Funnel writes "
                "additionally require an EDITOR or ADMIN identity."
            )
        self._http = httpx.Client(timeout=timeout, headers=self._auth_headers())

    def _auth_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._api_key:
            headers["SIGNOZ-API-KEY"] = self._api_key
        if self._jwt:
            headers["Authorization"] = f"Bearer {self._jwt}"
        return headers

    # -- lifecycle ---------------------------------------------------------- #
    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._http.close()

    def __enter__(self) -> SigNozFunnelClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- transport ---------------------------------------------------------- #
    def _request(self, method: str, path: str, json_body: dict[str, Any] | None = None) -> Any:
        """Issue a request and unwrap SigNoz's ``{"status","data"}`` envelope.

        Raises:
            SigNozAuthError: on 401.
            SigNozPermissionError: on 403.
            ZeroTraceNaNError: on the #12143 NaN 500.
            SigNozError: on anything else non-2xx.
        """
        url = f"{self.base_url}{path}"
        try:
            response = self._http.request(method, url, json=json_body)
        except httpx.RequestError as exc:
            raise SigNozError(f"Could not reach SigNoz at {url}: {exc}") from exc

        if response.status_code >= 400:
            self._raise_for_error(response, url)

        if not response.content:
            return None
        try:
            payload = response.json()
        except ValueError:
            return response.text
        return payload.get("data") if isinstance(payload, dict) and "data" in payload else payload

    @staticmethod
    def _raise_for_error(response: httpx.Response, url: str) -> None:
        body = response.text or ""
        status = response.status_code

        # SigNoz #12143: the body here is PLAIN TEXT, not JSON, e.g.
        # "app.ApiResponse.Data: []*v3.Row: v3.Row.Data: unsupported value: NaN"
        if status == 500 and "NaN" in body:
            raise ZeroTraceNaNError(NAN_BUG_NOTE, status_code=status, body=body)

        message = body.strip()
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                error = parsed.get("error")
                if isinstance(error, dict):
                    message = str(error.get("message") or message)
                elif error:
                    message = str(error)
        except ValueError:
            pass

        if status == 401:
            raise SigNozAuthError(
                f"SigNoz rejected the credentials (401) for {url}. The JWT is "
                f"probably expired -- re-authenticate. Server said: {message}",
                status_code=status,
                body=body,
            )
        if status == 403:
            raise SigNozPermissionError(
                f"Forbidden (403) for {url}. Funnel writes need an EDITOR or "
                f"ADMIN token; read-only service-account keys cannot create or "
                f"modify funnels. Server said: {message}",
                status_code=status,
                body=body,
            )
        raise SigNozError(
            f"SigNoz returned HTTP {status} for {url}: {message}",
            status_code=status,
            body=body,
        )

    @staticmethod
    def _rows(data: Any) -> list[dict[str, Any]]:
        """Flatten the ``[{"timestamp":..., "data": {...}}, ...]`` row envelope.

        ``data`` is ``null`` when a query matches nothing (e.g. error-traces on
        a healthy funnel), so this returns ``[]`` rather than raising.
        """
        if not data:
            return []
        if isinstance(data, dict):
            return [data]
        rows = []
        for item in data:
            if isinstance(item, dict):
                rows.append(_clean_floats(item.get("data", item)))
        return rows

    def _first_row(self, data: Any) -> dict[str, Any]:
        rows = self._rows(data)
        return rows[0] if rows else {}

    # -- CRUD --------------------------------------------------------------- #
    def list_funnels(self) -> list[dict[str, Any]]:
        """``GET /list`` -- every funnel visible to this identity."""
        return self._request("GET", f"{FUNNELS_PREFIX}/list") or []

    def get_funnel(self, funnel_id: str) -> dict[str, Any]:
        """``GET /{funnel_id}`` -- full funnel definition including its steps."""
        return self._request("GET", f"{FUNNELS_PREFIX}/{funnel_id}") or {}

    def find_funnel_by_name(self, name: str) -> dict[str, Any] | None:
        """Case-insensitive lookup by ``funnel_name``; ``None`` if absent."""
        target = name.strip().lower()
        for funnel in self.list_funnels():
            if str(funnel.get("funnel_name", "")).strip().lower() == target:
                return funnel
        return None

    def resolve_funnel_id(
        self, funnel_id: str | None = None, funnel_name: str | None = None
    ) -> str:
        """Return a funnel id given an id *or* a name.

        Raises:
            SigNozError: if neither is supplied, or the name matches nothing.
        """
        if funnel_id:
            return funnel_id
        if not funnel_name:
            raise SigNozError("Provide either funnel_id or funnel_name.")
        funnel = self.find_funnel_by_name(funnel_name)
        if not funnel:
            available = [f.get("funnel_name") for f in self.list_funnels()]
            raise SigNozError(
                f"No funnel named {funnel_name!r}. Available funnels: {available}"
            )
        return str(funnel["funnel_id"])

    def create_funnel(self, name: str, timestamp_ms: int | None = None) -> str:
        """``POST /new`` -- create an empty funnel, returning its new id."""
        data = self._request(
            "POST", f"{FUNNELS_PREFIX}/new", build_create_payload(name, timestamp_ms)
        )
        funnel_id = (data or {}).get("funnel_id") if isinstance(data, dict) else None
        if not funnel_id:
            raise SigNozError(f"Funnel created but no funnel_id came back: {data!r}")
        return str(funnel_id)

    def update_steps(
        self,
        funnel_id: str,
        steps: Sequence[FunnelStep],
        timestamp_ms: int | None = None,
    ) -> dict[str, Any]:
        """``PUT /steps/update`` -- replace a funnel's steps wholesale."""
        payload = build_steps_update_payload(funnel_id, steps, timestamp_ms)
        return self._request("PUT", f"{FUNNELS_PREFIX}/steps/update", payload) or {}

    def create_funnel_with_steps(
        self,
        name: str,
        steps: Iterable[FunnelStep | dict[str, Any]],
        timestamp_ms: int | None = None,
    ) -> dict[str, Any]:
        """Create a funnel **and** set its steps -- the two REST calls as one.

        Returns a dict with ``funnel_id``, the normalized ``steps``, and any
        ``warnings`` (e.g. an unsupported latency quantile).

        If step assignment fails the freshly created -- and now useless -- empty
        funnel is deleted so a partial failure does not litter the instance.
        """
        normalized = [
            step if isinstance(step, FunnelStep) else FunnelStep.from_dict(step) for step in steps
        ]
        warnings: list[str] = [w for step in normalized for w in step.warnings()]

        funnel_id = self.create_funnel(name, timestamp_ms)
        try:
            self.update_steps(funnel_id, normalized, timestamp_ms)
        except Exception:
            # Roll back so a failed step update doesn't leave an orphan funnel.
            try:
                self.delete_funnel(funnel_id)
            except Exception:  # pragma: no cover - best-effort cleanup
                pass
            raise

        return {
            "funnel_id": funnel_id,
            "funnel_name": name,
            "steps": [
                {"step": i, "label": s.label, "service": s.service_name, "span": s.span_name}
                for i, s in enumerate(normalized, start=1)
            ],
            "warnings": warnings,
        }

    def delete_funnel(self, funnel_id: str) -> None:
        """``DELETE /{funnel_id}`` -- permanently remove a funnel."""
        self._request("DELETE", f"{FUNNELS_PREFIX}/{funnel_id}")

    # -- analytics ---------------------------------------------------------- #
    def _analytics(
        self,
        funnel_id: str,
        endpoint: str,
        start_ns: int,
        end_ns: int,
        **extra: Any,
    ) -> Any:
        """POST to an ``/analytics/*`` endpoint with nanosecond bounds."""
        body: dict[str, Any] = {"start_time": start_ns, "end_time": end_ns, **extra}
        return self._request("POST", f"{FUNNELS_PREFIX}/{funnel_id}/analytics/{endpoint}", body)

    def validate_funnel(self, funnel_id: str, start_ns: int, end_ns: int) -> list[dict[str, Any]]:
        """``/analytics/validate`` -- sample trace ids that match the funnel.

        Useful as a smoke test: an empty list means the step definitions never
        co-occur in a single trace in this window.
        """
        return self._rows(self._analytics(funnel_id, "validate", start_ns, end_ns))

    def overview(self, funnel_id: str, start_ns: int, end_ns: int) -> dict[str, Any]:
        """``/analytics/overview`` -- END-TO-END metrics only, NaN-bug tolerant.

        Returns ``conversion_rate``, ``avg_rate``, ``errors``, ``avg_duration``
        and ``latency``, plus two honesty flags this client adds:

        * ``zero_trace_fallback`` -- ``True`` when SigNoz 500'd with NaN and we
          substituted zeros (SigNoz issue #12143);
        * ``caveats`` -- reminders that ``errors`` is a MAX across steps rather
          than a sum, and that the latency quantile is hardcoded to p99.
        """
        caveats = [
            "'errors' is greatest(...) -- a MAX across steps, not a sum.",
            "'latency' is hardcoded to the p99 quantile by SigNoz, regardless of "
            "the step's latency_type.",
        ]
        try:
            row = self._first_row(self._analytics(funnel_id, "overview", start_ns, end_ns))
        except ZeroTraceNaNError as exc:
            return {
                "conversion_rate": 0,
                "avg_rate": 0,
                "errors": 0,
                "avg_duration": 0,
                "latency": 0,
                "zero_trace_fallback": True,
                "note": str(exc),
                "caveats": caveats,
            }
        row.setdefault("conversion_rate", 0)
        row["zero_trace_fallback"] = False
        row["caveats"] = caveats
        return row

    def step_counts(self, funnel_id: str, start_ns: int, end_ns: int) -> dict[str, Any]:
        """``/analytics/steps`` -- the flat per-step span/error counts.

        This is the *only* endpoint that exposes per-step numbers, so it is the
        source of truth for per-step conversion.
        """
        return self._first_row(self._analytics(funnel_id, "steps", start_ns, end_ns))

    def step_overview(
        self,
        funnel_id: str,
        start_ns: int,
        end_ns: int,
        step_start: int = 1,
        step_end: int = 2,
    ) -> dict[str, Any]:
        """``/analytics/steps/overview`` -- metrics for one step *transition*.

        ``step_start``/``step_end`` are 1-based and must differ, otherwise
        SigNoz 500s with "step start and end cannot be the same". NaN-bug
        tolerant, like :meth:`overview`.
        """
        if step_start == step_end:
            raise ValueError(
                "step_start and step_end must differ -- SigNoz 500s with "
                "'step start and end cannot be the same' otherwise."
            )
        try:
            row = self._first_row(
                self._analytics(
                    funnel_id,
                    "steps/overview",
                    start_ns,
                    end_ns,
                    step_start=step_start,
                    step_end=step_end,
                )
            )
        except ZeroTraceNaNError as exc:
            return {
                "conversion_rate": 0,
                "avg_rate": 0,
                "errors": 0,
                "avg_duration": 0,
                "latency": 0,
                "zero_trace_fallback": True,
                "note": str(exc),
            }
        row["zero_trace_fallback"] = False
        return row

    def slow_traces(
        self,
        funnel_id: str,
        start_ns: int,
        end_ns: int,
        step_start: int = 1,
        step_end: int = 2,
    ) -> list[dict[str, Any]]:
        """``/analytics/slow-traces`` -- up to **5** slowest traces for a transition.

        SigNoz hardcodes ``ORDER BY duration_ms DESC LIMIT 5`` and the query is
        pairwise (step_start -> step_end semantics), so this can never return
        more than five rows no matter what you ask for.
        """
        return self._rows(
            self._analytics(
                funnel_id,
                "slow-traces",
                start_ns,
                end_ns,
                step_start=step_start,
                step_end=step_end,
            )
        )

    def error_traces(
        self,
        funnel_id: str,
        start_ns: int,
        end_ns: int,
        step_start: int = 1,
        step_end: int = 2,
    ) -> list[dict[str, Any]]:
        """``/analytics/error-traces`` -- sample traces that errored in a transition.

        Returns ``[]`` when nothing errored (SigNoz sends ``data: null`` there).
        """
        return self._rows(
            self._analytics(
                funnel_id,
                "error-traces",
                start_ns,
                end_ns,
                step_start=step_start,
                step_end=step_end,
            )
        )

    # -- high-level composite ------------------------------------------------ #
    def funnel_analytics(
        self,
        funnel_id: str | None = None,
        funnel_name: str | None = None,
        time_range: str = "24h",
        start: datetime | str | None = None,
        end: datetime | str | None = None,
    ) -> dict[str, Any]:
        """One-call funnel report: per-step conversion **plus** end-to-end metrics.

        Combines ``/analytics/steps`` (the only per-step source) with
        ``/analytics/overview`` (end-to-end), resolves the funnel's step labels
        from its definition, and handles the NaN bug. Every step row carries an
        explicit ``n`` trace count.
        """
        resolved_id = self.resolve_funnel_id(funnel_id, funnel_name)
        start_ns, end_ns = resolve_window_ns(time_range, start, end)

        definition = self.get_funnel(resolved_id)
        raw_steps = definition.get("steps") or []
        labels = [
            step.get("name") or f"{step.get('service_name')}:{step.get('span_name')}"
            for step in raw_steps
        ]

        counts = self.step_counts(resolved_id, start_ns, end_ns)
        steps = summarize_steps(counts, labels)
        end_to_end = self.overview(resolved_id, start_ns, end_ns)

        return {
            "funnel_id": resolved_id,
            "funnel_name": definition.get("funnel_name"),
            "time_range": {
                "requested": time_range if start is None else f"{start} .. {end}",
                "start_ns": start_ns,
                "end_ns": end_ns,
                "start_iso": datetime.fromtimestamp(start_ns / 1e9, UTC).isoformat(),
                "end_iso": datetime.fromtimestamp(end_ns / 1e9, UTC).isoformat(),
            },
            "steps": steps,
            "end_to_end": end_to_end,
            "totals": {
                "entered": steps[0]["n"] if steps else 0,
                "completed": steps[-1]["n"] if steps else 0,
                "overall_conversion_pct": steps[-1]["conversion_from_start_pct"] if steps else 0.0,
            },
            "semantics": (
                "Funnel steps must occur in the SAME trace. SigNoz matches the "
                "FIRST occurrence of each step (minIf) and enforces step order, "
                "so funnels are structurally blind to loops and retries. "
                + STRICT_ORDERING_NOTE
            ),
            "diagnostics": self._diagnose(steps),
        }

    @staticmethod
    def _diagnose(steps: list[dict[str, Any]]) -> list[str]:
        """Turn suspicious per-step numbers into plain-language explanations.

        A 0%-conversion step is ambiguous -- bad span name, or a real drop-off,
        or the same-clock-tick ordering trap -- so we name the candidates rather
        than leaving the caller to guess.
        """
        notes: list[str] = []
        if not steps:
            return notes
        if steps[0]["n"] == 0:
            notes.append(
                "Step 1 matched ZERO spans: its (service_name, span_name) likely "
                "does not exist, or the time window misses your data. Nothing "
                "downstream can be measured until this is fixed."
            )
        for row in steps[1:]:
            if row["n"] == 0 and row["step"] > 1:
                notes.append(
                    f"Step {row['step']} ({row['label']}) matched ZERO traces. "
                    "Either its span name is wrong, or no trace has it strictly "
                    "after the previous step. " + STRICT_ORDERING_NOTE
                )
        return notes
