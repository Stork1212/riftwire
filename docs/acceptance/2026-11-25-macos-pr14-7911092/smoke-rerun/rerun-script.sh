#!/bin/bash
# RIFTWIRE PR #14: re-run the smoke test at 7911092 after a port conflict.
# Run:   bash ~/Claude/pr14-smoke-7911092.sh
#
# The 15:05Z verification of 7911092 passed everything except smoke, which never started:
# port 18930 was still held by the smoke instance from the 04:43Z run (smoke_test.sh leaves
# its isolated instance running for a dashboard check, and the earlier script did not stop
# it). This script:
#   1. lists the leftover RIFTWIRE smoke instances (isolated data folders under
#      ~/Claude/riftwire-env/smoke-data-*) that are still running, and stops them with
#      scripts/stop.sh. Nothing else is touched: not the live demo, not PR #13, not the SSD.
#      If port 18930 is held by anything that is NOT one of those instances, it stops and
#      says what it is.
#   2. runs scripts/smoke_test.sh at 7911092 (real 60 GiB floor, isolated data, port 18930),
#   3. stops the new smoke instance afterwards.
set -u -o pipefail
EXPECTED=7911092c897d07aadedc6dd4ce3616142d154341
REPO="$HOME/Claude/riftwire"
ENVDIR="$HOME/Claude/riftwire-env"
PORT=18930
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT="$ENVDIR/pr14-smoke-7911092-$STAMP"
mkdir -p "$OUT"
SUM="$OUT/summary.txt"
say() { echo "$@" | tee -a "$SUM"; }
abort() { say "ABORT: $*"; exit 2; }

LOCK="$ENVDIR/.verify.lock"
mkdir "$LOCK" 2>/dev/null || { say "ABORT: another verification run is in progress ($LOCK exists)."; exit 3; }
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

cd "$REPO" || abort "repo not found at $REPO"
[ -z "$(git status --porcelain --untracked-files=normal)" ] || abort "working tree not clean"
git checkout -q phase2/integration || abort "checkout failed"
[ "$(git rev-parse HEAD)" = "$EXPECTED" ] || abort "HEAD is $(git rev-parse HEAD), expected $EXPECTED"
say "PR #14 smoke re-run  started $(date -u +%Y-%m-%dT%H:%M:%SZ)  HEAD $EXPECTED"

# ------------------------------------------------------------------ leftover smoke instances
alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }
STOPPED=0
for run in "$ENVDIR"/smoke-data-*/run; do
  [ -d "$run" ] || continue
  data="${run%/run}"
  live=""
  for f in "$run"/*.pid; do
    [ -f "$f" ] || continue
    pid=$(tr -dc '0-9' < "$f")
    alive "$pid" && live="$live $(basename "$f" .pid):$pid"
  done
  [ -n "$live" ] || continue
  say "leftover smoke instance: $data ($live )"
  RIFTWIRE_DATA_DIR="$data" scripts/stop.sh >> "$OUT/stop-leftovers.log" 2>&1 \
    && { say "   stopped"; STOPPED=$((STOPPED + 1)); } || say "   WARNING: stop.sh failed (see stop-leftovers.log)"
done
say "leftover smoke instances stopped: $STOPPED"
sleep 1
if lsof -nP -iTCP:$PORT -sTCP:LISTEN > "$OUT/port-$PORT.txt" 2>&1; then
  say "port $PORT is still in use by something that is not a RIFTWIRE smoke instance:"
  sed 's/^/   /' "$OUT/port-$PORT.txt" | tee -a "$SUM"
  abort "not stopping an unknown process; free port $PORT and run again"
fi
say "port $PORT is free"

# ------------------------------------------------------------------ smoke
say ""; say "== smoke (real 60 GiB floor, isolated data, port $PORT)"
RIFTWIRE_SMOKE_PORT=$PORT scripts/smoke_test.sh "$OUT/smoke-report.txt" > "$OUT/smoke.log" 2>&1
RC=$?
grep -E "^\s*(PASS|FAIL|SKIP)" "$OUT/smoke.log" | sed 's/^/   /' | tee -a "$SUM" >/dev/null
P=$(grep -cE "^\s*PASS" "$OUT/smoke.log"); F=$(grep -cE "^\s*FAIL" "$OUT/smoke.log"); S=$(grep -cE "^\s*SKIP" "$OUT/smoke.log")
say "smoke exit $RC: PASS $P  FAIL $F  SKIP $S"

NEW=$(grep -o "RIFTWIRE_DATA_DIR='[^']*'" "$OUT/smoke.log" | tail -1 | sed "s/RIFTWIRE_DATA_DIR='//; s/'$//")
if [ -n "$NEW" ]; then
  RIFTWIRE_DATA_DIR="$NEW" RIFTWIRE_API_PORT=$PORT scripts/stop.sh >> "$OUT/stop-new.log" 2>&1
  lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1 && say "WARNING: port $PORT still open" || say "smoke instance stopped (port $PORT closed)"
fi
[ -z "$(git status --porcelain --untracked-files=normal)" ] && say "working tree after: clean" || say "WARNING: working tree changed"
say ""
if [ "$RC" -eq 0 ] && [ "$F" -eq 0 ]; then say "SMOKE PASSED at 7911092"; else say "SMOKE FAILED (see smoke.log)"; fi
