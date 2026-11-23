# Installation

Target: Apple Silicon Mac (verified environment: M3 Pro, 18 GB, macOS 26.5.2). Everything
except FFmpeg installs **inside the project folder**; nothing system-wide.

## 1. Prerequisites

| Dependency | Required | On the development Mac (2026-11-23) | How to get it |
|---|---|---|---|
| Python | ≥ 3.11 (3.13 recommended) | 3.13.2 at `/Library/Frameworks/Python.framework/Versions/3.13/` | python.org installer |
| Node.js / npm | Node ≥ 20 | 24.17.0 / 11.13.0 | nodejs.org installer |
| FFmpeg + ffprobe | any recent | 9.1 via Homebrew, VideoToolbox available | `brew install ffmpeg` |
| Git | any | 2.39.3 (Apple) | Xcode Command Line Tools |

Check everything at once (read-only):

```bash
bash scripts/discover_env.sh && cat riftwire_environment.txt
```

> The default `python3` on the development Mac is 3.15, which is too new for some compiled
> wheels. `scripts/bootstrap.sh` prefers `python3.13` automatically; override with
> `RIFTWIRE_PYTHON=/path/to/python3.x`.

## 2. Get the code

```bash
git clone https://github.com/Stork1212/riftwire.git ~/Claude/riftwire
cd ~/Claude/riftwire
```

## 3. Bootstrap

```bash
scripts/bootstrap.sh
```

Creates `backend/.venv` (backend + dev dependencies), installs `frontend/node_modules`
(`npm ci`, exact versions from `package-lock.json`), copies `.env.example` → `.env` if no
`.env` exists, and creates the data directories. Re-running is safe.

## 4. Configure (optional)

Edit `.env` (all keys documented in [`.env.example`](../.env.example)). Important ones:

| Variable | Default | Notes |
|---|---|---|
| `RIFTWIRE_DATA_DIR` | `data` (inside the repo) | Point at an external APFS SSD later, e.g. `/Volumes/RiftSSD/riftwire-data`. `temp/` lives inside it, so atomic moves stay on one volume. |
| `RIFTWIRE_MIN_FREE_DISK_BYTES` | `64424509440` (60 GiB) | Imports that would leave less free space are refused (HTTP 507). |
| `RIFTWIRE_MAX_UPLOAD_BYTES` | 50 GiB | Per file. |
| `RIFTWIRE_WORKER_CONCURRENCY` | `1` | One heavy job at a time. |
| `RIFTWIRE_API_HOST` / `PORT` | `127.0.0.1` / `8000` | Keep on localhost. |
| `RIFTWIRE_FFMPEG_PATH` / `FFPROBE_PATH` | `ffmpeg` / `ffprobe` | Absolute paths if not on `PATH` (Homebrew: `/opt/homebrew/bin/…`). |

Environment variables override `.env`.

## 5. Database migrations

```bash
scripts/migrate.sh            # alembic upgrade head
scripts/migrate.sh current    # show the current revision
```

The application never creates tables itself; if the schema is behind, `/health/ready`
reports it and the API answers `503 schema_not_ready`. `scripts/start.sh` runs migrations
automatically.

## 6. Start and stop

```bash
scripts/start.sh          # API + worker; dashboard at http://127.0.0.1:8000
scripts/start.sh --dev    # additionally Vite dev server at http://127.0.0.1:5173 (hot reload)
scripts/status.sh         # processes + readiness JSON
scripts/stop.sh           # graceful (SIGTERM); worker finishes or releases its job
RIFTWIRE_CAFFEINATE=1 scripts/start.sh   # keep the Mac awake while the worker runs
```

Logs: `data/logs/api.log`, `data/logs/worker.log` (JSON, rotating, 10 MB × 5), plus
`*.out` for raw process output. PID files: `data/run/`.

## 7. Data locations

```
data/
├── riftwire.db (+ -wal, -shm)   SQLite database
├── recordings/                  imported originals, <uuid>.<ext>, read-only, never auto-deleted
├── thumbnails/                  <recording-id>.jpg
├── temp/                        in-flight uploads (.part), staged files (.staged), job temp outputs
├── exports/                     reserved for later phases
├── logs/                        api.log, worker.log (+ rotations), *.out
├── run/                         PID files
└── worker.lock                  single-worker OS lock
```

Back up `riftwire.db` (with the app stopped, or using `sqlite3 data/riftwire.db ".backup x.db"`)
together with `recordings/`.
