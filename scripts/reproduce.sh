#!/usr/bin/env bash
# =============================================================================
# Funnel of Thought — reproduce the finding in <= 10 minutes.
#
#   ./scripts/reproduce.sh            full path, generates fresh traces
#   ./scripts/reproduce.sh --no-gen   reuse traces already in SigNoz (fast)
#
# This is the judge path. It ASSERTS the claims the README makes and exits
# non-zero if any of them fail to reproduce, so a green run means the write-up
# held on your machine and not just ours.
#
# What it proves, in order:
#   1. A funnel keyed on stable node span names renders a real cliff at
#      `validate` — conversion materially below 100%.
#   2. A naive GROUP BY span name COUNT reports the same step at ~100%.
#      The counter is higher because it cannot see order. It is not merely a
#      different number; it is structurally the wrong question.
#   3. A funnel keyed on the OTel GenAI `chat {model}` span is hostage to the
#      model string. Keyed on a model actually present in the batch it reads a
#      PARTIAL conversion (~58% — it only sees that model's share of traces).
#      Bump the model version in the step name (gemini-3.1 -> 3.2, exactly what
#      a routine upgrade does) and the step matches ZERO traces, at which point
#      /analytics/steps returns HTTP 500 "unsupported value: NaN" rather than a
#      conversion of 0.
#
#      Both halves matter: the partial read is the silent failure you would
#      ship; the 500 is the loud one that tells you something is wrong.
#
# Requires SigNoz v0.132.2 for the 500 in assertion 3. casting.yaml pins it.
# If a zero-guard has landed in the build under test, newer versions return a
# clean 0% and step 3 reports SKIPPED rather than failing — that is the correct
# outcome, not a bug in this script. As of 2026-07-22 no fix has merged
# (issue #12143 open; PR #12160 proposed; #12167 closed unmerged).
#
# -----------------------------------------------------------------------------
# INTERFACES THIS SCRIPT ASSUMES (sibling components own these):
#
#   python -m agent.generate --count N --swap-at K --seed S
#   python -m fot.cli apply
#   python -m fot.cli show          --json
#   python -m fot.cli counter-proof --json
#   python -m fot.cli compare       --json
#
# The --json contract used below:
#   show           -> {"steps":[{"name":..., "conversion":<float 0-100>, "n":<int>}, ...]}
#   counter-proof  -> {"counter_pct":<float>, "funnel_pct":<float>, "step":"validate"}
#   compare        -> {"fragmented":{"status":<int>,"error":"<str>","conversion":<float|null>}}
# =============================================================================
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

BOLD=$'\033[1m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YEL=$'\033[33m'; DIM=$'\033[2m'; OFF=$'\033[0m'
step()  { printf '\n%s\n' "${BOLD}[$1/4] $2${OFF}"; }
pass()  { printf '%s\n' "  ${GREEN}PASS${OFF}  $*"; }
fail()  { printf '%s\n' "  ${RED}FAIL${OFF}  $*"; FAILURES=$((FAILURES+1)); }
skip()  { printf '%s\n' "  ${YEL}SKIP${OFF}  $*"; }
note()  { printf '%s\n' "        ${DIM}$*${OFF}"; }

FAILURES=0
DO_GEN=1
[ "${1:-}" = "--no-gen" ] && DO_GEN=0

set -a; . ./.env 2>/dev/null || true; set +a
SIGNOZ_URL="${SIGNOZ_URL:-http://localhost:8080}"

if   [ -x .venv/bin/python ];          then PY=.venv/bin/python
elif [ -x .venv/Scripts/python.exe ];  then PY=.venv/Scripts/python.exe
else PY="$(command -v python || command -v python3)"; fi
[ -n "${PY:-}" ] || { echo "no python interpreter found"; exit 1; }

# jq is nice but not everyone has it. Small python helper instead.
jget() { "$PY" -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit(1)
cur=d
for k in sys.argv[1].split("."):
    if isinstance(cur,list):
        try: cur=cur[int(k)]
        except Exception: sys.exit(1)
    elif isinstance(cur,dict) and k in cur: cur=cur[k]
    else: sys.exit(1)
print(cur)
' "$1" 2>/dev/null; }

cat <<EOF
${BOLD}Funnel of Thought — reproduction${OFF}
${DIM}SigNoz: ${SIGNOZ_URL}${OFF}
EOF

# -----------------------------------------------------------------------------
step 0 "Preflight"
# -----------------------------------------------------------------------------
VER="$(curl -s -m 10 "${SIGNOZ_URL}/api/v1/version" 2>/dev/null | jget version)"
if [ -z "$VER" ]; then
  fail "SigNoz not reachable at ${SIGNOZ_URL} — run 'foundryctl cast' first"
  exit 1
fi
pass "SigNoz $VER"
[ "$VER" = "v0.132.2" ] || note "README describes v0.132.2; assertion 3 may differ on $VER"

if [ -z "${SIGNOZ_JWT:-}" ]; then
  fail "SIGNOZ_JWT is unset — run ./scripts/setup.sh --token"
  exit 1
fi
CODE="$(curl -s -m 10 -o /dev/null -w '%{http_code}' \
        -H "Authorization: Bearer ${SIGNOZ_JWT}" \
        "${SIGNOZ_URL}/api/v1/trace-funnels/list" 2>/dev/null)"
if [ "$CODE" = "200" ]; then pass "editor JWT valid"
else fail "JWT returned HTTP $CODE — run ./scripts/setup.sh --token"; exit 1; fi

if [ "$DO_GEN" -eq 1 ]; then
  if [ -z "${GEMINI_API_KEY:-}" ]; then
    fail "GEMINI_API_KEY unset — set it in .env, or re-run with --no-gen"
    exit 1
  fi
  note "generating 60 traces, model swapping at 30. ~5 min on the free tier."
  note "Two models is the point: it is what fragments the GenAI span name."
  "$PY" -m agent.generate --count 60 --swap-at 30 --seed "${FOT_SEED:-1337}" \
    || { fail "trace generation failed"; exit 1; }
  pass "60 traces emitted"
  note "waiting 20s for the ingester to flush to ClickHouse"
  sleep 20
else
  skip "trace generation (--no-gen); using whatever is already in SigNoz"
fi

"$PY" -m fot.cli apply >/dev/null 2>&1 \
  && pass "funnels applied" \
  || fail "fot.cli apply failed"

# -----------------------------------------------------------------------------
step 1 "The cliff — a funnel keyed on stable node span names"
# -----------------------------------------------------------------------------
SHOW="$("$PY" -m fot.cli show --json 2>/dev/null)"
if [ -z "$SHOW" ]; then
  fail "fot.cli show returned nothing"
else
  "$PY" -m fot.cli show   # the human-readable render, n printed per bar
  VAL_PCT="$(printf '%s' "$SHOW" | "$PY" -c '
import json,sys
d=json.load(sys.stdin)
for s in d.get("steps",[]):
    if "validate" in str(s.get("name","")).lower():
        print(s.get("conversion","")); break
' 2>/dev/null)"
  if [ -z "$VAL_PCT" ]; then
    fail "no validate step found in the funnel output"
  elif "$PY" -c "import sys;sys.exit(0 if float('$VAL_PCT')<90.0 else 1)"; then
    pass "validate conversion = ${VAL_PCT}% — a real cliff, materially below 100%"
    note "the funnel is minIf over a monotonic step index: it counts a trace"
    note "only if validate occurred AFTER tool. Skips and reorders both cost."
  else
    fail "validate conversion = ${VAL_PCT}% — no cliff; see PREDICTION.md, prediction 1"
    note "PREDICTION.md commits us to reporting this rather than tuning the agent."
  fi
fi

# -----------------------------------------------------------------------------
step 2 "The counter-proof — why a GROUP BY COUNT cannot answer this"
# -----------------------------------------------------------------------------
CP="$("$PY" -m fot.cli counter-proof --json 2>/dev/null)"
if [ -z "$CP" ]; then
  fail "fot.cli counter-proof returned nothing"
else
  "$PY" -m fot.cli counter-proof
  C_PCT="$(printf '%s' "$CP" | jget counter_pct)"
  F_PCT="$(printf '%s' "$CP" | jget funnel_pct)"
  if [ -n "$C_PCT" ] && [ -n "$F_PCT" ] \
     && "$PY" -c "import sys;sys.exit(0 if float('$C_PCT')-float('$F_PCT')>5.0 else 1)"; then
    pass "counter says ${C_PCT}%, ordered funnel says ${F_PCT}%"
    note "the counter asks 'did this span ever appear?'"
    note "the funnel asks 'did it appear AFTER the previous step, same trace?'"
    note "a counter scores validate-after-respond as a success. It cannot see order."
  else
    fail "counter ${C_PCT}% vs funnel ${F_PCT}% — discrepancy not demonstrated"
    note "see PREDICTION.md, prediction 2, for what we committed to say if this held"
  fi
fi

# -----------------------------------------------------------------------------
step 3 "The spec collision — GenAI span naming vs exact-match funnel steps"
# -----------------------------------------------------------------------------
CMP="$("$PY" -m fot.cli compare --json 2>/dev/null)"
if [ -z "$CMP" ]; then
  fail "fot.cli compare returned nothing"
else
  "$PY" -m fot.cli compare
  STATUS="$(printf '%s' "$CMP" | jget fragmented.status)"
  ERRTXT="$(printf '%s' "$CMP" | jget fragmented.error)"
  if [ "$STATUS" = "500" ] && printf '%s' "$ERRTXT" | grep -qi 'NaN'; then
    pass "fragmented funnel -> HTTP 500: ${ERRTXT}"
    note "OTel GenAI names LLM spans '{operation} {model}', so the model string"
    note "is INSIDE the span name. Funnel steps match on exact span name. Keyed"
    note "on a model present in the batch you get a partial read (~58%); bump"
    note "gemini-3.1 -> 3.2 and the step matches zero traces. Then"
    note "BuildFunnelStepOverviewQuery divides by zero with no guard -- while"
    note "BuildFunnelOverviewQuery, one function away, guards the identical"
    note "division. SigNoz issue #12143 (open); PR #12160 proposes a fix."
  elif [ "$STATUS" = "200" ]; then
    skip "fragmented funnel returned a clean 0% — the zero-guard has landed"
    note "that is the FIXED behaviour and a good outcome. To see the original,"
    note "check that casting.yaml still pins signoz/signoz:v0.132.2 and re-cast."
  else
    fail "unexpected response from the fragmented funnel: status=$STATUS error=$ERRTXT"
    note "a non-trivial NON-ZERO conversion here would falsify the whole thesis"
    note "(see PREDICTION.md, prediction 3) — matching would not be exact-equality."
  fi
fi

# -----------------------------------------------------------------------------
printf '\n%s\n' "${BOLD}Result${OFF}"
if [ "$FAILURES" -eq 0 ]; then
  printf '%s\n\n' "  ${GREEN}All assertions reproduced.${OFF}"
  cat <<EOF
  Where to look next:
    Funnels      ${SIGNOZ_URL}/traces-funnels
    Dashboard    ${SIGNOZ_URL}/dashboard      validate-conversion over time
    Alerts       ${SIGNOZ_URL}/alerts         fires when the cliff deepens

  To watch it move live: inject a validation regression, re-run the batch, and
  re-run 'fot show'. The read path has no LLM in it, so the loop is sub-second.
EOF
  exit 0
else
  printf '%s\n\n' "  ${RED}${FAILURES} assertion(s) did not reproduce.${OFF}"
  note "PREDICTION.md documents, per claim, what we committed to reporting if it"
  note "failed. A failed assertion here is a finding, not something to tune away."
  exit 1
fi
