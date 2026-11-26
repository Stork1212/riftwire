#!/bin/bash
# RIFTWIRE M5 preparation: install Ollama (Homebrew), pull qwen3.5:4b into RIFTWIRE's models
# location, verify identity/licence/localhost binding, and benchmark it on the grounded
# highlight task. Pinned to phase2/m5-ai-prep at c7179ad.
# Run:   bash ~/Claude/m5-ollama-qwen-c7179ad.sh
#
# Approved by the owner (2026-11-25): installing Ollama and downloading qwen3.5:4b. Nothing
# else is downloaded. The server runs only for this script (no login service), bound to
# 127.0.0.1, cloud features off, one model loaded, one request at a time. The 60 GiB floor
# is checked before and after the download. The live demo, PR #13/#14 and the SSD test
# folders are not touched. Nothing is pushed. Results are synthetic engineering measures.
set -u -o pipefail
EXPECTED=c7179ad6de5af5b405ab66b1a6ac1af60f5e3567
BRANCH=phase2/m5-ai-prep
BUNDLE="$HOME/Claude/riftwire-env/riftwire-m5prep-c7179ad.bundle"
REPO="$HOME/Claude/riftwire"
MODELS_DIR="$REPO/data/models/ollama"
MODEL="qwen3.5:4b"
PORT=11434
FLOOR_GIB=60
PULL_HEADROOM_GIB=6
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT="$HOME/Claude/riftwire-env/m5-ollama-c7179ad-$STAMP"
mkdir -p "$OUT"
SUM="$OUT/summary.txt"
say() { echo "$@" | tee -a "$SUM"; }
abort() { say "ABORT: $*"; exit 2; }
SERVER_PID=""

LOCK="$HOME/Claude/riftwire-env/.verify.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  say "ABORT: another verification run is in progress ($LOCK exists)."
  say "If no run is active, remove it with: rmdir $LOCK"
  exit 3
fi
cleanup() {
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null; sleep 2; kill -9 "$SERVER_PID" 2>/dev/null
  fi
  rmdir "$LOCK" 2>/dev/null
}
trap cleanup EXIT
trap 'exit 130' INT TERM

free_gib() { df -k "$1" | awk 'NR==2 {printf "%.2f", $4/1048576}'; }
swap() { sysctl -n vm.swapusage; }

say "RIFTWIRE M5 Ollama + $MODEL  started $(date -u +%Y-%m-%dT%H:%M:%SZ)"
say "evidence folder: $OUT"
[ "$(uname -m)" = "arm64" ] || abort "Apple Silicon required"
[ -x /opt/homebrew/bin/brew ] || abort "Homebrew not found at /opt/homebrew"
export PATH="/opt/homebrew/bin:$PATH"

# ------------------------------------------------------------------ repository (pinned)
cd "$REPO" || abort "repo not found at $REPO"
DIRTY=$(git status --porcelain --untracked-files=normal)
[ -z "$DIRTY" ] || { echo "$DIRTY" > "$OUT/dirty-tree.txt"; abort "working tree not clean (see dirty-tree.txt)"; }
[ -f "$BUNDLE" ] || abort "bundle not found at $BUNDLE"
git bundle verify "$BUNDLE" > "$OUT/git.log" 2>&1 || abort "bundle does not apply (see git.log)"
git fetch "$BUNDLE" "$BRANCH:refs/remotes/bundle/m5-ai-prep" >> "$OUT/git.log" 2>&1 || abort "fetch from bundle failed"
if git show-ref --verify --quiet "refs/heads/$BRANCH"; then
  git checkout "$BRANCH" >> "$OUT/git.log" 2>&1 || abort "checkout failed"
  git merge --ff-only bundle/m5-ai-prep >> "$OUT/git.log" 2>&1 || abort "fast-forward failed"
else
  git checkout -b "$BRANCH" bundle/m5-ai-prep >> "$OUT/git.log" 2>&1 || abort "checkout failed"
fi
[ "$(git rev-parse HEAD)" = "$EXPECTED" ] || abort "HEAD is $(git rev-parse HEAD), expected $EXPECTED"
say "HEAD: $EXPECTED (branch $BRANCH; depends on PR #14)"
PY="$REPO/backend/.venv/bin/python"
[ -x "$PY" ] || abort "backend venv missing ($PY)"

# ------------------------------------------------------------------ no concurrent heavy work
if pgrep -f transcribe_child >/dev/null || pgrep -x ffmpeg >/dev/null; then
  abort "a Whisper or FFmpeg job is running; wait for it to finish (one heavy workload at a time)"
fi
if lsof -nP -iTCP:$PORT -sTCP:LISTEN > "$OUT/port-before.txt" 2>&1; then
  abort "something already listens on port $PORT (see port-before.txt); quit the Ollama app or server first"
fi

# ------------------------------------------------------------------ environment
{
  echo "date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  sw_vers; sysctl -n machdep.cpu.brand_string; echo "mem_bytes: $(sysctl -n hw.memsize)"
  echo "swap: $(swap)"; "$PY" --version
} > "$OUT/environment.txt" 2>&1
mkdir -p "$REPO/data/models" || abort "cannot create $REPO/data/models"
F0=$(free_gib "$REPO/data/models")
say "free space on the models volume: $F0 GiB (floor $FLOOR_GIB GiB, pull headroom $PULL_HEADROOM_GIB GiB)"
awk -v f="$F0" -v n="$((FLOOR_GIB + PULL_HEADROOM_GIB))" 'BEGIN{exit !(f >= n)}' \
  || abort "not enough free space to pull without breaching the $FLOOR_GIB GiB floor"
df -h "$REPO/data/models" > "$OUT/df-before.txt"

# ------------------------------------------------------------------ tests of the pinned branch
say ""; say "== test: scripts/test.sh"
scripts/test.sh > "$OUT/test.log" 2>&1; RC_test=$?
say "   exit $RC_test  ($(grep -E '[0-9]+ passed' "$OUT/test.log" | head -1))"

# ------------------------------------------------------------------ install Ollama (approved)
say ""; say "== Ollama"
if ! command -v ollama >/dev/null; then
  say "   installing with Homebrew: brew install ollama"
  brew install ollama > "$OUT/brew-install.log" 2>&1 || abort "brew install ollama failed (see brew-install.log)"
fi
OLLAMA=$(command -v ollama)
"$OLLAMA" --version > "$OUT/ollama-version.txt" 2>&1
brew info --json=v2 ollama > "$OUT/brew-info.json" 2>/dev/null
say "   $(head -1 "$OUT/ollama-version.txt") at $OLLAMA (not registered as a login service)"
mkdir -p "$MODELS_DIR" || abort "cannot create $MODELS_DIR"

env OLLAMA_HOST="127.0.0.1:$PORT" OLLAMA_MODELS="$MODELS_DIR" OLLAMA_NO_CLOUD=1 \
    OLLAMA_MAX_LOADED_MODELS=1 OLLAMA_NUM_PARALLEL=1 OLLAMA_KEEP_ALIVE=0 \
    OLLAMA_CONTEXT_LENGTH=8192 "$OLLAMA" serve > "$OUT/ollama-serve.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 60); do curl -s --max-time 1 "http://127.0.0.1:$PORT/api/version" > "$OUT/api-version.json" && break; sleep 0.5; done
[ -s "$OUT/api-version.json" ] || abort "ollama serve did not start (see ollama-serve.log)"
lsof -nP -iTCP:$PORT -sTCP:LISTEN > "$OUT/listen.txt" 2>&1
if grep -q "127.0.0.1:$PORT" "$OUT/listen.txt" && ! grep -Eq "\*:$PORT|0\.0\.0\.0:$PORT|\[::\]:$PORT" "$OUT/listen.txt"; then
  RC_local=0; say "   listening on 127.0.0.1:$PORT only"
else
  RC_local=1; say "   FAIL: not bound to localhost only (see listen.txt)"
fi
if grep -qi "cloud disabled: true" "$OUT/ollama-serve.log"; then RC_cloud=0; say "   cloud features disabled (log confirms)"
else RC_cloud=1; say "   WARNING: the log does not confirm 'cloud disabled: true' (see ollama-serve.log)"; fi

# ------------------------------------------------------------------ pull the approved model
say ""; say "== model $MODEL"
export OLLAMA_HOST="127.0.0.1:$PORT"
T0=$(date +%s)
"$OLLAMA" pull "$MODEL" > "$OUT/pull.log" 2>&1 || abort "pull failed (see pull.log)"
say "   pulled in $(( $(date +%s) - T0 )) s"
"$OLLAMA" list > "$OUT/ollama-list.txt" 2>&1
"$OLLAMA" show "$MODEL" > "$OUT/model-show.txt" 2>&1
"$OLLAMA" show --license "$MODEL" > "$OUT/model-license.txt" 2>&1
"$OLLAMA" show --modelfile "$MODEL" > "$OUT/model-modelfile.txt" 2>&1
MANIFEST="$MODELS_DIR/manifests/registry.ollama.ai/library/qwen3.5/4b"
if [ -f "$MANIFEST" ]; then
  cp "$MANIFEST" "$OUT/model-manifest.json"
  shasum -a 256 "$MANIFEST" | awk '{print "manifest sha256: " $1}' | tee -a "$SUM"
fi
du -sk "$MODELS_DIR" | awk '{printf "   models folder: %.2f GiB\n", $1/1048576}' | tee -a "$SUM"
if grep -q "Apache License" "$OUT/model-license.txt"; then RC_licence=0; say "   licence: Apache License 2.0 (model-license.txt)"
else RC_licence=1; say "   FAIL: licence is not Apache 2.0 (see model-license.txt)"; fi
F1=$(free_gib "$REPO/data/models")
say "   free space after pull: $F1 GiB"
awk -v f="$F1" -v n="$FLOOR_GIB" 'BEGIN{exit !(f >= n)}' || abort "free space fell below the $FLOOR_GIB GiB floor"

# ------------------------------------------------------------------ benchmark
say ""; say "== benchmark (synthetic grounded tasks; 2 repeats)"
echo "swap before: $(swap)" > "$OUT/swap.txt"
memory_pressure -Q > "$OUT/memory-pressure-before.txt" 2>&1
"$PY" bench/llm/llm_bench.py --model "$MODEL" --repeat 2 --out "$OUT" 2>&1 | tee "$OUT/bench.log" | sed 's/^/   /'
RC_bench=${PIPESTATUS[0]}
echo "swap after:  $(swap)" >> "$OUT/swap.txt"
memory_pressure -Q > "$OUT/memory-pressure-after.txt" 2>&1
"$OLLAMA" ps > "$OUT/ollama-ps-after.txt" 2>&1

kill "$SERVER_PID" 2>/dev/null; sleep 2
lsof -nP -iTCP:$PORT -sTCP:LISTEN > "$OUT/port-after.txt" 2>&1 && say "   WARNING: port $PORT still open" || say "   server stopped"
SERVER_PID=""
df -h "$REPO/data/models" > "$OUT/df-after.txt"

if [ -f "$OUT/llm-bench.json" ]; then
  "$PY" - "$OUT/llm-bench.json" <<'PYEOF' | tee -a "$SUM"
import json, sys
d = json.load(open(sys.argv[1]))
for m in d["models"]:
    if "error" in m:
        print("model error:", m["error"]); continue
    s = m["summary"]
    print("identity:", json.dumps(m["identity"]))
    keys = ["tasks_run", "accepted", "valid_first_try", "failed_validation", "provider_errors",
            "mean_precision", "mean_recall", "trap_selected", "padded", "unsupported_claims",
            "payoff_kept", "first_load_ms", "prompt_tok_per_s_median", "output_tok_per_s_median",
            "wall_s_median"]
    for k in keys:
        print(f"  {k}: {s[k]}")
    print("  memory:", json.dumps(s["memory"]))
PYEOF
fi
say ""
say "RESULT  test=$RC_test  localhost=$RC_local  cloud-off=$RC_cloud  licence=$RC_licence  bench=$RC_bench"
say "free space: before $F0 GiB, after $(free_gib "$REPO/data/models") GiB"
if [ "$RC_test" -eq 0 ] && [ "$RC_local" -eq 0 ] && [ "$RC_licence" -eq 0 ] && [ "$RC_bench" -eq 0 ]; then
  say "ALL PASSED (benchmark numbers are synthetic engineering measurements)"
else
  say "SOME STEPS FAILED (see above)"
fi
