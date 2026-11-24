#!/bin/bash
# Gracefully stop RIFTWIRE (SIGTERM). The worker finishes or releases its current job.
# A job interrupted by a forced kill is recovered automatically on the next start.
set -uo pipefail
. "$(dirname "$0")/_common.sh"

GRACE="$(env_value RIFTWIRE_WORKER_SHUTDOWN_GRACE_SECONDS 30)"
GRACE="${GRACE%.*}"
WAIT=$((GRACE + 20))

stop_one() {
  local name="$1" pid
  pid="$(read_pid "$name" || true)"
  if ! pid_alive "$pid"; then rm -f "$RUN_DIR/$name.pid"; return; fi
  info "Stopping $name (pid $pid)"
  kill -TERM "$pid" 2>/dev/null
  for _ in $(seq 1 $((WAIT * 2))); do
    pid_alive "$pid" || break
    sleep 0.5
  done
  if pid_alive "$pid"; then
    warn "$name did not exit after ${WAIT}s; sending SIGKILL (its job lease will be recovered on next start)"
    kill -KILL "$pid" 2>/dev/null
  fi
  rm -f "$RUN_DIR/$name.pid"
  ok "$name stopped"
}

stop_one vite
stop_one worker
stop_one api
stop_one caffeinate
