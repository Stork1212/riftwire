# Testing

All tests use **synthetic media generated with FFmpeg** (`testsrc2`, `sine`, `lavfi`), isolated
temporary data directories and a fresh SQLite database per test. No real footage is used.

## Everything at once

```bash
scripts/test.sh
```

Runs: Ruff → pytest → ESLint + `tsc --noEmit` → Vitest → `vite build`.

## Backend (pytest)

```bash
cd backend
.venv/bin/pytest -q                       # full suite (~35 s)
.venv/bin/pytest tests/test_worker_process.py -v   # real-process crash tests
.venv/bin/ruff check .
```

| File | What it covers |
|---|---|
| `test_media.py` | ffprobe metadata for MP4/MOV/MKV/WEBM, exact rational fps (30000/1001), VFR detection, no-audio, 0.2 s clip, rotation + anamorphic display size, corrupt/garbage/image/audio-only/truncated rejection, subprocess timeout + cancellation kill the process group, thumbnail aspect ratio |
| `test_storage_and_db.py` | 60 GiB default floor, configurable data dir, filename sanitizing, path-traversal guard, disk reservations, Alembic upgrade/downgrade, unmigrated DB detection, FK/WAL/busy_timeout pragmas, UTC timestamps, constraints |
| `test_ingest.py` | full pipeline, all formats, content-decided extension, duplicate, invalid media, unsupported type, size limit, insufficient disk (up-front and mid-stream), incomplete/interrupted uploads, path-traversal names, **64 MiB stream with < 8 MiB peak memory**, crash-matrix states (link-before-unlink, unlink-before-DB, both files missing, existing final file never overwritten, corrupted staged file), reconciler behaviour |
| `test_queue.py` | state-transition table, backoff, idempotent enqueue, CAS claiming, **8 concurrent claimers × 40 jobs → each exactly once**, retry limits, permanent errors, thumbnail-only failure, manual retry, **zombie attempt fenced out**, lease renewal, orphan killed *before* recovery, foreign-worker recovery, release |
| `test_worker_process.py` | real worker processes: pipeline + graceful stop, second worker refused (exit 3), unmigrated DB refused (exit 2), jobs survive restart, **hard crash (`os._exit`) at each finalize crash point → recovered, one file, correct hash**, crash between stage and DB commit, **SIGKILL worker → orphaned FFmpeg killed before retry**, PID-reuse guard, graceful shutdown releases the running job |
| `test_api.py` | liveness vs. readiness (unmigrated DB, missing FFmpeg), import flow, 409/413/415/422/507 errors, no server paths in error messages, pagination + validation, search, jobs, retry endpoint, 404s, events, system status, restricted CORS |

### Test-only hooks

Only active when `RIFTWIRE_ENV=test`:

- `RIFTWIRE_TEST_CRASH_AT=<point>` — hard-exit (`os._exit(137)`) at `after_stage_rename`,
  `after_intent_commit`, `finalize_before_link`, `finalize_after_link`, `finalize_after_unlink`.
- `RIFTWIRE_TEST_SLOW_FFMPEG_SECONDS=N` — THUMBNAIL jobs first run a real-time FFmpeg child of N
  seconds (to test orphan handling).

## Frontend (Vitest + Testing Library)

```bash
cd frontend
npm test            # vitest run
npm run lint && npm run typecheck && npm run build
```

`src/test/dashboard.test.tsx`: Overview (real counts, storage, worker, empty state, backend
unreachable), Recordings (empty state, formatted rows + thumbnails, search, detail with
frame rate and stage progress), Import (single upload despite repeated clicks, upload progress,
switch to processing progress, duplicate link, validation error), Processing Queue (status,
progress, attempts, errors, retry only for failed jobs, empty state), System (missing FFmpeg,
unresponsive worker). The API module is mocked in tests only; the app itself uses real data.

## End-to-end acceptance

```bash
backend/.venv/bin/python scripts/acceptance.py --report acceptance.json
```

Starts a real API, worker and the built dashboard against an **isolated temporary data
directory** (port 8765) and walks the Phase 1 acceptance scenario (19 steps): start, readiness,
dashboard served, import, progress, stored + hash + read-only, metadata vs. independent ffprobe,
thumbnail size, listing, jobs COMPLETED, restart, persistence, duplicate, invalid file,
**interrupted upload**, **SIGKILLed worker with orphaned FFmpeg**, disk floor, structured logs.

`--min-free-gib` defaults to the production 60 GiB. On a machine with less free space use a
smaller value (the dev container run used `--min-free-gib 1`).

Results are recorded in [`PHASE1_REPORT.md`](PHASE1_REPORT.md) and `docs/acceptance/`.
