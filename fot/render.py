"""Terminal rendering for Funnel of Thought.

Everything here is presentation only -- no I/O, no API calls -- so the layout can
be unit-tested against synthetic :class:`~fot.signoz.StepCounts`.

The design goal for :func:`render_funnel` is that a reader with no context can look
at one screenshot and answer three questions: how many runs entered, where did they
fall out, and how bad is it. Hence: absolute ``n`` printed *on* every bar (never
percentages alone), conversion measured both against entry and against the previous
step, and the worst cliff called out in words at the bottom.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from rich.box import HEAVY_HEAD, ROUNDED
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .funnels import Funnel
from .signoz import StepCounts

__all__ = [
    "console",
    "render_funnel",
    "render_compare",
    "render_counter_proof",
    "bar_text",
]

def _utf8_console() -> Console:
    """Build a console that can actually print box-drawing characters on Windows.

    The default Windows console codepage is cp1252, which cannot encode the block
    glyphs the bars are drawn with -- rich dies with ``UnicodeEncodeError`` partway
    through a render. Reconfiguring the streams to UTF-8 fixes it; no-op elsewhere.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass  # already UTF-8, or not reconfigurable (e.g. a pipe wrapper)
    # legacy_windows=False keeps the rounded/heavy box glyphs instead of rich's
    # ASCII downgrade, which it would otherwise apply whenever stdout is piped.
    return Console(legacy_windows=False)


console = _utf8_console()

BAR_WIDTH = 44
FILL_CHAR = "█"
EMPTY_CHAR = "░"


def _conversion_style(pct: float) -> str:
    """Map a step conversion percentage to a rich style (green -> red)."""
    if pct >= 95.0:
        return "bright_green"
    if pct >= 80.0:
        return "green"
    if pct >= 60.0:
        return "yellow"
    if pct >= 40.0:
        return "dark_orange"
    return "bright_red"


def bar_text(n: int, peak: int, style: str, width: int = BAR_WIDTH) -> Text:
    """Build a proportional bar with the absolute count ``n`` printed on it.

    Printing ``n`` on the bar itself is a hard requirement of this tool: a funnel
    rendered only as percentages hides the difference between "60% of 5000 runs"
    and "60% of 5 runs", and the second one means nothing.

    Args:
        n: absolute trace count for this step.
        peak: the largest count in the funnel, used to scale the bar.
        style: rich style for the filled portion.
        width: total bar width in characters.

    Returns:
        A :class:`rich.text.Text` of exactly ``width`` cells.
    """
    peak = max(peak, 1)
    fill = int(round(width * min(n, peak) / peak))
    fill = max(fill, 1) if n > 0 else 0
    chars = list(FILL_CHAR * fill + EMPTY_CHAR * (width - fill))

    label = f" n={n} "
    if fill >= len(label) + 2:
        start, inside = fill - len(label), True  # flush to the right edge of the fill
    else:
        start, inside = min(fill + 1, width - len(label)), False  # just past the fill
    start = max(start, 0)
    chars[start : start + len(label)] = list(label)

    text = Text("".join(chars[:width]))
    if fill:
        text.stylize(style, 0, fill)
    text.stylize("grey37", fill, width)
    # Keep the count legible whichever segment it landed on.
    text.stylize(
        "bold black on " + style if inside else "bold white",
        start,
        start + len(label),
    )
    return text


def _fmt_window(start_ns: int, end_ns: int) -> str:
    """Format an analytics window as ``YYYY-MM-DD HH:MM -> YYYY-MM-DD HH:MM UTC``."""
    fmt = "%Y-%m-%d %H:%M"
    start = datetime.fromtimestamp(start_ns / 1e9, timezone.utc).strftime(fmt)
    end = datetime.fromtimestamp(end_ns / 1e9, timezone.utc).strftime(fmt)
    return f"{start} -> {end} UTC"


def render_funnel(
    funnel: Funnel,
    counts: StepCounts,
    *,
    window: str,
    start_ns: int,
    end_ns: int,
    out: Console | None = None,
) -> None:
    """Render one funnel as a horizontal bar chart with conversion columns.

    Args:
        funnel: the definition being rendered (supplies names and services).
        counts: per-step trace counts from ``/analytics/steps``.
        window: human window label such as ``30d``.
        start_ns: window start (nanoseconds), for the header.
        end_ns: window end (nanoseconds), for the header.
        out: console override, mainly for testing.
    """
    out = out or console
    cumulative = counts.cumulative_pct()
    per_step = counts.step_pct()
    worst = counts.biggest_drop()
    worst_idx = worst[0] if worst else -1
    peak = max(counts.totals) if counts.totals else 0

    # Build the table first so the header/footer panels can be measured to match it.
    table = Table(box=HEAVY_HEAD, header_style="bold grey62", pad_edge=False, expand=False)
    table.add_column("#", justify="right", style="grey54", width=2)
    table.add_column("step", style="bold", min_width=10)
    table.add_column("traces reaching this step, in order", width=BAR_WIDTH)
    table.add_column("of entry", justify="right", width=9)
    table.add_column("vs prev", justify="right", width=11)
    table.add_column("lost", justify="right", width=6)

    for i, label in enumerate(counts.labels):
        n = counts.totals[i]
        step_pct = per_step[i]
        style = _conversion_style(step_pct)
        is_worst = i == worst_idx
        lost = counts.totals[i - 1] - n if i > 0 else 0

        table.add_row(
            str(i + 1),
            Text(label, style="bold bright_red" if is_worst else "bold white"),
            bar_text(n, peak, style),
            Text(f"{cumulative[i]:.1f}%", style=_conversion_style(cumulative[i])),
            Text(
                f"{step_pct:.1f}% ◀" if is_worst else f"{step_pct:.1f}%",
                style=("bold " if is_worst else "") + style,
            ),
            Text(f"-{lost}" if lost > 0 else "·", style="bright_red" if lost > 0 else "grey37"),
        )
    width = out.measure(table).maximum

    chain = Text.assemble(
        *[
            part
            for i, label in enumerate(funnel.labels)
            for part in ((" → ", "grey42"),) * (i > 0) + ((label, "bold cyan"),)
        ]
    )
    header = Group(
        Text.assemble(
            ("FUNNEL OF THOUGHT", "bold white"),
            ("  ·  ", "grey42"),
            (funnel.name, "bold magenta"),
            ("   ", ""),
            ("reasoning contract", "grey42"),
        ),
        chain,
        Text.assemble(
            ("service ", "grey62"), (funnel.service, "white"),
            ("   window ", "grey62"), (window, "white"),
            ("   ", ""), (_fmt_window(start_ns, end_ns), "grey50"),
        ),
        Text.assemble(
            ("entered ", "grey62"), (f"{counts.entered}", "bold white"),
            (" traces      completed ", "grey62"),
            (f"{counts.totals[-1] if counts.totals else 0}", "bold white"),
            (" traces      end-to-end ", "grey62"),
            (f"{cumulative[-1] if cumulative else 0:.1f}%",
             "bold " + _conversion_style(cumulative[-1] if cumulative else 0)),
        ),
    )
    out.print(Panel(header, box=ROUNDED, border_style="cyan", padding=(0, 2), width=width))
    out.print(table)

    # --- footer: name the cliff in plain language --------------------------------
    if counts.degraded:
        out.print(
            Panel(
                Text(
                    "Analytics degraded: SigNoz returned HTTP 500 'unsupported value: NaN' "
                    "because at least one step matched zero traces (server-side avgIf over an "
                    "empty set). Counts shown as 0 rather than crashing -- SigNoz issue #12143.",
                    style="yellow",
                ),
                box=ROUNDED, border_style="yellow", title="[bold yellow]degraded[/]",
                title_align="left", padding=(0, 2), width=width,
            )
        )
    elif counts.entered == 0:
        out.print(
            Panel(
                Text(
                    "No traces entered this funnel in the window. Widen it with --since, or "
                    "check that the service and span names in the definition match what the "
                    "agent actually emits.",
                    style="yellow",
                ),
                box=ROUNDED, border_style="yellow", padding=(0, 2), width=width,
            )
        )
    elif worst:
        idx, pct_lost, lost = worst
        prev_label, cur_label = counts.labels[idx - 1], counts.labels[idx]
        body = Text.assemble(
            ("biggest drop-off  ", "grey62"),
            (f"{prev_label} → {cur_label}", "bold bright_red"),
            ("   ", ""),
            (f"-{lost} traces", "bold bright_red"),
            (f"  ({pct_lost:.1f}% of the {counts.totals[idx - 1]} that reached "
             f"{prev_label})", "grey70"),
        )
        note = Text.assemble(
            ("meaning  ", "grey62"),
            (f"{pct_lost:.0f}% of runs that got as far as '{prev_label}' never "
             f"reached '{cur_label}' in trace order.", "white"),
        )
        out.print(
            Panel(Group(body, note), box=ROUNDED, border_style="red",
                  title="[bold red]cliff[/]", title_align="left", padding=(0, 2),
                  width=width)
        )
    else:
        out.print(
            Panel(
                Text("No drop-off: every run that entered completed the contract in order.",
                     style="bright_green"),
                box=ROUNDED, border_style="green", padding=(0, 2), width=width,
            )
        )


def render_compare(
    left: tuple[Funnel, StepCounts],
    right: tuple[Funnel, StepCounts],
    *,
    window: str,
    out: Console | None = None,
) -> None:
    """Render two funnels side by side for a control-vs-treatment readout.

    Steps are matched by position, which is the only comparison that is meaningful
    for an ordered contract; a warning is printed when the two funnels have
    different shapes.

    Args:
        left: ``(funnel, counts)`` for arm A.
        right: ``(funnel, counts)`` for arm B.
        window: human window label.
        out: console override.
    """
    out = out or console
    (fa, ca), (fb, cb) = left, right

    pa, pb = ca.step_pct(), cb.step_pct()
    cum_a, cum_b = ca.cumulative_pct(), cb.cumulative_pct()
    rows = max(len(ca.labels), len(cb.labels))

    table = Table(box=HEAVY_HEAD, header_style="bold grey62", expand=False)
    table.add_column("#", justify="right", style="grey54", width=2)
    table.add_column("step", style="bold", min_width=9)
    table.add_column(f"{fa.name}  n", justify="right", width=9)
    table.add_column(f"{fa.name}  conv", justify="right", width=11)
    table.add_column(f"{fb.name}  n", justify="right", width=9)
    table.add_column(f"{fb.name}  conv", justify="right", width=11)
    table.add_column("delta", justify="right", width=9)

    for i in range(rows):
        label = ca.labels[i] if i < len(ca.labels) else cb.labels[i]
        na = ca.totals[i] if i < len(ca.totals) else 0
        nb = cb.totals[i] if i < len(cb.totals) else 0
        va = pa[i] if i < len(pa) else 0.0
        vb = pb[i] if i < len(pb) else 0.0
        delta = va - vb
        if abs(delta) < 0.05:
            dstyle, dtext = "grey50", "="
        elif delta > 0:
            dstyle, dtext = "bright_green", f"+{delta:.1f}pp"
        else:
            dstyle, dtext = "bright_red", f"{delta:.1f}pp"
        table.add_row(
            str(i + 1),
            label,
            Text(str(na), style="white"),
            Text(f"{va:.1f}%", style=_conversion_style(va)),
            Text(str(nb), style="white"),
            Text(f"{vb:.1f}%", style=_conversion_style(vb)),
            Text(dtext, style="bold " + dstyle),
        )
    out.print(table)

    e2e_a = cum_a[-1] if cum_a else 0.0
    e2e_b = cum_b[-1] if cum_b else 0.0
    gap = e2e_a - e2e_b
    verdict = (
        f"{fa.name} completes the contract {abs(gap):.1f}pp "
        f"{'more' if gap >= 0 else 'less'} often than {fb.name} "
        f"({e2e_a:.1f}% vs {e2e_b:.1f}% end-to-end, n={ca.entered} vs n={cb.entered})."
    )
    out.print(
        Panel(Text(verdict, style="bold white"), box=ROUNDED,
              border_style="bright_green" if gap >= 0 else "red",
              title="[bold]verdict[/]", title_align="left", padding=(0, 2))
    )


def render_counter_proof(
    funnel: Funnel,
    counts: StepCounts,
    *,
    step_index: int,
    naive_present: int,
    naive_total: int,
    ordering: dict[str, int],
    window: str,
    out: Console | None = None,
) -> None:
    """Contrast a naive span counter against the ordered funnel.

    The point being proven is *structural*, not numeric: a ``GROUP BY span_name``
    counter observes **presence** of a span in a trace and is blind to its
    **position**. It therefore cannot distinguish an agent that validated its tool
    output before answering from one that emitted the same span in the wrong order
    (or in an unrelated code path). The funnel evaluates per-trace sequencing, so
    the gap between the two numbers is exactly the set of traces that contain the
    span but violate the contract.

    Args:
        funnel: the contract under test.
        counts: ordered per-step counts from SigNoz.
        step_index: 0-based index of the step being scrutinised (usually validate).
        naive_present: distinct traces containing that step's span, any position.
        naive_total: distinct traces in the service (naive denominator).
        ordering: output of :meth:`~fot.signoz.SigNozClient.ordering_breakdown`.
        window: human window label.
        out: console override.
    """
    out = out or console
    step = funnel.steps[step_index]
    label = counts.labels[step_index]
    prev_label = counts.labels[step_index - 1] if step_index > 0 else "entry"

    naive_pct = 100.0 * naive_present / naive_total if naive_total else 0.0
    ordered_n = counts.totals[step_index] if step_index < len(counts.totals) else 0
    ordered_pct = counts.cumulative_pct()[step_index] if counts.totals else 0.0

    table = Table(box=HEAVY_HEAD, header_style="bold grey62", expand=False)
    table.add_column("measurement", style="bold", min_width=24)
    table.add_column("what it actually asks", style="grey62", min_width=46)
    table.add_column("n", justify="right", width=9)
    table.add_column("says", justify="right", width=8)

    table.add_row(
        Text("naive span counter", style="bold bright_red"),
        Text(f"GROUP BY name, count(DISTINCT trace_id)\nWHERE name = '{step.span}'"),
        Text(f"{naive_present}/{naive_total}", style="white"),
        Text(f"{naive_pct:.1f}%", style="bold bright_red"),
    )
    table.add_row(
        Text("ordered trace funnel", style="bold bright_green"),
        Text(f"traces reaching step {step_index + 1} in trace order\n"
             f"({' → '.join(counts.labels[: step_index + 1])})"),
        Text(f"{ordered_n}/{counts.entered}", style="white"),
        Text(f"{ordered_pct:.1f}%", style="bold bright_green"),
    )
    width = out.measure(table).maximum

    out.print(
        Panel(
            Text.assemble(
                ("COUNTER-PROOF  ", "bold white"),
                (f"is '{label}' actually happening?", "bold yellow"),
                ("     funnel ", "grey62"), (funnel.name, "magenta"),
                ("   window ", "grey62"), (window, "white"),
            ),
            box=ROUNDED, border_style="yellow", padding=(0, 2), width=width,
        )
    )
    out.print(table)

    gap = naive_pct - ordered_pct
    out_of_order = ordering.get("out_of_order", 0)

    lines = [
        Text.assemble(
            ("the counter says ", "grey70"), (f"{naive_pct:.1f}%", "bold bright_red"),
            (" · the funnel says ", "grey70"), (f"{ordered_pct:.1f}%", "bold bright_green"),
            ("  →  gap ", "grey70"), (f"{gap:.1f}pp", "bold yellow"),
        ),
        Text(""),
        Text.assemble(
            ("why the counter is ", "white"), ("structurally", "bold underline"),
            (" wrong, not merely different", "white"),
        ),
        Text(
            f"A counter observes only that an '{step.span}' span EXISTS somewhere in a trace. "
            f"It has no notion of position, so it scores a run identically whether {label} ran "
            f"after {prev_label} (the contract) or before it (a violation). Aggregating "
            f"per-span and then dividing discards the per-trace join entirely; no amount of "
            f"extra GROUP BY columns recovers it, because ordering is a property of the trace, "
            f"not of the span.",
            style="grey78",
        ),
    ]
    if out_of_order > 0:
        lines += [
            Text(""),
            Text.assemble(
                ("evidence  ", "grey62"),
                (f"{out_of_order} traces", "bold bright_red"),
                (f" contain an '{step.span}' span that fires at or before "
                 f"'{counts.labels[step_index - 1] if step_index else 'entry'}'.", "white"),
            ),
            Text(
                "Those traces are scored as success by the counter and as failure by the "
                "funnel. That set is the gap.",
                style="grey78",
            ),
        ]
    elif abs(gap) < 0.05:
        lines += [
            Text(""),
            Text.assemble(
                ("note  ", "grey62"),
                ("both numbers agree on this dataset because every '", "white"),
                (step.span, "cyan"),
                ("' span here happens to be correctly ordered.", "white"),
            ),
            Text(
                "Agreement is a property of this data, not of the method: the counter cannot "
                "detect the difference, so it will keep reporting the same number once the "
                "ordering breaks. The funnel is what notices.",
                style="grey78",
            ),
        ]
    lines += [
        Text(""),
        Text.assemble(
            ("in one line  ", "grey62"),
            ("a counter measures presence; a funnel measures sequence. "
             "Only one of those is the contract.", "bold white"),
        ),
    ]
    out.print(
        Panel(Group(*lines), box=ROUNDED, border_style="yellow",
              title="[bold yellow]structural, not numeric[/]", title_align="left",
              padding=(1, 2), width=width)
    )
