# Demo video — narration script & shot list

**Target: 2:50.** Narration ~420 words at ~150 wpm. Every number spoken is measured; nothing is dramatised.

**If you re-record in your own voice:** read at a steady, unhurried pace, and pause where marked `//`. The timings below are what the captions are cut to — if your read runs long, regenerate the SRT rather than rushing.

---

### 1 · The question · 0:00–0:22

> **[SHOT: title card → `diagram-01-contract.png`]**

"Every AI agent has a contract it's supposed to follow. Mine has four steps: plan, call a tool, validate the result, then respond. //
We trust agents to follow that order, and then we basically never check. //
A trace shows you one run. An eval scores the final answer. Neither one tells you how often the agent actually finished thinking."

---

### 2 · Two numbers · 0:22–0:44

> **[SHOT: `meme-01-two-numbers.png`, hold]**

"So I measured it two ways, on the same hundred and twenty-five traces. //
A normal span counter said my validate step ran in one hundred percent of runs. //
A funnel said sixty-four percent. //
Same traces. Both queries correct. Only one of them was asking the right question."

---

### 3 · The funnel, live · 0:44–1:15

> **[SHOT: live terminal — `fot show cognition --since 6h` typing and rendering]**

"This is a funnel over the agent's reasoning, running against SigNoz. //
A hundred and twenty-five runs start. A hundred and twenty-five reach the tool call. //
Then it falls off a cliff — only eighty reach validate, in order. Sixty-four percent. //
Notice the counts are printed on every bar. A percentage without a denominator isn't evidence."

---

### 4 · It lives in SigNoz · 1:15–1:37

> **[SHOT: live SigNoz UI — Traces → Funnels → cognition, scroll to metrics]**

"And this isn't my tool marking its own homework. Here's the same funnel inside SigNoz itself. //
Conversion rate: sixty-four percent. Total spans: one two five, one two five, eighty, eighty. Down thirty-six percent on step three. //
Trace Funnels are a SigNoz primitive — Langfuse, LangSmith and Phoenix don't expose anything like it."

---

### 5 · Why a counter can't see it · 1:37–2:10

> **[SHOT: live terminal — `fot counter-proof cognition --since 6h`]**

"So why did the counter say one hundred percent? //
Because a counter only asks: did this span exist anywhere in the trace. It has no idea where. //
A funnel asks: did it happen *after* the previous step. //
Forty-five runs emitted a validate span — before the tool result existed. The agent validated an answer that hadn't arrived yet. //
Every one of those is a success to a counter, and a failure to a funnel. Ordering is a property of the trace, not of the span."

---

### 6 · The violation, in one trace · 2:10–2:27

> **[SHOT: live SigNoz UI — trace waterfall, highlight agent.validate]**

"Here's one of them. Plan, then validate — in red — and only then the tool call. //
Validate finished before the tool it was checking even started. Nine spans, one error, and the run still returned a confident answer."

---

### 7 · The agent reads its own funnel · 2:27–2:45

> **[SHOT: live terminal — MCP JSON-RPC call returning funnel analytics]**

"Zero of SigNoz's forty-one MCP tools touch funnels, so I shipped the missing ones. //
Now the agent calls get funnel analytics and reads its own conversion rate — a loop that wasn't expressible before. //
And the read path has no model in it. It's sub-second, and it costs nothing."

---

### 8 · Close · 2:45–2:58

> **[SHOT: live SigNoz UI — alert rule Firing → end card]**

"Which means it can just run: a threshold rule that goes red when the agent stops validating before it answers. //
Watch validate-step conversion. Alert on the cliff. That's the whole idea."

> **[END CARD: `github.com/wiz-abhi/funnel-of-thought`]**

---

## Shot list (for capture)

| # | Source | Duration | Notes |
|---|---|---|---|
| 1 | Title card + `diagram-01-contract.png` | 22s | Ken-Burns-free; static, 1s crossfade |
| 2 | `meme-01-two-numbers.png` | 22s | static hold |
| 3 | Live terminal: `fot show cognition --since 6h` | 31s | real command, real output |
| 4 | Live browser: SigNoz Funnels → `cognition` | 22s | real page, slow scroll to metrics |
| 5 | Live terminal: `fot counter-proof cognition --since 6h` | 33s | real command |
| 6 | Live browser: trace `d5ba3cc315e495a16446aabd84470361` | 17s | waterfall visible |
| 7 | Live terminal: MCP `tools/call get_funnel_analytics` | 18s | real JSON-RPC over stdio |
| 8 | Live browser: alert rule Firing + end card | 13s | requires `fot gauges` running |

**Honesty note for the description:** shots 3, 5 and 7 are real commands producing real output; shots 4, 6 and 8 are the live SigNoz UI. Nothing is mocked. The `fot gauges` emitter must be running for shot 8, since the alert reads a gauge it publishes.
