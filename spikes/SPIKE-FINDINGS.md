# Day-1 Spike Findings (2026-07-22)

Live spikes against self-hosted SigNoz **v0.132.2 (ee:Y)**. Every number below was produced on this machine — nothing here is assumed. These are the load-bearing facts the build depends on, and several are blog material.

---

## ✅ SPIKE 1 — The cognition funnel produces a real cliff (FR3)

Emitted 60 synthetic traces of `agent.plan → agent.tool → agent.validate → agent.respond`, where `agent.validate` is **structurally absent** (span never created) in ~40% of runs.

```
per-step traces:      [60, 60, 36, 36]
conversion vs step 1: 100% → 100% → 60% → 60%
step-pair conversion: 1→2: 100%   2→3: 60%   3→4: 100%
/analytics/overview:  200  {"conversion_rate": 60, "avg_duration": 17.6, "latency": 18.8}
```

**The cliff lands exactly on the validate step.** This is the hero screenshot mechanism, verified.

---

## 🔴 SPIKE 2 — Funnels require **strictly increasing** timestamps (new, undocumented, blog-worthy)

The first attempt at Spike 1 **failed**, and the reason is subtle enough that it deserves its own section.

I emitted 100 traces with the four spans in the correct order and the correct names. The funnel reported:

```
per-step traces: [100, 6, 0, 0]     ← should have been [100, 100, 63, 63]
/analytics/overview: HTTP 500 "unsupported value: NaN"
```

**Root cause.** The generated SQL computes `minIf(timestamp, ...)` per step and gates conversion on `t2_time > t1_time` — a **strict** inequality. My spans were instantaneous no-ops, so every span in a trace landed on the *identical* timestamp:

```
07a0ca6e…  agent.plan       2026-07-21 19:18:45.436580500   duration_nano=0
07a0ca6e…  agent.respond    2026-07-21 19:18:45.436580500   duration_nano=0
07a0ca6e…  agent.tool       2026-07-21 19:18:45.436580500   duration_nano=0
07a0ca6e…  agent.validate   2026-07-21 19:18:45.436580500   duration_nano=0
```

Equal timestamps fail `>`, so ~94% of traces were judged non-converting. Adding ~4 ms of work per span fixed it completely (Spike 1's numbers).

**Consequence, and it generalizes beyond this project:** *a Trace Funnel is blind to steps that complete within the same clock tick.* Any sufficiently fast pipeline — or any mocked/stubbed test run — will read near-0% conversion and then crash the analytics API with a 500. Real LLM calls (1–2 s) are never at risk; **stub and unit-test modes are**, which is exactly where a developer would first try the feature and conclude it is broken.

→ Build consequence: stub mode must simulate non-trivial per-node duration. The sleeps are **load-bearing, not padding**.

---

## ✅ SPIKE 3 — The model-swap crash is real and trivially reproducible (FR5)

The teaching payload, verified in one run. OTel GenAI semconv names LLM spans `{operation} {model}`; funnels match span names by **exact equality**. So a routine model version bump silently orphans the step:

```
BEFORE swap — funnel keyed on "chat gemini-3.1-flash-lite"
  per-step traces: [60, 35, 35]
  /analytics/overview: 200  conversion_rate = 58.33

AFTER swap  — same funnel, model bumped to "chat gemini-3.2-flash-lite"
  per-step traces: [60, 0, 0]          ← step 2 matches nothing
  /analytics/overview:        500  "unsupported value: NaN"
  /analytics/steps/overview:  500  "unsupported value: NaN"
```

**Bump your model version → your funnel silently reads 0% → the analytics API returns HTTP 500.** No error, no warning, no migration path. This affects every OpenLLMetry / OpenLIT / OpenInference user who A/B tests, falls back between providers, or upgrades a model.

Filed as SigNoz issue [#12143](https://github.com/SigNoz/signoz/issues/12143); community PR [#12160](https://github.com/SigNoz/signoz/pull/12160) implements the guard.

---

## ⚠️ SPIKE 4 — The counter-vs-funnel proof (FR4) needs **out-of-order** validate, not skipped validate

The PRD claims a naive `GROUP BY span_name COUNT` reports validate ≈ 100% while the ordered funnel reports ≈ 61%. **That is only true in one specific scenario**, and the spike disproved the naive version:

- If validate is simply **skipped** in 40% of traces, a naive count also reports **60%** — identical to the funnel. The comparison proves nothing.
- The discrepancy appears only when the validate span **exists but fires out of order** (agent "validates" before the tool result is available — reasoning-action mismatch). Then the counter sees the span in 100% of traces while the funnel sees correct sequencing in only ~61%.

→ Build consequence: the agent needs three run modes — healthy, skipped, and **out-of-order** — with the demo config tuned so ~100% of traces *contain* a validate span but only ~61% have it *correctly positioned*. That gap is the proof that a counter is **structurally** wrong (it cannot see order), not merely different.

### ✅ CONFIRMED on real agent data (2026-07-22)

Once the agent's out-of-order mode was implemented, the discrepancy reproduced exactly. Measured over **165 real traces** from `service.name = fot-agent`:

```sql
WITH traces AS (
  SELECT trace_id,
         minIf(timestamp, name='agent.tool')     AS t_tool,
         minIf(timestamp, name='agent.validate') AS t_val,
         countIf(name='agent.validate')          AS has_val
  FROM signoz_traces.distributed_signoz_index_v3
  WHERE resource_string_service$$name='fot-agent'
  GROUP BY trace_id HAVING countIf(name='agent.plan') > 0
)
SELECT count()                                AS total_traces,          -- 165
       countIf(has_val > 0)                   AS naive_has_validate,    -- 159  (96.4%)
       countIf(has_val > 0 AND t_val > t_tool) AS correctly_ordered     -- 102  (61.8%)
FROM traces
```

| Question | Answer |
|---|---|
| "Does the agent validate?" (naive counter) | **96.4%** — the span is there |
| "Does the agent validate *after seeing the tool result*?" (ordered funnel) | **61.8%** |

**A 34.6-percentage-point gap.** Those are traces where the validate span exists — so every counter, every `GROUP BY`, every "% of runs that validated" dashboard reports success — but the agent validated *before* the tool result was available. It is reasoning-action mismatch, and it is invisible to anything that counts rather than sequences. This is the money screenshot.

---

## Verified API notes (beyond the earlier capability map)

- Create/update timestamps are **milliseconds** (validated to 1e12–1e13); analytics windows are **nanoseconds**. Mixing them is the most common failure.
- Omit each step's `id` → the server generates a UUID. Passing `"id": "1"` → `invalid UUID length: 1`.
- `/analytics/steps` is the source of per-step trace counts (`total_sN_spans`). `/analytics/overview` gives end-to-end only, with a hardcoded p99 latency and `errors` as a **max** across steps, not a sum.
- `/analytics/steps/overview` requires `step_start` + `step_end` (1-based).
- Admin/editor JWT required for writes; a read-only service-account key gets `403 "only editors/admins can access this resource"` on create.
- **Two distinct causes** of the NaN-500 that tooling must distinguish: (a) the step matches no spans at all, and (b) the step matches spans but never satisfies strict ordering — including the same-clock-tick case above.

## ✅ HEADLINE NUMBERS — measured on real Gemini-backed traces (2026-07-22)

The figures above were established on stubbed traces. They were then reproduced on **124 live runs** against `gemini-3.1-flash-lite` (window 04:51–05:58 UTC; all stub data predates it by ~10 hours, so the window is uncontaminated). The live numbers are cleaner than the stubbed ones:

```
FUNNEL OF THOUGHT · cognition        service fot-agent   window 2h
  1  plan       n=125   100.0%
  2  tool       n=125   100.0%
  3  validate   n=80     64.0%  ◀  -45 traces
  4  respond    n=80     64.0%

COUNTER-PROOF
  naive span counter    125/125   100.0%   "an agent.validate span exists"
  ordered trace funnel   80/125    64.0%   "validate happened after tool"
  gap                              36.0pp  45 traces
```

**The counter says 100%. The funnel says 64%.** Every run emitted a validate span, so every presence-based metric — every `GROUP BY`, every "% of runs that validated" dashboard — reports perfect compliance. 45 of those runs validated *before* the tool result existed. Use these as the published figures.

## 🔴 UNPLANNED INCIDENT — the generator hung silently for 34 minutes

Worth writing up, because it is the project's own thesis happening to the project by accident.

The first live batch stopped dead after 64 of 120 runs. Traces had been landing at a steady 4/min, then nothing for 34 minutes. The process stayed alive. No error, no exception, no partial span — from the outside it looked like slow work. Suspicion was rate limiting; a direct probe of the Gemini API during the stall returned **HTTP 200 in 1.4 s**, so the API was entirely healthy.

Root cause: `ChatGoogleGenerativeAI` was constructed with no `timeout` and no `max_retries`, so `invoke()` could block indefinitely on a stalled connection. Fixed with a 45 s timeout and `max_retries=2`; the re-run completed 60/60 in 889 s with no stalls.

This is the canonical agent failure mode — **an unbounded wait that emits nothing and looks healthy** — and it arrived unplanned, on real infrastructure, in the tooling built to expose exactly that. No process check would have caught it; the funnel would have shown entries flatlining.

## 🔑 RESOLVED — API key (all earlier data was stub-mode)

Earlier in the build, no LLM key existed on the machine, so every `fot-agent` trace came from `--stub` mode. A key was supplied on 2026-07-22 and 124 live runs were generated, which is where the headline numbers above come from.

Two notes that still matter:

- **`fot-agent` now contains both stub and live traces.** Stub runs are all at 2026-07-21 19:00 UTC; live runs are 2026-07-22 04:51–05:58 UTC. Any published figure must be scoped to the live window (`fot show cognition --since 2h` at the time of writing) or regenerated into a clean service name. Do not quote a 30-day window — it silently mixes the two.
- **The mechanism never depended on this.** A funnel computes over span names and timestamps and cannot distinguish a stubbed span from a live one, so the stubbed findings held exactly. What live data buys is *provenance*, which the blog guide weights heavily — not correctness.

## Reference fixtures left in place

- `service.name = fot-spike2` — 60 traces with the four node spans plus fragmenting `chat gemini-3.1-flash-lite` / `chat gemini-3.1-flash` spans.
- Funnel `spike2-cognition` — the verified 4-step funnel producing the 100/100/60/60 cliff.
