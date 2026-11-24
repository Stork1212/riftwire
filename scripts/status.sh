#!/bin/bash
# Show whether the RIFTWIRE processes are running and the API's readiness.
set -uo pipefail
. "$(dirname "$0")/_common.sh"
for svc in api worker vite; do
  pid="$(read_pid $svc || true)"
  if pid_alive "$pid"; then ok "$svc running (pid $pid)"; else echo "  $svc not running"; fi
done
curl -s "http://$API_HOST:$API_PORT/health/ready" 2>/dev/null || echo "  API not reachable on $API_HOST:$API_PORT"
echo
