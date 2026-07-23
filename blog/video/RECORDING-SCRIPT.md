# Recording script — v2 (your voice)

**Hard cap: the finished video must be under 3:00** (submission form: YouTube, ≤3 minutes, must cover about → tech stack/architecture → demo → learnings). This script is ~405 words; at a relaxed ~145 wpm it lands ≈ 2:48 spoken + 3.5s end card ≈ **2:52 total**. If your read comes in over **2:55, re-record the slowest section** rather than letting me trim content.

## How to record

- **One file per section, 8 files.** Name them exactly `section1.wav` … `section8.wav` (m4a/mp3 also fine) and drop them in `blog/video/audio-user/`.
- Mono, any normal sample rate (44.1/48 kHz). A phone in a quiet room is fine — hold it ~20 cm away, consistent distance.
- `//` means **pause about half a second**. Don't say it.
- If you fluff a line, pause, and restart the whole sentence — I'll trim silences automatically; just don't restart mid-sentence.
- Read numbers exactly as written ("one hundred and twenty-five", "sixty-four percent").
- **SigNoz** = "SIG-noz". **fot** = spell it or say "F-O-T" — your call, but be consistent.
- Steady and unhurried beats fast and clean. The captions are cut to your audio, so pace is yours.

---

## §1 — Intro: what this is and why · ~86 words · ~35s
*(on screen: title card → the reasoning-contract diagram → "what it ships" card → architecture)*

> Hi, I'm Abhishek, and this is Funnel of Thought — built for the Agents of SigNoz hackathon. //
> The idea: AI agents have a reasoning contract — mine is plan, tool, validate, respond — and nothing today measures whether they honour it. //
> So I built three things: an OpenTelemetry-instrumented LangGraph agent; fot, a CLI that turns SigNoz Trace Funnels into funnels-as-code over reasoning steps; and signoz-funnel-mcp — the funnel tools SigNoz's MCP server doesn't ship. //
> All of it runs on self-hosted SigNoz, installed with Foundry.

## §2 — Two numbers · ~41 words · ~17s
*(on screen: the 100% vs 64% panel)*

> My agent has a validation step. A dashboard told me it ran in one hundred percent of runs. A funnel over the same one hundred and twenty-five traces said sixty-four percent. //
> Same traces. Both queries correct. Only one asked the right question.

## §3 — The funnel, live · ~49 words · ~20s
*(on screen: terminal, `fot show cognition`)*

> Here's the funnel, live, over the agent's reasoning. One twenty-five runs start. One twenty-five reach the tool call. //
> Then the cliff: only eighty reach validate, in order. Sixty-four percent. //
> And the counts are printed on every bar — a percentage without a denominator isn't evidence.

## §4 — It lives in SigNoz · ~44 words · ~18s
*(on screen: SigNoz Funnels UI)*

> This isn't my tool marking its own homework — here's the same funnel inside SigNoz itself. Sixty-four percent. One twenty-five, one twenty-five, eighty, eighty. Down thirty-six percent on step three. //
> Trace Funnels are a SigNoz primitive. No other observability backend ships one.

## §5 — Why the counter lies · ~63 words · ~26s
*(on screen: terminal, `fot counter-proof`)*

> So why did the counter say one hundred? //
> A counter only asks: did the span exist. A funnel asks: did it happen after the previous step. //
> Forty-five runs emitted their validate span before the tool result existed — the agent validated an answer that hadn't arrived yet. //
> A counter measures presence. A funnel measures sequence. Ordering lives in the trace, not the span.

## §6 — One trace · ~27 words · ~11s
*(on screen: trace waterfall, validate in red)*

> Here's one of those runs. Plan — then validate, in red — and only then the tool call. //
> It finished validating before the thing it was validating existed.

## §7 — The agent reads its own funnel · ~43 words · ~18s
*(on screen: terminal, MCP call)*

> None of SigNoz's forty-one MCP tools reach funnels, so I shipped the missing ones. //
> Here, the agent calls get-funnel-analytics and reads its own conversion rate. //
> The read path has no model in it — it's sub-second, deterministic, and free.

## §8 — Learnings, and the close · ~52 words · ~22s
*(on screen: the firing alert → end card)*

> What did I learn? Funnels need strictly increasing timestamps. Two correct specs can be silently incompatible — that finding is now two SigNoz issues and a pull request. And agents fail politely: no errors, just skipped homework. //
> So: watch validate-step conversion, and alert on the cliff. Mine is firing right now.

---

**When you're done:** drop the 8 files in `blog/video/audio-user/` and tell me. I'll trim silences, measure your real durations, re-cut the captions to your voice, re-conform every shot, and rebuild — the pipeline warns if the total crosses 2:55.
