# The observed agent, as a one-shot batch job.
#
# This is the only container in the project that can make an LLM call, and it
# makes them once and exits. Everything downstream -- the funnel, the CLI, the
# MCP server, the dashboard, the alert -- is deterministic REST over spans that
# already landed.
#
#   docker compose --profile batch up --build agent-batch
#
# Build context is the repo root (see docker-compose.yml), so paths below are
# repo-relative. .dockerignore keeps the venvs and git history out.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so editing agent code does not reinstall the world.
COPY agent/requirements.txt ./agent/requirements.txt
RUN pip install --no-cache-dir -r agent/requirements.txt

COPY agent/ ./agent/

# Two models on purpose. OTel's GenAI convention names an LLM span
# `{operation} {model}`, so the model string lands INSIDE the span name and one
# logical step fragments across N names. Generating under two models is what
# makes a funnel keyed on `chat <model>` read a partial number.
#
# No GEMINI_API_KEY -> --stub: identical span shape, zero network, seconds.
# That keeps this image runnable by anyone, key or not.
COPY <<'EOF' /app/run-batch.sh
#!/bin/sh
set -eu
RUNS="${FOT_BATCH_SIZE:-150}"
RATE="${FOT_VALIDATE_RATE:-0.64}"
SEED="${FOT_SEED:-1337}"
A="${FOT_MODEL:-gemini-3.1-flash-lite}"
B="${FOT_MODEL_B:-gemini-3.1-flash}"
RUNS_A=$(( RUNS * 2 / 3 ))
RUNS_B=$(( RUNS - RUNS_A ))

STUB=""
if [ -z "${GEMINI_API_KEY:-}" ]; then
  echo "GEMINI_API_KEY unset -- generating with --stub (no LLM calls)"
  STUB="--stub"
fi

for pair in "$A:$RUNS_A" "$B:$RUNS_B"; do
  model="${pair%:*}"; runs="${pair##*:}"
  [ "$runs" -gt 0 ] || continue
  echo "==> $runs runs on $model"
  # shellcheck disable=SC2086
  python -m agent.generate --runs "$runs" --model "$model" \
    --validate-rate "$RATE" --seed "$SEED" $STUB
done
EOF
RUN chmod +x /app/run-batch.sh

# Non-root. Nothing here writes to disk.
RUN useradd -r -u 10001 fot
USER 10001

CMD ["/app/run-batch.sh"]
