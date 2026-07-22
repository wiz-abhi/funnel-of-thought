# `fot` — reasoning contracts as code

The core library and CLI. It defines an agent's **reasoning contract** as a SigNoz Trace Funnel, measures it, and renders the answer in a terminal.

A reasoning contract is the sequence of spans a well-behaved agent must emit, **in order, within a single trace**:

```
plan → tool → validate → respond
```

Order is the entire point. A `validate` span that exists but fires *before* the tool result is a broken contract — and no `GROUP BY span_name COUNT` can tell the difference. See [`counter-proof`](#fot-counter-proof-funnel).

## Install

```bash
python -m venv .venv && .venv/Scripts/pip install -e .   # Windows
export SIGNOZ_URL=http://localhost:8080
# auth: any one of these
export SIGNOZ_API_KEY=...                                # or
export SIGNOZ_JWT=...                                    # or
export SIGNOZ_EMAIL=... SIGNOZ_PASSWORD=...              # auto-login, cached
```

Writes (creating funnels, dashboards, alerts) require an **editor/admin** token — a read-only service-account key returns `403 only editors/admins can access this resource`.

## Commands

### `fot apply [defs.yaml]`
Creates or updates funnels from [`funnels/cognition.yaml`](funnels/cognition.yaml). Idempotent — re-running updates steps in place rather than creating duplicates. Every string in the YAML supports `${VAR}` / `${VAR:-default}` interpolation, so one file targets a laptop, CI, and prod without forking.

### `fot show <funnel>`
The main view. Renders each step as a bar with the **absolute trace count `n`** on it, conversion from entry and from the previous step, and the biggest drop-off called out in plain language.

```
│ 1 │ plan       │ █████████████████████████████████████ n=165  │  100.0% │   100.0% │     · │
│ 2 │ tool       │ ███████████████████████████████████ n=156 ░░ │   94.5% │    94.5% │    -9 │
│ 3 │ validate   │ ████████████████████ n=96 ░░░░░░░░░░░░░░░░░░ │   58.2% │  61.5% ◀ │   -60 │
│ 4 │ respond    │ ████████████████████ n=96 ░░░░░░░░░░░░░░░░░░ │   58.2% │   100.0% │     · │

biggest drop-off  tool → validate  -60 traces  (38.5% of the 156 that reached tool)
```

`n` is printed on every bar deliberately: a percentage without a denominator is not evidence.

### `fot counter-proof <funnel>`
Proves a naive span counter cannot measure a contract. Runs both measurements over the same traces:

| measurement | asks | n | says |
|---|---|---|---|
| naive span counter | does an `agent.validate` span exist anywhere in the trace? | 159/165 | **96.4%** |
| ordered trace funnel | did validate happen *after* tool? | 96/165 | **58.2%** |

The 38.2pp gap is real traces where validate fired at or before `tool`. A counter scores them as success; the funnel scores them as failure. Aggregating per-span and then dividing discards the per-trace join entirely — no amount of extra `GROUP BY` columns recovers it, because **ordering is a property of the trace, not of the span**.

### `fot compare <a> <b>`
Side-by-side, step for step. Used for the control arm: an identical contract measured against a control service, so the delta is attributable rather than asserted.

### `fot gauges <funnel>`
Re-emits per-step conversion as OTLP gauges (`fot.funnel.step.conversion`, `fot.funnel.step.n`, labelled by funnel/step/order). This is what makes funnel analytics **dashboardable and alertable** — SigNoz's funnel UI can't do that, because funnel analytics live behind their own REST endpoints rather than in the query builder.

### `fot dashboard apply` / `fot alert apply`
Applies [`dashboards/funnel-conversion.json`](../dashboards/funnel-conversion.json) and [`alerts/validate-dropoff.json`](../alerts/validate-dropoff.json). The alert is a fixed, justified threshold (validate-step conversion < 90%), not a rolling baseline — this build started on July 20, so there is no honest 7-day baseline to compute, and SigNoz's anomaly detection is licence-gated in BasicPlan.

### `fot ls` / `fot rm <name>`
List and delete funnels.

## Things that will bite you

These cost real debugging time and are encoded in [`signoz.py`](signoz.py):

1. **Funnel steps require *strictly* increasing timestamps.** The generated SQL is `minIf(timestamp, …)` per step, gated on `t2 > t1`. Spans that complete within the same clock tick are *not* ordered. A correctly-named, correctly-ordered set of instantaneous spans measured **6% conversion instead of 100%**, then returned HTTP 500. A funnel is blind to steps faster than your clock.
2. **Create/update timestamps are milliseconds; analytics windows are nanoseconds.** Mixing them is the most common failure.
3. **Omit each step's `id`** and the server generates a UUID. Passing `"id": "1"` fails with `invalid UUID length: 1`.
4. **A step matching zero traces returns HTTP 500** (`unsupported value: NaN`) instead of 0% conversion — and the body is **plain text, not JSON**, so calling `.json()` on it throws and masks the real cause. Filed as [SigNoz #12143](https://github.com/SigNoz/signoz/issues/12143). `fot` catches it and renders 0% with a footnote.
5. **Never request `latency_type: p50`** — it silently returns p99. Verified: p50 and p99 both report `18.673343`, while p90 reports `17.963510` and p95 `18.011850`. A median cannot exceed the p90 of the same data.
6. **`/analytics/steps` is the source of per-step counts.** `/analytics/overview` returns end-to-end only, with a hardcoded p99 latency and `errors` as a **max** across steps rather than a sum.
7. **Funnel definitions do not round-trip**: the server echoes `filters.op` back lowercased (`"and"`) though writes require `"AND"`.
8. **Funnels are `minIf` + monotonic**, so they see first occurrence and enforce order — structurally blind to loops and retries. Funnel the linear contract; loops want a graph. This is a mapped boundary, not a bug.
