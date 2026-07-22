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
# INTERFACES THIS SCRIPT ASSUMES (sibling components own these):
#
#   python -m agent.generate --count N --swap-at K --seed S
#   python -m fot.cli apply [--only funnels|dashboard|alert]
#   python -m fot.cli show
#
# If one of those isn't wired up yet, the step reports which one and the script
# keeps going where it safely can, so a partial repo still bootstraps.
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
  note "Credentials are used once, for one POST to ${SIGNOZ_URL}/api/v1/login,"
  note "and are never written to disk. Only the returned JWT is stored, in"
  note ".env (gitignored)."

  local email password resp token
  printf '  SigNoz email: '
  read -r email
  printf '  SigNoz password: '
  read -rs password
  printf '\n'

  resp="$(curl -s -m 20 -X POST "${SIGNOZ_URL}/api/v1/login" \
            -H 'Content-Type: application/json' \
            --data-binary "$(python -c 'import json,sys;print(json.dumps({"email":sys.argv[1],"password":sys.argv[2]}))' "$email" "$password")" \
          2>/dev/null)"
  unset password

  token="$(printf '%s' "$resp" | python -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
# SigNoz has moved this field around between versions; try the known shapes.
for path in (("data","accessToken"), ("accessToken",), ("data","access_token")):
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            cur = None; break
        cur = cur[k]
    if isinstance(cur, str) and cur:
        print(cur); break
' 2>/dev/null)"

  if [ -z "$token" ]; then
    warn "no token returned — check the email/password and that setup is complete"
    note "raw response: $(printf '%s' "$resp" | head -c 200)"
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

command -v python >/dev/null 2>&1 || command -v python3 >/dev/null 2>&1 \
  || die "python not found — Python 3.11+ required"
PY_BIN="$(command -v python || command -v python3)"
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
  say "Generating traces (the only step that calls an LLM)"
  if [ -z "${GEMINI_API_KEY:-}" ]; then
    warn "GEMINI_API_KEY is empty — skipping batch generation"
    note "set it in .env, then: ./scripts/setup.sh --skip-batch=false"
  else
    note "~${BATCH_COUNT} traces, offline, free-tier Gemini. Expect 5-15 min at 15 RPM."
    note "The model swaps halfway through — that is what fragments the LLM span name."
    if "$VENV_PY" -m agent.generate --count "$BATCH_COUNT" \
         --swap-at "$(( BATCH_COUNT / 2 ))" --seed "${FOT_SEED:-1337}"; then
      ok "batch complete"
      note "waiting 15s for the ingester to flush to ClickHouse"
      sleep 15
    else
      warn "batch generation failed or agent.generate is not available yet"
    fi
  fi
else
  say "Generating traces"; note "skipped (--skip-batch)"
fi

# -----------------------------------------------------------------------------
# Apply funnels / dashboard / alert
# -----------------------------------------------------------------------------
say "Applying SigNoz objects"
if "$VENV_PY" -m fot.cli apply; then
  ok "funnels, dashboard and alert applied"
else
  warn "fot.cli apply failed or is not available yet"
  note "once fot/ is wired up:  fot apply"
fi

# -----------------------------------------------------------------------------
# Done
# -----------------------------------------------------------------------------
cat <<EOF

${BOLD}Setup complete.${OFF}

  Next:
    ${BOLD}fot show${OFF}                  per-step conversion, n on every bar
    ${BOLD}fot counter-proof${OFF}         naive counter vs the ordered funnel
    ${BOLD}fot compare${OFF}               working funnel vs the fragmented one
    ${BOLD}./scripts/reproduce.sh${OFF}    the full 10-minute reproduction

  In the UI:
    SigNoz            ${SIGNOZ_URL}
    Funnels           ${SIGNOZ_URL}/traces-funnels
    Dashboards        ${SIGNOZ_URL}/dashboard
    Alerts            ${SIGNOZ_URL}/alerts

  MCP:
    SigNoz's own (41 tools)   http://localhost:8000
    ours (4 funnel tools)     signoz-funnel-mcp   — see README, "Using the MCP server"

  ${DIM}If funnel calls start returning 401, the JWT expired:
  ./scripts/setup.sh --token${OFF}
EOF
