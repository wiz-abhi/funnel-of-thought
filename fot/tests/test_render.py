"""Rendering tests -- specifically that the headline chart survives a small console.

`fot show` is the output this whole project is built around, and the default
Windows `cmd.exe` is 80 columns. A hardcoded 44-wide bar made the table 94 cells
wide; rich absorbed the overflow by collapsing the `step` column to zero width,
so the step names -- the entire point of the chart -- silently disappeared.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from fot.funnels import load_suite
from fot.render import BAR_WIDTH, MIN_BAR_WIDTH, bar_text, render_funnel
from fot.signoz import StepCounts

LABELS = ["plan", "tool", "validate", "respond"]
PUBLISHED = [125, 125, 80, 80]


def _render(width: int) -> list[str]:
    buf = io.StringIO()
    out = Console(width=width, legacy_windows=False, file=buf, force_terminal=False)
    render_funnel(
        load_suite().get("cognition"),
        StepCounts(labels=LABELS, totals=PUBLISHED),
        window="30d",
        start_ns=1784219000000000000,
        end_ns=1784305400000000000,
        out=out,
    )
    return buf.getvalue().splitlines()


@pytest.mark.parametrize("width", [80, 100, 120, 200])
def test_chart_never_overflows_the_console(width):
    lines = _render(width)
    widest = max(len(line.rstrip()) for line in lines)
    assert widest <= width, f"overflowed by {widest - width} cells at width={width}"


@pytest.mark.parametrize("width", [80, 100, 120])
def test_every_step_name_survives_at_narrow_widths(width):
    """The regression this file exists for: labels must not be crushed away."""
    body = "\n".join(_render(width))
    for label in LABELS:
        assert label in body, f"step label {label!r} vanished at width={width}"


@pytest.mark.parametrize("width", [80, 100, 120])
def test_absolute_counts_stay_on_the_bars(width):
    """A funnel shown only as percentages hides 60%-of-5 versus 60%-of-5000."""
    body = "\n".join(_render(width))
    assert "n=125" in body
    assert "n=80" in body


def test_conversion_numbers_are_present_at_80_columns():
    body = "\n".join(_render(80))
    assert "64.0%" in body and "100.0%" in body
    assert "-45" in body  # the lost column is not clipped away


# --------------------------------------------------------------------------
# bar_text invariants
# --------------------------------------------------------------------------


@pytest.mark.parametrize("width", [MIN_BAR_WIDTH, 30, BAR_WIDTH])
@pytest.mark.parametrize("n,peak", [(0, 0), (0, 125), (1, 125), (80, 125), (125, 125), (200, 125)])
def test_bar_text_is_exactly_the_requested_width(n, peak, width):
    assert len(bar_text(n, peak, "green", width=width).plain) == width


def test_bar_text_handles_a_negative_count_without_crashing():
    assert len(bar_text(-5, 125, "green", width=30).plain) == 30
