#!/bin/bash
# Create the isolated M0 benchmark environment: bench/.venv (Python 3.13).
# Project-local only. Installs the exact, hash-pinned packages in bench/requirements.txt
# (no PyTorch). Refuses to run if it would take free space below the 60 GiB floor + margin.
# Remove completely with:  rm -rf bench/.venv
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"
VENV="$ROOT/bench/.venv"
FLOOR_GIB=60
MARGIN_GIB=1
NEED_MB=600   # ~426 MB installed + pip build/temp overhead

PY="${RIFTWIRE_PYTHON:-}"
for c in "$PY" python3.13 /Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13; do
  [ -n "$c" ] && command -v "$c" >/dev/null 2>&1 && { PY="$(command -v "$c")"; break; }
done
[ -n "$PY" ] || { echo "✗ python3.13 not found (set RIFTWIRE_PYTHON)"; exit 1; }
[ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ] || \
  echo "! Not macOS/arm64: mlx will not install here (tests can still run in backend/.venv)."

free_kb=$(df -k "$ROOT" | awk 'NR==2{print $4}')
need_kb=$(( (FLOOR_GIB + MARGIN_GIB) * 1024 * 1024 + NEED_MB * 1024 ))
if [ "$free_kb" -lt "$need_kb" ]; then
  echo "✗ Not enough free space: $((free_kb/1048576)) GiB free; need ${FLOOR_GIB} GiB floor + ${MARGIN_GIB} GiB margin + ~${NEED_MB} MB."
  exit 1
fi
echo "▸ Free space OK: $((free_kb/1048576)) GiB (floor ${FLOOR_GIB} GiB)"

[ -d "$VENV" ] || "$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --no-deps --require-hashes --no-cache-dir \
  -r "$ROOT/bench/requirements.txt"
echo "▸ Installed into bench/.venv ($(du -sh "$VENV" | cut -f1))"
"$VENV/bin/python" - <<'EOF'
import importlib.util as u
import mlx_whisper, yt_dlp.version as v
print("✓ mlx-whisper", mlx_whisper.__version__, "| yt-dlp", v.__version__,
      "| torch installed:", u.find_spec("torch") is not None)
EOF
