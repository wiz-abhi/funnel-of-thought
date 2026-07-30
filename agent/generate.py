"""Batch trace generator for the Funnel of Thought demo.

Runs N independent agent traces against a local SigNoz collector.

    # offline smoke test, no LLM calls, compressed latencies
    python -m agent.generate --runs 10 --stub --fast --console

    # the demo dataset: every trace HAS a validate span, only 64% are ordered
    # correctly -- a naive span counter says 100%, the funnel says 64%
    python -m agent.generate --runs 125 --validate-rate 0.64 --stub

    # the teaching demo: same steps, different model => LLM span name changes
    python -m agent.generate --runs 40 --stub --model gemini-3.1-flash
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time

from .graph import RunMode, build_graph, run_once
from .llm import DEFAULT_MODEL, LLMClient, LLMError, resolve_model
from .otel_setup import init_telemetry, shutdown_telemetry
from .timing import FAST_SCALE, Timing
from .tools import QUESTIONS


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agent.generate",
        description="Generate plan->tool->validate->respond traces for SigNoz funnels.",
    )
    p.add_argument("--runs", type=int, default=150, help="number of traces (default: 150)")
    p.add_argument(
        "--model",
        default=None,
        help=f"Gemini model id (default: $FOT_MODEL or {DEFAULT_MODEL}). "
        "Changing this changes the LLM span name -- that is the point.",
    )
    p.add_argument(
        "--validate-rate",
        type=float,
        default=None,
        help="fraction of runs where validate runs in the CORRECT position, "
        "after tool (default: $FOT_VALIDATE_RATE or 0.64). This is the injected "
        "ground truth the funnel is supposed to recover.",
    )
    p.add_argument(
        "--out-of-order-rate",
        type=float,
        default=None,
        help="fraction of runs where the validate span is emitted BEFORE tool -- "
        "present in the trace but violating the contract. Default "
        "($FOT_OUT_OF_ORDER_RATE, else fill the remainder) makes 100%% of traces "
        "contain a validate span while only --validate-rate of them are ordered "
        "correctly. Any remainder after both rates skips validate entirely.",
    )
    p.add_argument("--stub", action="store_true", help="no LLM calls; identical span shape")
    p.add_argument(
        "--fast",
        action="store_true",
        help=f"compress simulated node latency by {FAST_SCALE:g}x for quick runs. "
        "Never reaches zero -- spans must keep distinct, ordered timestamps.",
    )
    p.add_argument("--seed", type=int, default=42, help="RNG seed for reproducible runs")
    p.add_argument(
        "--rpm",
        type=float,
        default=12.0,
        help="requests-per-minute pacing for live runs (default: 12, under the "
        "15 RPM Gemini free-tier limit). Ignored in --stub mode.",
    )
    p.add_argument("--console", action="store_true", help="also print spans to stdout")
    p.add_argument("--no-otlp", action="store_true", help="do not export to the collector")
    p.add_argument(
        "--auto-instrument",
        action="store_true",
        help="additionally enable OpenInference LangChain auto-instrumentation "
        "(adds bare node-name spans: plan, tool, validate, respond)",
    )
    p.add_argument("--question", default=None, help="pin every run to one question")
    return p


def resolve_rates(
    validate_rate: float | None, out_of_order_rate: float | None
) -> tuple[float, float]:
    """Resolve the healthy / out-of-order split, falling back to env then default.

    ``out_of_order_rate`` defaults to "whatever is left", which yields the demo
    configuration: every trace contains a validate span, but only
    ``validate_rate`` of them have it in the correct position.
    """
    if validate_rate is None:
        # 0.64 so a default run reproduces the exact number the README, blog and
        # demo video quote: round(125 * 0.64) == 80 of 125 traces == 64.0%.
        validate_rate = float(os.environ.get("FOT_VALIDATE_RATE", "0.64"))
    if out_of_order_rate is None:
        env_ooo = os.environ.get("FOT_OUT_OF_ORDER_RATE")
        out_of_order_rate = float(env_ooo) if env_ooo is not None else 1.0 - validate_rate

    for label, value in (
        ("--validate-rate", validate_rate),
        ("--out-of-order-rate", out_of_order_rate),
    ):
        if not 0.0 <= value <= 1.0:
            raise SystemExit(f"{label} must be between 0 and 1, got {value}")
    if validate_rate + out_of_order_rate > 1.0 + 1e-9:
        raise SystemExit(
            f"--validate-rate ({validate_rate}) + --out-of-order-rate "
            f"({out_of_order_rate}) cannot exceed 1.0"
        )
    return validate_rate, out_of_order_rate


def plan_schedule(
    runs: int, validate_rate: float, out_of_order_rate: float, rng: random.Random
) -> list[RunMode]:
    """Decide up front how each run behaves.

    We allocate exact counts and shuffle, rather than flipping an independent
    coin per run. That makes the funnel's conversion rate land on the intended
    number instead of wobbling with sample noise -- important when the whole
    story is "validate converts at 64%".
    """
    n_healthy = round(runs * validate_rate)
    n_ooo = round(runs * out_of_order_rate)
    n_skipped = max(0, runs - n_healthy - n_ooo)
    # Rounding can overshoot by one; trim the out-of-order bucket to compensate.
    overflow = (n_healthy + n_ooo + n_skipped) - runs
    if overflow > 0:
        n_ooo = max(0, n_ooo - overflow)

    schedule = (
        [RunMode.HEALTHY] * n_healthy
        + [RunMode.OUT_OF_ORDER] * n_ooo
        + [RunMode.SKIPPED] * n_skipped
    )
    rng.shuffle(schedule)
    return schedule


#: Single-character marker printed per run, for scanning the progress log.
_MARKER = {RunMode.HEALTHY: "V", RunMode.OUT_OF_ORDER: "!", RunMode.SKIPPED: "-"}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.question and args.question not in QUESTIONS:
        print(
            f"warning: {args.question!r} is not in the mock corpus; "
            "the search tool will return a miss.",
            file=sys.stderr,
        )

    validate_rate, ooo_rate = resolve_rates(args.validate_rate, args.out_of_order_rate)
    model = resolve_model(args.model)
    rng = random.Random(args.seed)
    schedule = plan_schedule(args.runs, validate_rate, ooo_rate, rng)
    timing = Timing(scale=FAST_SCALE if args.fast else 1.0)

    init_telemetry(
        console=args.console,
        otlp=not args.no_otlp,
        auto_instrument=args.auto_instrument,
    )

    try:
        llm = LLMClient(model, stub=args.stub, rng=rng)
    except LLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        shutdown_telemetry()
        return 2

    app = build_graph()

    # Pace live runs so we stay under the free-tier RPM ceiling. Each run makes
    # up to 3 LLM calls, so budget the interval per call, not per run.
    calls_per_run = 3 if any(m is not RunMode.SKIPPED for m in schedule) else 2
    interval = 0.0 if args.stub else (60.0 / args.rpm) * calls_per_run

    mode_label = "stub" if args.stub else f"live:{model}"
    n_healthy = sum(1 for m in schedule if m is RunMode.HEALTHY)
    n_ooo = sum(1 for m in schedule if m is RunMode.OUT_OF_ORDER)
    n_skipped = sum(1 for m in schedule if m is RunMode.SKIPPED)

    print(
        f"Funnel of Thought :: {args.runs} runs | mode={mode_label} | "
        f"validate-rate={validate_rate:.2f} | out-of-order-rate={ooo_rate:.2f} | "
        f"seed={args.seed}"
        + (" | fast" if args.fast else "")
        + ("" if args.stub else f" | rpm={args.rpm:g} (~{interval:.1f}s/run)")
    )

    trace_ids: list[str] = []
    validated_ok = 0
    failures = 0
    started = time.monotonic()

    for i, mode in enumerate(schedule, start=1):
        run_started = time.monotonic()
        try:
            result = run_once(
                app,
                llm=llm,
                rng=rng,
                mode=mode,
                timing=timing,
                question=args.question,
                run_index=i,
            )
        except Exception as exc:  # keep the batch alive; one bad run is not fatal
            failures += 1
            print(f"  [{i}/{args.runs}] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue

        trace_ids.append(result["trace_id"])
        if result["validated"]:
            validated_ok += 1
        print(
            f"  [{i}/{args.runs}] {_MARKER[mode]} trace={result['trace_id']} "
            f"{mode.value:<12} q={result['question'][:38]!r}"
        )

        if interval:
            remaining = interval - (time.monotonic() - run_started)
            if remaining > 0 and i < args.runs:
                time.sleep(remaining)

    shutdown_telemetry()

    elapsed = time.monotonic() - started
    completed = len(trace_ids)
    has_validate_span = n_healthy + n_ooo
    print("\n--- summary ---")
    print(
        f"traces emitted           : {completed}/{args.runs}"
        + (f" ({failures} failed)" if failures else "")
    )
    print(
        f"validate span present    : {has_validate_span} "
        f"({has_validate_span / args.runs:.1%})   <- what a naive span counter sees"
    )
    print(
        f"  ordered correctly (V)  : {n_healthy} ({n_healthy / args.runs:.1%})"
        "   <- what the funnel sees"
    )
    print(f"  out of order (!)       : {n_ooo} ({n_ooo / args.runs:.1%})")
    print(f"validate span absent (-) : {n_skipped} ({n_skipped / args.runs:.1%})")
    print(f"validation passed        : {validated_ok}")
    print(f"model                    : {model}{' (stub)' if args.stub else ''}")
    print(f"elapsed                  : {elapsed:.1f}s")
    if trace_ids:
        print(f"sample trace_id          : {trace_ids[0]}")
    return 1 if failures and not completed else 0


if __name__ == "__main__":
    raise SystemExit(main())
