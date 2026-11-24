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
| Storage | 512 GB SSD, **~70 GiB free** at inspection | ≥ 60 GiB free floor enforced per volume → only ~9 GB usable internally; storage locations move to an external APFS SSD with the verified migration tool (§19) |
| OS | macOS 26.5, M3 Pro | No Docker; native processes; Homebrew at `/opt/homebrew` (no sudo); project venv on Python 3.13 |

Storage reality check: 1 h of 1080p60 capture ≈ 10–25 GB, 4K60 HEVC ≈ 25–50 GB. With the
60 GB floor, the internal SSD realistically holds ~10–25 hours of 1080p footage. Storage
locations move to the external SSD with the migration tool (§19); the database, logs and
thumbnails stay internal.

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
| Storage | One `RIFTWIRE_DATA_DIR`; 60 GiB floor | **Implemented in M1 (§19)**: configurable recordings/temp/models/exports/assets locations; volume identity; per-volume floor; persistent reservations; cross-volume crash-safe transfer; offline-volume safety; verified migration. External APFS SSD (ordered: SanDisk 1 TB Portable SSD), capacity-independent |
| Sources | Local upload | Rights/provenance records with user confirmation; permitted acquisition only (no bypassing); selective VOD sections |
| AI | none | Provider interface (Ollama default; OpenAI/Anthropic optional), $0 default budget, usage ledger, per-game versioned prompts, evidence-validated outputs |
| Editing/publishing | none | Per-channel brand kits and presets; VideoToolbox renders; human-approved publishing via official APIs (YouTube Shorts first) |
| Storage lifecycle | Originals never auto-deleted; temp cleanup by token/age | **Retention manager** (Phase 5, PRODUCT_SPEC §6a): generated renders and their intermediates are deleted 72 h after the final API-verified publication to all intended targets. Hourly persistent sweep with overdue recovery; fenced intent → same-volume trash → final eligibility/lock gate + unlink fingerprint → volume/file identity check → unlink (blocked → restore, never overwrite; identity mismatch → held for the owner; recovery from `unlinking` re-verifies identity); volume identity verified; locks honoured; metadata and originals preserved; read-only verification only (no remote deletion) |

Each phase gets its own technical proposal for approval before implementation. A proposal
that changes this document's guarantees must say so explicitly.

**Note on deletion rules.** Phase 1's guarantee "never delete original recordings
automatically" is unchanged and also binds the retention manager. The retention manager is
the only component that may delete *generated* video files. Its eligibility rules (all
re-checked at deletion time) and crash-safety design are specified in PRODUCT_SPEC §6a and
will be detailed in the Phase 4/5 technical proposals.

---

## 19. Storage architecture (M1, implemented)

Owner decisions (2026-11-24):
- APFS is required for every location on macOS, with **no exFAT override**.
- The database, application, logs and thumbnails stay internal.
- The 60 GiB floor is mandatory on every volume.
- Physical SSD acceptance is deferred until the drive arrives.

### 19.1 Locations and the layout
- **Location kinds:** `recordings`, `temp`, `models`, `exports`, `assets`.
- **Authority:** the `storage_locations` table, one row per kind, holding path, location
  UUID, volume UUID, the OS's volume UUID, filesystem and floor.
- **Startup:** every process (API, worker, migration tool) calls `locations.load_layout`
  before touching any file, and the layout is applied to `Settings`. `recordings_dir`,
  `temp_dir`, etc. resolve through it.
- **Stored paths** stay relative (`recordings/<id>.mp4`). `Settings.managed_path` resolves the
  first component through the layout, with traversal protection. Moving a location never
  rewrites recording rows.
- **Registration:** only the internal defaults (`<data_dir>/<kind>`) are registered
  automatically. External locations are set **only** by the verified migration tool (§19.6).
  There is no API to change a location: changes need both processes stopped.
- **Startup directories:** `ensure_internal_dirs` creates internal directories only. A
  deleted internal default folder is recreated with its recorded identity. An external
  folder is **never** created or recreated.
- **No fallback path:** once a layout is loaded, `Settings.location(kind)` for an
  unregistered kind raises `StorageOffline`; it never returns the internal default path.
  Every write path calls `get_layout()`, which raises if no layout is loaded.
- **Drive probe failures** (for example `diskutil` errors) never crash startup. The kind
  stays unregistered/offline, and an event is recorded.

### 19.2 Volume identity and offline safety
- Each location root holds `.riftwire-location.json`: kind, location UUID, volume UUID and
  the OS volume UUID. It is written atomically and exclusively (`link(2)`), and never
  overwritten.
- `locations.verify` checks, read-only, in order. Any failure makes the location
  **offline**, with a stable reason code:
  1. the drive is mounted (`/Volumes/<name>` must be a mount point);
  2. the root exists;
  3. the marker exists, is readable, passes strict schema validation (exact keys,
     format 1, known kind, UUID fields, ≤ 64 KiB) and matches the row. An invalid marker
     means offline (`marker_unreadable`), never an exception (review fix 5). Markers are
     validated before they are written too;
  4. the OS volume UUID matches (catches a different drive with a copied folder). On macOS:
     mount point from `df -P`, filesystem from `mount`, VolumeUUID from
     `diskutil info -plist <mount point>` (diskutil rejects plain folders). All read-only.
     - On macOS the VolumeUUID is **required** (review fix 2). If diskutil fails or returns
       no valid UUID, the probe raises `IdentityUnavailable`: registration is refused, the
       location reports `probe_failed`, and a migration stops.
     - A row recorded without a UUID reports `identity_unrecorded`. The one exception:
       internal defaults inside the data directory get their UUID recorded at startup,
       because the internal disk cannot be swapped. External locations are never adopted
       this way.
     - **Identity caching (macOS).** Probing costs three subprocesses, so results are
       cached for up to 10 s. The key is the volume root's identity: device, root path,
       root inode and root creation time, never the device number alone.
     - A different drive mounted at the same path with a reused device number has a
       different root creation time. It misses the cache and is probed afresh. A result
       is stored only if the volume root is unchanged after the probe.
     - On Linux, which has no creation time, nothing is cached.
     - The first fix for the review removed caching altogether. That made a full Mac test
       run 7× slower, so the cache came back with this key.
  5. the filesystem is allowed (`apfs` on macOS);
  6. the root is writable.
- **Nothing falls back.** An offline location raises `StorageOffline` (HTTP 503
  `storage_offline`) for writes. Readiness reports `storage_offline:<kind>`. The dashboard
  shows the reason. Nothing is redirected to another location, and no folder is created.
- **Worker:** each job type declares the locations it needs (INGEST: temp + recordings;
  METADATA: recordings; THUMBNAIL: recordings only, because thumbnails are rendered next to
  their internal final location and never via temp).
  - The claim loop skips job types whose locations are offline (cached check, 2 s). Those
    jobs stay queued with their attempts intact. Events `storage_offline` / `storage_online`
    are recorded, and a reconcile runs on remount.
  - A job that loses its drive mid-way, or raises while its drive is now offline, is
    **deferred** (`queue.defer`): RETRY_WAIT with the attempt **not** counted.
- **Reconciler:** never lists, cleans or creates anything in an offline location. It records
  `reconcile_skipped_offline`.

### 19.3 Persistent per-volume disk reservations
- **Table:** `disk_reservations`, shared by the API, the worker and the migration tool.
- **Admission:** in one `BEGIN IMMEDIATE` transaction, `free − Σ active(volume) − bytes ≥
  floor(volume)`. Concurrent callers in any process serialize on the SQLite write lock.
  `floor(volume)` is the strictest of the configured floor and every location's floor on
  that volume.
- **Admission before writing (review fix 3):** no byte is written that was not admitted.
  - Uploads of known length reserve the full length up front.
  - Uploads of unknown length are admitted in windows of 256 MiB. Before any write that
    exceeds the admitted credit, the next window is admitted first.
  - At `finish()` the floor is checked once more, and the upload is refused if another
    program has since used the space.
- **Leases:** `bytes` is what the holder may still write. Holders renew with their remaining
  bytes every 256 MB and every 60 s, *before* the next write, re-checking the floor. Rows expire (default 15 min) and
  are reclaimed, so a crashed holder never blocks space forever. A renewal after reclamation
  re-admits under the same id.
- **Holders:**
  - uploads (`upload:<id>`, temp volume);
  - cross-volume finalize (`job:<id>:<token>`, recordings volume; renewed during the copy
    by `reservations.Keeper`);
  - thumbnails (`job:<id>:<token>`, 4 MiB on the internal volume);
  - migrations (`migration:<id>`, destination volume; renewed during each copy and after
    each file).
- **Floor:** below 60 GiB is refused at settings validation unless `RIFTWIRE_ENV=test`.

### 19.4 Crash-safe cross-volume transfer (`services/transfer.py`)
```
src ─copy─► <dest>/.staging/<name>.<token>.partial   (O_EXCL, fsync, fsync dir)
    ─verify─► SHA-256 of the STAGED file, re-read from disk via an fd kept open
    ─promote─► link(staged, dst)  — atomic; EEXIST if dst exists; never overwrites
    ─► fsync dst dir; unlink staged; fsync staging dir
```
- **Guard:** callers pass a `guard` that re-verifies the destination's identity immediately
  before promotion: the recordings location for finalize, the destination marker for
  migrations. This narrows the window in which a swapped drive could receive the file.
- **Concurrency:** `link(2)` refuses an existing target in the kernel. With concurrent
  attempts (threads or processes), exactly one promotes; the others verify the winner's file
  (`already_present`) or fail permanently on different content.
- **Swap protection:** the descriptor used for verification stays open until after
  promotion, so the inode cannot be recycled. The staged path must still be the same
  regular file (device, inode, size, mtime, ctime) as the one verified.
- **No hard links:** a filesystem without them fails permanently ("APFS required"). There is
  no non-atomic fallback.
- **Ingest finalize:** same volume uses `link` as before. EXDEV (temp and recordings on
  different drives) means: reserve on the recordings volume, then transfer. Crash points
  `transfer_copy_midway`, `transfer_before_verify`, `transfer_verified` and
  `transfer_after_promote` are covered by real-SIGKILL tests.

### 19.5 Maintenance lock
`storage-maintenance.lock` (OS lock, released if the holder dies) is held by the migration
tool for its whole run. The tool takes it **first**, then the worker lock, and only then
checks that no API is running (review: the earlier order left a window for an API to start
between the check and the lock). While it is held:
- the worker refuses to start;
- uploads are refused (503);
- an API started meanwhile skips lease recovery and the reconciler, so it can never delete a
  file being migrated.

### 19.6 Verified migration (`scripts/migrate_storage.py`, `services/migration.py`)
- **Dry run by default:** `plan` is strictly read-only. It loads the layout without
  registering anything, and creates no folders and no rows.
- **Destination checks:**
  - absolute path, and not the current location;
  - no overlap with any location;
  - the drive is mounted, and nothing is created directly under `/` or `/Volumes`;
  - filesystem allowed and writable;
  - empty, or the same unfinished migration's folder;
  - free space minus the planned bytes stays at or above the floor. On resume the planned
    bytes are only what is still to be copied (files without a full-size copy at the
    destination), minus the run's own stale partials, which are removed before their file
    is copied again (review fix 4).
- **`--execute`:**
  1. Takes the maintenance lock, then the worker lock, then refuses if the API answers on
     its port.
  2. Writes the journal row **first**, so every later crash point is resumable. Then it
     creates the destination one level at a time below the planned anchor, which must
     still be the same device as at planning, and writes the marker.
     - The destination drive's OS volume UUID is recorded in the journal
       (`dest_hw_volume_uuid`) the first time it is identified.
     - Every later check compares the drive's current identity against it (on macOS
       possibly served from the same-volume-root cache, which a replacement drive never
       hits): on resume, before each file,
       before each promotion, before cleanup, before the final re-hash and before the
       switch. A marker copied to another drive never authorizes that drive (review fix 1).
     - The switch records the approved UUID, never whatever happens to be mounted.
  3. Journals every file in `storage_migration_items` (PENDING → VERIFIED, committed per
     file).
  4. Transfers each file under a destination reservation (§19.4). Recordings are verified
     against the SHA-256 recorded at import.
  5. Re-hashes every destination file.
  6. Switches the `storage_locations` row in **one transaction**.
- **Resumable:** re-running continues and never re-copies verified files, unless a
  verified file is missing or changed at the destination, in which case it is copied again.
  A stale `.partial` left by a hard crash (own token) is removed first. `--abort` gives up
  and leaves the location unchanged.
- **Drive vanishes mid-run:** subfolders are created one level at a time below the verified
  destination root, never with `parents=True`. The destination identity (marker and OS
  volume UUID) is re-checked before each file, before each promotion, before cleanup,
  before the final re-hash and before the switch. A vanished drive
  stops the run and nothing is recreated.
- **Never deletes originals:** they are listed for the owner. Migrating real user data needs
  separate owner approval.
- **Journal choice:** the journal lives in the database rather than a JSONL file, so each
  file's state change is transactional.

### 19.7 Dashboard
Storage page, read-only:
- each drive: free/total, floor, reserved, headroom, below-floor warning;
- each location: folder, drive, filesystem, online/offline with reason, usage;
- an offline banner.

`GET /api/v1/storage` returns the same data, and `/api/v1/system/status` includes it.

### 19.8 Write inventory: floor and no-fallback guarantees

| Write | Volume | Online check | Floor / reservation |
|---|---|---|---|
| Upload `.part` (`temp`) | temp | before open; every 256 MB / 60 s | `upload:<id>`, renewed with remaining bytes |
| Stage rename `.part` → `.staged` | temp | before rename | none needed (no new data) |
| Finalize, same volume (`link`) | recordings | job start | none needed (no new data) |
| Finalize, cross-volume (`transfer`) | recordings | job start + guard before promote | `job:<id>:<token>`, full size, renewed during copy |
| Thumbnail (`.job-*.tmp.jpg` → `<id>.jpg`) | internal | recordings at job start | `job:<id>:<token>`, 4 MiB |
| Migration copies | destination | before each file and promotion | `migration:<id>`, remaining bytes, renewed |
| Identity markers, `.staging` folders, lock and pid files | various | root verified first | tiny; not gated |
| SQLite database (WAL) and rotating logs (≤ 60 MB) | internal | n/a | **explicit exception:** not gated, so the app can always record state and errors; both are small |

Folders are never created with `parents=True` below a location root, and never under an
absent `/Volumes/<name>`. Every path is resolved through the loaded layout.

### 19.9 Residual risks and race windows (documented, accepted)

Verification and the action that follows it are separate system calls. RIFTWIRE narrows the
windows between them; it does not claim to close them. The remaining windows:

- **Check-then-write (uploads, staged copies).**
  - Data is written through a file descriptor opened after verification.
  - If the drive is swapped later, writes to that descriptor fail with an I/O error. They
    cannot land on the new drive.
  - The window is between verification and `open()`: microseconds.
- **Check-then-promote (`link(2)`).**
  - The guard re-verifies the drive's marker and OS volume UUID immediately before
    `link`. A swap in the microseconds between the guard and `link` is not excluded.
  - In that case `link` either fails (the staged file is not on the new drive) or creates
    the name on the old, detached drive. It never overwrites a file.
- **Check-then-delete (reconciler, migration cleanup of its own partial).**
  - Deletion targets RIFTWIRE-generated names (`<uuid>.part`,
    `<name>.<token>.partial`, `.job-<id>-<token>.tmp.jpg`) in a location verified just
    before.
  - A swap in between could only remove a file with the same generated name on the other
    drive. In practice that means a clone of our own temporary files.
  - Recordings and other originals are never deleted.
- **Migration switch.** The destination is verified (marker and OS volume UUID) immediately
  before the switch transaction. A swap after the switch makes the location `wrong_volume`
  (offline), never writable.
- **Byte-identical clones:** a block-level clone of an APFS volume can carry the same
  VolumeUUID *and* the copied marker. It would be accepted as the same drive; its content is
  identical by construction.
- **Reservation leases.**
  - Every holder renews before its lease can lapse, *before* writing the next bytes
    (uploads), or at least every 60 s during a copy (`Keeper`).
  - An expired reservation is re-admitted only through the full floor check.
  - Remaining window: during a cross-volume copy, `Keeper` renews between chunks. A
    single chunk write that itself stalled longer than the lease (15 min, for example on
    a hung device) could complete after its reservation was reclaimed. Its size is
    bounded by one chunk.
- **Space used by other programs** is not reserved. It is seen at the next admission or
  renewal, and at upload finalization, which then refuses rather than going below the
  floor.
- **Physical behaviour is not yet exercised:** real I/O errors on unplug, sleep/wake
  unmounts and multi-GB throughput are pending the physical-SSD acceptance gate (checklist
  §11).
