#!/usr/bin/env bash
# =============================================================================
# Funnel of Thought — one-command bootstrap.
#
#   ./scripts/setup.sh              full bootstrap
#   ./scripts/setup.sh --token      only (re-)mint the SigNoz editor JWT
#   ./scripts/setup.sh --skip-batch everything except trace generation
#   ./scripts/setup.sh --help
#
# Assumes SigNoz is already up (`foundryctl cast`). This script deliberately
# does NOT cast, restart or otherwise touch the SigNoz stack — it only reads
# from it and writes funnels/dashboards/alerts through the API.
#
# -----------------------------------------------------------------------------
# INTERFACES THIS SCRIPT USES (kept in sync with the modules that own them):
#
#   python -m agent.generate --runs N --validate-rate R --seed S [--model M] [--stub]
#   python -m fot.cli apply [--only NAME]
#   python -m fot.cli dashboard apply
#   python -m fot.cli alert apply
#   python -m fot.cli gauges all
#   python -m fot.cli show
# =============================================================================
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

BOLD=$'\033[1m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YEL=$'\033[33m'; DIM=$'\033[2m'; OFF=$'\033[0m'
say()  { printf '%s\n' "${BOLD}==>${OFF} $*"; }
ok()   { printf '%s\n' "  ${GREEN}ok${OFF}   $*"; }
warn() { printf '%s\n' "  ${YEL}warn${OFF} $*"; }
die()  { printf '%s\n' "  ${RED}fail${OFF} $*" >&2; exit 1; }
note() { printf '%s\n' "       ${DIM}$*${OFF}"; }

TOKEN_ONLY=0
SKIP_BATCH=0
BATCH_COUNT="${FOT_BATCH_SIZE:-150}"

while [ $# -gt 0 ]; do
  case "$1" in
    --token)      TOKEN_ONLY=1 ;;
    --skip-batch) SKIP_BATCH=1 ;;
    --count)      BATCH_COUNT="${2:-150}"; shift ;;
    -h|--help)    sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)            die "unknown flag: $1  (try --help)" ;;
  esac
  shift
done

# -----------------------------------------------------------------------------
# .env
# -----------------------------------------------------------------------------
say "Environment file"
if [ ! -f .env ]; then
  cp .env.example .env
  ok "created .env from .env.example"
  warn "GEMINI_API_KEY is empty — set it before generating traces"
  note "https://aistudio.google.com/apikey  (free tier is plenty)"
else
  ok ".env exists"
fi

# shellcheck disable=SC1091
set -a; . ./.env 2>/dev/null || true; set +a
SIGNOZ_URL="${SIGNOZ_URL:-http://localhost:8080}"

# Resolve an interpreter up front. `--token` runs before the venv section, so it
# needs this too; prefer the venv (it has httpx) and fall back to system python.
PY_BIN="$(command -v python || command -v python3 || true)"
[ -n "$PY_BIN" ] || die "python not found — Python 3.11+ required"
pick_py() {
  if   [ -x .venv/bin/python ];         then printf '%s' .venv/bin/python
  elif [ -x .venv/Scripts/python.exe ]; then printf '%s' .venv/Scripts/python.exe
  else printf '%s' "$PY_BIN"; fi
}

# Rewrites a KEY=value line in .env in place, without printing the value.
# Uses python because sed -i portability across Git Bash / macOS / GNU is a
# reliable source of pain.
env_set() {
  python - "$1" "$2" <<'PY'
import io, os, sys
key, val = sys.argv[1], sys.argv[2]
path = ".env"
lines = io.open(path, encoding="utf-8").read().splitlines() if os.path.exists(path) else []
out, seen = [], False
for line in lines:
    if line.startswith(key + "="):
        out.append(f"{key}={val}"); seen = True
    else:
        out.append(line)
if not seen:
    out.append(f"{key}={val}")
io.open(path, "w", encoding="utf-8").write("\n".join(out) + "\n")
PY
}

# -----------------------------------------------------------------------------
# Token — the step people get stuck on
# -----------------------------------------------------------------------------
mint_token() {
  say "SigNoz editor token"
  note "Funnel WRITES (/api/v1/trace-funnels/new, /steps/update) are gated on"
  note "EditAccess. A SigNoz API key does NOT carry it — you need a login JWT"
  note "from an admin or editor account. That is what this step mints."
  note ""
  note "Credentials are used once, for one POST to"
  note "${SIGNOZ_URL}/api/v2/sessions/email_password, and are never written to"
  note "disk. Only the returned JWT is stored, in .env (gitignored)."

  local email password token py
  printf '  SigNoz email: '
  read -r email
  printf '  SigNoz password: '
  read -rs password
  printf '\n'

  # Delegate to fot.signoz.SigNozClient.login(). It first discovers the orgID
  # that /api/v2/sessions/email_password requires. Do NOT hand-roll this against
  # the legacy /api/v1/login path: that route no longer exists and the SPA
  # answers it with index.html and HTTP 200, so you get no token and no error.
  py="$(pick_py)"
  token="$(SIGNOZ_URL="$SIGNOZ_URL" "$py" - "$email" "$password" <<'PY'
import os, sys
try:
    from fot.signoz import SigNozClient, SigNozError
except ImportError as exc:
    sys.exit(f"cannot import fot.signoz ({exc}); run ./scripts/setup.sh first")
try:
    with SigNozClient(os.environ.get("SIGNOZ_URL", "http://localhost:8080")) as client:
        print(client.login(sys.argv[1], sys.argv[2]))
except SigNozError as exc:
    sys.exit(f"login failed: {exc}")
except Exception as exc:
    sys.exit(f"login failed: {type(exc).__name__}: {exc}")
PY
)"
  unset password

  if [ -z "$token" ]; then
    warn "no token returned — check the email/password and that setup is complete"
    return 1
  fi

  env_set SIGNOZ_JWT "$token"
  export SIGNOZ_JWT="$token"
  ok "editor JWT written to .env (${#token} chars)"
  note "JWTs expire. When funnel calls start 401ing, re-run: ./scripts/setup.sh --token"
}

if [ "$TOKEN_ONLY" -eq 1 ]; then
  mint_token || exit 1
  exit 0
fi

# -----------------------------------------------------------------------------
# Preflight
# -----------------------------------------------------------------------------
say "Preflight"

# PY_BIN was resolved above, before the token step, which also needs it.
PY_VER="$("$PY_BIN" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
case "$PY_VER" in
  3.1[1-9]|3.[2-9]*) ok "python $PY_VER" ;;
  *) die "python $PY_VER found, 3.11+ required" ;;
esac

if command -v docker >/dev/null 2>&1; then ok "docker present"; else warn "docker not on PATH"; fi

VER_JSON="$(curl -s -m 10 "${SIGNOZ_URL}/api/v1/version" 2>/dev/null)"
if printf '%s' "$VER_JSON" | grep -q '"version"'; then
  ok "SigNoz reachable at ${SIGNOZ_URL} — $(printf '%s' "$VER_JSON" | tr -d '{}"' | cut -d, -f1)"
else
  die "SigNoz not reachable at ${SIGNOZ_URL}. Run 'foundryctl cast' first."
fi

# -----------------------------------------------------------------------------
# Virtualenv + deps
# -----------------------------------------------------------------------------
say "Python environment"
if [ ! -d .venv ]; then
  "$PY_BIN" -m venv .venv || die "venv creation failed"
  ok "created .venv"
else
  ok ".venv exists"
fi

if [ -x .venv/bin/python ]; then VENV_PY=.venv/bin/python          # POSIX
elif [ -x .venv/Scripts/python.exe ]; then VENV_PY=.venv/Scripts/python.exe  # Windows
else die "cannot locate the venv interpreter"; fi

"$VENV_PY" -m pip install --quiet --upgrade pip >/dev/null 2>&1
if "$VENV_PY" -m pip install --quiet -e ".[all]"; then
  ok "installed funnel-of-thought[all]"
else
  warn "editable install failed — a sibling package may not be importable yet"
  note "re-run this script once agent/, fot/ and signoz_funnel_mcp/ have __init__.py"
fi

# -----------------------------------------------------------------------------
# Token (unless one is already present)
# -----------------------------------------------------------------------------
if [ -z "${SIGNOZ_JWT:-}" ]; then
  mint_token || warn "continuing without a JWT — funnel creation will fail"
else
  # Cheap liveness probe: a read that requires auth.
  code="$(curl -s -m 10 -o /dev/null -w '%{http_code}' \
          -H "Authorization: Bearer ${SIGNOZ_JWT}" \
          "${SIGNOZ_URL}/api/v1/trace-funnels/list" 2>/dev/null)"
  if [ "$code" = "200" ]; then
    say "SigNoz editor token"; ok "existing SIGNOZ_JWT is valid"
  else
    say "SigNoz editor token"; warn "existing SIGNOZ_JWT returned HTTP $code — re-minting"
    mint_token || warn "continuing without a valid JWT"
  fi
fi

# -----------------------------------------------------------------------------
# Generate traces
# -----------------------------------------------------------------------------
if [ "$SKIP_BATCH" -eq 0 ]; then
  say "Generating traces"

  # No Gemini key is not a blocker. --stub emits the identical span shape with
  # zero LLM calls, so every funnel result below is reproducible offline and in
  # seconds. The only thing you lose is real model latency in the waterfall.
  STUB=""
  if [ -z "${GEMINI_API_KEY:-}" ]; then
    warn "GEMINI_API_KEY is empty — using --stub (no LLM calls, same spans)"
    note "for live Gemini latencies instead: set GEMINI_API_KEY in .env and re-run"
    STUB="--stub"
  else
    note "live Gemini on the free tier: expect 5-15 min at 15 RPM."
    note "to run this offline in seconds instead, unset GEMINI_API_KEY"
  fi

  # Two models on purpose. The GenAI convention puts the model INSIDE the span
  # name, so a funnel keyed on `chat <model>` only ever sees that model's share
  # of traffic -- which is what makes the `genai` funnel read a partial number.
  # Neither model here is FOT_MODEL_SWAP, so the `fragmented` funnel keyed on
  # that bumped version matches zero traces and triggers the NaN 500.
  MODEL_A="${FOT_MODEL:-gemini-3.1-flash-lite}"
  MODEL_B="${FOT_MODEL_B:-gemini-3.1-flash}"
  RUNS_A=$(( BATCH_COUNT * 2 / 3 ))
  RUNS_B=$(( BATCH_COUNT - RUNS_A ))

  # --validate-rate is the INJECTED GROUND TRUTH, pinned so the number in the
  # README reproduces exactly rather than drifting with the default. 0.64 means
  # 64% of runs validate in the correct position; the remaining 36% emit the
  # validate span BEFORE the tool result exists. A presence counter cannot tell
  # those apart -- that gap is the whole finding, and it is measured, not guessed.
  RATE="${FOT_VALIDATE_RATE:-0.64}"
  note "${BATCH_COUNT} traces, validate-rate=${RATE} (injected ground truth)"

  gen_ok=1
  for pair in "$MODEL_A:$RUNS_A" "$MODEL_B:$RUNS_B"; do
    model="${pair%:*}"; runs="${pair##*:}"
    [ "$runs" -gt 0 ] || continue
    note "  ${runs} runs on ${model}"
    # shellcheck disable=SC2086
    "$VENV_PY" -m agent.generate --runs "$runs" --model "$model" \
      --validate-rate "$RATE" --seed "${FOT_SEED:-1337}" $STUB \
      || { gen_ok=0; break; }
  done

  if [ "$gen_ok" -eq 1 ]; then
    ok "batch complete (${BATCH_COUNT} traces across 2 models)"
    note "waiting 15s for the ingester to flush to ClickHouse"
    sleep 15
  else
    die "trace generation failed — nothing below can work without traces"
  fi
else
  say "Generating traces"; note "skipped (--skip-batch)"
fi

# -----------------------------------------------------------------------------
# Apply funnels / dashboard / alert
# -----------------------------------------------------------------------------
say "Applying SigNoz objects"
if "$VENV_PY" -m fot.cli apply; then
  ok "funnels applied"
else
  die "fot.cli apply failed — check the JWT (funnel writes need EditAccess)"
fi

# `apply` only creates funnels. The dashboard and the alert rule are separate
# objects, and BOTH read the metric `fot.funnel.step.conversion` -- which only
# `fot gauges` emits. Miss any of these three and the dashboard renders empty
# panels and the alert never evaluates, which used to be exactly what happened.
if "$VENV_PY" -m fot.cli dashboard apply; then
  ok "dashboard applied"
else
  warn "dashboard apply failed — panels will be missing (funnels still work)"
fi

if "$VENV_PY" -m fot.cli alert apply; then
  ok "alert rule applied"
else
  warn "alert apply failed — the drop-off alert will not fire"
fi

if "$VENV_PY" -m fot.cli gauges all; then
  ok "step conversions published as OTel gauges"
  note "this is what fills the dashboard and arms the alert. Re-run it after any"
  note "new batch, or on a schedule, to turn the funnel into a time series."
else
  warn "gauge emission failed — dashboard/alert will have no data to read"
fi

# -----------------------------------------------------------------------------
# Done
# -----------------------------------------------------------------------------
cat <<EOF

${BOLD}Setup complete.${OFF}

  Next:
    ${BOLD}fot show${OFF}                  per-step conversion, n on every bar
    ${BOLD}fot counter-proof${OFF}         naive counter vs the ordered funnel
    ${BOLD}fot compare${OFF}               stable-name funnel vs the GenAI-keyed one
    ${BOLD}./scripts/reproduce.sh${OFF}    asserts every claim in the README

  In the UI:
    SigNoz            ${SIGNOZ_URL}
    Funnels           ${SIGNOZ_URL}/traces-funnels
    Dashboards        ${SIGNOZ_URL}/dashboard
    Alerts            ${SIGNOZ_URL}/alerts

  MCP:
    SigNoz's own (41 tools)   http://localhost:8000
    ours (5 funnel tools)     signoz-funnel-mcp   — see signoz_funnel_mcp/README.md

  ${DIM}If funnel calls start returning 401, the JWT expired:
  ./scripts/setup.sh --token${OFF}
EOF
