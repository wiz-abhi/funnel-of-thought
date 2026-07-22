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

## 🔑 OPEN ITEM — all data so far is stub-mode (no API key on this machine)

`GEMINI_API_KEY` is **not set in any scope** (Process/User/Machine — checked 2026-07-22), and neither are `GOOGLE_API_KEY`, `CEREBRAS_API_KEY`, or `OPENAI_API_KEY`. The key that worked during the warm-up appears to have been rotated since.

Consequence: every trace currently in SigNoz under `fot-agent` came from `--stub` mode. **The mechanism is unaffected** — a funnel computes over span names and timestamps and cannot tell a stubbed span from a real one, so every number in this document stands. What's affected is *provenance*: the blog cannot claim "I watched a real LLM agent" on stub data without lying, and the judging guide weights real hands-on experience heavily.

**Action before the blog is written:** set a Gemini key and regenerate one batch —

```bash
export GEMINI_API_KEY=...              # free tier is sufficient
python -m agent.generate --runs 150 --seed 42     # drop --stub
```

~150 runs ≈ 300–600 model calls, comfortably inside the free tier's 1000 RPD at the default `--rpm 12` pacing. Until then, any claim of live-model data must say "simulated".

## Reference fixtures left in place

- `service.name = fot-spike2` — 60 traces with the four node spans plus fragmenting `chat gemini-3.1-flash-lite` / `chat gemini-3.1-flash` spans.
- Funnel `spike2-cognition` — the verified 4-step funnel producing the 100/100/60/60 cliff.
