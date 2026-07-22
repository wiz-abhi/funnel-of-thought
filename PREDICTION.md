# PREDICTION.md

**Written 2026-07-22. Committed before any funnel analytics response was read.**

This file exists so that the numbers in the blog post are *attributable* rather
than *asserted*. Anyone can run an experiment, look at the result, and then
describe the result as though they expected it. The only defence against that
is to write the expectation down first, in public, with enough precision that
it can be shown to be wrong.

Its companion is the control funnel: an identical funnel definition run against
a batch generated with the validation regression *disabled*. A drop-off that
appears in the treatment and not in the control is caused by the thing we
changed. A drop-off that appears in both is caused by something else, and we
would have to say so.

Git is the timestamp. `git log --diff-filter=A -- PREDICTION.md` shows when this
file entered the repository; the first funnel analytics capture is a later
commit. If those two commits are ever out of order, this document is worthless
and should be treated as such.

---

## What we are about to measure

A 4-node LangGraph agent — `plan → tool → validate → respond` — under stock
OpenTelemetry instrumentation, run as an offline batch. Two funnels over the
same traces:

- **The working funnel**, keyed on the stable node span names that LangGraph's
  instrumentation emits regardless of which model is in use.
- **The counter-proof funnel**, keyed on the LLM child span name, which the
  OpenTelemetry GenAI semantic conventions render as `{operation} {model}` —
  e.g. `chat gemini-3.1-flash-lite`.

And one naive baseline: a Query Builder `GROUP BY span name → COUNT`, which is
what a developer reaches for before they know funnels exist.

---

## Prediction 1 — the validate step is where the cliff is

**We predict per-step conversion of roughly `100% → 95±5% → 60±10% → ~98%`,**
with the large loss falling on the `plan → tool → validate` transition and the
`validate → respond` transition staying high.

Why we expect that shape:

1. The funnel is `minIf` over a monotonically increasing step index. It counts
   a trace as having reached step *k* only if step *k* occurred **after** step
   *k−1*. So it penalises two different things at once: steps that never ran,
   and steps that ran out of order. `validate` is the step most exposed to
   both, because it is the one the agent has a local incentive to skip — if the
   tool call returned something that looks well-formed, the cheapest path to an
   answer is straight to `respond`.
2. `plan → tool` should lose only the traces where planning concluded no tool
   was needed. That is a small, deliberate fraction.
3. `validate → respond` should be near-total: once validation has run, the
   agent has no reason not to answer.

**How we would know we were wrong.** If `validate` conversion comes back above
90%, the agent honours its contract too reliably to be interesting, and the
premise "agents silently skip their own validation step" is not demonstrated by
this agent. We would have to either say the contract held, or redesign the
agent to fail more — and redesigning it to fail more is exactly the authored-
fault move we criticise in other submissions. If that happens we will report
the high number and drop the cliff framing rather than tune the agent until it
misbehaves.

If instead conversion comes back near 0%, we predict the cause is mechanical,
not behavioural: a span-name mismatch between the funnel definition and what
the instrumentor actually emitted. That is a bug in our funnel, not a finding
about the agent, and it must be ruled out before any number is published.

---

## Prediction 2 — the counter and the funnel disagree, structurally

**We predict the naive `GROUP BY span name → COUNT` reports `validate` at or
near 100% of traces while the ordered funnel reports it in the low 60s.**

The prediction that actually matters is not the size of the gap but its
*direction and its cause*. The counter must be **higher**, and it must be
higher for a reason that no amount of care with a counter can fix: a counter
answers "did this span ever appear?" while a funnel answers "did this span
appear *after* the previous one, in the same trace?" A counter cannot see
order. If the agent validates *after* responding — which is a real failure mode
and arguably a worse one than not validating at all — the counter scores it as
a success.

**How we would know we were wrong.** Three ways:

- The counter and the funnel agree within a couple of points. Then order is not
  actually being violated in this dataset, the discrepancy is not demonstrated,
  and the "a counter is structurally wrong" claim reduces to "a counter is
  theoretically wrong but empirically fine here." We would have to say that.
- The counter reads *lower* than the funnel. That would mean we have
  misunderstood the semantics of one of the two queries, and the finding is
  void until reconciled.
- The gap exists but is fully explained by traces that are simply missing the
  span rather than having it out of order. Then the honest description is "a
  counter over-counts incomplete traces," which is a weaker and much less
  interesting claim than the one we are making. We commit to reporting the
  split between *missing* and *misordered* rather than lumping them together.

---

## Prediction 3 — the model swap produces 0%, then HTTP 500

**We predict that a funnel keyed on the `chat {model}` LLM span will read 0%
conversion for traces generated with the other model, and that requesting
`/analytics/steps` when a step matches zero traces returns
`HTTP 500: unsupported value: NaN` rather than a conversion of 0.**

Why: Trace Funnels match a step by *exact* span name equality
(`resource_string_service$$name = step.service AND name = step.span_name`),
ANDed with an optional per-step SQL clause. There is no wildcard and no OR. The
GenAI convention puts the model name *inside* the span name, so one logical
step fragments into N literal names the moment you A/B, version-bump or fall
back a model. Downstream, `BuildFunnelStepOverviewQuery` computes
`round(total_sEnd * 100.0 / total_sStart, 2)` with no zero-guard — while
`BuildFunnelOverviewQuery`, one function away in the same file, guards the same
division with `if(total_s1_spans > 0, …)`. Divide by zero, get `NaN`, fail to
serialise, 500.

This is the prediction we are most confident in, because it is read off the
source rather than inferred from behaviour, and because the 500 has already
been reproduced once and filed as SigNoz issue **#12143**. Community PR
**#12160** implements the fix.

**How we would know we were wrong.** If the endpoint returns a clean `0%`, the
guard has landed in the version under test and the crash beat is gone — which
is *fine*, and we would rewrite that section as "here is a bug that got fixed
between filing and writing," which is a perfectly good story and a better
outcome for SigNoz users. This is exactly why `casting.yaml` pins
`signoz/signoz:v0.132.2`: so the blog and the repo agree about which version is
being described, and a reader can check both behaviours deliberately.

The genuinely falsifying result would be the funnel reading a *non-zero,
non-trivial* conversion on the fragmented span name. That would mean matching
is not exact-equality after all, and the entire semconv-fragmentation thesis
collapses.

---

## Prediction 4 — the loop boundary holds

**We predict the funnel cannot distinguish an agent that validated once from an
agent that validated five times after four failures,** because `minIf` takes
the first qualifying occurrence and monotonicity discards the rest.

We are not testing this to discover it — it follows from the aggregation. We
state it because it is the honest ceiling on the whole approach, and we would
rather name it than have a judge find it. The claim this project makes is
scoped to *linear* contracts. Loops want a graph, and that is a different
instrument.

**How we would know we were wrong.** If a retry-heavy batch moves the funnel's
per-step numbers in a way that tracks retry count, then funnels see more than
we think and the boundary should be redrawn — in SigNoz's favour.

---

## Prediction 5 — p50 is a lie

**We predict that requesting `latency_type: "p50"` from the funnel analytics
endpoint returns the p99 value, silently and without error** — the
`latencyQuantile` switch implements only `p90` and `p95` and falls through to a
`0.99` default for everything else.

**How we would know we were wrong.** If the p50 figure differs from the p99
figure on the same window, the switch has a case we did not find in source, and
we withdraw the claim. We will verify by requesting p50 and p99 over an
identical window and diffing them, not by reading the code twice.

---

## On being wrong

Three of the five predictions above are cheap to be right about, because they
were read off SigNoz's source rather than guessed. Predictions 1 and 2 are the
ones with real risk, and they are the two the headline depends on.

If prediction 1 or 2 fails, that is the more interesting blog post, and we will
write that one instead. "I pre-registered a hypothesis about my agent's
reasoning contract and the data refused it" is a better artefact than a
confirmed guess, because it is evidence the measurement was capable of
returning an answer we did not want. A funnel that only ever agrees with its
author is not an instrument.

What we will *not* do: quietly widen a tolerance, tune the agent until the
cliff appears, or re-run the batch with a different seed until the numbers
cooperate. The seed is fixed (`FOT_SEED=1337`), the batch size is fixed, and
the first analytics response we read is the one that gets reported.
