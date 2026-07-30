"""Unit tests for the conversion arithmetic and the definition loader.

No network, no SigNoz, no Docker. These cover the code that turns a raw
``/analytics/steps`` payload into the numbers the README quotes -- the layer
where a wrong answer is *plausible* rather than obviously broken, and therefore
the layer most worth pinning down.

The named cases below reproduce the published dataset exactly:
125 traces, steps [125, 125, 80, 80], validate at 64.0%.
"""

from __future__ import annotations

import os

import pytest

from fot.funnels import Step, load_suite
from fot.signoz import StepCounts, ms_now, ns_now, parse_duration

# The published dataset. Every number in the README derives from this shape.
PUBLISHED = [125, 125, 80, 80]
LABELS = ["plan", "tool", "validate", "respond"]


# --------------------------------------------------------------------------
# conversion arithmetic
# --------------------------------------------------------------------------


def test_cumulative_pct_reproduces_the_published_64_percent():
    counts = StepCounts(labels=LABELS, totals=PUBLISHED)
    assert counts.cumulative_pct() == [100.0, 100.0, 64.0, 64.0]
    assert counts.entered == 125


def test_step_pct_is_relative_to_the_previous_step_not_the_first():
    """The two views answer different questions and must not be conflated."""
    counts = StepCounts(labels=LABELS, totals=PUBLISHED)
    # validate loses 36% of what reached tool, then respond loses nothing.
    assert counts.step_pct() == [100.0, 100.0, 64.0, 100.0]


def test_biggest_drop_finds_validate_and_counts_the_lost_traces():
    counts = StepCounts(labels=LABELS, totals=PUBLISHED)
    idx, pct_lost, lost = counts.biggest_drop()
    assert LABELS[idx] == "validate"
    assert lost == 45  # 125 - 80, the gap the whole project is about
    assert pct_lost == pytest.approx(36.0)


@pytest.mark.parametrize(
    "totals",
    [
        [0, 0, 0, 0],  # nothing entered the funnel
        [],  # no steps at all
    ],
)
def test_empty_data_yields_zeros_not_a_zero_division(totals):
    counts = StepCounts(labels=LABELS[: len(totals)], totals=totals)
    assert counts.cumulative_pct() == [0.0] * len(totals)
    assert counts.step_pct() == [0.0] * len(totals)
    assert counts.biggest_drop() is None
    assert counts.entered == 0


def test_a_step_can_exceed_its_predecessor_without_breaking_the_math():
    """Not expected from a funnel, but it must not produce a negative or a crash."""
    counts = StepCounts(labels=["a", "b"], totals=[10, 20])
    assert counts.step_pct() == [100.0, 200.0]
    assert counts.biggest_drop() is None


def test_errors_default_to_one_zero_per_step():
    counts = StepCounts(labels=LABELS, totals=PUBLISHED)
    assert counts.errors == [0, 0, 0, 0]


def test_degraded_counts_carry_the_status_that_caused_them():
    """A NaN 500 and a genuine 0% are different findings; keep them separable."""
    clean = StepCounts(labels=LABELS, totals=[125, 125, 0, 0])
    degraded = StepCounts(
        labels=LABELS, totals=[0, 0, 0, 0], degraded=True, nan_status=500,
        nan_error="unsupported value: NaN",
    )
    assert not clean.degraded and clean.nan_status is None
    assert degraded.degraded and degraded.nan_status == 500
    assert "NaN" in degraded.nan_error


# --------------------------------------------------------------------------
# time units -- the gotcha that silently returns a 1970 window
# --------------------------------------------------------------------------


def test_ms_and_ns_helpers_differ_by_six_orders_of_magnitude():
    """Funnel create/update wants milliseconds; analytics wants nanoseconds."""
    assert ns_now() // 1_000_000 == pytest.approx(ms_now(), abs=1000)
    assert len(str(ns_now())) - len(str(ms_now())) == 6


@pytest.mark.parametrize(
    "text,seconds",
    [("30d", 30 * 86400), ("24h", 86400), ("90m", 5400), ("45s", 45), ("2w", 1209600)],
)
def test_parse_duration_known_units(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("bad", ["30", "d30", "30y", "", "-1d", "abc", "1.5h"])
def test_parse_duration_rejects_junk(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)


# --------------------------------------------------------------------------
# definition loading and env interpolation
# --------------------------------------------------------------------------


def test_default_suite_defines_all_four_arms():
    """reproduce.sh depends on `cognition` and `fragmented` both existing."""
    suite = load_suite()
    names = {f.name for f in suite}
    assert {"cognition", "control", "genai", "fragmented"} <= names


def test_fragmented_arm_keys_on_a_model_that_is_not_generated():
    """That mismatch is the whole point: it must not accidentally match."""
    suite = load_suite()
    generated = os.environ.get("FOT_MODEL", "gemini-3.1-flash-lite")
    fragmented_span = suite.get("fragmented").steps[-1].span
    assert fragmented_span.startswith("chat ")
    assert generated not in fragmented_span


def test_env_default_applies_when_the_variable_is_set_but_empty(monkeypatch):
    """Shell `:-` semantics. Returning "" here would match no spans and then
    report a confident 0% conversion, which is the worst possible failure."""
    monkeypatch.setenv("FOT_SERVICE", "")
    assert load_suite().get("cognition").steps[0].service == "fot-agent"


def test_env_override_is_honoured(monkeypatch):
    monkeypatch.setenv("FOT_SERVICE", "my-service")
    assert load_suite().get("cognition").steps[0].service == "my-service"


def test_empty_span_is_rejected_rather_than_matching_nothing():
    with pytest.raises(ValueError, match="empty"):
        Step(service="svc", span="", label="plan")


def test_p50_latency_type_is_rejected():
    """SigNoz silently returns p99 for p50 (issue #12220), so refuse to ask."""
    with pytest.raises(ValueError, match="p50"):
        Step(service="svc", span="agent.plan", label="plan", latency_type="p50")


def test_step_payload_is_one_based_and_omits_id():
    """Sending "id" fails with `invalid UUID length`; step_order is 1-based."""
    payload = Step(service="svc", span="agent.plan", label="plan").to_payload(1)
    assert payload["step_order"] == 1
    assert payload["service_name"] == "svc"
    assert payload["span_name"] == "agent.plan"
    assert "id" not in payload
