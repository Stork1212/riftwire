#!/bin/bash
# RIFTWIRE M5 inference re-verification, pinned to phase2/m5-ai-prep at 9f7b499.
# Run:   bash ~/Claude/m5-inference-9f7b499.sh
#
# Reuses the Ollama install and the qwen3.5:4b model downloaded at 05:13Z (digest pinned);
# DOWNLOADS NOTHING: if the model is missing or different, it stops. The server runs only for
# this script, bound to 127.0.0.1, cloud off, one model, one request at a time.
# Steps: suite -> staged probe (health, model, plain text, residency, JSON mode, schema mode,
# RIFTWIRE path) -> grounded benchmark -> memory/swap snapshots before, during and after
# (incl. 30 s and 60 s after server shutdown). ALL PASSED requires real generated tokens,
# zero provider errors and at least one accepted answer. Results are synthetic engineering
# measurements, not highlight quality. The live demo, PR #13/#14 and SSD folders are untouched.
set -u -o pipefail
EXPECTED=9f7b499ec0b0b8f0f1b619832bf6cd1e97ed3e10
MODEL_DIGEST=7f3251fa6878a78a606bcb3c074283306e5ad2ea411a7774a9dd98a4bc7f2d13
BRANCH=phase2/m5-ai-prep
BUNDLE="$HOME/Claude/riftwire-env/riftwire-m5prep-9f7b499.bundle"
REPO="$HOME/Claude/riftwire"
MODELS_DIR="$REPO/data/models/ollama"
MODEL="qwen3.5:4b"
PORT=11434
FLOOR_GIB=60
PULL_HEADROOM_GIB=0
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT="$HOME/Claude/riftwire-env/m5-inference-9f7b499-$STAMP"
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
snap() {
  { echo "== $1  $(date -u +%H:%M:%SZ)"; echo "swap: $(swap)"; memory_pressure -Q 2>/dev/null | tail -1
    vm_stat | grep -E "Pages (free|active|inactive|speculative|purgeable|wired down|occupied by compressor)|Swapins|Swapouts"
    ps -axo pid=,rss=,comm= | awk '$3 ~ /ollama/ {printf "ollama pid %s rss %.1f MB\n", $1, $2/1024}'
  } >> "$OUT/memory-snapshots.txt" 2>&1
}

say "RIFTWIRE M5 inference re-verification ($MODEL)  started $(date -u +%Y-%m-%dT%H:%M:%SZ)"
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
say "free space on the models volume: $F0 GiB (floor $FLOOR_GIB GiB)"
awk -v f="$F0" -v n="$((FLOOR_GIB + PULL_HEADROOM_GIB))" 'BEGIN{exit !(f >= n)}' \
  || abort "free space is below the $FLOOR_GIB GiB floor"
df -h "$REPO/data/models" > "$OUT/df-before.txt"

# ------------------------------------------------------------------ tests of the pinned branch
say ""; say "== test: scripts/test.sh"
scripts/test.sh > "$OUT/test.log" 2>&1; RC_test=$?
say "   exit $RC_test  ($(grep -E '[0-9]+ passed' "$OUT/test.log" | head -1))"

# ------------------------------------------------------------------ Ollama (already installed)
say ""; say "== Ollama"
command -v ollama >/dev/null || abort "ollama is not installed (it was installed at 05:13Z); not installing again"
[ -d "$MODELS_DIR/manifests" ] || abort "no models in $MODELS_DIR; this script downloads nothing"
snap "before server start"
OLLAMA=$(command -v ollama)
"$OLLAMA" --version 2>/dev/null > "$OUT/ollama-version.txt"
brew info --json=v2 ollama > "$OUT/brew-info.json" 2>/dev/null
say "   $(grep -o '[0-9][0-9.]*' "$OUT/ollama-version.txt" | tail -1) at $OLLAMA (not registered as a login service)"
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

# ------------------------------------------------------------------ the model already on disk
say ""; say "== model $MODEL (reused; no download)"
export OLLAMA_HOST="127.0.0.1:$PORT"
curl -s --max-time 10 "http://127.0.0.1:$PORT/api/tags" > "$OUT/api-tags.json"
GOT=$("$PY" -c "import json,sys; d=json.load(open(sys.argv[1])); print(next((m['digest'] for m in d.get('models',[]) if m.get('name')==sys.argv[2]),''))" "$OUT/api-tags.json" "$MODEL")
[ -n "$GOT" ] || abort "$MODEL is not installed; this script downloads nothing"
[ "$GOT" = "$MODEL_DIGEST" ] || abort "$MODEL digest is $GOT, expected $MODEL_DIGEST (not the model tested at 05:13Z)"
say "   digest $GOT (matches the 05:13Z download)"
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
awk -v f="$F1" -v n="$FLOOR_GIB" 'BEGIN{exit !(f >= n)}' || abort "free space fell below the $FLOOR_GIB GiB floor"

# ------------------------------------------------------------------ staged probe
say ""; say "== probe (simplest request first)"
snap "before probe"
"$PY" bench/llm/probe.py --model "$MODEL" --out "$OUT" 2>&1 | tee "$OUT/probe.log" | cut -c1-240 | sed 's/^/   /' | tee -a "$SUM"
RC_probe=${PIPESTATUS[0]}
snap "after probe (model unloaded)"

# ------------------------------------------------------------------ benchmark
say ""; say "== benchmark (synthetic grounded tasks; 2 repeats)"
"$PY" bench/llm/llm_bench.py --model "$MODEL" --repeat 2 --out "$OUT" 2>&1 | tee "$OUT/bench.log" | sed 's/^/   /'
RC_bench=${PIPESTATUS[0]}
snap "after benchmark (model unloaded)"
"$OLLAMA" ps > "$OUT/ollama-ps-after.txt" 2>&1

kill "$SERVER_PID" 2>/dev/null; sleep 2
lsof -nP -iTCP:$PORT -sTCP:LISTEN > "$OUT/port-after.txt" 2>&1 && say "   WARNING: port $PORT still open" || say "   server stopped"
SERVER_PID=""
snap "server stopped"; sleep 30; snap "30 s after shutdown"; sleep 30; snap "60 s after shutdown"
grep -E "^== |^swap" "$OUT/memory-snapshots.txt" | paste - - | sed 's/^/   /' | tee -a "$SUM"
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
    print("verified:", m.get("verified"), " gate failures:", m.get("gate_failures"))
    keys = ["tasks_run", "inference_verified", "generations_with_output", "output_constraint",
            "constraint_notes", "errors_by_message", "thinking_chars_max", "accepted", "valid_first_try", "failed_validation", "provider_errors",
            "mean_precision", "mean_recall", "trap_selected", "padded", "unsupported_claims",
            "payoff_kept", "first_load_ms", "prompt_tok_per_s_median", "output_tok_per_s_median",
            "wall_s_median"]
    for k in keys:
        print(f"  {k}: {s[k]}")
    mem = {k: v for k, v in s["memory"].items() if k != "timeline_s_swapmb_availmb"}
    print("  memory:", json.dumps(mem))
PYEOF
fi
say ""
say "RESULT  test=$RC_test  localhost=$RC_local  cloud-off=$RC_cloud  licence=$RC_licence  probe=$RC_probe  bench=$RC_bench"
say "free space: before $F0 GiB, after $(free_gib "$REPO/data/models") GiB"
if [ "$RC_test" -eq 0 ] && [ "$RC_local" -eq 0 ] && [ "$RC_licence" -eq 0 ] && [ "$RC_probe" -eq 0 ] && [ "$RC_bench" -eq 0 ]; then
  say "ALL PASSED: genuine local inference verified (generated tokens, no provider errors, accepted answers)."
  say "Benchmark numbers are synthetic engineering measurements, not highlight quality."
else
  say "NOT VERIFIED: see the probe and GATE lines above."
fi
