#!/bin/bash
# shellcheck disable=SC2015  # "check && pass || fail" is intentional: pass/fail never fail
# RIFTWIRE smoke test of the real launch scripts (start.sh / status.sh / stop.sh) with small
# synthetic media, against an ISOLATED data directory (your real library is never touched).
#
#   scripts/smoke_test.sh [report_file]
#
# Leaves RIFTWIRE running at the end so the dashboard can be checked in a browser; it prints
# the exact command to stop it. Env: RIFTWIRE_SMOKE_PORT (default 8000).
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ENVDIR="$HOME/Claude/riftwire-env"
mkdir -p "$ENVDIR"
REPORT="${1:-$ENVDIR/smoke-$STAMP.txt}"
export RIFTWIRE_DATA_DIR="$ENVDIR/smoke-data-$STAMP"
export RIFTWIRE_API_PORT="${RIFTWIRE_SMOKE_PORT:-8000}"
export RIFTWIRE_CAFFEINATE=1
export RIFTWIRE_URL_ACQUISITION_ENABLED=false   # an isolated test never contacts a platform
PY="$ROOT/backend/.venv/bin/python"
BASE="http://127.0.0.1:$RIFTWIRE_API_PORT"
MEDIA="$RIFTWIRE_DATA_DIR-media"
FAILS=0

exec > >(tee "$REPORT") 2>&1

pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; FAILS=$((FAILS + 1)); }
skip() { printf 'SKIP  %s\n' "$*"; }
check() { local name="$1"; shift; if "$@" >/dev/null 2>&1; then pass "$name"; else fail "$name"; fi; }
json() { "$PY" -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }
pidof_svc() { cat "$RIFTWIRE_DATA_DIR/run/$1.pid" 2>/dev/null; }

echo "RIFTWIRE smoke test  $STAMP"
echo "commit:   $(git rev-parse --short HEAD 2>/dev/null) ($(git rev-parse --abbrev-ref HEAD 2>/dev/null))"
echo "platform: $(uname -sm) $(sw_vers -productVersion 2>/dev/null)"
echo "data dir: $RIFTWIRE_DATA_DIR (isolated)"
echo "port:     $RIFTWIRE_API_PORT"
echo

[ -x "$PY" ] || { fail "backend/.venv exists (run scripts/bootstrap.sh)"; exit 1; }
if curl -s -o /dev/null "$BASE/health"; then
  fail "port $RIFTWIRE_API_PORT is free (something is already listening; stop it or set RIFTWIRE_SMOKE_PORT)"
  exit 1
fi

# ---------------------------------------------------------------- 1. start
if scripts/start.sh; then pass "start.sh exits 0"; else fail "start.sh exits 0"; fi
API_PID="$(pidof_svc api)"; WORKER_PID="$(pidof_svc worker)"; CAF_PID="$(pidof_svc caffeinate)"
check "API process running (pid $API_PID)" alive "$API_PID"
check "worker process running (pid $WORKER_PID)" alive "$WORKER_PID"
if [ "$(uname -s)" = "Darwin" ]; then
  check "caffeinate running while worker runs (pid $CAF_PID)" alive "$CAF_PID"
else
  skip "caffeinate (macOS only)"
fi
check "status.sh runs" scripts/status.sh
check "/health ok" sh -c "curl -fsS $BASE/health | grep -q '\"ok\"'"
READY="$(curl -s "$BASE/health/ready" | json "d['ready']")"
[ "$READY" = "True" ] && pass "/health/ready ready=true" || fail "/health/ready ready=true (got $READY)"
check "API bound to 127.0.0.1 only" sh -c "lsof -nP -iTCP:$RIFTWIRE_API_PORT -sTCP:LISTEN | grep -q '127.0.0.1:$RIFTWIRE_API_PORT'"
check "dashboard HTML served at /" sh -c "curl -fsS $BASE/ | grep -q '<div id=\"root\">'"
check "dashboard deep link served (/recordings)" sh -c "curl -fsS $BASE/recordings | grep -q '<div id=\"root\">'"
WSTATE="$(curl -s "$BASE/api/v1/system/status" | json "d['worker']['state']")"
[ "$WSTATE" = "RUNNING" ] && pass "worker heartbeat state RUNNING" || fail "worker state RUNNING (got $WSTATE)"

# ---------------------------------------------------------------- 2. synthetic media
mkdir -p "$MEDIA"
ffmpeg -v error -nostdin -y -f lavfi -i "testsrc2=size=1920x1080:rate=30000/1001:duration=6" \
  -f lavfi -i "sine=frequency=440:duration=6" -c:v libx264 -preset veryfast -pix_fmt yuv420p \
  -c:a aac "$MEDIA/smoke-heist.mp4" && pass "synthetic 1080p H.264/AAC clip" || fail "synthetic mp4"
ffmpeg -v error -nostdin -y -f lavfi -i "testsrc2=size=1280x720:rate=60:duration=4" \
  -c:v libvpx-vp9 -deadline realtime -cpu-used 8 -b:v 1M "$MEDIA/smoke-chase.webm" \
  && pass "synthetic 720p60 VP9 clip (no audio)" || fail "synthetic webm"

imp() {  # imp <file> <name> -> prints "HTTP_CODE BODY"
  curl -s -w '\n%{http_code}' -X POST -H 'Content-Type: application/octet-stream' \
    --data-binary @"$1" "$BASE/api/v1/recordings/import?filename=$2"
}
R1="$(imp "$MEDIA/smoke-heist.mp4" smoke-heist.mp4)"; C1="$(echo "$R1" | tail -1)"
R2="$(imp "$MEDIA/smoke-chase.webm" smoke-chase.webm)"; C2="$(echo "$R2" | tail -1)"
[ "$C1" = "201" ] && pass "import mp4 → 201" || fail "import mp4 → 201 (got $C1)"
[ "$C2" = "201" ] && pass "import webm → 201" || fail "import webm → 201 (got $C2)"
ID1="$(echo "$R1" | sed '$d' | json "d['recording']['id']" 2>/dev/null)"
ID2="$(echo "$R2" | sed '$d' | json "d['recording']['id']" 2>/dev/null)"
C3="$(imp "$MEDIA/smoke-heist.mp4" again.mp4 | tail -1)"
[ "$C3" = "409" ] && pass "duplicate import → 409" || fail "duplicate → 409 (got $C3)"

# ---------------------------------------------------------------- 3. processing
ready_both() {
  for id in "$ID1" "$ID2"; do
    s="$(curl -s "$BASE/api/v1/recordings/$id" | json "d['status']+'/'+d['thumbnail_status']")"
    [ "$s" = "READY/READY" ] || return 1
  done
}
for _ in $(seq 1 120); do ready_both && break; sleep 0.5; done
check "both recordings READY with thumbnails (≤60 s)" ready_both
for id in "$ID1" "$ID2"; do
  CT="$(curl -s -o /dev/null -w '%{content_type}' "$BASE/api/v1/recordings/$id/thumbnail")"
  [ "$CT" = "image/jpeg" ] && pass "thumbnail JPEG for ${id:0:8}" || fail "thumbnail JPEG for ${id:0:8} (got $CT)"
done
META="$(curl -s "$BASE/api/v1/recordings/$ID1" | json "f\"{d['width']}x{d['height']} {d['fps_rational']} {d['video_codec']}/{d['audio_codec']}\"")"
[ "$META" = "1920x1080 30000/1001 h264/aac" ] && pass "metadata $META" || fail "metadata (got $META)"

# ---------------------------------------------------------------- 4. stop
if scripts/stop.sh; then pass "stop.sh exits 0"; else fail "stop.sh exits 0"; fi
sleep 1
alive "$API_PID" && fail "API process gone" || pass "API process gone"
alive "$WORKER_PID" && fail "worker process gone" || pass "worker process gone"
if [ "$(uname -s)" = "Darwin" ]; then
  alive "$CAF_PID" && fail "caffeinate gone" || pass "caffeinate gone"
fi
curl -s -o /dev/null "$BASE/health" && fail "port closed after stop" || pass "port closed after stop"
WROW="$("$PY" - "$RIFTWIRE_DATA_DIR/riftwire.db" <<'EOF'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
print(c.execute("select status from workers order by last_heartbeat_at desc limit 1").fetchone()[0])
EOF
)"
[ "$WROW" = "STOPPED" ] && pass "worker recorded STOPPED (graceful)" || fail "worker STOPPED (got $WROW)"
ls "$RIFTWIRE_DATA_DIR/run/"*.pid >/dev/null 2>&1 && fail "PID files removed" || pass "PID files removed"

# ---------------------------------------------------------------- 5. restart + persistence
if scripts/start.sh; then pass "start.sh again exits 0"; else fail "start.sh again exits 0"; fi
N="$(curl -s "$BASE/api/v1/recordings" | json "d['total']")"
J="$(curl -s "$BASE/api/v1/jobs?status=COMPLETED" | json "d['total']")"
[ "$N" = "2" ] && pass "2 recordings persisted across restart" || fail "recordings persisted (got $N)"
[ "$J" = "6" ] && pass "6 COMPLETED jobs persisted across restart" || fail "jobs persisted (got $J)"

echo
echo "RESULT: $FAILS failure(s)"
echo
echo "RIFTWIRE is still running for the dashboard check: $BASE"
echo "When finished, stop it with:"
echo "  RIFTWIRE_DATA_DIR='$RIFTWIRE_DATA_DIR' RIFTWIRE_API_PORT=$RIFTWIRE_API_PORT scripts/stop.sh"
echo "report: $REPORT"
exit $FAILS
