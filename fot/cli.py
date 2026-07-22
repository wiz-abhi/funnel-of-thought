"""``fot`` -- the Funnel of Thought command line.

Measures an AI agent's reasoning contract (``plan -> tool -> validate -> respond``)
as a SigNoz trace funnel, from definitions kept in version control.

Commands:
    apply          create/update funnels from a YAML definition
    show           render a funnel as a terminal bar chart (the headline view)
    compare        put two funnels side by side (control vs treatment)
    counter-proof  contrast the ordered funnel with a naive span counter
    gauges         re-emit step conversions as OTLP metrics
    ls / rm        list and delete funnels
    dashboard      apply a dashboard-as-code JSON
    alert          apply an alert-rule-as-code JSON
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.table import Table
from rich.text import Text

from .funnels import DEFAULT_SUITE_PATH, Funnel, FunnelSuite, load_suite
from .render import console, render_compare, render_counter_proof, render_funnel
from .signoz import SigNozClient, SigNozError, StepCounts, ns_now, parse_duration

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DASHBOARD = REPO_ROOT / "dashboards" / "funnel-conversion.json"
DEFAULT_ALERT = REPO_ROOT / "alerts" / "validate-dropoff.json"

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
    help="Measure an agent's reasoning contract as a SigNoz trace funnel.",
)
dashboard_app = typer.Typer(no_args_is_help=True, help="Dashboard as code.")
alert_app = typer.Typer(no_args_is_help=True, help="Alert rules as code.")
app.add_typer(dashboard_app, name="dashboard")
app.add_typer(alert_app, name="alert")

SinceOpt = typer.Option("30d", "--since", "-s", help="Lookback window, e.g. 24h, 7d, 30d.")
DefsOpt = typer.Option(None, "--defs", "-f", help="Funnel definition YAML.")


def _fail(message: str) -> None:
    """Print an error and exit non-zero."""
    console.print(Text(f"error: {message}", style="bold red"))
    raise typer.Exit(1)


def _load(defs: Optional[Path]) -> FunnelSuite:
    """Load a funnel suite, exiting cleanly on a bad path or malformed file."""
    try:
        return load_suite(defs or DEFAULT_SUITE_PATH)
    except (FileNotFoundError, ValueError) as exc:
        _fail(str(exc))
        raise  # unreachable; keeps type-checkers happy


def _window(since: str) -> tuple[int, int]:
    """Convert a ``--since`` string into a ``(start_ns, end_ns)`` window."""
    try:
        seconds = parse_duration(since)
    except ValueError as exc:
        _fail(str(exc))
        raise
    end = ns_now()
    return end - seconds * 1_000_000_000, end


def _resolve(client: SigNozClient, funnel: Funnel) -> str:
    """Return the SigNoz id for ``funnel``, applying it first if it does not exist."""
    existing = client.find_funnel(funnel.name)
    if existing:
        return existing.get("funnel_id") or existing.get("id")
    console.print(
        Text(f"funnel '{funnel.name}' not in SigNoz yet; applying it now.", style="yellow")
    )
    funnel_id, _ = funnel.apply(client)
    return funnel_id


def _counts(client: SigNozClient, funnel: Funnel, start_ns: int, end_ns: int) -> StepCounts:
    """Fetch step analytics for ``funnel``, applying the definition if needed."""
    funnel_id = _resolve(client, funnel)
    return client.step_analytics(funnel_id, start_ns, end_ns, funnel.labels)


# --------------------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------------------


@app.command()
def apply(
    defs: Optional[Path] = typer.Argument(None, help="Funnel definition YAML."),
    only: Optional[str] = typer.Option(None, "--only", help="Apply just this funnel."),
) -> None:
    """Create or update funnels in SigNoz from a YAML definition (idempotent)."""
    suite = _load(defs)
    targets = [f for f in suite if only is None or f.name == only]
    if not targets:
        _fail(f"no funnel named {only!r} in {suite.path.name}")

    table = Table(box=None, pad_edge=False)
    table.add_column("funnel", style="bold")
    table.add_column("steps", justify="right", style="grey62")
    table.add_column("action")
    table.add_column("id", style="grey54")

    try:
        with SigNozClient() as client:
            for funnel in targets:
                funnel_id, created = funnel.apply(client)
                table.add_row(
                    funnel.name,
                    str(len(funnel.steps)),
                    Text("created", style="bright_green")
                    if created
                    else Text("updated", style="cyan"),
                    funnel_id,
                )
    except SigNozError as exc:
        _fail(str(exc))

    console.print(table)
    console.print(Text(f"applied {len(targets)} funnel(s) from {suite.path}", style="grey62"))


@app.command()
def show(
    name: str = typer.Argument("cognition", help="Funnel name from the definition file."),
    since: str = SinceOpt,
    defs: Optional[Path] = DefsOpt,
) -> None:
    """Render a funnel as a terminal bar chart, with absolute n on every bar."""
    suite = _load(defs)
    try:
        funnel = suite.get(name)
    except KeyError as exc:
        _fail(str(exc))
        return
    start_ns, end_ns = _window(since)
    try:
        with SigNozClient() as client:
            counts = _counts(client, funnel, start_ns, end_ns)
    except SigNozError as exc:
        _fail(str(exc))
        return
    render_funnel(funnel, counts, window=since, start_ns=start_ns, end_ns=end_ns)


@app.command()
def compare(
    left: str = typer.Argument(..., help="First funnel (treatment)."),
    right: str = typer.Argument(..., help="Second funnel (control)."),
    since: str = SinceOpt,
    defs: Optional[Path] = DefsOpt,
) -> None:
    """Compare two funnels side by side, step for step."""
    suite = _load(defs)
    try:
        fa, fb = suite.get(left), suite.get(right)
    except KeyError as exc:
        _fail(str(exc))
        return
    start_ns, end_ns = _window(since)
    try:
        with SigNozClient() as client:
            ca = _counts(client, fa, start_ns, end_ns)
            cb = _counts(client, fb, start_ns, end_ns)
    except SigNozError as exc:
        _fail(str(exc))
        return
    render_compare((fa, ca), (fb, cb), window=since)


@app.command("counter-proof")
def counter_proof(
    name: str = typer.Argument("cognition", help="Funnel to scrutinise."),
    step: str = typer.Option("validate", "--step", help="Step label to put on trial."),
    since: str = SinceOpt,
    defs: Optional[Path] = DefsOpt,
) -> None:
    """Show why a naive span counter cannot measure a reasoning contract.

    Runs the naive ``GROUP BY span name`` query straight against ClickHouse and
    prints it beside the ordered funnel result. A counter sees only that a span is
    *present*; the funnel sees whether it is in the right *place*.
    """
    suite = _load(defs)
    try:
        funnel = suite.get(name)
    except KeyError as exc:
        _fail(str(exc))
        return
    labels = funnel.labels
    if step not in labels:
        _fail(f"step {step!r} not in funnel {name!r}; have: {', '.join(labels)}")
    idx = labels.index(step)
    if idx == 0:
        _fail("counter-proof needs a step after the first one (ordering is the point)")

    start_ns, end_ns = _window(since)
    try:
        with SigNozClient() as client:
            counts = _counts(client, funnel, start_ns, end_ns)
            service = funnel.steps[idx].service
            span = funnel.steps[idx].span
            naive = client.naive_span_counts(service, [span], start_ns, end_ns)
            total = client.total_traces(service, start_ns, end_ns)
            ordering = client.ordering_breakdown(
                service, funnel.steps[idx - 1].span, span, start_ns, end_ns
            )
    except SigNozError as exc:
        _fail(str(exc))
        return

    render_counter_proof(
        funnel,
        counts,
        step_index=idx,
        naive_present=naive.get(span, 0),
        naive_total=total,
        ordering=ordering,
        window=since,
    )


@app.command()
def gauges(
    name: str = typer.Argument("cognition", help="Funnel to publish, or 'all'."),
    since: str = SinceOpt,
    defs: Optional[Path] = DefsOpt,
    endpoint: Optional[str] = typer.Option(None, "--endpoint", help="OTLP base URL."),
) -> None:
    """Re-emit per-step conversion as OTLP gauges so it can be dashboarded and alerted.

    Funnel analytics are not metrics, so they are invisible to dashboards and alert
    rules. This publishes ``fot.funnel.step.conversion`` / ``.step_conversion`` /
    ``.n`` with ``funnel`` and ``step`` attributes, which is what the shipped
    dashboard and alert rule read.
    """
    from .gauges import DEFAULT_OTLP_ENDPOINT, GaugeEmitter

    suite = _load(defs)
    if name == "all":
        targets = list(suite)
    else:
        try:
            targets = [suite.get(name)]
        except KeyError as exc:
            _fail(str(exc))
            return

    start_ns, end_ns = _window(since)
    emitter = GaugeEmitter(endpoint or DEFAULT_OTLP_ENDPOINT)

    table = Table(box=None, pad_edge=False)
    table.add_column("funnel", style="magenta")
    table.add_column("step", style="bold")
    table.add_column("n", justify="right")
    table.add_column("conversion", justify="right", style="cyan")

    try:
        with SigNozClient() as client:
            for funnel in targets:
                counts = _counts(client, funnel, start_ns, end_ns)
                for label, n, pct in emitter.record(funnel, counts):
                    table.add_row(funnel.name, label, str(n), f"{pct:.1f}%")
        points = emitter.flush()
    except (SigNozError, RuntimeError) as exc:
        _fail(str(exc))
        return
    finally:
        emitter.shutdown()

    console.print(table)
    console.print(
        Text(f"emitted {points} gauge points to {emitter.endpoint}", style="bright_green")
    )


@app.command("ls")
def list_funnels() -> None:
    """List every trace funnel that exists in SigNoz."""
    try:
        with SigNozClient() as client:
            funnels = client.list_funnels()
    except SigNozError as exc:
        _fail(str(exc))
        return
    table = Table(box=None, pad_edge=False)
    table.add_column("name", style="bold")
    table.add_column("steps", justify="right", style="grey62")
    table.add_column("id", style="grey54")
    for funnel in funnels:
        table.add_row(
            funnel.get("funnel_name", "?"),
            str(len(funnel.get("steps") or [])),
            funnel.get("funnel_id") or funnel.get("id", "?"),
        )
    console.print(table)
    console.print(Text(f"{len(funnels)} funnel(s)", style="grey62"))


@app.command("rm")
def remove(name: str = typer.Argument(..., help="Funnel name to delete from SigNoz.")) -> None:
    """Delete a funnel from SigNoz by name."""
    try:
        with SigNozClient() as client:
            existing = client.find_funnel(name)
            if not existing:
                _fail(f"no funnel named {name!r} in SigNoz")
                return
            client.delete_funnel(existing.get("funnel_id") or existing.get("id"))
    except SigNozError as exc:
        _fail(str(exc))
        return
    console.print(Text(f"deleted funnel {name!r}", style="bright_green"))


@dashboard_app.command("apply")
def dashboard_apply(
    path: Optional[Path] = typer.Argument(None, help="Dashboard JSON (v5 widget schema)."),
) -> None:
    """Create the funnel-conversion dashboard from checked-in JSON."""
    path = path or DEFAULT_DASHBOARD
    if not path.exists():
        _fail(f"dashboard file not found: {path}")
    body = json.loads(path.read_text(encoding="utf-8"))
    try:
        with SigNozClient() as client:
            dashboard_id = client.create_dashboard(body)
            base = client.base_url
    except SigNozError as exc:
        _fail(str(exc))
        return
    console.print(Text(f"dashboard created: {body.get('title')}", style="bright_green"))
    console.print(Text(f"  id  {dashboard_id}", style="grey62"))
    console.print(Text(f"  url {base}/dashboard/{dashboard_id}", style="grey62"))


@alert_app.command("apply")
def alert_apply(
    path: Optional[Path] = typer.Argument(None, help="Alert rule JSON."),
) -> None:
    """Create the validate-step drop-off alert from checked-in JSON."""
    path = path or DEFAULT_ALERT
    if not path.exists():
        _fail(f"alert file not found: {path}")
    body = json.loads(path.read_text(encoding="utf-8"))
    try:
        with SigNozClient() as client:
            client.create_rule(body)
    except SigNozError as exc:
        _fail(str(exc))
        return
    console.print(Text(f"alert rule created: {body.get('alert')}", style="bright_green"))


def main() -> None:
    """Console-script entry point."""
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
