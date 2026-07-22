# The observed agent

A 4-node LangGraph agent — `plan → tool → validate → respond` — instrumented with
OpenTelemetry. It exists to produce the traces that the SigNoz Trace Funnel is
computed over.

The agent answers a small question using a **mocked** search tool (no external
network calls; it is a deterministic generator over a five-document in-memory
corpus). The only network dependency is the LLM, and `--stub` removes even that.

## Quick start

```bash
# from the repo root
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r agent/requirements.txt

# offline smoke test — no LLM calls, compressed latencies, spans to stdout
./.venv/Scripts/python.exe -m agent.generate --runs 10 --stub --fast --console --no-otlp

# the demo dataset (150 traces to a local SigNoz at localhost:4318)
./.venv/Scripts/python.exe -m agent.generate --runs 150 --stub
```

## Span shape

One run is **one trace**. Guaranteed structurally: the run opens a root span, then
passes the OTel `Context` through the LangGraph state and parents every node span
at it explicitly. LangGraph may execute nodes on a worker thread, and contextvar
propagation across executors is not guaranteed, so relying on ambient context
alone would risk the loop splitting across traces — which would read as 0%
conversion and kill the funnel.

```
fot.agent.run                        root, stable name
  agent.plan                         funnel step 1
    chat {model}                     GenAI-semconv LLM span
  agent.tool                         funnel step 2
    search.query                     mocked retrieval
  agent.validate                     funnel step 3  (see modes below)
    chat {model}
  agent.respond                      funnel step 4
    chat {model}
```

Two naming layers, on purpose:

- **Node spans are stable and model-independent** (`agent.plan`, `agent.tool`,
  `agent.validate`, `agent.respond`). SigNoz funnels match steps with an exact
  span-name equality — there is no wildcard — so these are what a working funnel
  keys on.
- **LLM spans follow the OTel GenAI convention `{operation} {model}`**, e.g.
  `chat gemini-3.1-flash-lite`. That name deliberately embeds the model, so
  swapping models fragments one logical step across N span names. A funnel keyed
  on it drops to 0% and `/analytics/overview` then 500s on the NaN conversion
  rate. That is the teaching demo, and it is reproducible with `--model`.

LLM spans carry `gen_ai.operation.name`, `gen_ai.system`, `gen_ai.request.model`,
`gen_ai.response.model`, `gen_ai.agent.name`, `gen_ai.usage.input_tokens`,
`gen_ai.usage.output_tokens`, plus `fot.step` so you can slice LLM spans by
reasoning step even though the span name only knows the model.

### Why these spans are hand-written

Stock LangGraph auto-instrumentation (`openinference-instrumentation-langchain`)
*does* emit stable, model-independent node spans — verified, see "Spike result"
below. We still emit our own `agent.*` spans because:

1. The auto span names are the bare node ids (`plan`, `tool`, …) — generic enough
   to collide with unrelated spans in a shared service.
2. Auto-instrumentation names LangChain LLM spans after the class
   (`ChatGoogleGenerativeAI`), which is model-*independent* — the exact opposite
   of what the fragmentation demo needs.
3. We need identical span shape in `--stub` mode, where no LangChain call happens.

Pass `--auto-instrument` to additionally turn the stock instrumentation on; it
layers the bare node-name spans alongside ours.

## The three run modes

Each run is assigned one mode up front by the batch runner:

| Mode | Path | `agent.validate` span |
|---|---|---|
| `healthy` | `plan → tool → validate → respond` | present, correctly ordered |
| `out_of_order` | `plan → validate → tool → respond` | **present but before `tool`** |
| `skipped` | `plan → tool → respond` | **absent entirely** |

The skip is *structural* — a conditional edge routes around the node, so the body
never executes and no span is emitted. The funnel measures the absence of the
span, not a flag on it.

`out_of_order` is what makes the demo prove something. If validate were only ever
skipped, a naive `GROUP BY span_name` count would report the same percentage the
funnel does. When validate instead runs *early* — before the tool result it is
supposed to check exists — a counter sees it in 100% of traces while the ordered
funnel sees only 62%. A counter is structurally blind to per-trace ordering; that
is the whole argument.

Modes are allocated as exact counts and then shuffled, not sampled per-run, so
the conversion rate lands on the intended number instead of wobbling with sample
noise.

## ⚠️ The per-node sleeps are LOAD-BEARING — do not remove them

`agent/timing.py` makes every node sleep for a randomized interval. This is not
cosmetic padding for a prettier waterfall.

A SigNoz Trace Funnel computes `minIf(timestamp, ...)` per step and requires each
step's timestamp to be **strictly greater** than the previous step's. Mocked nodes
that do no real work complete inside the same clock tick, so all four spans land
on an identical timestamp with `duration_nano = 0`. The strict `t2 > t1`
comparison then fails, per-step trace counts collapse (we measured **6/100**
instead of 100/100), and `/analytics/overview` returns **HTTP 500** because the
conversion rate is NaN.

Live Gemini calls take 1–2s and are safe on their own. It is specifically
`--stub` and the mocked search tool that are at risk. `--fast` scales the sleeps
down for quick runs but never to zero — `timing.MIN_SLEEP_S` (4ms) is a hard
floor.

## Flags

`python -m agent.generate [flags]`

| Flag | Default | Meaning |
|---|---|---|
| `--runs N` | `150` | number of traces to emit |
| `--model ID` | `$FOT_MODEL`, else `gemini-3.1-flash-lite` | Gemini model id. Changing it changes the LLM span name — that is the point. |
| `--validate-rate F` | `$FOT_VALIDATE_RATE`, else `0.62` | fraction of runs where validate runs in the **correct** position |
| `--out-of-order-rate F` | `$FOT_OUT_OF_ORDER_RATE`, else **the remainder** (`1 − validate-rate`) | fraction where the validate span is emitted **before** `tool` |
| `--stub` | off | no LLM calls; identical span shape. Use it — Gemini free tier is 15 RPM. |
| `--fast` | off | compress simulated node latency 0.02x (floor still enforced) |
| `--seed N` | `42` | RNG seed; runs are reproducible |
| `--rpm F` | `12` | pacing for live runs, under the 15 RPM free-tier ceiling. Ignored with `--stub`. |
| `--console` | off | also print every span to stdout |
| `--no-otlp` | off | do not export to the collector |
| `--auto-instrument` | off | also enable OpenInference LangChain auto-instrumentation |
| `--question Q` | random | pin every run to one question from the corpus |

Any remainder after `--validate-rate` + `--out-of-order-rate` becomes `skipped`
runs. With the defaults the remainder is zero, so **100% of traces contain a
validate span and 62% have it correctly ordered** — the demo configuration.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) | — | required for live runs; read from env, never logged. Without it, live runs exit with a clear error and `--stub` still works. |
| `FOT_MODEL` | `gemini-3.1-flash-lite` | default model |
| `FOT_VALIDATE_RATE` | `0.62` | default correct-order rate |
| `FOT_OUT_OF_ORDER_RATE` | remainder | default early-validate rate |
| `FOT_AGENT_NAME` | `funnel-of-thought` | reported as `gen_ai.agent.name` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4318` | traces go to `/v1/traces`, logs to `/v1/logs` |
| `OTEL_SERVICE_NAME` | `fot-agent` | `service.name` on every span |
| `FOT_ENV` | `local` | `deployment.environment` |

Logs are emitted through the stdlib `logging` module via the OTel
`LoggingHandler`, so every record carries the active `trace_id` and `span_id` —
that is what powers the trace→logs jump in the SigNoz UI.

## Layout

| File | Purpose |
|---|---|
| `otel_setup.py` | tracer + logger providers, OTLP/HTTP exporters, resource attributes |
| `graph.py` | the 4-node graph, the three run modes, per-run tracing |
| `llm.py` | Gemini client and the hand-built GenAI-semconv LLM span; stub mode |
| `tools.py` | mocked search corpus and the `search.query` span |
| `timing.py` | the load-bearing simulated latencies |
| `generate.py` | batch runner / CLI |

## Verifying

```bash
# span name census
docker exec signoz-telemetrystore-clickhouse-0-0 clickhouse-client -q \
  "SELECT name, count() FROM signoz_traces.distributed_signoz_index_v3
   WHERE resource_string_service\$\$name='fot-agent'
   GROUP BY name FORMAT PrettyCompact"
```

To reproduce the counter-vs-funnel contrast, compare
`countDistinctIf(trace_id, name='agent.validate')` against an ordered
`minIf(timestamp, name=...)` comparison per trace. On the 150-run dataset the
counter reports 150/150 (100%) while the ordered funnel reports 93/150 (62%).

## Spike result

Question: does stock LangGraph OTel instrumentation emit stable, model-independent
node span names?

**Yes.** A trivial two-node graph under
`openinference-instrumentation-langchain` produced exactly these span names:

```
"plan"
"respond"
"LangGraph"      <- root
```

All three shared one `trace_id`, with the node spans parented at the `LangGraph`
root. So the names are stable and model-independent — the funnel assumption
holds. We nonetheless emit our own namespaced `agent.*` spans for the three
reasons listed under "Why these spans are hand-written" above; this is a
deliberate choice, not a workaround for missing instrumentation.
