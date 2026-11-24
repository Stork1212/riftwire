#!/bin/bash
# One-shot M0 run on the owner's Mac (approved 2026-11-23; launcher corrections 2026-11-23).
#
#   plan (refuse on floor/budget) → setup bench/.venv → fetch tiny+base
#   → small only if the floor allows it → benchmark → verify the git tree is unchanged.
#
# Safety:
# * Every step's exit status is checked; any failure stops the run. The ONLY tolerated
#   failure is "small skipped because of the free-space floor" (exit code 10 from run.py).
# * Free space is sampled every 5 s on EVERY volume the run writes to (repo/venv, data dir,
#   TMPDIR, HF_HOME, results). If any falls below the 60 GiB floor, all child processes are
#   stopped.
# * The run refuses to start in a git tree with uncommitted or untracked (non-ignored) changes,
#   and checks after the run that HEAD and the tree are unchanged. Use a dedicated worktree.
# * Total footprint (on disk + this run's PEAK) must be ≤ RIFTWIRE_BENCH_BUDGET_GB (default 3.5,
#   owner-approved 2026-11-24); the floor checks are unchanged. The run refuses otherwise.
#
# Resume only the signals step of an earlier run (same safety checks, no downloads):
#   bench/run_m0.sh --signals-only ~/Claude/riftwire-env/bench-<UTC>
#
# Exit codes: 0 ok · 1 step failed · 3 floor breach during run · 4 git tree not clean/changed
#             10 refused: free-space floor · 11 refused: over approved disk budget
set -uo pipefail
SIGNALS_ONLY=""
if [ "${1:-}" = "--signals-only" ]; then
  [ -f "${2:-}/results.json" ] || { echo "✗ --signals-only needs an earlier results dir"; exit 1; }
  SIGNALS_ONLY="$(cd "$2" && pwd -P)"
fi
cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd -P)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${SIGNALS_ONLY:-${RIFTWIRE_BENCH_OUT:-$ROOT/../riftwire-env/bench-$STAMP}}"
mkdir -p "$OUT" && OUT="$(cd "$OUT" && pwd -P)"
BUDGET_GB="${RIFTWIRE_BENCH_BUDGET_GB:-3.5}"   # owner-approved 2026-11-24
FLOOR_GIB=60
# The floor can only be raised (e.g. to test the watchdog), never lowered.
[ "${RIFTWIRE_BENCH_FLOOR_GIB:-60}" -gt 60 ] 2>/dev/null && FLOOR_GIB=$RIFTWIRE_BENCH_FLOOR_GIB
FLOOR_KB=$((FLOOR_GIB * 1024 * 1024))

# Everything the run writes stays in known, monitored places.
export RIFTWIRE_DATA_DIR="${RIFTWIRE_DATA_DIR:-$ROOT/data}"
DATA="$RIFTWIRE_DATA_DIR"
export TMPDIR="$DATA/bench/tmp" HF_HOME="$DATA/bench/hf-home"
export HF_HUB_DISABLE_XET=1 HF_XET_CHUNK_CACHE_SIZE_BYTES=0 HF_HUB_DISABLE_TELEMETRY=1
export PIP_NO_CACHE_DIR=1
mkdir -p "$DATA" "$TMPDIR" "$HF_HOME"
VENV_PY="$ROOT/bench/.venv/bin/python"

log() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$OUT/console.log"; }
die() { log "✗ $2"; exit "$1"; }

# ---- git safety -------------------------------------------------------------------------
git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1 || die 4 "not a git checkout"
HEAD0="$(git -C "$ROOT" rev-parse HEAD)"
DIRTY="$(git -C "$ROOT" status --porcelain --untracked-files=normal)"
[ -z "$DIRTY" ] || { echo "$DIRTY"; die 4 "uncommitted/untracked changes in $ROOT — refusing. Use a dedicated worktree (see bench/README.md)."; }
log "git: $(git -C "$ROOT" rev-parse --abbrev-ref HEAD) @ ${HEAD0:0:12} (clean)"

# ---- volumes to watch ------------------------------------------------------------------
mount_of() { df -P "$1" | awk 'NR==2{print $6}'; }
VOLS=""
for p in "$ROOT" "$DATA" "$TMPDIR" "$HF_HOME" "$OUT"; do
  m="$(mount_of "$p")"
  case " $VOLS " in *" $m "*) ;; *) VOLS="$VOLS $m" ;; esac
done
free_kb() {
  # Test hook only: a file per volume (mount path with / → _) simulates its free space.
  local d="${RIFTWIRE_BENCH_TEST_FREE_DIR:-}" hook
  hook="$d/$(echo "$1" | tr / _)"
  if [ -n "$d" ] && [ -f "$hook" ]; then cat "$hook"
  elif [ -n "$d" ] && [ -f "$d/_default" ]; then cat "$d/_default"
  else df -Pk "$1" | awk 'NR==2{print $4}'; fi
}
log "watching volumes:$VOLS (floor ${FLOOR_GIB} GiB each)"

[ -f "$OUT/free_space.csv" ] || echo "utc,volume,free_kb,free_gib,phase" > "$OUT/free_space.csv"
echo "before" > "$OUT/.phase"
sample_all() {
  for m in $VOLS; do
    f=$(free_kb "$m")
    printf '%s,%s,%s,%s,%s\n' "$(date -u +%FT%TZ)" "$m" "$f" \
      "$(awk -v k="$f" 'BEGIN{printf "%.2f",k/1048576}')" "$(cat "$OUT/.phase")" \
      >> "$OUT/free_space.csv"
    if [ "$f" -lt "$FLOOR_KB" ] && [ ! -e "$OUT/ABORTED_FLOOR" ]; then
      echo "!!! FLOOR BREACH on $m: $((f/1048576)) GiB free < ${FLOOR_GIB} GiB — stopping run" \
        | tee -a "$OUT/console.log"
      touch "$OUT/ABORTED_FLOOR"
    fi
  done
}

MAIN=$$
killtree() { for c in $(pgrep -P "$1"); do killtree "$c"; done; kill -TERM "$1" 2>/dev/null; }
(
  SELF=$(sh -c 'echo $PPID')   # this watchdog's pid (macOS bash 3.2 has no $BASHPID)
  while kill -0 "$MAIN" 2>/dev/null; do
    sample_all
    if [ -e "$OUT/ABORTED_FLOOR" ]; then   # keep stopping every child until the run ends
      for c in $(pgrep -P "$MAIN"); do [ "$c" != "$SELF" ] && killtree "$c"; done
      sleep 1; continue
    fi
    sleep 5
  done
) &
WATCH=$!
trap 'killtree $WATCH 2>/dev/null; [ -e "$OUT/ABORTED_FLOOR" ] && echo "✗ RUN STOPPED: free space fell below the ${FLOOR_GIB} GiB floor (see $OUT/free_space.csv)"' EXIT

phase() {
  [ -e "$OUT/ABORTED_FLOOR" ] && die 3 "aborted: free space fell below ${FLOOR_GIB} GiB"
  echo "$1" > "$OUT/.phase"; sample_all; log "▶ $1"
}
# run_step CMD... : run with output teed to the log; returns the COMMAND's exit status.
run_step() { "$@" 2>&1 | tee -a "$OUT/console.log"; return "${PIPESTATUS[0]}"; }
after_step() {  # after_step RC WHAT
  [ -e "$OUT/ABORTED_FLOOR" ] && die 3 "aborted during $2: free space fell below ${FLOOR_GIB} GiB"
  [ "$1" -eq 0 ] && return 0
  [ "$1" -eq 10 ] && die 10 "$2 refused by the free-space floor (exit 10)"
  die 1 "$2 failed (exit $1)"
}

# ---- interpreter for the plan (stdlib only, runs before the venv exists) ---------------
PY313="${RIFTWIRE_PYTHON:-}"
for c in "$PY313" python3.13 /Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13; do
  [ -n "$c" ] && command -v "$c" >/dev/null 2>&1 && { PY313="$(command -v "$c")"; break; }
done
[ -n "$PY313" ] || die 1 "python3.13 not found (set RIFTWIRE_PYTHON)"

# ---- resume: signals only -----------------------------------------------------------------
if [ -n "$SIGNALS_ONLY" ]; then
  [ -x "$VENV_PY" ] || die 1 "bench/.venv missing; run the full benchmark first"
  phase "plan (signals only; budget ${BUDGET_GB} GB)"
  run_step "$PY313" bench/run.py plan --signals-only --models tiny --out "$OUT" \
    --budget-gb "$BUDGET_GB"
  after_step $? "signals plan"
  phase "signals (resume into $OUT)"
  run_step "$VENV_PY" bench/run.py all --resume --skip-transcription --out "$OUT"
  after_step $? "signals benchmark"
  phase "after"
  [ "$(git -C "$ROOT" rev-parse HEAD)" = "$HEAD0" ] || die 4 "HEAD changed during the run"
  [ -z "$(git -C "$ROOT" status --porcelain --untracked-files=normal)" ] \
    || die 4 "git tree changed during the run"
  log "git tree unchanged · results: $OUT/results.md"
  exit 0
fi

# ---- 1. plan: per-volume PEAK estimate vs floor and approved budget -----------------------
phase "plan (tiny,base,small; budget ${BUDGET_GB} GB)"
MODELS="tiny,base,small"
run_step "$PY313" bench/run.py plan --models "$MODELS" --out "$OUT" --budget-gb "$BUDGET_GB"
RC=$?
if [ "$RC" -eq 10 ] || [ "$RC" -eq 11 ]; then
  log "full plan refused (exit $RC); re-planning without small"
  MODELS="tiny,base"
  run_step "$PY313" bench/run.py plan --models "$MODELS" --out "$OUT" --budget-gb "$BUDGET_GB"
  RC=$?
  [ "$RC" -eq 11 ] && die 11 "estimated peak exceeds the approved ${BUDGET_GB} GB budget even without small — owner approval needed (RIFTWIRE_BENCH_BUDGET_GB)"
  [ "$RC" -eq 10 ] && die 10 "not enough free space for tiny+base above the ${FLOOR_GIB} GiB floor"
fi
[ "$RC" -eq 0 ] || die 1 "plan failed (exit $RC)"
log "planned models: $MODELS"

# ---- 2. setup ----------------------------------------------------------------------------
phase "setup bench/.venv"
run_step bench/setup_bench.sh; after_step $? "setup"

# ---- 3. models -----------------------------------------------------------------------------
phase "fetch tiny,base"
run_step "$VENV_PY" bench/run.py fetch-models --models tiny,base --confirm
after_step $? "download of tiny/base"
case "$MODELS" in
  *small*)
    phase "fetch small"
    run_step "$VENV_PY" bench/run.py fetch-models --models small --confirm
    RC=$?
    [ -e "$OUT/ABORTED_FLOOR" ] && die 3 "aborted during small download"
    if [ "$RC" -eq 10 ]; then
      MODELS="tiny,base"; log "small SKIPPED: free-space floor (the only tolerated skip)"
    elif [ "$RC" -ne 0 ]; then
      die 1 "small download FAILED (exit $RC; network/checksum/install) — not skipping"
    fi ;;
esac

# ---- 4. benchmark --------------------------------------------------------------------------
phase "benchmark ($MODELS)"
run_step "$VENV_PY" bench/run.py all --models "$MODELS" --out "$OUT"
after_step $? "benchmark"

# ---- 5. verify nothing in the git tree changed --------------------------------------------
phase "after"
[ "$(git -C "$ROOT" rev-parse HEAD)" = "$HEAD0" ] || die 4 "HEAD changed during the run"
[ -z "$(git -C "$ROOT" status --porcelain --untracked-files=normal)" ] \
  || die 4 "git tree changed during the run: $(git -C "$ROOT" status --porcelain | head -5)"
for m in $VOLS; do
  MIN=$(awk -F, -v m="$m" 'NR>1 && $2==m {print $4}' "$OUT/free_space.csv" | sort -n | head -1)
  log "minimum free on $m: ${MIN} GiB (floor ${FLOOR_GIB} GiB)"
done
log "samples: $(($(wc -l < "$OUT/free_space.csv")-1)) · git tree unchanged · results: $OUT/results.md"
exit 0
