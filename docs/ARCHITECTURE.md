# RIFTWIRE — Architecture (Phase 1: Foundation — implemented, v0.1.0)

Status: **Approved in principle (2026-11-23)**, subject to three owner requirements, now
incorporated:

1. Crash-safe ingestion, including a crash between database insert and final file move → §6.
2. Worker crashes, expired leases and orphaned FFmpeg processes can never cause concurrent
   processing or corrupt recordings → §7.
3. The read-only macOS environment inspection must be complete before any dependency is
   installed → §16.

Implementation authorized 2026-11-23, verified on the target Mac, independently reviewed
twice, and merged into `main` as `3c52e53` (PR #1). See `docs/PHASE1_REPORT.md`. §17 lists
implementation details added within this design; §18 summarizes how the approved roadmap
extends it.

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
| Storage | 512 GB SSD, **~70 GiB free** at inspection | ≥ 60 GB free floor enforced → only ~9 GB usable internally; external APFS SSD for `RIFTWIRE_DATA_DIR` recommended |
| OS | macOS 26.5, M3 Pro | No Docker; native processes; Homebrew at `/opt/homebrew` (no sudo); project venv on Python 3.13 |

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
| staging_filename | `<import_id>.staged` in `temp/` until finalized; NULL afterwards |
| status | STAGED · PROCESSING · READY · FAILED (see §6) |
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
| lease_token | random per-attempt **fencing token**; every write by a running job must match it (§7) |
| process_pid, process_started_at | the FFmpeg/ffprobe child currently running for this attempt (§7.4) |
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

## 6. Ingestion flow (crash-safe)

**Owner requirement:** ingestion must survive a crash at any point, including between the
database insert and the final file move, without losing, duplicating, overwriting or
deleting a recording.

### 6.1 Design principle

The database row is written **before** the file is moved and acts as a write-ahead
**intent record**. Moving the file into `recordings/` is done by exactly one idempotent
operation, the `INGEST` job, which can be re-run any number of times and always converges.
The API never moves files itself, so there is never more than one actor finalizing a file.

### 6.2 Request path (API process)

```
POST /api/v1/recordings/import   (raw streamed body, ?filename=…)
 1. Pre-checks: extension allow-list, Content-Length ≤ max size,
    free space ≥ Content-Length + RIFTWIRE_MIN_FREE_DISK_BYTES (60 GB)        → 415/413/507
 2. Stream 1 MB chunks → temp/<import_id>.part, SHA-256 + byte count as it streams;
    free space re-checked every 256 MB; abort + delete .part on disconnect or limit
 3. fsync(.part)
 4. ffprobe: allowed container family, ≥1 real video stream (not cover art)  → 415/422
 5. ffmpeg: decode first frame (corrupt / undecodable codec)                   → 422
 6. rename .part → temp/<import_id>.staged          (atomic; marks "validated")
 7. ONE transaction:
      INSERT recordings(status=STAGED, sha256, staging_filename, stored_filename=<id>.<ext>)
      INSERT jobs(INGEST)
    sha256 UNIQUE violation → delete .staged, 409 with the existing recording id
 8. 201 Created (recording visible in dashboard as STAGED → processing)
```

Every file the API writes lives in `temp/`. Nothing in `recordings/` is ever written by the
API.

### 6.3 Finalize step (INGEST job, idempotent)

```
src = temp/<staging_filename>        dst = recordings/<stored_filename>
case src exists, dst missing   → verify SHA-256 of src
                                 os.link(src, dst)   # fails if dst exists: never overwrites
                                 fsync(dst), fsync(recordings/)  → then unlink(src), fsync(temp/)
case src exists, dst exists    → crash happened after link: verify dst hash == sha256,
                                 then unlink(src). If hash differs → FAILED, alert,
                                 touch nothing (never overwrite, never delete)
case src missing, dst exists   → crash after unlink: verify dst hash; done
case both missing              → recording FAILED "file missing", ERROR event; nothing deleted
then (fenced, one transaction) → staging_filename = NULL, status = PROCESSING,
                                 enqueue METADATA
finally                        → chmod dst 0444 (read-only: no later stage can modify it)
```

`os.link` + `unlink` is used instead of `rename` because `link` refuses to replace an
existing file, giving "never overwrite" at the filesystem level. `temp/` and `recordings/`
are always on the same volume (both inside `RIFTWIRE_DATA_DIR`).

**Fallback for filesystems without hard links** (`EPERM`/`ENOTSUP`/`EXDEV`/`EMLINK` from
`os.link`; added after review of PR #1). The final path never holds partial data:
1. Copy into an **attempt-unique staging file** on the destination volume,
   `recordings/.staging/<rec_id>.<lease_token>.partial` (`open(..., "xb")`), then fsync.
2. Verify the copy's SHA-256 (a mismatch is a transient error and is retried).
3. **Promote without overwriting:** under an exclusive `flock` on
   `recordings/.staging/<rec_id>.lock`, check that the final path does not exist, then
   `os.rename` (atomic on one volume). If it exists, leave it untouched and verify it.
4. fsync the directory, then unlink the temp source (same as the link path).

Incomplete staging files from dead attempts are removed by the reconciler (§6.5), because
their lease token no longer owns a RUNNING job.

### 6.4 Crash matrix

| Crash point | State left behind | Recovery |
|---|---|---|
| During upload (steps 1–3) | `temp/*.part`, no row | Startup sweep deletes `.part` files older than `RIFTWIRE_STALE_TEMP_SECONDS` |
| After step 6, before commit | `temp/*.staged`, no row | Startup sweep deletes `.staged` files with **no matching row** older than the threshold (never registered, so not a recording) |
| After commit, before INGEST runs | row STAGED + `.staged` + INGEST PENDING | Worker runs INGEST normally |
| During INGEST, before link | same as above, INGEST RUNNING | Lease expires (§7) → INGEST retried |
| After link, before unlink | file in **both** places | INGEST re-run verifies hash, unlinks temp copy |
| After unlink, before DB update | file only in `recordings/`, row STAGED | INGEST re-run verifies, updates row |
| Row STAGED but INGEST job missing (should be impossible — same transaction) | — | Startup reconciler re-enqueues INGEST (unique constraint makes it a no-op if present) |

### 6.5 Startup reconciler (API and worker)

1. `.part` older than threshold → delete (temp only).
2. `.staged` with no recording row and older than threshold → delete (temp only).
3. Recording rows in STAGED → ensure an INGEST job exists and is not terminal.
4. Files in `recordings/` with **no** row → `WARNING` event "orphan recording file";
   **never deleted** automatically.
5. READY rows whose file is missing → `ERROR` event; status unchanged, shown on dashboard.

Error mapping: unsupported type 415 · invalid/corrupt 422 · too large 413 · duplicate 409 ·
low disk 507.

The upload is a raw streamed body rather than multipart so the server writes the file once
(multipart parsing spools to a system temp file first, doubling disk writes on 20–50 GB files).

## 7. Job queue and worker (crash-safe execution)

**Owner requirement:** worker crashes, expired leases and orphaned FFmpeg subprocesses must
never cause concurrent processing of the same job or corrupt a recording.

### 7.1 Claiming

Compare-and-set inside `BEGIN IMMEDIATE`:

```sql
UPDATE jobs SET status='RUNNING', claimed_by=:worker, lease_token=:new_random_token,
       lease_expires_at=:now+lease, attempt_count=attempt_count+1, started_at=:now
WHERE id=:id AND status IN ('PENDING','RETRY_WAIT') AND next_attempt_at <= :now;
```

Only the claimer whose update affects 1 row runs the job. A fresh `lease_token` is generated
per attempt.

### 7.2 Fencing (prevents a "zombie" attempt from acting)

Every write a running job makes — progress, completion, failure, recording updates,
enqueueing the next stage — is a single transaction whose first statement is:

```sql
UPDATE jobs SET updated_at=:now WHERE id=:id AND status='RUNNING' AND lease_token=:token;
-- 0 rows → the attempt has lost its lease: roll back, kill its subprocess, stop.
```

So once a lease is recovered and the job re-claimed with a new token, the old attempt can no
longer change anything in the database, even if it is still alive (e.g. after a laptop wake).

### 7.3 Output safety (prevents a zombie from corrupting files)

- **Original recordings are never written by any job.** They are opened read-only and
  `chmod 0444` after finalize.
- Every generated file is written to an **attempt-unique** temp path
  (`temp/<job_id>.<lease_token>.<ext>`) and published with an atomic `os.replace` to its
  deterministic final path **inside the fenced transaction** (the write lock is held, so the
  lease cannot be recovered between the fence check and the rename).
- Two attempts can never write the same temp file; a zombie's leftover temp output is
  swept by age and never becomes a final file.

### 7.4 Subprocess (FFmpeg/ffprobe) control

- Children start with `start_new_session=True` (own process group) so the whole group can be
  killed; argument lists only; every call has a timeout → `SIGTERM` to the group, then
  `SIGKILL` after 5 s.
- The child's PID and start time are recorded on the job row (`process_pid`,
  `process_started_at`) before it does any work.
- If the lease-renewal thread fails a fence check, the worker kills the child group
  immediately and abandons the attempt.
- **Orphan cleanup (fence → terminate → transition).** Recovery never acts on information
  read earlier:
  1. **Fence:** one transaction re-checks eligibility (RUNNING, lease still expired or
     foreign) and only then replaces the lease token with a recovery token, reading the
     recorded PID in the same transaction. An owner that renewed first makes the job
     ineligible (skipped, nothing killed). An owner that renews later fails the token
     check and stops its own child.
  2. **Terminate:** kill the recorded child only if the PID is alive **and** its start time
     and executable (`ps -o lstart=,comm=`) match what was recorded (PID-reuse guard). If
     it cannot be confirmed dead (`kill_failed`), the job stays fenced and a later pass
     retries.
  3. **Transition:** RETRY_WAIT / FAILED by compare-and-set on the recovery token.
  If the recoverer dies after step 1, the recovery fence expires after 30 s and the job is
  recovered by a later pass.

### 7.5 Single worker process + lease recovery

- The worker takes an exclusive OS lock (`fcntl.flock` on `data/worker.lock`) at start. A
  second worker process exits with a clear error. The OS releases the lock automatically if
  the worker dies, so **holding the lock proves no other worker process is alive**.
- Concurrency (`RIFTWIRE_WORKER_CONCURRENCY`, default 1) is threads inside that one process.
- On startup (lock held): RUNNING jobs claimed by any other worker id are recovered
  immediately (orphan child killed first, §7.4) rather than waiting for lease expiry.
- While running: a heartbeat thread renews leases every `lease/3` s (fenced) and updates the
  `workers` row. Every poll also recovers RUNNING jobs whose lease has expired.
- Recovered jobs → RETRY_WAIT with backoff, or FAILED once `max_attempts` is reached; a
  `WARNING` event records the recovery.

### 7.6 Retries, idempotency, shutdown

- Transient errors (timeouts, I/O) → RETRY_WAIT with backoff `base × 2^(attempt-1)`,
  capped. Permanent errors (invalid/corrupt media, hash mismatch) → FAILED immediately.
- Handlers are idempotent: INGEST per §6.3; METADATA overwrites the same fields; THUMBNAIL
  skips if a valid output exists. Completed jobs are never re-claimed.
- SIGINT/SIGTERM: stop claiming, let the current job finish within a grace period, otherwise
  kill the child group and release the lease (job → RETRY_WAIT); mark worker STOPPED.
- macOS sleep/lid close: leases expire during sleep; on wake the renewal fence fails and the
  attempt kills its child and stops. Optional `caffeinate -i` wrapper in `scripts/start.sh`.

### 7.7 Pipeline

`INGEST` (finalize + verify, §6.3) → `METADATA` (full ffprobe → DB, recording READY) →
`THUMBNAIL`. Each stage enqueues the next inside its fenced completion transaction.

### 7.8 Crash-safety tests (required)

- Kill (`SIGKILL`) the worker mid-INGEST at each point in the crash matrix (§6.4) via test
  hooks; restart; assert exactly one stored file, correct hash, recording READY.
- Two workers racing to claim: each job executes exactly once.
- Zombie attempt: expire the lease, re-claim, then let the old attempt try to complete →
  rejected by the fence; final outputs come only from the new attempt.
- Orphaned FFmpeg: SIGKILL the worker while a long FFmpeg child runs; restart; assert the
  child is killed before the job is retried; PID-reuse guard tested with a mismatched start time.
- Second worker process refused by the lock.

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
run the ingestion reconciler (§6.5) → take the worker lock and recover leases/orphans (§7.5) → check FFmpeg → report readiness. Originals are never deleted
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

See §18 (approved roadmap) and `docs/PRODUCT_SPEC.md` / `docs/ROADMAP.md`.

## 16. Environment gate (before any install)

No dependency (Homebrew package, Python, Node) is installed until:

1. `scripts/discover_env.sh` (read-only: no installs, no sudo, no writes except its own
   report file) has been run on the development Mac;
2. the results are recorded in `docs/PROJECT_STATUS.md`;
3. the owner has approved the specific install list derived from them.

The script reports macOS version, chip, cores, memory, disk free on the data volume vs. the
60 GB floor, filesystem type, Homebrew prefix, Python (≥ 3.11 required), Git, FFmpeg/ffprobe
(versions, VideoToolbox support), Node/npm, SQLite version, and Xcode command-line tools.

## 17. Implementation notes (Phase 1 build, within the approved design)

No architectural changes were made. Details added during implementation:

- **Truncation check:** media validation decodes one frame at the start *and* near the end of
  the file; a recording cut off mid-capture is rejected (422) even though its first frame decodes.
- **Error hygiene:** FFmpeg/ffprobe stderr is scrubbed of internal paths before it reaches API
  error messages or job errors.
- **Worker states** in `workers`: RUNNING, STOPPING, STOPPED, DEAD (marked by the next lock
  holder); the API reports UNRESPONSIVE when the heartbeat is older than
  `RIFTWIRE_WORKER_STALE_AFTER_SECONDS` (20 s). Worker exit codes: 2 = schema not at head,
  3 = another worker holds the lock.
- **API startup** also runs the ingestion reconciler and recovers *expired* leases (it never
  recovers live foreign leases; only the lock-holding worker does that).
- **Test-only hooks** (`RIFTWIRE_ENV=test` only): named crash points and a slow-FFmpeg child,
  used by the real-process crash tests and `scripts/acceptance.py`.
- **Thumbnail endpoint** returns an SVG placeholder (`X-Thumbnail-Fallback: true`) while the
  thumbnail is pending or after it failed.
- **Reproducible installs:** `backend/constraints.txt` (pip) and `frontend/package-lock.json`.
- **Review round 1 (PR #1) fixes:**
  - recovery is fence → terminate → transition (§7.4);
  - the copy fallback stages, verifies and promotes without overwriting (§6.3);
  - attempt-scoped temp and staging files are cleaned by lease token, not by age; files
    are listed before tokens are read;
  - startup runs recovery before the reconciler.
- **Runtime files:** PID files in `data/run/`, raw process output in `data/logs/*.out`.

## 18. Planned evolution (approved roadmap; not implemented)

The roadmap (`docs/ROADMAP.md`, owner decisions D1–D9 in `docs/PRODUCT_SPEC.md`) was approved
in principle on 2026-11-23. It **extends** this architecture without replacing it:

| Concern | Phase 1 (implemented) | Planned extension |
|---|---|---|
| Work execution | SQLite queue: leases, fencing, recovery, orphan cleanup | Same queue; new job types (probe/acquire, transcribe, signals, highlight detection, render, publish), all idempotent and fenced |
| Storage | One `RIFTWIRE_DATA_DIR`; 60 GiB floor | Configurable recordings/temp/models/exports/assets locations; per-volume floor; cross-volume crash-safe finalize; offline-volume safety; 2 TB external APFS SSD |
| Sources | Local upload | Rights/provenance records with user confirmation; permitted acquisition only (no bypassing); selective VOD sections |
| AI | none | Provider interface (Ollama default; OpenAI/Anthropic optional), $0 default budget, usage ledger, per-game versioned prompts, evidence-validated outputs |
| Editing/publishing | none | Per-channel brand kits and presets; VideoToolbox renders; human-approved publishing via official APIs (YouTube Shorts first) |
| Storage lifecycle | Originals never auto-deleted; temp cleanup by token/age | **Retention manager** (Phase 5, PRODUCT_SPEC §6a): generated renders and their intermediates are deleted 72 h after the final API-verified publication to all intended targets. Hourly persistent sweep with overdue recovery; fenced intent → same-volume trash → unlink; volume identity verified; locks honoured; metadata and originals preserved; read-only verification only (no remote deletion) |

Each phase gets its own technical proposal for approval before implementation. A proposal
that changes this document's guarantees must say so explicitly.

**Note on deletion rules.** Phase 1's guarantee "never delete original recordings
automatically" is unchanged and also binds the retention manager. The retention manager is
the only component that may delete *generated* video files. Its eligibility rules (all
re-checked at deletion time) and crash-safety design are specified in PRODUCT_SPEC §6a and
will be detailed in the Phase 4/5 technical proposals.

