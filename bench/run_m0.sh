#!/bin/bash
# One-shot M0 run on the owner's Mac (approved 2026-11-23):
#   setup bench/.venv → fetch tiny+base → small only if the floor check passes → benchmark.
# Free space is measured before setup and every 5 s throughout (free_space.csv). If free
# space ever drops below the 60 GiB floor, the watchdog stops the whole run (hard limit).
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${RIFTWIRE_BENCH_OUT:-$ROOT/../riftwire-env/bench-$STAMP}"
mkdir -p "$OUT"
FLOOR_GIB=60
# The floor can only be raised (e.g. for testing the watchdog), never lowered.
[ "${RIFTWIRE_BENCH_FLOOR_GIB:-60}" -gt 60 ] 2>/dev/null && FLOOR_GIB=$RIFTWIRE_BENCH_FLOOR_GIB
FLOOR_KB=$((FLOOR_GIB * 1024 * 1024))
PY="$ROOT/bench/.venv/bin/python"
DATA="${RIFTWIRE_DATA_DIR:-$ROOT/data}"; mkdir -p "$DATA"

free_kb() {
  # Test hook only: simulate free space from a file (used to test the watchdog).
  if [ -n "${RIFTWIRE_BENCH_TEST_FREE_KB_FILE:-}" ]; then cat "$RIFTWIRE_BENCH_TEST_FREE_KB_FILE"
  else df -k "$DATA" | awk 'NR==2{print $4}'; fi
}
log() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$OUT/console.log"; }

echo "utc,free_kb,free_gib,phase" > "$OUT/free_space.csv"
echo "before" > "$OUT/.phase"
sample() { printf '%s,%s,%s,%s\n' "$(date -u +%FT%TZ)" "$1" "$(awk -v k="$1" 'BEGIN{printf "%.2f",k/1048576}')" "$(cat "$OUT/.phase")" >> "$OUT/free_space.csv"; }

MAIN=$$
killtree() { for c in $(pgrep -P "$1"); do killtree "$c"; done; kill -TERM "$1" 2>/dev/null; }
(
  SELF=$(sh -c 'echo $PPID')   # this watchdog's pid (macOS bash 3.2 has no $BASHPID)
  while kill -0 "$MAIN" 2>/dev/null; do
    f=$(free_kb); sample "$f"
    if [ "$f" -lt "$FLOOR_KB" ] && [ ! -e "$OUT/ABORTED_FLOOR" ]; then
      echo "!!! FLOOR BREACH: $((f/1048576)) GiB free < ${FLOOR_GIB} GiB — stopping run" \
        | tee -a "$OUT/console.log"
      touch "$OUT/ABORTED_FLOOR"
    fi
    if [ -e "$OUT/ABORTED_FLOOR" ]; then   # keep stopping every child until the run ends
      for c in $(pgrep -P "$MAIN"); do [ "$c" != "$SELF" ] && killtree "$c"; done
      sleep 1; continue
    fi
    sleep 5
  done
) &
WATCH=$!
trap 'kill $WATCH 2>/dev/null; [ -e "$OUT/ABORTED_FLOOR" ] && echo "✗ RUN STOPPED: free space fell below the ${FLOOR_GIB} GiB floor (see $OUT/free_space.csv)"' EXIT

phase() {
  [ -e "$OUT/ABORTED_FLOOR" ] && { log "✗ aborted: free space fell below ${FLOOR_GIB} GiB"; exit 3; }
  echo "$1" > "$OUT/.phase"; sample "$(free_kb)"; log "▶ $1 (free $(( $(free_kb) / 1048576 )) GiB)"
}

phase "before-setup"
phase "setup"
bench/setup_bench.sh 2>&1 | tee -a "$OUT/console.log"; [ "${PIPESTATUS[0]}" -eq 0 ] || { log "✗ setup failed"; exit 1; }
phase "plan"
"$PY" bench/run.py plan 2>&1 | tee -a "$OUT/console.log"
phase "fetch tiny,base"
"$PY" bench/run.py fetch-models --models tiny,base --confirm 2>&1 | tee -a "$OUT/console.log"
[ "${PIPESTATUS[0]}" -eq 0 ] || { log "✗ tiny/base download failed"; exit 1; }
MODELS="tiny,base"
phase "fetch small (only if floor stays satisfied)"
if "$PY" bench/run.py fetch-models --models small --confirm 2>&1 | tee -a "$OUT/console.log"; \
   [ "${PIPESTATUS[0]}" -eq 0 ]; then MODELS="tiny,base,small"; else log "small skipped (floor)"; fi
phase "benchmark ($MODELS)"
"$PY" bench/run.py all --models "$MODELS" --out "$OUT" 2>&1 | tee -a "$OUT/console.log"
RC=${PIPESTATUS[0]}
phase "after"
MIN=$(awk -F, 'NR>1{print $3}' "$OUT/free_space.csv" | sort -n | head -1)
log "Minimum free space observed: ${MIN} GiB (floor ${FLOOR_GIB} GiB). Samples: $(($(wc -l < "$OUT/free_space.csv")-1))"
log "Results: $OUT/results.md   (exit $RC)"
exit "$RC"
