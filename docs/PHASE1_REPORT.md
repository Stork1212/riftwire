# RIFTWIRE — Phase 1 Report (v0.1.0)

_Date: 2026-11-23 · Branch: `phase1/foundation` · Author: Claude (AI engineer), for owner review_

## 0. Bottom line

The Phase 1 foundation is implemented and **verified in the Linux development container**:
148 backend tests, 13 frontend tests, lint/typecheck, production build, a 19-step
real-process acceptance run and a real-browser UI check all pass.

**Not yet verified on the target Mac.** The agent can read and write files on the Mac but
cannot run macOS commands. The Mac has FFmpeg **9.1**, while the container has FFmpeg
**6.1**. Phase 1 should be considered complete only after the owner runs the macOS
verification in §9 and the results are recorded here.

### Re-verification from a fresh GitHub clone (2026-11-23, 23:40 UTC)

To rule out anything that existed only in the working copy, branch `phase1/foundation` at
`f49f7fa` was cloned fresh from GitHub into an empty directory and set up with
`scripts/bootstrap.sh` (pinned `constraints.txt`, `npm ci`), exactly as the owner would:

| Check | Result |
|---|---|
| `scripts/test.sh`: ruff, pytest, eslint, tsc, vitest, vite build | ✅ exit 0. **148 passed**, 0 failed (pytest); **13 passed**, 0 failed (vitest); build OK |
| `scripts/acceptance.py --min-free-gib 1` | ✅ **19/19 steps** ([JSON](acceptance/2026-11-23-fresh-clone-f49f7fa.json)) |

Failures: **none**. Only non-blocking warnings: a Starlette deprecation notice in tests and a
setuptools "editable install" notice during bootstrap.

## 1. What was implemented

| Area | Implementation |
|---|---|
| Configuration | Typed `pydantic-settings` (`RIFTWIRE_*` env / `.env`), `.env.example`, 60 GiB free-space floor default, configurable `RIFTWIRE_DATA_DIR`, localhost binding |
| Logging | JSON structured logs (timestamp, level, component, job_id, recording_id, message, error), rotating files `api.log` / `worker.log`; sensitive keys redacted; important events also stored in `system_events` |
| Database | SQLite via SQLAlchemy 2.x; Alembic migration `0001` (recordings, jobs, system_events, workers); FK enforcement, WAL, busy timeout, `BEGIN IMMEDIATE`; check constraints; UTC-aware timestamps; exact rational frame rate (`fps_num/fps_den`). App verifies schema at startup, never creates tables |
| Ingestion (§6) | Raw streamed upload (1 MB writes, never in memory), SHA-256 while streaming, extension allow-list + content validation (ffprobe container/video stream + decode at start **and** near end to catch truncation), duplicate detection, size limit, disk-space reservation + mid-stream re-check, sanitized display names, UUID stored names, DB intent row (STAGED) + INGEST job in one transaction, idempotent finalize (`link` → fsync → `unlink`, never overwrite), read-only originals, startup reconciler |
| Queue / worker (§7) | CAS claiming, per-attempt fencing tokens on every write, leases + heartbeat renewal, expired-lease and dead-worker recovery, FFmpeg in own process group with PID + start-time recorded, PID-reuse-safe orphan kill before retry, single-worker `flock`, exponential backoff, permanent vs. transient errors, manual retry, graceful shutdown that releases jobs, `workers` heartbeat table |
| Media | ffprobe metadata (duration, display resolution with rotation/SAR, exact fps + VFR flag, codecs, audio presence, container); thumbnails (aspect-preserving 480×270 box, deterministic path, skip if valid, fallback frame, SVG placeholder) |
| API | `/health`, `/health/ready`, `/api/v1/system/status`, recordings list/search/detail/import/thumbnail, jobs list/detail/retry, events; pagination `{items,total,limit,offset}`; consistent error JSON; restricted CORS; no absolute paths in responses or error messages; built dashboard served with SPA fallback |
| Dashboard | React 19 + TypeScript + Vite: Overview, Recordings (+ detail), Import (separate upload vs. processing progress, double-submit guard), Processing Queue (retry), System; dark theme, responsive, polling, empty/loading/error states, real data only |
| Scripts | `bootstrap.sh` (project-local only), `migrate.sh`, `start.sh` / `stop.sh` / `status.sh`, `test.sh`, `acceptance.py`, `discover_env.sh` |
| Docs | README, ARCHITECTURE, INSTALLATION, TESTING, TROUBLESHOOTING, this report, PROJECT_STATUS, screenshots |

## 2. Files created or modified

Code: ~3,100 lines of backend code (`backend/app`), ~1,430 lines of backend tests,
~1,520 lines of frontend code and tests (`frontend/src`), plus scripts and docs; 92 tracked
files. Key paths:

```
backend/app/core/{config,logging,errors,testhooks}.py
backend/app/db/{base,session,migrations}.py      backend/alembic/versions/0001_initial_schema.py
backend/app/models/entities.py                   backend/app/schemas/api.py
backend/app/services/{storage,media,ingest,queue,events,system}.py
backend/app/workers/{handlers,worker}.py         backend/app/api/routes.py   backend/app/main.py
backend/tests/{conftest,helpers,test_*}.py       backend/{pyproject.toml,constraints.txt,alembic.ini}
frontend/src/{App,main}.tsx  pages/*  components/*  services/*  types/*  test/*
scripts/*   .env.example   .gitignore (extended)   README.md   docs/*
```

## 3. Dependencies installed

| Where | What |
|---|---|
| **System (Mac), owner-approved** | `brew install ffmpeg` → FFmpeg/ffprobe **9.1** at `/opt/homebrew/bin`, hwaccels `videotoolbox vulkan`, encoders `h264_videotoolbox hevc_videotoolbox prores_videotoolbox` (verified from the owner's inspection report, 23:00 UTC) |
| Backend venv (project-local) | fastapi 0.165.0, uvicorn 0.54.0, SQLAlchemy 2.1.9, alembic 1.20.1, pydantic 2.14.4, pydantic-settings 2.16.3; dev: pytest 9.2.0, httpx 0.28.1, ruff 0.17.6. Pinned in `backend/constraints.txt` |
| Frontend (project-local) | react 19.3.1, react-router-dom 7.18.6; dev: vite 8.3.8, typescript 6.0.3, vitest 5.1.1, Testing Library, ESLint 10 + typescript-eslint. Pinned in `package-lock.json` |

Nothing else was installed system-wide. No AI models were downloaded and no paid services
are used.

## 4. Tests executed and results (dev container)

Environment: Ubuntu Linux x86_64, Python 3.13.18, FFmpeg 6.1.1, Node 22.23, Chrome (headless).

| Suite | Command | Result |
|---|---|---|
| Backend lint | `ruff check .` | ✅ clean |
| Backend tests | `pytest -q` | ✅ **148 passed** (~33 s) |
| Backend tests on Python 3.11 | separate 3.11.18 venv | ✅ 148 passed (minimum supported version) |
| Shell scripts | `shellcheck` | ✅ clean (found and fixed one real bug in `bootstrap.sh`) |
| Frontend lint + types | `eslint src`, `tsc -b --noEmit` | ✅ clean |
| Frontend tests | `vitest run` | ✅ **13 passed** |
| Production build | `vite build` | ✅ 284 kB JS (89 kB gzip), 7.8 kB CSS |
| End-to-end acceptance | `scripts/acceptance.py --min-free-gib 1` | ✅ **19/19 steps** ([JSON](acceptance/2026-11-23-linux-dev-container.json)) |

The acceptance run used a 1 GiB floor because the container has only ~30 GB free. The
60 GiB production default is covered by unit tests and by acceptance step X3.

### Acceptance steps (spec §14 + extras)

| # | Step | Result |
|---|---|---|
| 1–3 | Start backend, start worker, readiness + heartbeats | ✅ |
| 4 | Dashboard served by the API (+ assets, SPA deep links) | ✅ |
| 5–6 | Import 1080p 29.97 fps H.264/AAC clip; upload streamed in chunks; processing states PENDING→RUNNING→COMPLETED observed | ✅ |
| 7 | Stored in `recordings/`, SHA-256 matches, file read-only, `temp/` empty | ✅ |
| 8 | Metadata equals an independent ffprobe (1920×1080, 30000/1001, 12 012 ms, h264/aac) | ✅ |
| 9 | Thumbnail 480×270 JPEG | ✅ |
| 10–11 | Listed; INGEST/METADATA/THUMBNAIL all COMPLETED | ✅ |
| 12–13 | Restart API + worker; recording and job history persist | ✅ |
| 14–15 | Same video again → HTTP 409 `duplicate_recording` with existing id | ✅ |
| 16–17 | Random-bytes ".mp4" → 422 `invalid_media` (no server paths in message); `.txt` → 415; nothing stored | ✅ |
| 18 | Tests and builds pass (`scripts/test.sh`) | ✅ |
| X1 | Client disconnects at 33 % of upload → partial file deleted, `upload_interrupted` event | ✅ |
| X2 | Worker **SIGKILLed** during THUMBNAIL with a live FFmpeg child → FFmpeg survives as an orphan → restarted worker kills it (PID + start time verified) **before** retrying → job completes on attempt 2 | ✅ |
| X3 | Import refused with 507 when the floor would be breached | ✅ |
| X4 | `api.log` / `worker.log` are structured JSON | ✅ |

## 5. Features verified manually / visually

Using a real headless Chrome against the running stack (`scripts/start.sh`):
- All pages render with real data and no console errors. Screenshots are in
  `docs/screenshots/`.
- **Browser upload of a 111 MB 1440p60 synthetic MOV** through the Import page: the button is
  disabled during upload, processing stages are shown, and the recording becomes READY with
  correct metadata.
- Re-uploading the same file shows the duplicate message with a link to the existing
  recording.
- The mobile (390 px) layout was checked.
- Defects found this way and **fixed**: drop-zone layout, overflowing stage badges,
  underlined link-buttons, FFmpeg copyright text in version strings, and an absolute temp
  path leaking into an error message.

## 6. Implemented but NOT yet verified

| Item | Why not verified | How it will be verified |
|---|---|---|
| Everything on **macOS / Apple Silicon** | Agent cannot execute macOS commands | Owner runs §9 |
| Behaviour with **FFmpeg 9.1** | Container has 6.1. Output formats used (`-print_format json`, `framecrc`, `-display_rotation` in fixtures, `force_divisible_by`) are long-standing, but untested on 9.x | §9 test suite + acceptance on the Mac |
| `ps -o lstart=` / `comm=` output on macOS (orphan detection) | BSD `ps` differs from procps | `test_worker_process.py` + acceptance X2 on the Mac |
| `RIFTWIRE_CAFFEINATE=1` | macOS-only | Manual check on the Mac |
| Recordings larger than ~120 MB | Disk limits in the container; owner instruction to use small files | Streaming is memory-bounded by test (64 MiB with < 8 MiB peak); multi-GB files are deferred until an external SSD is available |
| Hard-link fallback copy path (filesystems without hard links) | APFS and ext4 both support links | Code path reviewed; not exercised |

## 7. Known issues

1. **Low disk headroom on the Mac:** ~69 GiB free against a 60 GiB floor leaves ~9 GiB for
   imports. Development is unaffected; real footage needs an external APFS SSD.
2. **Browser upload copies the file:** the original stays where it was recorded, so each
   recording uses double the disk space until the original is removed. An import-by-move
   or watch-folder option is proposed for Phase 2.
3. A stray 3.8 MB build file (`shellcheck_py…whl`) was committed in `f6e2644` and removed in
   `c828a53`. It stays in this branch's history; a **squash merge** keeps it out of `main`.
4. The Starlette test client prints a deprecation notice about `httpx`. This is harmless
   and only affects tests.
5. The thumbnail frame is taken at 10% of the duration (max 30 s). It is representative
   but not "best-frame"; smart selection belongs to Phase 2.
6. There is no authentication. That is intended for a localhost-only Phase 1; the API is
   bound to `127.0.0.1`.

## 8. Remaining risks

| Risk | Mitigation in place | Residual |
|---|---|---|
| Laptop sleep mid-job | lease expiry, fencing, orphan cleanup | Job restarts from the beginning (no resume for long jobs) |
| Disk filling during capture + import | 60 GiB floor, reservations, mid-stream checks | Other apps can consume space concurrently |
| SQLite write contention as volume grows | WAL, short transactions, single worker | Revisit if many workers are needed (unlikely on one Mac) |
| FFmpeg behaviour differences across versions | content-based checks, generous timeouts | Pending Mac verification (§9) |
| Very large originals × 2 copies | — | External SSD + import-by-move (Phase 2) |

## 9. Instructions to launch RIFTWIRE (and finish macOS verification)

```bash
git clone https://github.com/Stork1212/riftwire.git ~/Claude/riftwire
cd ~/Claude/riftwire
git checkout phase1/foundation           # until merged into main
scripts/bootstrap.sh                     # uses python3.13, npm ci, creates .env
scripts/test.sh 2>&1 | tee ~/Claude/riftwire-env/mac-tests.txt
backend/.venv/bin/python scripts/acceptance.py --report ~/Claude/riftwire-env/mac-acceptance.json
scripts/start.sh                         # open http://127.0.0.1:8000
```

The two output files land in a folder the agent can read; it will record the results here.

## 10. Recommended Phase 2 plan (requires owner approval)

Goal: turn stored recordings into **grounded, scored highlight candidates**, with no editing
or publishing yet.

1. **Import-by-move / watch folder** (OBS output folder → managed storage without copying)
   and an external-SSD data-dir migration helper.
2. **New job types on the same queue:** `PROXY` (low-res analysis proxy via VideoToolbox),
   `AUDIO_EXTRACT`, `TRANSCRIBE` (whisper.cpp or MLX Whisper, local, Metal-accelerated,
   one at a time to respect 18 GB), `AUDIO_EVENTS` (loudness spikes, laughter/shouting
   energy), `SCENE_DETECT` (FFmpeg scene scores / PySceneDetect).
3. **Candidate generator:** fuse signals into time-ranged candidates stored in a
   `clip_candidates` table with the evidence behind each score, so nothing is fabricated.
4. **Optional selective cloud ranking:** send only the top-N candidates, as short low-res
   proxies plus transcript, to a paid vision model behind a cost cap and an explicit approval
   gate.
5. **Review UI:** candidate timeline, preview playback, approve/reject (human in the loop),
   exported as a machine-readable clip manifest.
6. **Acceptance:** precision on a hand-labelled set of the owner's *authorized* recordings,
   cost per recording-hour, and throughput per hour on the M3 Pro.
