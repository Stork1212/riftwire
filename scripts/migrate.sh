#!/bin/bash
# Apply database migrations (alembic upgrade head). Safe to run repeatedly.
#   scripts/migrate.sh            upgrade to latest
#   scripts/migrate.sh current    show current revision
set -euo pipefail
. "$(dirname "$0")/_common.sh"
require_venv
mkdir -p "$DATA_DIR"
cd "$BACKEND"
if [ "${1:-upgrade}" = "current" ]; then
  "$VENV/bin/alembic" current
else
  "$VENV/bin/alembic" upgrade head
  ok "Database schema is up to date"
fi
