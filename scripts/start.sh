#!/bin/bash
# Start RIFTWIRE: migrations → API (+ built dashboard) → worker.
#   scripts/start.sh          API on http://127.0.0.1:8000 serving the built dashboard
#   scripts/start.sh --dev    also start the Vite dev server on http://127.0.0.1:5173
# Set RIFTWIRE_CAFFEINATE=1 to keep the Mac awake while the worker runs.
set -euo pipefail
. "$(dirname "$0")/_common.sh"
require_venv

DEV=0
[ "${1:-}" = "--dev" ] && DEV=1
mkdir -p "$RUN_DIR" "$DATA_DIR/logs"

for svc in api worker vite; do
  pid="$(read_pid $svc || true)"
  if pid_alive "$pid"; then die "$svc already running (pid $pid). Use scripts/stop.sh first."; fi
  rm -f "$RUN_DIR/$svc.pid"
done

"$(dirname "$0")/migrate.sh"

if [ ! -f "$FRONTEND/dist/index.html" ] || [ -n "$(find "$FRONTEND/src" -newer "$FRONTEND/dist/index.html" -print -quit 2>/dev/null)" ]; then
  info "Building dashboard"
  ( cd "$FRONTEND" && npm run build --silent >/dev/null ) || die "frontend build failed (cd frontend && npm run build)"
fi

info "Starting API on http://$API_HOST:$API_PORT"
( cd "$BACKEND" && exec nohup "$VENV/bin/uvicorn" app.main:app --host "$API_HOST" --port "$API_PORT" \
    --no-access-log >>"$DATA_DIR/logs/api.out" 2>&1 ) &
echo $! > "$RUN_DIR/api.pid"

info "Starting worker"
( cd "$BACKEND" && exec nohup "$PY" -m app.workers.worker >>"$DATA_DIR/logs/worker.out" 2>&1 ) &
WORKER_PID=$!
echo $WORKER_PID > "$RUN_DIR/worker.pid"

if [ "${RIFTWIRE_CAFFEINATE:-0}" = "1" ] && command -v caffeinate >/dev/null 2>&1; then
  caffeinate -i -w "$WORKER_PID" & echo $! > "$RUN_DIR/caffeinate.pid"
  ok "caffeinate active while the worker runs"
fi

if [ $DEV -eq 1 ]; then
  info "Starting Vite dev server on http://127.0.0.1:5173"
  ( cd "$FRONTEND" && exec nohup npx vite --host 127.0.0.1 >>"$DATA_DIR/logs/vite.out" 2>&1 ) &
  echo $! > "$RUN_DIR/vite.pid"
fi

for _ in $(seq 1 60); do
  if curl -fsS "http://$API_HOST:$API_PORT/health" >/dev/null 2>&1; then break; fi
  pid_alive "$(read_pid api)" || die "API exited during startup — see $DATA_DIR/logs/api.log and api.out"
  sleep 0.5
done
curl -fsS "http://$API_HOST:$API_PORT/health" >/dev/null 2>&1 || die "API did not become healthy"
sleep 1
pid_alive "$WORKER_PID" || die "worker exited during startup — see $DATA_DIR/logs/worker.log and worker.out"

if curl -fsS "http://$API_HOST:$API_PORT/health/ready" >/dev/null 2>&1; then
  ok "Ready"
else
  warn "API is up but not ready: $(curl -s "http://$API_HOST:$API_PORT/health/ready")"
fi
echo
ok "Dashboard: http://$API_HOST:$API_PORT"
[ $DEV -eq 1 ] && ok "Dev dashboard: http://127.0.0.1:5173"
echo "  Logs: $DATA_DIR/logs   Stop: scripts/stop.sh"
