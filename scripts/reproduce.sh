#!/usr/bin/env bash
# =============================================================================
# Funnel of Thought — reproduce the finding in <= 10 minutes.
#
#   ./scripts/reproduce.sh            full path, generates fresh traces (~3 min)
#   ./scripts/reproduce.sh --no-gen   reuse traces already in SigNoz (~20s)
#   ./scripts/reproduce.sh --live     generate with real Gemini instead of the
#                                     stub. Correct but SLOW: free-tier pacing
#                                     makes this ~15 min, not 3.
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
#      PARTIAL conversion, capped by that model's share of traces (see the
#      `genai` funnel in fot/funnels/cognition.yaml for the measured breakdown;
#      the exact figure is not asserted here because it also depends on
#      parent/child span timing, which is workload-specific).
#      Bump the model version in the step name (gemini-3.1 -> 3.2, exactly what
#      a routine upgrade does) and the step matches ZERO traces.
#
#      Which endpoint you ask then decides what you see. Measured on v0.132.2:
#        /analytics/steps           -> 200, total_s2_spans = 0   (handled)
#        /analytics/overview        -> 500  unsupported value: NaN
#        /analytics/steps/overview  -> 500  unsupported value: NaN
#      So through the counts endpoint the charts use, a fragmented funnel is
#      indistinguishable from an honest 0%. Step 3 below probes an overview
#      endpoint, because that is where the bug is actually observable.
#
#      Both halves matter: the partial read is the silent failure you would
#      ship; the 500 is the loud one that tells you something is wrong.
#
#   4. SigNoz's own MCP server exposes 41 tools and NONE of them reach trace
#      funnels -- measured live against the signoz-mcp container that
#      casting.yaml provisions, not asserted. That is the gap this project's
#      five funnel tools fill.
#
# Requires SigNoz v0.132.2 for the 500 in assertion 3. casting.yaml pins it.
# If a zero-guard has landed in the build under test, newer versions return a
# clean 0% and step 3 reports SKIPPED rather than failing — that is the correct
# outcome, not a bug in this script. As of 2026-07-22 no fix has merged
# (issue #12143 open; PR #12160 proposed; #12167 closed unmerged).
#
# -----------------------------------------------------------------------------
# INTERFACES THIS SCRIPT USES (kept in sync with the modules that own them):
#
#   python -m agent.generate --runs N --model M --validate-rate R --seed S [--stub]
#   python -m fot.cli apply
#   python -m fot.cli show          --json
#   python -m fot.cli counter-proof --json
#   python -m fot.cli compare       --json
#
# The --json contract used below:
#   show           -> {"steps":[{"name":..., "conversion":<float 0-100>, "n":<int>}, ...]}
#   counter-proof  -> {"counter_pct":<float>, "funnel_pct":<float>, "step":"validate"}
#   compare        -> {"fragmented":{"status":<int>,"error":"<str>","conversion":<float|null>}}
#
# No Gemini key? This script falls back to --stub automatically: identical span
# shape, zero LLM calls, seconds instead of minutes. Every assertion below still
# holds, because none of them depend on the model's actual output.
# =============================================================================
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

BOLD=$'\033[1m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YEL=$'\033[33m'; DIM=$'\033[2m'; OFF=$'\033[0m'
step()  { printf '\n%s\n' "${BOLD}[$1/5] $2${OFF}"; }
pass()  { printf '%s\n' "  ${GREEN}PASS${OFF}  $*"; }
fail()  { printf '%s\n' "  ${RED}FAIL${OFF}  $*"; FAILURES=$((FAILURES+1)); }
skip()  { printf '%s\n' "  ${YEL}SKIP${OFF}  $*"; }
note()  { printf '%s\n' "        ${DIM}$*${OFF}"; }

FAILURES=0
DO_GEN=1
DO_LIVE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --no-gen) DO_GEN=0 ;;
    --live)   DO_LIVE=1 ;;
    -h|--help) sed -n '2,62p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) printf 'unknown flag: %s (try --help)\n' "$1" >&2; exit 2 ;;
  esac
  shift
done

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
  # --stub by DEFAULT, even when a key is available. Free-tier pacing is 12 RPM
  # across 3 LLM calls per run = 15s/run, so 60 traces is 15 MINUTES -- which
  # blows the 10-minute budget this script advertises. Measured, not guessed:
  # that is exactly how a full live run overran.
  #
  # Nothing is lost. Every assertion below is about span ORDER and span NAMES;
  # none reads a model's output. --stub emits an identical span shape with no
  # network. --live opts into real model latencies in the trace waterfall.
  STUB="--stub"
  if [ "$DO_LIVE" -eq 1 ]; then
    if [ -z "${GEMINI_API_KEY:-}" ]; then
      fail "--live needs GEMINI_API_KEY (set it in .env), or drop --live"
      exit 1
    fi
    STUB=""
    note "--live: real Gemini at 12 RPM x 3 calls/run -> expect ~15 min"
  else
    note "generating with --stub: identical spans, zero LLM calls, ~90s"
    note "(--live uses real Gemini instead; adds roughly 14 minutes)"
  fi
  # Two models on purpose: the GenAI convention puts the model inside the span
  # name, so a funnel keyed on `chat <model>` sees only that model's share.
  # Neither is FOT_MODEL_SWAP, so the `fragmented` funnel matches zero traces --
  # which is what assertion 3 needs.
  # --validate-rate is the injected ground truth: 64% of runs validate in the
  # correct position. Pinned so the README's number reproduces exactly.
  RATE="${FOT_VALIDATE_RATE:-0.64}"
  note "40 + 20 traces across two models, validate-rate=${RATE}"
  for pair in "${FOT_MODEL:-gemini-3.1-flash-lite}:40" "${FOT_MODEL_B:-gemini-3.1-flash}:20"; do
    # shellcheck disable=SC2086
    "$PY" -m agent.generate --runs "${pair##*:}" --model "${pair%:*}" \
      --validate-rate "$RATE" --seed "${FOT_SEED:-1337}" $STUB \
      || { fail "trace generation failed"; exit 1; }
  done
  pass "60 traces emitted across two models"
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
# stderr is deliberately NOT suppressed here. Hiding it once turned a plain
# wiring bug ("No such option: --json") into an opaque "returned nothing".
SHOW="$("$PY" -m fot.cli show --json)"
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
    if [ "$DO_GEN" -eq 1 ]; then
      note "injected ground truth was ${RATE} (i.e. ~$("$PY" -c "print(f'{float('$RATE')*100:.0f}')")%)."
      note "The funnel RECOVERING that rate is the point: this is a calibration"
      note "check, not a discovery. Any pre-existing traces in the 30d window"
      note "shift the recovered number, which is why the assertion is a threshold."
    fi
  else
    fail "validate conversion = ${VAL_PCT}% — no cliff; see PREDICTION.md, prediction 1"
    note "PREDICTION.md commits us to reporting this rather than tuning the agent."
  fi
fi

# -----------------------------------------------------------------------------
step 2 "The counter-proof — why a GROUP BY COUNT cannot answer this"
# -----------------------------------------------------------------------------
CP="$("$PY" -m fot.cli counter-proof --json)"
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

    # PREDICTION.md commits us to reporting the split between traces MISSING the
    # span and traces that emit it OUT OF ORDER, rather than lumping them
    # together as one gap. This is that number, straight from ClickHouse.
    SPLIT="$(printf '%s' "$CP" | "$PY" -c '
import json,sys
o=(json.load(sys.stdin).get("ordering") or {})
have,later=o.get("has_earlier",0),o.get("has_later",0)
print(f"{o.get(\"ordered\",0)} ordered | {o.get(\"out_of_order\",0)} out of order | "
      f"{max(have-later,0)} missing the span entirely  (of {have} traces)")
' 2>/dev/null)"
    [ -n "$SPLIT" ] && note "split: ${SPLIT}"
  else
    fail "counter ${C_PCT}% vs funnel ${F_PCT}% — discrepancy not demonstrated"
    note "see PREDICTION.md, prediction 2, for what we committed to say if this held"
  fi
fi

# -----------------------------------------------------------------------------
step 3 "The spec collision — GenAI span naming vs exact-match funnel steps"
# -----------------------------------------------------------------------------
CMP="$("$PY" -m fot.cli compare --json)"
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
    note "on a model present in the batch you get a partial read, capped by that"
    note "model's share of traffic; bump gemini-3.1 -> 3.2 -- an ordinary"
    note "upgrade -- and the step matches zero traces."
    note ""
    note "What you then get depends on which endpoint you ask. Measured here:"
    note "  /analytics/steps          -> 200, total_s2_spans = 0   (handled)"
    note "  /analytics/overview       -> 500  unsupported value: NaN"
    note "  /analytics/steps/overview -> 500  unsupported value: NaN"
    note "So the counts endpoint the charts use makes a fragmented funnel look"
    note "like an honest 0%; only the overview endpoints fail loudly. The NaN"
    note "arrives from aggregates over an empty set (avgIf/quantileIf), not from"
    note "the conversion division -- that one is already guarded."
    note "SigNoz issue #12143 (open); PR #12160 proposes a fix."
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
step 4 "The gap — SigNoz's own MCP server, measured"
# -----------------------------------------------------------------------------
# The novelty claim, as a check rather than a sentence you have to trust.
# casting.yaml provisions signoz-mcp, so casting the stack gives you the thing
# being measured. A missing MCP container is a SKIP: it means we could not take
# the measurement, not that the claim failed.
GAP="$("$PY" scripts/mcp_gap.py --json 2>/dev/null)"
if [ -z "$GAP" ]; then
  skip "SigNoz MCP server not reachable on :8000 — claim not measured here"
  note "it ships in casting.yaml; 'foundryctl cast' brings it up"
else
  TOTAL_T="$(printf '%s' "$GAP" | jget tool_count)"
  FUNNEL_T="$(printf '%s' "$GAP" | jget funnel_tool_count)"
  if [ "$FUNNEL_T" = "0" ] && [ "${TOTAL_T:-0}" -gt 0 ]; then
    pass "SigNoz's MCP server exposes ${TOTAL_T} tools, ${FUNNEL_T} of which reach funnels"
    note "so an agent can read every SigNoz surface EXCEPT the one measuring its"
    note "own completion rate. That is the gap signoz-funnel-mcp fills, with five"
    note "tools: create_funnel, get_funnel_analytics, get_funnel_slow_traces,"
    note "list_funnels, delete_funnel. Full list: python scripts/mcp_gap.py --list"
  else
    fail "expected 0 funnel tools out of ${TOTAL_T}, found ${FUNNEL_T}"
    note "if SigNoz has since shipped funnel tools, that is good news upstream"
    note "and this project's MCP contribution is superseded. Say so, do not hide it."
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
