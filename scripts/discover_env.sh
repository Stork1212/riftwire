#!/bin/bash
# RIFTWIRE — read-only development environment inspection (macOS).
#
# Safe to run: installs nothing, needs no sudo, changes no settings.
# The only file it writes is the report (default: ./riftwire_environment.txt).
#
# Usage:  bash scripts/discover_env.sh [report_path]

set -u
REPORT="${1:-./riftwire_environment.txt}"
DATA_DIR="${RIFTWIRE_DATA_DIR:-$HOME}"

# Look in the common Homebrew prefixes too, in case the shell PATH lacks them.
PATH="$PATH:$HOME/.homebrew/bin:/opt/homebrew/bin:/usr/local/bin"

section() { printf '\n== %s\n' "$1"; }

# On macOS without Xcode Command Line Tools, /usr/bin/{git,python3,...} are stubs that
# open an install dialog when run. Never run them in that case.
CLT_OK=1
if [ "$(uname -s)" = "Darwin" ] && ! xcode-select -p >/dev/null 2>&1; then CLT_OK=0; fi
is_stub() { [ "$CLT_OK" = 0 ] && case "$1" in /usr/bin/*) return 0;; esac; return 1; }
ver() {  # ver <cmd> [version-flag]
  local c="$1" flag="${2:---version}" p
  p="$(command -v "$c" 2>/dev/null)"
  if [ -z "$p" ]; then printf '%-12s MISSING\n' "$c"; return; fi
  if is_stub "$p"; then printf '%-12s %s | macOS stub (Xcode CLT not installed)\n' "$c" "$p"; return; fi
  printf '%-12s %s | %s\n' "$c" "$p" "$("$c" "$flag" 2>&1 | head -1)"
}

{
  echo "RIFTWIRE environment report"
  echo "generated_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"

  section "macOS"
  sw_vers 2>/dev/null || uname -a

  section "Hardware"
  echo "chip:          $(sysctl -n machdep.cpu.brand_string 2>/dev/null)"
  echo "arch:          $(uname -m)"
  echo "cores_total:   $(sysctl -n hw.ncpu 2>/dev/null)"
  echo "cores_perf:    $(sysctl -n hw.perflevel0.physicalcpu 2>/dev/null)"
  echo "cores_eff:     $(sysctl -n hw.perflevel1.physicalcpu 2>/dev/null)"
  mem=$(sysctl -n hw.memsize 2>/dev/null || echo 0)
  echo "memory_gb:     $((mem / 1073741824))"
  echo "model:         $(sysctl -n hw.model 2>/dev/null)"

  section "Storage"
  df -h / "$DATA_DIR" 2>/dev/null
  free_kb=$(df -k "$DATA_DIR" 2>/dev/null | awk 'NR==2{print $4}')
  if [ -n "${free_kb:-}" ]; then
    free_gb=$((free_kb / 1048576))
    echo "free_gb_on_data_volume: $free_gb (floor 60 → usable for footage: $((free_gb - 60)) GB)"
  fi
  echo "filesystem: $(diskutil info / 2>/dev/null | awk -F: '/File System Personality/{gsub(/^ +/,"",$2);print $2}')"

  section "Developer tools"
  echo "xcode_clt: $(xcode-select -p 2>/dev/null || echo MISSING)"
  ver brew
  [ -n "$(command -v brew)" ] && echo "brew_prefix: $(brew --prefix 2>/dev/null)"
  ver git
  for py in python3 python3.11 python3.12 python3.13 python3.14; do ver "$py"; done
  ver node
  ver npm
  ver sqlite3
  py3="$(command -v python3 2>/dev/null)"
  if [ -n "$py3" ] && ! is_stub "$py3"; then
    echo "python_sqlite: $("$py3" -c 'import sqlite3;print(sqlite3.sqlite_version)' 2>/dev/null)"
  fi

  section "Media"
  ver ffmpeg -version
  ver ffprobe -version
  if command -v ffmpeg >/dev/null 2>&1; then
    echo "hwaccels: $(ffmpeg -hide_banner -hwaccels 2>/dev/null | tail -n +2 | tr '\n' ' ')"
    echo "videotoolbox_encoders: $(ffmpeg -hide_banner -encoders 2>/dev/null \
      | awk '/videotoolbox/{print $2}' | tr '\n' ' ')"
  fi

  section "Homebrew packages of interest"
  if command -v brew >/dev/null 2>&1; then
    brew list --versions 2>/dev/null | grep -Ei '^(python|ffmpeg|node|git|sqlite)' \
      || echo "(none of python/ffmpeg/node/git/sqlite installed via brew)"
  fi

  section "Existing RIFTWIRE folders (names only)"
  found=0
  for d in "$HOME"/riftwire* "$HOME"/Claude/riftwire* "$HOME"/Developer/riftwire*; do
    [ -e "$d" ] && { echo "$d"; found=1; }
  done
  [ "$found" = 0 ] && echo "(none found)"
} > "$REPORT" 2>&1

echo "Report written to: $REPORT"
