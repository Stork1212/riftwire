# RIFTWIRE — Architecture (Phase 1: Foundation)

Status: **Proposed — awaiting owner approval.** Implementation has not started.

## 1. Goals for Phase 1

Deliver a working local application that can:
- import gameplay recordings (MP4, MOV, MKV, WEBM) safely, including very large files;
- detect duplicates, reject invalid media with clear errors;
- extract technical metadata and generate thumbnails in a persistent background queue
  that survives restarts and crashes;
- show recordings, jobs, storage and system health on a local dashboard.

Explicitly out of scope: AI highlight detection, editing/rendering, publishing, third-party auth.

## 2. Hardware profile and constraints

| Resource | Value | Design consequence |
|---|---|---|
| CPU/GPU | Apple Silicon (arm64) | FFmpeg uses all cores per job; VideoToolbox HW encode for Phase 3 |
| Memory | 18 GB unified | Stream all media I/O; one heavy job at a time; Phase 2 ML models run sequentially and quantized |
| Storage | 512 GB SSD | ≥ 60 GB free floor enforced; data dir relocatable to an external APFS SSD |
| OS | macOS | No Docker; native processes; per-user Homebrew (no sudo) |

Storage reality check: 1 h of 1080p60 capture ≈ 10–25 GB, 4K60 HEVC ≈ 25–50 GB. With the
60 GB floor, the internal SSD realistically holds ~10–25 hours of 1080p footage. Plan an
external SSD for `RIFTWIRE_DATA_DIR` as volume grows.

## 3. System overview

```
 Browser ── http://127.0.0.1:8000 (built UI) / :5173 (Vite dev)
    │  REST /api/v1 + polling (~2 s)
    ▼
 ┌──────────────┐        ┌─────────────────┐        ┌──────────────────┐
 │ API process  │──SQL──▶│ SQLite (WAL)    │◀──SQL──│ Worker process   │
 │ FastAPI      │        │ recordings      │        │ claims jobs      │
 │ upload/valid.│        │ jobs (+leases)  │        │ INGEST→METADATA  │
 │ status/health│        │ system_events   │        │   →THUMBNAIL     │
 └──────┬───────┘        │ workers         │        │ heartbeats       │
        │                └─────────────────┘        └────────┬─────────┘
        └──────── data/ recordings · thumbnails · temp · exports · logs ┘
                            ffprobe / ffmpeg (subprocess)
```

Two OS processes, one database file. No brokers, no containers.

### Why this shape
- **SQLite + WAL** handles one writer and many readers comfortably on one machine, needs no
  server, and is trivially backed up. All write transactions use `BEGIN IMMEDIATE` and a
  busy timeout to avoid lock-upgrade deadlocks between API and worker.
- **Separate worker process** isolates FFmpeg crashes/hangs from the API and lets the queue
  keep running while the UI is closed.
- **Chained, idempotent jobs** form the pipeline skeleton later phases plug into
  (TRANSCRIBE, ANALYZE, RENDER, PUBLISH…).

## 4. Directory structure

```
riftwire/
├── backend/
│   ├── app/
│   │   ├── api/         # routers: health, system, recordings, jobs, events
│   │   ├── core/        # config (typed settings), logging, errors
│   │   ├── db/          # engine, pragmas, session helpers
│   │   ├── models/      # SQLAlchemy models
│   │   ├── schemas/     # Pydantic request/response models
│   │   ├── services/    # media, ingest, queue, storage, events, system
│   │   ├── workers/     # worker process + job handlers
│   │   └── main.py
│   ├── alembic/         # migrations
│   ├── tests/
│   └── pyproject.toml
├── frontend/
│   ├── src/{components,pages,services,types}
│   └── package.json
├── data/                # git-ignored; RIFTWIRE_DATA_DIR
│   ├── riftwire.db
│   ├── recordings/ thumbnails/ temp/ exports/ logs/
├── scripts/             # discover_env, bootstrap, migrate, start, stop
├── docs/
├── CLAUDE.md  README.md  .env.example  .gitignore
```

## 5. Data model

### recordings
| Column | Notes |
|---|---|
| id | UUID (text) |
| original_filename | sanitized display name only |
| stored_filename | `<uuid>.<ext>`, unique |
| file_path | relative to data dir (portable) |
| sha256 | unique → duplicate detection |
| file_size_bytes | |
| container_format | from ffprobe |
| duration_ms | |
| width, height | display dimensions |
| fps_num, fps_den | exact rational frame rate (e.g. 30000/1001) |
| is_variable_frame_rate | avg vs. real frame rate mismatch |
| video_codec, audio_codec | audio nullable |
| has_audio | |
| imported_at | UTC |
| status | IMPORTED · PROCESSING · READY · FAILED |
| thumbnail_status | PENDING · READY · FAILED |
| error_message | |

### jobs
| Column | Notes |
|---|---|
| id, recording_id (FK) | |
| job_type | INGEST · METADATA · THUMBNAIL |
| status | PENDING · RUNNING · COMPLETED · FAILED · RETRY_WAIT · CANCELLED |
| progress_percent | 0–100 |
| attempt_count, max_attempts | |
| created_at, updated_at, started_at, completed_at | UTC |
| next_attempt_at | backoff schedule |
| lease_expires_at, claimed_by | crash recovery |
| last_error | |

Unique `(recording_id, job_type)` → enqueueing is idempotent.

Allowed transitions (enforced in code):
```
PENDING    → RUNNING | CANCELLED
RUNNING    → COMPLETED | FAILED | RETRY_WAIT
RETRY_WAIT → RUNNING | CANCELLED
FAILED     → PENDING            (manual retry only)
COMPLETED, CANCELLED → (terminal)
```

### system_events
id, timestamp, severity, component, event_type, message, related_job_id, related_recording_id.

### workers *(addition)*
id, hostname, pid, concurrency, started_at, last_heartbeat_at, status, current_job_id —
real worker liveness for the dashboard.

Connection pragmas: `foreign_keys=ON`, `busy_timeout=15000`, `journal_mode=WAL`,
`synchronous=NORMAL`. Startup verifies the schema is at Alembic head and reports
"not ready" otherwise; it never creates tables itself.

## 6. Ingestion flow

```
POST /api/v1/recordings/import  (raw streamed body, filename in query)
 1. Check extension allow-list, Content-Length ≤ max size, free space ≥ size + 60 GB
 2. Stream 1 MB chunks → data/temp/<uuid>.part, SHA-256 as it streams,
    re-check free space periodically; abort + delete .part on disconnect/limit
 3. ffprobe: allowed container family, ≥1 real video stream (not cover art)
 4. ffmpeg: decode first frame (catches corrupt/undecodable codecs)
 5. Duplicate check by sha256 → 409 with existing recording id
 6. INSERT recording + INGEST job (one transaction), then atomic link/rename
    into data/recordings/<uuid>.<ext> (never overwrites)
 7. 201 Created → dashboard switches from "upload progress" to "processing progress"
```

The upload is a raw streamed body rather than multipart so the server writes the file once
(multipart parsing spools to a system temp file first, doubling disk writes on 20–50 GB
files). Stale `.part` files older than a configurable age are removed at API startup; files
in `recordings/` are never touched.

Error mapping: unsupported type 415 · invalid/corrupt 422 · too large 413 · duplicate 409 ·
low disk 507.

## 7. Job queue and worker

- **Claim:** compare-and-set `UPDATE jobs SET status='RUNNING', claimed_by=?, lease_expires_at=?,
  attempt_count=attempt_count+1 WHERE id=? AND status IN ('PENDING','RETRY_WAIT') AND
  next_attempt_at <= now`; only the worker whose update hits 1 row runs the job.
- **Lease + heartbeat:** a background thread renews the lease every lease/3 seconds and
  updates the worker heartbeat.
- **Completion** is conditional on still owning the lease, so a worker that lost its lease
  cannot overwrite a newer attempt.
- **Recovery:** on startup and every poll, RUNNING jobs with expired leases go to
  RETRY_WAIT (or FAILED if attempts are exhausted) and an event is logged.
- **Retries:** transient errors → backoff `base × 2^(attempt-1)` capped; permanent errors →
  FAILED immediately.
- **Concurrency:** `RIFTWIRE_WORKER_CONCURRENCY` (default 1).
- **Shutdown:** SIGINT/SIGTERM stops claiming, lets the current job finish within a grace
  period, marks the worker STOPPED. A hard kill is handled by lease expiry.

Pipeline: `INGEST` (verify stored file size + SHA-256) → `METADATA` (full ffprobe → DB,
recording READY) → `THUMBNAIL`. Each stage enqueues the next in the same transaction that
completes it.

## 8. Media handling

- **Metadata:** duration, display resolution (rotation and sample aspect ratio applied),
  exact frame rate, VFR flag, codecs, audio presence, container. Handles no-audio, VFR,
  very short, very large and odd-metadata files.
- **Thumbnails:** deterministic path `thumbnails/<recording_id>.jpg`; frame at ~10 % of
  duration (start for very short clips); scaled with `force_original_aspect_ratio=decrease`
  after square-pixel correction, so no distortion; skipped if a valid one exists; falls back
  to frame 0, then to a placeholder image served by the API. Thumbnail failure never
  affects the recording.
- All subprocess calls use argument lists, configurable timeouts, captured stderr.

## 9. API (`/api/v1`, bound to 127.0.0.1)

| Method | Path | Purpose |
|---|---|---|
| GET | /health | liveness |
| GET | /health/ready | readiness: DB, migrations, FFmpeg/ffprobe, data dirs |
| GET | /api/v1/system/status | version, worker status, counts, storage, recent errors |
| GET | /api/v1/recordings | paginated, searchable |
| GET | /api/v1/recordings/{id} | detail incl. jobs |
| POST | /api/v1/recordings/import | streamed upload |
| GET | /api/v1/recordings/{id}/thumbnail | JPEG or placeholder |
| GET | /api/v1/jobs | paginated, filter by status/type/recording |
| GET | /api/v1/jobs/{id} | |
| POST | /api/v1/jobs/{id}/retry | FAILED → PENDING |
| GET | /api/v1/events | paginated, filter by severity |

## 10. Dashboard

Dark, restrained gaming-media look; responsive. Pages: **Overview** (totals, job counts by
status, disk usage vs. 60 GB floor, worker status), **Recordings** (searchable table with
thumbnails + detail view), **Import** (upload progress separate from processing progress,
double-submit protection, validation errors), **Processing Queue** (jobs with retry),
**System** (version, backend/worker/FFmpeg status, data dir, storage, recent errors).
Polling only; real data only; meaningful empty states.

## 11. Configuration

Typed settings (`RIFTWIRE_*` env vars / `.env`): environment, API host/port, data dir,
database path, max upload size, min free disk (default 60 GiB), worker concurrency, retry
limits, backoff, lease duration, log level, FFmpeg/ffprobe paths and timeouts, thumbnail size.

## 12. Logging and recovery

JSON logs (timestamp, level, component, job_id, recording_id, message, error) to rotating
files in `data/logs/`. Startup sequence: verify DB + migrations → ensure directories →
recover expired leases → check FFmpeg → report readiness. Originals are never deleted
automatically.

## 13. Testing strategy

Backend (pytest, isolated temp dirs/DBs, synthetic FFmpeg fixtures): migrations, recording
creation, metadata extraction, duplicates, invalid media, large-file streaming
(memory-bounded), filename/path safety, insufficient disk, job state transitions,
concurrent claiming, retry limits, expired-lease recovery, real worker-kill recovery,
thumbnails, API validation and pagination.
Frontend (Vitest + Testing Library): dashboard rendering, import workflow, job status display;
`npm run build` must pass.
Acceptance: the 18-step Phase 1 scenario (start → import → verify → restart → duplicate →
invalid file → interrupted-job recovery), recorded in `docs/PHASE1_REPORT.md`.

## 14. Known risks / limits

- Browser upload keeps the original plus a managed copy (2× disk). Phase 2 proposal:
  watch-folder / import-by-move.
- Atomic rename requires `temp/` and `recordings/` on the same volume; external drives
  should be APFS.
- Laptop sleep kills the worker mid-job (recovered via leases); optional `caffeinate` wrapper.
- 18 GB memory is the binding constraint for Phase 2 ML; models must run sequentially.

## 15. Looking ahead (not Phase 1)

Phase 2 adds job types (TRANSCRIBE via whisper.cpp/MLX, AUDIO_PEAKS, SCENE_DETECT,
HIGHLIGHT_CANDIDATES) and a clip-manifest table with confidence scores — all grounded in
timestamps from real footage. Cloud AI is used only for final ranking where it measurably
beats local models.
