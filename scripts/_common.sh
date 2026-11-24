#!/bin/bash
# Shared helpers for RIFTWIRE scripts (bash 3.2 compatible: the macOS default shell).

# shellcheck disable=SC2034  # variables are used by the scripts that source this file
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"
VENV="$BACKEND/.venv"
PY="$VENV/bin/python"

# Homebrew (Apple Silicon) may be missing from non-login shells.
case ":$PATH:" in *":/opt/homebrew/bin:"*) ;; *) PATH="/opt/homebrew/bin:$PATH" ;; esac
export PATH

# Read a single key from .env without executing it (values are plain strings).
env_value() {
  local key="$1" default="$2" v=""
  if [ -n "${!key:-}" ]; then echo "${!key}"; return; fi
  if [ -f "$ROOT/.env" ]; then
    v="$(grep -E "^[[:space:]]*$key=" "$ROOT/.env" | tail -1 | cut -d= -f2- | sed -e 's/^["'\'']//' -e 's/["'\'']$//')"
  fi
  if [ -n "$v" ]; then echo "$v"; else echo "$default"; fi
}

DATA_DIR="$(env_value RIFTWIRE_DATA_DIR data)"
case "$DATA_DIR" in /*) ;; *) DATA_DIR="$ROOT/$DATA_DIR" ;; esac
RUN_DIR="$DATA_DIR/run"
API_HOST="$(env_value RIFTWIRE_API_HOST 127.0.0.1)"
API_PORT="$(env_value RIFTWIRE_API_PORT 8000)"

info()  { printf '\033[1;36m▸\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m✓\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m!\033[0m %s\n' "$*" >&2; }
die()   { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

pid_alive() { [ -n "$1" ] && kill -0 "$1" 2>/dev/null; }
read_pid() { [ -f "$RUN_DIR/$1.pid" ] && cat "$RUN_DIR/$1.pid"; }

require_venv() {
  [ -x "$PY" ] || die "backend/.venv not found — run scripts/bootstrap.sh first"
}
