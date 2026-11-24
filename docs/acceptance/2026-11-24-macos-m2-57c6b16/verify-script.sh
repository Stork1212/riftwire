#!/bin/bash
# RIFTWIRE M2 Mac verification, pinned to the reviewed commit. Run: bash ~/Claude/m2-verify-57c6b16.sh
set -u -o pipefail
EXPECTED=57c6b1637e498d0c621d53604ec545d3636d38fb
BRANCH=phase2/m2-rights-sources
REPO="$HOME/Claude/riftwire"
FLOOR_GIB=60
MARGIN_GIB=2
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT="$HOME/Claude/riftwire-env/m2-verify-57c6b16-$STAMP"
mkdir -p "$OUT"
SUM="$OUT/summary.txt"
say() { echo "$@" | tee -a "$SUM"; }
abort() { say "ABORT: $*"; exit 2; }

LOCK="$HOME/Claude/riftwire-env/.verify.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  say "ABORT: another verification run is in progress ($LOCK exists; it shares ports 8000/8765)."
  say "If no run is active, remove it with: rmdir $LOCK"
  exit 3
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT
trap 'exit 130' INT TERM

cd "$REPO" || abort "repo not found at $REPO"
say "RIFTWIRE M2 Mac verification  started $(date -u +%Y-%m-%dT%H:%M:%SZ)"
say "evidence folder: $OUT"

DIRTY=$(git status --porcelain --untracked-files=normal)
[ -z "$DIRTY" ] || { echo "$DIRTY" > "$OUT/dirty-tree.txt"; abort "working tree not clean (see dirty-tree.txt); nothing was switched"; }
say "working tree: clean"

git fetch origin "$BRANCH" > "$OUT/git.log" 2>&1 || abort "git fetch failed (see git.log)"
git checkout "$BRANCH" >> "$OUT/git.log" 2>&1 || abort "git checkout failed (see git.log)"
git merge --ff-only "origin/$BRANCH" >> "$OUT/git.log" 2>&1 || abort "fast-forward failed (see git.log)"
HEAD_SHA=$(git rev-parse HEAD)
[ "$HEAD_SHA" = "$EXPECTED" ] || abort "HEAD is $HEAD_SHA, expected $EXPECTED"
say "HEAD: $HEAD_SHA (matches)"

{
  echo "== machine"; sw_vers; uname -m; sysctl -n machdep.cpu.brand_string; sysctl -n hw.memsize
  echo "== tools"; backend/.venv/bin/python --version; ffmpeg -version | head -1; node -v; npm -v; git --version
  echo "== volume of repo"; diskutil info "$(df -P . | tail -1 | awk '{print $NF}')" | grep -E "File System Personality|Volume UUID|Mount Point" || true
} > "$OUT/environment.txt" 2>&1

free_gib() { df -k "$REPO" | tail -1 | awk '{printf "%.2f", $4/1048576}'; }
BEFORE=$(free_gib)
df -k "$REPO" > "$OUT/df-before.txt"
say "free space before: $BEFORE GiB (floor $FLOOR_GIB GiB, abort below $((FLOOR_GIB+MARGIN_GIB)) GiB)"
awk -v f="$BEFORE" -v m=$((FLOOR_GIB+MARGIN_GIB)) 'BEGIN{exit !(f>=m)}' || abort "not enough free space to run safely above the 60 GiB floor"

run() {
  local name=$1; shift
  say ""; say "== $name: $*"
  local t0=$(date +%s)
  "$@" > "$OUT/$name.log" 2>&1
  local rc=$?
  say "   exit code $rc  ($(( $(date +%s) - t0 )) s)  free now $(free_gib) GiB"
  eval "RC_$name=$rc"
}

run test        scripts/test.sh
run acceptance  backend/.venv/bin/python scripts/acceptance.py --min-free-gib $FLOOR_GIB --report "$OUT/acceptance.json"
for d in "$HOME"/Claude/riftwire-env/smoke-data-*; do
  if [ -f "$d/run/api.pid" ] || [ -f "$d/run/worker.pid" ]; then
    say "stopping leftover smoke-test instance: $(basename "$d")"
    RIFTWIRE_DATA_DIR="$d" RIFTWIRE_API_PORT=8000 scripts/stop.sh >> "$OUT/smoke-cleanup.log" 2>&1
  fi
done
run smoke       scripts/smoke_test.sh "$OUT/smoke-report.txt"

AFTER=$(free_gib)
df -k "$REPO" > "$OUT/df-after.txt"
say ""
say "free space after: $AFTER GiB (before $BEFORE GiB)"

say ""; say "== counts"
grep -E "[0-9]+ (passed|failed|skipped|error)" "$OUT/test.log" | tee -a "$SUM"
grep -E "Test Files|Tests " "$OUT/test.log" | tee -a "$SUM"
grep -Ei "^(PASS|FAIL|SKIP)|steps|passed|failed" "$OUT/acceptance.log" | tail -3 | tee -a "$SUM"
grep -cE "^\s*PASS" "$OUT/smoke.log" | sed 's/^/smoke PASS lines: /' | tee -a "$SUM"
grep -cE "^\s*FAIL" "$OUT/smoke.log" | sed 's/^/smoke FAIL lines: /' | tee -a "$SUM"
grep -cE "^\s*SKIP" "$OUT/smoke.log" | sed 's/^/smoke SKIP lines: /' | tee -a "$SUM"
say ""; say "== warnings (all logs)"
grep -iHn "warn" "$OUT"/test.log "$OUT"/acceptance.log "$OUT"/smoke.log > "$OUT/warnings.txt"
say "warning lines: $(wc -l < "$OUT/warnings.txt" | tr -d ' ')  (details in warnings.txt)"
git status --porcelain --untracked-files=normal > "$OUT/tree-after.txt"
say "working tree after: $([ -s "$OUT/tree-after.txt" ] && echo 'CHANGED (see tree-after.txt)' || echo clean)"

say ""
say "RESULT  test=$RC_test  acceptance=$RC_acceptance  smoke=$RC_smoke"
if [ "$RC_test" -eq 0 ] && [ "$RC_acceptance" -eq 0 ] && [ "$RC_smoke" -eq 0 ]; then say "ALL PASSED"; exit 0; else say "FAILURES (see logs)"; exit 1; fi
