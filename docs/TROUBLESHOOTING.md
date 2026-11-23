# Troubleshooting

Start with `scripts/status.sh`, the **System** page, and the JSON logs in `data/logs/`
(`api.log`, `worker.log`). Every error that matters is also stored as a system event
(`GET /api/v1/events?severity=ERROR`).

## Startup

| Symptom | Cause | Fix |
|---|---|---|
| `backend/.venv not found` | bootstrap not run | `scripts/bootstrap.sh` |
| bootstrap picks the wrong Python | several Pythons installed | `RIFTWIRE_PYTHON=/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13 scripts/bootstrap.sh` |
| `/health/ready` → `database.ok=false`, API returns `503 schema_not_ready` | migrations not applied | `scripts/migrate.sh` then restart |
| worker exits with code **2** | schema not at head | `scripts/migrate.sh` |
| worker exits with code **3**, "another worker holds …/worker.lock" | a worker is already running for this data dir | `scripts/status.sh`; stop the other one. The lock is released automatically if a worker dies, so a stale lock file is harmless. |
| `… already running (pid N)` from start.sh | previous run still alive | `scripts/stop.sh` |
| port 8000 in use | another app | set `RIFTWIRE_API_PORT` in `.env` |
| `ffmpeg MISSING` on System page | FFmpeg not installed / not on PATH | `brew install ffmpeg`, or set `RIFTWIRE_FFMPEG_PATH=/opt/homebrew/bin/ffmpeg` and `RIFTWIRE_FFPROBE_PATH=/opt/homebrew/bin/ffprobe` |

## Imports

| HTTP / code | Meaning | What to do |
|---|---|---|
| 415 `unsupported_media` | extension not MP4/MOV/MKV/WEBM | convert/remux the file |
| 422 `invalid_media` | not a decodable video by content (corrupt, truncated, audio-only, image…) | check the file in a player; re-export or re-record |
| 409 `duplicate_recording` | identical file (SHA-256) already imported | link in the error opens the existing recording |
| 413 `payload_too_large` | over `RIFTWIRE_MAX_UPLOAD_BYTES` | raise the limit if intended |
| 507 `insufficient_storage` | free space would drop under `RIFTWIRE_MIN_FREE_DISK_BYTES` (60 GiB) | free space, or move `RIFTWIRE_DATA_DIR` to an external APFS SSD |
| 503 `ffmpeg_missing` / `inspection_timeout` | tools missing / very slow media inspection | install FFmpeg; raise `RIFTWIRE_FFPROBE_TIMEOUT_SECONDS` / `RIFTWIRE_FFMPEG_TIMEOUT_SECONDS` |

Interrupted uploads (browser closed, network drop) delete their `.part` file immediately and
log an `upload_interrupted` event. Anything left behind by a crash is swept from `temp/` at the
next start once older than `RIFTWIRE_STALE_TEMP_SECONDS` (1 h).

## Jobs and recovery

| Symptom | Explanation / fix |
|---|---|
| Recordings stay **Staged** | worker not running → `scripts/start.sh` (imports are accepted without a worker; processing waits) |
| Job in **Retry wait** | transient error; it will retry with exponential backoff (5 s, 10 s, 20 s … max 10 min) up to 3 attempts |
| Job **Failed** | permanent error (e.g. hash mismatch, missing file) or attempts exhausted. Fix the cause, then **Retry** in the Processing Queue (`POST /api/v1/jobs/{id}/retry`) |
| Worker killed / Mac slept mid-job | automatic: on the next start the worker kills any orphaned FFmpeg (verified by PID + start time + name) and re-queues the job. A `lease_recovered` event records it. |
| Worker shows **Unresponsive** | no heartbeat for 20 s → process hung or killed. `scripts/stop.sh && scripts/start.sh` |
| Thumbnail **Failed** | recording stays READY; the dashboard shows a placeholder. Retry the THUMBNAIL job. |
| Event `orphan_recording_file` | a file in `recordings/` has no DB row. It is never deleted automatically; inspect and remove manually if unwanted. |
| Event `recording_file_missing` | a READY recording's file was removed outside RIFTWIRE. Restore it from backup. |
| INGEST failed with "SHA-256 mismatch" | stored/staged bytes differ from the upload — disk or manual tampering. Nothing was overwritten; the staged copy is kept in `temp/` for inspection. |

## Disk space

The floor counts only space `df` reports as free; macOS Finder may show more (purgeable
space). With ~70 GiB free and a 60 GiB floor, only ~9–10 GiB is importable on the internal SSD.
Move data to an external APFS SSD:

1. `scripts/stop.sh`
2. copy `data/` to the new location (e.g. `rsync -a data/ /Volumes/RiftSSD/riftwire-data/`)
3. set `RIFTWIRE_DATA_DIR=/Volumes/RiftSSD/riftwire-data` in `.env`
4. `scripts/start.sh`; check the System page shows the new data directory

Use APFS (not exFAT): hard links and atomic renames are required.

## Reset (destructive — development only)

Removing `data/` deletes all recordings and history. Do this only intentionally:
`scripts/stop.sh`, move `data/` aside (e.g. `mv data data.old-$(date +%s)`), `scripts/start.sh`.
