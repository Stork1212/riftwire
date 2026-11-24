#!/bin/bash
# Create the project-local environments. Installs nothing system-wide:
#   backend/.venv            (Python 3.13 virtualenv + backend dependencies)
#   frontend/node_modules    (npm ci from package-lock.json)
#   .env                     (copied from .env.example only if missing)
#   data/…                   (data directories)
set -euo pipefail
. "$(dirname "$0")/_common.sh"

pick_python() {
  local c
  for c in "${RIFTWIRE_PYTHON:-}" python3.13 \
           /Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13 \
           python3.12 python3.11 python3.14; do
    [ -n "$c" ] || continue
    if command -v "$c" >/dev/null 2>&1 && \
       "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      command -v "$c"; return 0
    fi
  done
  return 1
}

info "Checking prerequisites"
PYBIN="$(pick_python)" || die "Python ≥ 3.11 not found (3.13 recommended). Set RIFTWIRE_PYTHON=/path/to/python3.13"
ok "Python: $PYBIN ($("$PYBIN" --version 2>&1))"

command -v node >/dev/null 2>&1 || die "Node.js not found (need ≥ 20)"
NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]')"
[ "$NODE_MAJOR" -ge 20 ] || die "Node.js $NODE_MAJOR found; need ≥ 20"
ok "Node: $(node --version), npm $(npm --version)"

if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
  ok "FFmpeg: $(ffmpeg -hide_banner -version | head -1)"
else
  warn "FFmpeg/ffprobe not found. Install (needs owner approval): brew install ffmpeg"
fi

if [ -d "$VENV" ] && ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
  die "backend/.venv exists but uses an unsupported Python; remove it manually and re-run"
fi
if [ ! -d "$VENV" ]; then
  info "Creating backend/.venv with $PYBIN"
  "$PYBIN" -m venv "$VENV"
fi
info "Installing backend dependencies into backend/.venv"
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -c "$BACKEND/constraints.txt" -e "${BACKEND}[dev]"
ok "Backend environment ready ($("$PY" --version))"

info "Installing frontend dependencies (npm ci)"
( cd "$FRONTEND" && npm ci --no-audit --no-fund --loglevel=error )
ok "Frontend dependencies installed"

if [ ! -f "$ROOT/.env" ]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
  ok "Created .env from .env.example"
else
  ok ".env already exists (left unchanged)"
fi

mkdir -p "$DATA_DIR"/recordings "$DATA_DIR"/thumbnails "$DATA_DIR"/temp \
         "$DATA_DIR"/exports "$DATA_DIR"/logs "$RUN_DIR"
ok "Data directory: $DATA_DIR"

echo
ok "Bootstrap complete. Next: scripts/start.sh"
