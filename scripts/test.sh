#!/bin/bash
# Full verification: backend lint + tests, frontend lint + typecheck + tests + build.
set -euo pipefail
. "$(dirname "$0")/_common.sh"
require_venv

info "Backend: ruff"
( cd "$BACKEND" && "$VENV/bin/ruff" check . )
info "Backend: pytest"
( cd "$BACKEND" && "$VENV/bin/pytest" -q )
info "M0 benchmark harness: ruff + pytest (no mlx needed)"
( cd "$ROOT/bench" && "$VENV/bin/ruff" check . && "$VENV/bin/pytest" -q -p no:cacheprovider tests )
info "Frontend: lint + typecheck"
( cd "$FRONTEND" && npx eslint src && npx tsc -b --noEmit )
info "Frontend: tests"
( cd "$FRONTEND" && npx vitest run )
info "Frontend: production build"
( cd "$FRONTEND" && npm run build --silent )
ok "All checks passed"
