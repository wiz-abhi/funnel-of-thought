# Blog outline — Funnel of Thought

**Target:** 1,000–1,500 words. Budget below sums to **exactly 1,500**; write to it,
then cut 10% in the edit pass. Platform: Dev.to (hackathon rules exclude LinkedIn).

**The hero is the live, self-observing agent and the speed of the loop.**
Not the bug. The `500` is a ~20-second mid-beat about why this was hard to
build — it is never the climax, never the headline, never the last thing a
reader remembers. If a reader finishes and their takeaway is "SigNoz has a bug,"
the post failed. The takeaway must be: *"I can measure whether my agent honours
its own reasoning contract, and the measurement is fast enough to watch move."*

---

## Title options

1. **"I gave my AI agent a funnel over its own reasoning. It told me exactly where it stops thinking."**
   *Leads with the capability and the payoff. Concrete, first-person, no jargon.
   Recommended — it is the only one of the three that promises a new thing you
   can do rather than a thing that is broken.*

2. **"Funnel of Thought: measuring whether your agent actually finishes its own reasoning"**
   *The straight, product-shaped title. Safest and most searchable; slightly
   flatter as a hook.*

3. **"Your agent has a reasoning contract. Here is how to measure it — in about a second."**
   *Leads on speed, which is the axis nothing LLM-in-the-loop can match. Use if
   the demo video turns out to be the strongest asset.*

**Rejected:** anything of the shape *"I pointed SigNoz's funnel at an agent and
it crashed."* It is the most clickable sentence available and it is the wrong
promise — it makes the post a bug report about the hosts' product, buries the
capability, and reads as a negative result. The crash earns two sentences in
beat 4, no more.

---

## Narrative arc — 5 beats, single thread

### Beat 1 — The contract nobody measures · **200 words**

Open on the gap, not the tooling. Every agent has an implicit contract:
`plan → tool → validate → respond`. We trust agents to honour it and we
essentially never check. Traces show you *one* run; evals score the *output*.
Neither answers the population question: **of the runs that started reasoning,
what fraction actually completed each step, in order?**

Name the thing that makes this answerable: SigNoz ships a **funnel primitive**,
and no other observability backend does. Langfuse, LangSmith, Phoenix,
Braintrust all show traces; none has a funnel. Quote SigNoz's own AI page on
tracking drop-off across AI agent pipelines and credit it as the framing that
pointed the way — they got there first, this post is the working adapter.

Close the beat with the concrete promise the rest of the post pays off: a live
funnel over an agent's cognition, and the agent reading it over itself.

> **Guard:** no bug foreshadowing here. Not even a wink.

---

### Beat 2 — Building the funnel over cognition · **320 words**

The build, with real config and real numbers. This is the "replicable" beat the
blog guide weights hardest.

- A 4-node LangGraph agent under stock OpenTelemetry. Say plainly that the
  agent is authored — a clean contract needs a discrete `validate` node — and
  that **no span name is authored**. Service `fot-agent`, root span
  `fot.agent.run`, node spans `agent.plan` / `agent.tool` / `agent.validate` /
  `agent.respond`, LLM children named by the OTel GenAI convention:
  `chat gemini-3.1-flash-lite`.
- The funnel definition, as code. The one non-obvious rule: **steps match on
  exact span name** (`service AND name`, ANDed with an optional per-step SQL
  clause). No wildcards. So key the funnel on names that are *identities* —
  the node spans — not names that are *descriptions*.
- **The result, with n on every bar:** `125 → 125 → 80 → 80`.
  **100% → 100% → 64.0%.** Live Gemini window, 2026-07-22 04:51-05:58 UTC.

Then interpret it, because the number is the point: **a third of the runs never
validated before answering, in order.** The agent has a validation step; it
skips it or runs it late in most runs. That is invisible to a trace view and
invisible to an eval that only scores final answers.

**Screenshot:** the funnel in SigNoz's own UI with counts on every bar.

> **Guard:** resist explaining the funnel's SQL here. Beat 4 is where mechanism
> lives, and only as much as the story needs.

---

### Beat 3 — Why the obvious query cannot answer this · **190 words**

The counter-proof, and the most transferable idea in the post.

Before funnels, the natural move is Query Builder: `GROUP BY span name → COUNT`.
Run it on the same traces and `agent.validate` reports ~100%. The funnel says
64.0%. Both queries are correct; they answer different questions.

- A counter asks **"did this span ever appear?"**
- A funnel asks **"did it appear *after* the previous step, in the same trace?"**

State the consequence sharply: an agent that validates *after* responding — a
real and arguably worse failure than not validating — scores as a **success**
in the counter and a **failure** in the funnel. A counter is not a coarser
answer, it is the wrong question. Order is the whole content of the metric.

**Screenshot:** counter beside funnel, same window, same traces. This is the
money image.

---

### Beat 4 — Three potholes on the way · **250 words**

The "why this was hard" beat. Brisk, technical, generous in tone, and **over
quickly**. All three items get an issue link and a one-line diagnosis. Total
reading time ~20 seconds each.

1. **Funnels need strictly increasing timestamps** (`t2 > t1`). Steps that
   complete inside the same clock tick collapse conversion to ~0. This is why
   the agent's per-node work is load-bearing — a no-op node is invisible to a
   funnel. *Genuinely useful, undocumented, and the most practically valuable
   thing in this section for anyone building one.*

2. **A step matching zero traces returns `HTTP 500: unsupported value: NaN`,
   not 0%.** `BuildFunnelStepOverviewQuery` divides without a zero-guard, while
   `BuildFunnelOverviewQuery` — one function away — guards the identical
   division. Reached by bumping the model `gemini-3.1 → 3.2`: the GenAI
   convention puts the model *inside* the span name, so one logical step
   fragments into N literal names and the funnel matches nothing. Two specs,
   both correct, silently incompatible. Filed as **#12143** (open).

3. **`latency_type: "p50"` returns p99.** Measured: p50 = `18.673343`,
   byte-identical to p99, while p90 = `17.96` and p95 = `18.01`. A median
   reporting higher than the p90 and p95 of the same distribution.

**Tone guard:** "here is the locked door I opened to lean harder on your
funnel," never "you missed this." Credit generously; these are the findings of
someone who used the feature hard, and say so.

---

### Beat 5 — The loop closes: the agent reads its own funnel · **350 words**

**The finale. The longest beat. The reason the post exists.**

Land the gap first, in one line: **0 of SigNoz's 41 shipped MCP tools touch
funnels** (live-enumerated). An agent could reach traces, metrics, logs,
dashboards and alerts — everything except the one primitive that measures its
own completion rate.

So: `signoz-funnel-mcp`, four tools — `create_funnel`,
`get_funnel_analytics`, `get_funnel_slow_traces`, `list_funnels`. Show the MCP
client config block. It is about ten lines.

Then the demo, which is the whole payoff:

- The agent emits spans → SigNoz computes the funnel → **the agent calls
  `get_funnel_analytics` and reads its own conversion rate.** A closed loop
  that was not previously expressible.
- **Then make it move.** Inject a validation regression, re-run, watch the
  `validate` bar sink and the drop-off alert fire — *while you are still
  looking at the screen.*

Make the speed argument explicitly, because it is the one axis nothing
LLM-in-the-loop can match: **the read path has no model in it.** It is REST
over spans that already landed, plus arithmetic. Build-and-re-read is
sub-second, repeatable, live. An AI-SRE agent that reasons about telemetry
takes ~40 seconds and costs money per investigation; this takes ~1 second and
costs nothing, because it is an *instrument*, not an *investigator*. That is
also why it can run continuously as a dashboard panel and an alert rather than
on-demand as a query.

Close with the dashboard-as-code panel and the firing alert: the funnel made
durable and operational.

**Media:** the terminal capture of the agent calling `get_funnel_analytics`,
then the ≤3-min video leading with the live cliff deepening.

---

### Landing — the boundary, the takeaway, the disclosure · **190 words**

**The boundary (~90 words).** Funnels aggregate with `minIf` over a monotonic
step index: first occurrence wins, order is enforced. So they are structurally
blind to loops and retries — validated once and validated five times after four
failures are the same funnel. Name Phoenix's agent-path graph and Datadog's
execution-loop view as the loop-aware complements. Then state the thesis this
scopes to: **funnel the linear contract; loops want a graph.** Owning the
ceiling is the contribution, not a caveat.

**The takeaway (~60 words).** One adoptable sentence: *watch validate-step
conversion, with an alert on the cliff.* One gauge, one threshold, "tell me
when my agent stops validating before it answers." Everything else in the post
is how to get there.

**Then:** `PREDICTION.md` was committed before any analytics response was read,
with per-claim falsification criteria and a control funnel. One sentence, one
link — it is the credibility anchor, not a section.

**Then the AI disclosure paragraph** (below, verbatim), then links: repo,
issues #12143 / #12160, SigNoz docs.

---

## Word budget

| Beat | Words |
|---|---:|
| 1 · The contract nobody measures | 200 |
| 2 · Building the funnel over cognition | 320 |
| 3 · Why the obvious query cannot answer this | 190 |
| 4 · Three potholes on the way | 250 |
| 5 · The loop closes (finale) | 350 |
| Landing · boundary + takeaway + disclosure | 190 |
| **Total** | **1,500** |

Beats 2 and 5 together are 45% of the budget — deliberately. They are the build
and the payoff. Beat 4, the entire bug section, is 17%: enough to be credible,
short enough that it cannot become the story.

**If over budget, cut in this order:** the p50 pothole (beat 4, item 3) → the
`avg_duration`/`latency` figures in beat 2 → the Phoenix/Datadog naming in the
landing. **Never cut:** the `125 → 125 → 80 → 80` numbers, the counter-vs-funnel
contrast, or the live regression demo.

---

## Embedded media

In priority order — if only three survive the edit, they are the first three.

1. **The money image:** naive counter (100.0%) beside the ordered funnel (64.0%),
   same traces, same window, **n printed on every bar**.
2. **The live loop (video, ≤3 min):** inject the validation regression → the
   `validate` bar sinks → the alert fires. Leads the video, first 20 seconds.
3. **Terminal capture:** the agent calling `get_funnel_analytics` through
   `signoz-funnel-mcp` and reading its own conversion rate.
4. The corrected cliff in SigNoz's Trace Funnels UI (`125 → 125 → 80 → 80`).
5. The dashboard-as-code panel — validate-conversion over time — plus the
   firing drop-off alert.
6. The `500: unsupported value: NaN` response, verbatim. **One small image, in
   beat 4 only.** Do not use it as the cover image.
7. The MCP client config block (code, not screenshot — it must be copy-pasteable).

**Cover image:** the funnel bars, not the stack trace.

**Every screenshot with a number in it needs `n` visible.** A conversion
percentage without a denominator is not evidence.

---

## AI disclosure paragraph (publish verbatim)

> **AI disclosure.** Claude Code was used as a development assistant throughout
> this project — drafting boilerplate and scaffolding, exploring SigNoz's Go and
> ClickHouse source to locate the funnel query builders, reviewing the diagnosis
> behind the NaN guard, and editing this post. Every architectural decision,
> both bug diagnoses, and every factual claim here was made and verified by a
> human against a live SigNoz v0.132.2. No paid LLM API is used in the shipped
> product: the funnel MCP server, the `fot` CLI, the dashboard and the alert make
> **zero LLM calls at runtime** and are pure REST plus arithmetic. The only model
> in the system is the agent being *observed*, run offline as a batch data
> generator on free-tier `gemini-3.1-flash-lite` and `gemini-3.1-flash`.
> `PREDICTION.md` was committed before any funnel data was read, and a control
> funnel runs beside the treatment, so the reported drop-off is attributable
> rather than asserted. All project code was written after the hackathon opened
> on 2026-07-20.

---

## Pre-publish checklist

- [ ] Word count ≤ 1,500.
- [ ] Every command in the post was run, in order, from a clean clone.
- [ ] Every screenshot shows `n`.
- [ ] The word "crash" does not appear in the title, subtitle, or cover image.
- [ ] The last 150 words are about the working instrument, not the bug.
- [ ] Upstream described as **filed / open / proposed** — never "merged".
      (#12143 open; #12160 proposes a fix; #12167 closed unmerged 2026-07-21.)
- [ ] SigNoz credited by name in beat 1 and in the landing.
- [ ] AI disclosure present and unedited. **Omitting it is disqualification.**
- [ ] Repo link, issue links, and the video embed all resolve.
- [ ] Published to Dev.to (not LinkedIn), public, submitted via the project form.
