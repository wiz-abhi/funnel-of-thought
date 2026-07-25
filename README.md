![Funnel of Thought — a counter measures presence, a funnel measures sequence](docs/media/banner.gif)

# Funnel of Thought

**An AI agent that watches itself think — and tells you the exact step where it stopped.**

**▶ Live demo: [wiz-abhi-funnel-of-thought.static.hf.space](https://wiz-abhi-funnel-of-thought.static.hf.space)** · [3-min video](blog/video/funnel-of-thought-demo.mp4) · [the blog](blog/POST.md)

![The cognition funnel: 125 runs start, 80 reach validate in order](docs/media/hero-funnel.gif)

`plan → tool → validate → respond` is a contract. Most agents are trusted to
honour it and never measured against it. Funnel of Thought measures it, as a
conversion funnel, in SigNoz, live — and ships the funnel MCP tools that let
the agent run that measurement over its own traces without a human in the loop.

## The 30-second version

My agent has a validation step. A dashboard said it ran in **100% of runs**.
A funnel over the same **125 traces** said **64%**.

![Counter says 100%, funnel says 64%, gap 36pp](docs/media/counter-proof.gif)

Both queries are correct. They answer different questions — and 45 runs emitted
their `validate` span **before the tool result existed**, so the agent validated
an answer that had not arrived yet. Every presence-based metric you own scores
those runs as a success.

> **A counter measures presence. A funnel measures sequence.
> Only one of those is the contract.**

![One trace, two readers: the counter says 100%, the funnel says 64%](docs/media/contract-sketch.png)

The read path is deterministic REST over spans that already landed. No LLM
sits between you and the answer, so building a funnel and reading it back is a
sub-second operation you can repeat while the agent is still running. Inject a
validation regression and watch the `validate` bar sink and the alert fire
while you are still looking at the screen.

> SigNoz already markets Trace Funnels for exactly this shape of problem —
> tracking "drop-offs across your AI agent pipelines." They built the only
> funnel primitive in the observability space, and they pointed it at agents
> first. This project is the adapter that makes that pairing work on a
> stock-OpenTelemetry agent, plus the four MCP tools that make it reachable
> from inside one.

---

## The problem

Every LLM-observability tool on the market will show you traces. Langfuse,
LangSmith, Phoenix, Braintrust — all of them can show you what your agent did
on one request. None of them has a **funnel primitive**: a way to ask *"of the
agent runs that started reasoning, what fraction actually completed each step,
in order?"*

SigNoz has one. It is genuinely the differentiated thing in their product for
this use case. But pointing it at a spec-compliant agent turns out to need
knowledge that isn't written down anywhere, and here is what we found doing it.

**1. Funnel steps match on *exact* span name.** Each step compiles to
`resource_string_service$$name = <step.service> AND name = <step.span_name>`,
ANDed with an optional per-step raw SQL clause. No wildcards, no OR, no regex.
That's a reasonable design — it's what makes the query fast — but it means the
span name you pick is load-bearing in a way nothing warns you about.

**2. The OpenTelemetry GenAI semantic conventions put the model name inside
the span name.** An LLM call is named `{operation} {model}` — `chat
gemini-3.1-flash-lite`. Follow the spec exactly and one *logical* step
fragments into N *literal* span names the moment you A/B a model, bump a
version, or fall back to a secondary provider. A funnel keyed on that step then
matches a fraction of your traces and silently reads 0%. Nothing is
misconfigured. Both specs are being followed. They just disagree about whether
a span name is an identity or a description.

**3. A step matching zero traces returns `HTTP 500: unsupported value: NaN`
instead of 0%.** `BuildFunnelStepOverviewQuery` computes
`round(total_sEnd * 100.0 / total_sStart, 2)` with no zero-guard — while
`BuildFunnelOverviewQuery`, one function away in the same file, guards the
identical division with `if(total_s1_spans > 0, …)`. So the failure mode of
finding #2 is not a `0%` you could debug from; it's a 500 that looks like the
backend is down. Filed as [SigNoz issue #12143](https://github.com/SigNoz/signoz/issues/12143),
still open; community [PR #12160](https://github.com/SigNoz/signoz/pull/12160)
*proposes* a fix, and the guard it adds is the one that already exists next
door. (A separate attempt, #12167, was closed unmerged on 2026-07-21.) No fix
has merged at time of writing.

**4. `latency_type: "p50"` silently returns p99.** The `latencyQuantile` switch
implements `p90` and `p95` and defaults everything else — including `p50` — to
`0.99`. Measured on our own data: p50 = `18.673343`, byte-identical to p99,
while p90 = `17.96` and p95 = `18.01`. A median reporting *higher* than the p90
and p95 of the same distribution. Unfiled at time of writing; a two-line switch
case fixes it.

**5. Funnels need strictly increasing timestamps between steps** (`t2 > t1`).
Steps that complete inside the same clock tick don't just tie — conversion
collapses to ~0 and the analytics call 500s. A funnel is therefore blind to
steps that complete within one clock tick, which is easy to hit with fast or
no-op nodes. This is undocumented and it's the single most useful thing we
learned building one; it's why our agent's per-node work is load-bearing.

**6. Zero of SigNoz's 41 shipped MCP tools touch funnels.** Live-enumerated
against the running MCP server. Their MCP surface is broad and good — traces,
metrics, logs, dashboards, alerts — but the funnel primitive, the one thing no
competitor has, is the one thing an agent cannot reach through it.

None of these is a criticism of a product we're building on top of. Findings
#1 and #3–#5 are the kind of thing you only hit by leaning hard on a feature,
and #6 is a gap that exists because funnels are newer than the MCP server.
We closed them because we wanted to use the funnel, not because we wanted to
report on it.

**The one that isn't a bug:** funnels aggregate with `minIf` over a
monotonically increasing step index. They see the *first* occurrence and they
enforce *order*. That makes them structurally blind to loops and retries — an
agent that validated once and an agent that validated five times after four
failures are the same funnel. This is not fixable and we do not treat it as a
defect; see [Limitations](#limitations--roadmap). It is the boundary that
defines where this instrument applies: **funnel the linear contract; loops want
a graph.**

---

## It runs in SigNoz, not just in my terminal

The funnel is a real SigNoz object — the CLI and SigNoz's own UI agree to the
decimal:

![SigNoz Funnels UI showing the cognition funnel at 64.00% conversion](blog/assets/04-signoz-funnel.png)

And the violation is visible in a single trace — `agent.validate` in red,
finishing *before* the `agent.tool` call whose output it was supposed to check:

![Trace waterfall: plan, validate (red, out of order), tool, respond](blog/assets/07-trace-flamegraph.png)

---

## Architecture

![Architecture sketch: agent to OTLP to SigNoz, fanning out to the CLI, the MCP server, and a dashboard + alert](docs/media/arch-sketch.png)

```
   ┌───────────────────────────────────────────────────────────────────────┐
   │  agent/  — 4-node LangGraph agent, the observed subject                │
   │           service.name = fot-agent    root span = fot.agent.run        │
   │                                                                        │
   │       plan ────▶ tool ────▶ validate ────▶ respond                     │
   │                                                                        │
   │   stock OTel instrumentation.                                          │
   │     node spans:  agent.plan  agent.tool                                │
   │                  agent.validate  agent.respond   STABLE across models  │
   │     llm  spans:  "chat <model>"                  FRAGMENTS on a swap   │
   │   free-tier Gemini, offline batch only — never on the read path        │
   └──────────────────────────────┬────────────────────────────────────────┘
                                  │ OTLP :4318
                                  ▼
   ┌───────────────────────────────────────────────────────────────────────┐
   │  SigNoz v0.132.2  (cast by Foundry from casting.yaml)                  │
   │                                                                        │
   │    Traces ──▶ ClickHouse ──▶ Trace Funnels                             │
   │        keyed on stable node spans  ──▶  the true cliff                 │
   │        keyed on "chat <model>"     ──▶  0%, then NaN 500               │
   └───┬──────────────────────────────────────────────┬────────────────────┘
       │  REST                                        │  REST
       │  /api/v1/trace-funnels/new                   │  .../analytics/steps
       │  /steps/update   (ms timestamps)             │  (ns windows)
       ▼                                              ▼
   ┌────────────────────────────────┐   ┌────────────────────────────────┐
   │ signoz_funnel_mcp/  (stdio)    │   │ fot/  — library + CLI          │
   │   create_funnel                │   │   fot apply    fot show        │
   │   get_funnel_analytics         │   │   fot compare  fot gauges      │
   │   get_funnel_slow_traces       │   │   fot counter-proof            │
   │   list_funnels                 │   └───────────────┬────────────────┘
   └───────────────┬────────────────┘                   │
                   │                                    │ conversion + latency
                   │  the agent reads a funnel          │ as OTel gauges
                   │  over its OWN traces               ▼
                   │                     ┌────────────────────────────────┐
                   └────────────────────▶│ dashboards/ + alerts/          │
                                         │  validate-conversion panel     │
                                         │  drop-off threshold alert      │
                                         └────────────────────────────────┘

   Everything below the agent box is deterministic. Zero LLM calls at runtime.
```

The loop that closes: the agent emits spans → SigNoz computes the funnel →
the agent calls `get_funnel_analytics` through `signoz-funnel-mcp` → the agent
reads its own conversion rate. That last hop is the one that didn't exist
before, because no MCP tool reached funnels.

---

## Quickstart

**Prerequisites:** Docker, Python 3.11+, and a free
[Gemini API key](https://aistudio.google.com/apikey). Windows users: use Git
Bash, or the PowerShell twin noted at each step.

### 1. Install Foundry and cast SigNoz

```bash
curl -fsSL https://signoz.io/foundry.sh | bash
export PATH="$HOME/.local/bin:$PATH"

git clone https://github.com/wizabhi/funnel-of-thought.git
cd funnel-of-thought

foundryctl cast          # reads casting.yaml, brings up SigNoz + its MCP server
```

`casting.yaml` and `casting.yaml.lock` are both committed, so this is
reproducible. Two things in that file are deliberate and worth knowing:

- **SigNoz is pinned to `v0.132.2`,** not `latest`. The NaN-500 in finding #3
  is a behaviour of that version; once [PR #12160](https://github.com/SigNoz/signoz/pull/12160)
  merges, `latest` will return a clean `0%` and the reproduction below will
  show something different from what this README describes. Pinning keeps the
  claims checkable. To see the *fixed* behaviour, change the image tag and
  re-cast.
- **SigNoz's MCP server is enabled** (`spec.mcp.spec.enabled: true`; Foundry
  defaults it to `false`). It lands on `signoz-network` at `signoz-mcp:8000`,
  published to `localhost:8000`. That's the 41-tool baseline our four tools
  extend.

If you'd rather not install `foundryctl`, `pours/deployment/compose.yaml` is
the forged output and is committed too:
`docker compose -f pours/deployment/compose.yaml up -d`.

Wait for `http://localhost:8080` to come up, create the admin account in the
UI, and note the email/password — the next step needs them.

### 2. Configure

```bash
cp .env.example .env
$EDITOR .env              # set GEMINI_API_KEY
```

### 3. Install and get a token

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[all]"

./scripts/setup.sh --token         # Windows: .\scripts\setup.ps1 -Token
```

**Why a token step exists at all.** Funnel *write* endpoints
(`/api/v1/trace-funnels/new`, `/steps/update`) are gated behind `EditAccess`,
and a SigNoz API key does not carry it. You need a **login JWT** from an
admin/editor account:

```bash
curl -s -X POST http://localhost:8080/api/v1/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"..."}' | jq -r .data.accessToken
```

`scripts/setup.sh --token` does this for you and writes the result into `.env`
as `SIGNOZ_JWT`. JWTs expire — when funnel calls start returning 401, re-run
it. This is the single most common way the quickstart fails.

### 4. Bootstrap everything

```bash
./scripts/setup.sh                 # Windows: .\scripts\setup.ps1
```

Which checks your environment, applies the funnels, dashboard and alert, runs
the batch generator, and prints where to look. Roughly 10 minutes, most of it
Gemini rate limits on the batch.

### 5. See the cliff

```bash
fot show                    # per-step conversion, n printed on every bar
fot counter-proof           # the naive counter beside the ordered funnel
fot compare                 # working funnel vs the fragmented one
```

On our run, over 56 traces:

```
agent.plan       ████████████████████████████████████████  56   100.0%
agent.tool       ██████████████████████████████████        47    83.9%
agent.validate   ██████████████████                        25    44.6%
agent.respond    ██████████████████                        25    44.6%

avg_duration 720.99ms   ·   latency 1079.61ms
```

**Fewer than half the runs validated before answering, in order.** The agent
has a validation step and skips it — or runs it late — most of the time. That
is invisible in a trace view, which shows you one run, and invisible to an eval
that only scores the final answer.

Then open **SigNoz → Traces → Funnels** at `http://localhost:8080` for the
same numbers in the product's own UI, and the dashboard for conversion over
time.

### Reproduce the finding in ≤10 minutes

```bash
./scripts/reproduce.sh
```

This is the judge path. It generates a two-model batch, builds both funnels,
prints the working cliff, then deliberately triggers the fragmented funnel and
shows you the `500: unsupported value: NaN` verbatim. Exits non-zero if
anything it asserts fails to reproduce, so a green run means the claims in this
README held on *your* machine, not just ours.

### Using the MCP server

Add to your MCP client config (Claude Desktop / Claude Code / Cursor):

```json
{
  "mcpServers": {
    "signoz-funnel": {
      "command": "signoz-funnel-mcp",
      "env": {
        "SIGNOZ_URL": "http://localhost:8080",
        "SIGNOZ_JWT": "<your editor JWT>"
      }
    }
  }
}
```

Then ask your agent: *"Create a funnel over my agent's reasoning steps and tell
me which one is losing traces."* Four tools: `create_funnel`,
`get_funnel_analytics`, `get_funnel_slow_traces`, `list_funnels`.

A container image is available too — `docker compose --profile mcp build
funnel-mcp` — but stdio via the console script is the shorter path.

---

## How it uses SigNoz

Remove SigNoz and this project cannot exist. There is no funnel primitive
anywhere else to port it to. Every surface below is load-bearing, not
decorative.

| Surface | How it's used | Why it's load-bearing |
|---|---|---|
| **Traces** | The substrate. Stock-OTel agent spans — stable node spans plus the fragmenting `chat {model}` LLM children — are the raw material every funnel is computed over. | No traces, no funnel. Nothing downstream exists. |
| **Trace Funnels** | The core primitive and the product. Two funnels over one dataset: keyed on stable node spans (works) and on the GenAI-convention LLM span (0% → 500). The project reaches below the UI into the generated ClickHouse SQL. | This *is* the project. It's also the surface no competitor ships. |
| **Query Builder** | The counter-proof: a `GROUP BY span name → COUNT` that reports `validate` at ~100% while the ordered funnel reports it far lower. Order-blindness made visible. | Proves a counter is *structurally* wrong, not merely a different number. |
| **Metrics** | Per-step conversion and latency from `/analytics/steps` re-emitted as OTel gauges, so a point-in-time funnel becomes a time series. | Without this the funnel is a screenshot; with it, it's a monitorable signal. |
| **Dashboards** | Dashboard-as-code (v5 schema, committed JSON) charting `validate`-step conversion over time beside step latency. | Where "watch one metric" actually lives. |
| **Alerts** | A threshold rule that fires when `validate` conversion drops below the floor — the trigger that turns a chart into an operational signal. | Converts diagnosis into something that pages you. It's what makes the live demo live. |
| **MCP** | Both the primary programmatic path *and* the gap we filled. `signoz-funnel-mcp` ships the four funnel tools that 0 of the 41 existing tools provide, so an agent can build and read a funnel over itself. | The adoptable artefact — installable in 60 seconds, useful without the rest of this repo. |

---

## What's novel

- **A funnel over cognition, not commerce.** Trace Funnels were built for
  request pipelines. Pointing them at an agent's *reasoning contract* — where
  the conversion rate answers "does my agent honour its own steps?" — is a use
  SigNoz's own AI page gestures at and which, as far as we can find, nobody has
  actually made work end-to-end on a stock-instrumented agent.
- **The four missing MCP tools.** 0 of 41. An agent that can query traces,
  metrics, logs, dashboards and alerts but cannot reach the one primitive that
  measures its own completion rate. Now it can.
- **A named, reproducible incompatibility between two specs that are both
  right.** OTel GenAI says a span name describes the model. Trace Funnels say a
  span name identifies a step. Neither is wrong; together they silently produce
  0%. That's a boundary worth writing down, and it generalises past SigNoz to
  any exact-match step matcher.
- **Pre-registration.** [`PREDICTION.md`](PREDICTION.md) was committed before
  any analytics response was read, with explicit falsification criteria for
  each claim and a control funnel beside the treatment. The reported drop-off
  is attributable rather than asserted.
- **Speed as the feature.** The read path has no LLM in it, so the loop
  *build funnel → read conversion → change something → read again* runs in
  under a second, repeatedly, on live data. That's what makes an agent
  measuring itself in real time possible at all.

---

## AI usage disclosure

Per the hackathon rules, disclosed in full.

**Claude Code (Anthropic) was used as a development assistant throughout this
project** — for drafting boilerplate and scaffolding, reviewing the
ClickHouse/Go analysis behind the NaN-guard diagnosis, exploring SigNoz's
source to locate the relevant query builders, and editing prose in this README
and the accompanying blog post. All architectural decisions, the choice of what
to build, the diagnosis of the two bugs, and every claim of fact were made and
verified by a human against a live SigNoz instance.

**No AI writes to the shipped runtime.** The funnel tooling — the MCP server,
the `fot` CLI, the dashboard and the alert — makes zero LLM calls. Its
behaviour is pure REST plus arithmetic.

**The only model in the system is the one being observed.** `agent/` calls
free-tier Gemini (`gemini-3.1-flash-lite` and `gemini-3.1-flash`) offline, as a
batch, to generate the traces the funnel is computed over. It is the subject of
the measurement, not part of the instrument. Swap it for any OTel-instrumented
agent and everything else works unchanged.

All project code was written after the hackathon opened on 2026-07-20.
Preparatory research, the PRD, and the verified REST API map predate it; no
implementation does.

---

## Limitations & roadmap

We'd rather state these than have you find them.

**Funnels cannot see loops.** `minIf` plus a monotonic step index means the
first qualifying occurrence wins and order is enforced. An agent that validated
once and an agent that validated five times after four failures produce the
same funnel. Since retry storms are among the most common real agent failures,
this is a genuine ceiling — not on our implementation, on the primitive.
Phoenix's agent-path graph and Datadog's execution-loop view are the loop-aware
complements; a graph view is the natural follow-up, and it belongs on SigNoz
rather than beside it.

**The observed agent is authored.** We wrote the 4-node agent, because we
needed a clean contract with a discrete `validate` node. We say so plainly. What
is *not* authored is the defect: stock OTel named the spans, the GenAI
convention put the model in the name, and an ordinary model swap did the rest.
The claim is "unauthored *defect*," never "unauthored *agent*."

**Exact-name matching is a real constraint, not a bug.** Findings #1 and #2
combine into a footgun, but exact matching is what makes the ClickHouse query
fast. The fix isn't wildcards; it's knowing to key funnels on span names that
are *identities* (`agent.validate`) rather than names that are *descriptions*
(`chat gemini-3.1-flash`). Node spans give you identities for free. That's the
transferable lesson, and it holds for any exact-match step matcher.

**Single-service scope.** Every funnel here is over one `service.name`. Steps
carry a service field so cross-service funnels should work, but we haven't
tested a multi-service reasoning contract.

**Upstream state.** Issue #12143 is filed, diagnosed, and **open** — no fix has
merged. Community PR #12160 *proposes* one; a separate attempt, #12167, was
closed unmerged on 2026-07-21. The p50→p99 mislabel is diagnosed here and being
filed with a patch attached. We describe all upstream work as *filed*, *open*
or *proposed* — never merged.

**Roadmap.** A loop-aware graph companion for the failures funnels structurally
miss; multi-service reasoning contracts; upstreaming `signoz-funnel-mcp` into
SigNoz's own MCP server so the four tools stop being a separate install.

---

## Repository layout

```
agent/                4-node LangGraph agent + batch trace generator
signoz_funnel_mcp/    MCP stdio server — the four missing funnel tools
fot/                  core library + `fot` CLI
dashboards/           dashboard-as-code (SigNoz v5 schema)
alerts/               drop-off threshold alert
scripts/              setup.sh / setup.ps1 / reproduce.sh
blog/                 the write-up and its outline
casting.yaml          Foundry installation spec  (committed)
casting.yaml.lock     forged lockfile            (committed)
pours/                forged deployment manifests (committed)
docker-compose.yml    OUR containers, joined to signoz-network
PREDICTION.md         pre-registered hypotheses, committed before data
```

---

## Acknowledgements

To the SigNoz team, for building the only funnel primitive in observability and
for making the source readable enough that both bugs here could be root-caused
from it in an afternoon. To **kunalpandey1** for
[PR #12160](https://github.com/SigNoz/signoz/pull/12160). To WeMakeDevs and
SigNoz for the Agents of SigNoz hackathon.

## License

[MIT](LICENSE).
