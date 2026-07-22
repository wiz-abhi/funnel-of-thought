"""Unit tests for payload construction and time conversion.

Pure functions only -- these run with no SigNoz instance and no network. Each
test corresponds to a real API gotcha that cost a failed request to discover.
"""

from __future__ import annotations

import math

import pytest

from signoz_funnel_mcp.client import (
    FunnelStep,
    NAN_BUG_NOTE,
    P50_WARNING,
    SigNozFunnelClient,
    build_create_payload,
    build_step_payload,
    build_steps_update_payload,
    now_ms,
    parse_duration_to_seconds,
    resolve_window_ns,
    summarize_steps,
    to_ns,
)


# --------------------------------------------------------------------------- #
# Gotcha #3: steps must OMIT `id`
# --------------------------------------------------------------------------- #
def test_step_payload_omits_id_entirely():
    """Passing any non-UUID id fails with 'invalid UUID length'; we send none."""
    payload = build_step_payload(FunnelStep(service_name="svc", span_name="span"), 1)
    assert "id" not in payload


def test_step_payload_has_required_fields_and_defaults():
    payload = build_step_payload(FunnelStep(service_name="svc", span_name="span"), 3)
    assert payload == {
        "step_order": 3,
        "service_name": "svc",
        "span_name": "span",
        "filters": {"items": [], "op": "AND"},
        "latency_pointer": "start",
        "latency_type": "p99",
        "has_errors": False,
    }


def test_step_payload_includes_optional_name_only_when_set():
    named = build_step_payload(FunnelStep("svc", "span", name="browse"), 1)
    assert named["name"] == "browse"
    assert "name" not in build_step_payload(FunnelStep("svc", "span"), 1)


def test_step_from_dict_accepts_short_aliases():
    step = FunnelStep.from_dict({"service": "frontend", "span": "GET /cart"})
    assert (step.service_name, step.span_name) == ("frontend", "GET /cart")


def test_step_from_dict_rejects_missing_span():
    with pytest.raises(ValueError, match="service and a span"):
        FunnelStep.from_dict({"service": "frontend"})


# --------------------------------------------------------------------------- #
# Gotcha #10: p50 is silently p99
# --------------------------------------------------------------------------- #
def test_p50_latency_type_produces_warning():
    warnings = FunnelStep("svc", "span", latency_type="p50").warnings()
    assert len(warnings) == 1
    assert P50_WARNING in warnings[0]


@pytest.mark.parametrize("latency_type", ["p90", "p95", "p99"])
def test_supported_latency_types_produce_no_warning(latency_type):
    assert FunnelStep("svc", "span", latency_type=latency_type).warnings() == []


# --------------------------------------------------------------------------- #
# Gotchas #1 and #2: millisecond timestamps, required on both calls
# --------------------------------------------------------------------------- #
def test_create_payload_timestamp_is_in_millisecond_range():
    payload = build_create_payload("checkout")
    assert 1e12 <= payload["timestamp"] < 1e13
    assert payload["funnel_name"] == "checkout"


def test_create_payload_strips_whitespace_and_rejects_blank_name():
    assert build_create_payload("  checkout  ")["funnel_name"] == "checkout"
    with pytest.raises(ValueError, match="non-empty"):
        build_create_payload("   ")


@pytest.mark.parametrize(
    "bad_timestamp",
    [
        1784663265,  # seconds -- the classic mistake
        1784663265000000000,  # nanoseconds -- the other classic mistake
        0,
    ],
)
def test_create_payload_rejects_non_millisecond_timestamps(bad_timestamp):
    with pytest.raises(ValueError, match="MILLISECONDS"):
        build_create_payload("checkout", timestamp_ms=bad_timestamp)


def test_steps_update_payload_always_includes_timestamp():
    """Omitting `timestamp` on /steps/update yields 400 'timestamp is required'."""
    payload = build_steps_update_payload(
        "abc", [FunnelStep("a", "x"), FunnelStep("b", "y")]
    )
    assert "timestamp" in payload
    assert 1e12 <= payload["timestamp"] < 1e13
    assert payload["funnel_id"] == "abc"


def test_steps_update_assigns_contiguous_one_based_order():
    payload = build_steps_update_payload(
        "abc", [FunnelStep("a", "x"), FunnelStep("b", "y"), FunnelStep("c", "z")]
    )
    assert [s["step_order"] for s in payload["steps"]] == [1, 2, 3]


def test_steps_update_requires_at_least_two_steps():
    with pytest.raises(ValueError, match="at least 2 steps"):
        build_steps_update_payload("abc", [FunnelStep("a", "x")])


def test_now_ms_is_in_the_accepted_range():
    assert 1e12 <= now_ms() < 1e13


# --------------------------------------------------------------------------- #
# Gotcha #4: analytics windows are NANOSECONDS
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "expected"),
    [("30s", 30), ("30m", 1800), ("24h", 86400), ("7d", 604800), ("2w", 1209600)],
)
def test_parse_duration_units(text, expected):
    assert parse_duration_to_seconds(text) == expected


def test_parse_duration_is_case_and_space_insensitive():
    assert parse_duration_to_seconds(" 24H ") == 86400


@pytest.mark.parametrize("bad", ["24", "abc", "", "0h", "-5d", "24 hours"])
def test_parse_duration_rejects_junk(bad):
    with pytest.raises(ValueError):
        parse_duration_to_seconds(bad)


def test_resolve_window_returns_nanoseconds_not_milliseconds():
    start_ns, end_ns = resolve_window_ns("24h", now=1_700_000_000.0)
    assert end_ns == 1_700_000_000_000_000_000
    assert end_ns - start_ns == 86_400 * 1_000_000_000
    assert end_ns > 1e18  # unmistakably ns, not ms


def test_resolve_window_accepts_explicit_iso_bounds():
    start_ns, end_ns = resolve_window_ns(
        None, start="2026-07-01T00:00:00Z", end="2026-07-02T00:00:00Z"
    )
    assert end_ns - start_ns == 86_400 * 1_000_000_000


def test_resolve_window_rejects_half_specified_bounds():
    with pytest.raises(ValueError, match="both"):
        resolve_window_ns(None, start="2026-07-01T00:00:00Z")


def test_resolve_window_rejects_inverted_bounds():
    with pytest.raises(ValueError, match="after its start"):
        resolve_window_ns(None, start="2026-07-02T00:00:00Z", end="2026-07-01T00:00:00Z")


def test_to_ns_handles_z_suffix_and_naive_datetimes():
    from datetime import datetime

    assert to_ns("1970-01-01T00:00:01Z") == 1_000_000_000
    assert to_ns(datetime(1970, 1, 1, 0, 0, 1)) == 1_000_000_000
    assert to_ns(1.5) == 1_500_000_000


# --------------------------------------------------------------------------- #
# Gotcha #6: per-step conversion is built from /analytics/steps
# --------------------------------------------------------------------------- #
def test_summarize_steps_matches_live_response_shape():
    """Shape taken verbatim from a live /analytics/steps response."""
    counts = {
        "total_s1_spans": 22,
        "total_s1_errored_spans": 0,
        "total_s2_spans": 22,
        "total_s2_errored_spans": 0,
        "total_s3_spans": 0,
        "total_s3_errored_spans": 0,
    }
    rows = summarize_steps(counts, ["answer", "search", "finish"])

    assert [r["n"] for r in rows] == [22, 22, 0]
    assert rows[0]["conversion_from_previous_pct"] == 100.0
    assert rows[1]["conversion_from_previous_pct"] == 100.0
    assert rows[2]["conversion_from_previous_pct"] == 0.0
    assert rows[2]["dropped_from_previous"] == 22
    assert rows[2]["conversion_from_start_pct"] == 0.0


def test_summarize_steps_computes_partial_dropoff():
    counts = {"total_s1_spans": 100, "total_s2_spans": 40, "total_s3_spans": 10}
    rows = summarize_steps(counts, ["a", "b", "c"])
    assert rows[1]["conversion_from_previous_pct"] == 40.0
    assert rows[2]["conversion_from_previous_pct"] == 25.0
    assert rows[2]["conversion_from_start_pct"] == 10.0
    assert rows[1]["dropped_from_previous"] == 60


def test_summarize_steps_never_returns_nan_on_empty_funnel():
    """A wholly empty funnel must yield zeros, never NaN -- charts render this."""
    rows = summarize_steps({}, ["a", "b", "c"])
    assert [r["n"] for r in rows] == [0, 0, 0]
    for row in rows:
        for key in ("conversion_from_previous_pct", "conversion_from_start_pct"):
            assert not math.isnan(row[key])


def test_summarize_steps_always_emits_n_for_every_step():
    """Every step row must carry an explicit n -- charts label each bar with it."""
    rows = summarize_steps({"total_s1_spans": 5}, ["a", "b"])
    assert all("n" in row for row in rows)
    assert [r["n"] for r in rows] == [5, 0]


def test_summarize_steps_matches_verified_reference_funnel():
    """Numbers taken from the verified live 'spike2-cognition' funnel."""
    counts = {
        "total_s1_spans": 60,
        "total_s2_spans": 60,
        "total_s3_spans": 36,
        "total_s4_spans": 36,
    }
    rows = summarize_steps(counts, ["plan", "tool", "validate", "respond"])
    assert [r["n"] for r in rows] == [60, 60, 36, 36]
    assert [r["conversion_from_previous_pct"] for r in rows] == [100.0, 100.0, 60.0, 100.0]
    assert rows[-1]["conversion_from_start_pct"] == 60.0


# --------------------------------------------------------------------------- #
# Strict ordering trap: a 0-trace step must be explained, not just reported
# --------------------------------------------------------------------------- #
def test_diagnostics_flag_empty_first_step():
    rows = summarize_steps({}, ["a", "b"])
    notes = SigNozFunnelClient._diagnose(rows)
    assert any("Step 1 matched ZERO spans" in n for n in notes)


def test_diagnostics_explain_strict_ordering_for_empty_later_step():
    """The same-clock-tick trap must be named, since it is invisible otherwise."""
    rows = summarize_steps({"total_s1_spans": 22, "total_s2_spans": 0}, ["a", "b"])
    notes = SigNozFunnelClient._diagnose(rows)
    assert any("strictly" in n.lower() for n in notes)
    assert any("clock tick" in n for n in notes)


def test_diagnostics_silent_on_a_healthy_funnel():
    rows = summarize_steps({"total_s1_spans": 60, "total_s2_spans": 36}, ["a", "b"])
    assert SigNozFunnelClient._diagnose(rows) == []


def test_nan_note_names_both_causes():
    """Users hit both causes; the message must not imply only a missing span."""
    assert "matches no spans" in NAN_BUG_NOTE
    assert "clock tick" in NAN_BUG_NOTE
    assert "#12143" in NAN_BUG_NOTE
