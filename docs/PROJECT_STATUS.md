# RIFTWIRE — Project Status

_Last updated: 2026-11-23_

## Current phase
**Phase 1 — Foundation: planning.** Architecture **approved in principle** (2026-11-23,
after external review) subject to three requirements, now written into
`docs/ARCHITECTURE.md` (§6, §7, §16). No application code has been committed.

## Completed
- [x] GitHub repository `Stork1212/riftwire` connected; `main` verified (initial commit `faf765e`).
- [x] Phase 1 requirements received and reviewed.
- [x] Read-only environment inspection completed on the Mac (see below).
- [x] Proposed architecture, data model, queue design, API and dashboard (`docs/ARCHITECTURE.md`).
- [x] Engineering instructions and coding standards (`CLAUDE.md`).
- [x] Owner requirement 1 — crash-safe ingestion design: DB row as write-ahead intent,
      single idempotent finalize step (link → fsync → unlink), crash matrix, startup
      reconciler (ARCHITECTURE §6).
- [x] Owner requirement 2 — crash-safe execution design: per-attempt fencing tokens,
      attempt-unique output paths, process-group control of FFmpeg, PID-reuse-safe orphan
      cleanup, single-worker OS lock (ARCHITECTURE §7).
- [x] Owner requirement 3 — environment gate defined (ARCHITECTURE §16);
      `scripts/discover_env.sh` written (read-only, guards against the macOS
      Command Line Tools install prompt) and placed on the Mac at
      `~/Claude/riftwire-env/discover_env.sh`.

## Environment (development Mac) — inspected 2026-11-23 22:51 UTC

Source: `scripts/discover_env.sh`, run by the owner on the Mac (read-only).

| Item | Result | Phase 1 verdict |
|---|---|---|
| macOS | 26.5.2 (build 25F84) | OK |
| Chip | Apple M3 Pro, 11 cores (5 performance + 6 efficiency), model Mac15,6 | OK |
| Memory | 18 GB unified | OK |
| Disk | 460 GiB APFS container, **~70 GiB free** on the data volume | ⚠️ **Blocker for real footage** (see below) |
| Filesystem | APFS | OK (atomic link/rename supported) |
| Xcode Command Line Tools | installed (`/Library/Developer/CommandLineTools`) | OK |
| Homebrew | 6.0.3 at **`/opt/homebrew`** (standard prefix; the earlier `~/.homebrew` assumption was wrong) | OK |
| Git | 2.39.3 (Apple Git-146) | OK |
| Python | default `python3` = **3.15.2**; also **3.13.2** and 3.14.7 (python.org framework installs); no 3.11/3.12 | OK — use **3.13** for the project venv (see below) |
| SQLite | CLI 3.51.0; Python's `sqlite3` module 3.53.4 | OK (WAL, `RETURNING`, etc.) |
| Node.js / npm | v24.17.0 / 11.13.0 (`/usr/local/bin`, official installer) | OK |
| **FFmpeg / ffprobe** | **MISSING** | ❌ **Required — must be installed** |
| Existing RIFTWIRE folders | only `~/Claude/riftwire-env` (the inspection script) | No conflicts |

### Missing dependencies

| Dependency | Needed for | Proposed install (awaiting owner approval) |
|---|---|---|
| FFmpeg + ffprobe | metadata, validation, thumbnails, all later rendering | `brew install ffmpeg` |

That is the **only** missing system dependency. Notes:
- Homebrew's `ffmpeg` bottle is built with VideoToolbox (hardware encode/decode on the M3
  Pro), needed in Phase 3. It pulls in a number of codec libraries; expect roughly 1 GB of
  disk. Verify afterwards with `ffmpeg -hide_banner -hwaccels` (should list `videotoolbox`).
- Installing into `/opt/homebrew` needs no `sudo` as long as the owner's account owns that
  folder, which is normal for a standard Homebrew install.
- Everything else (FastAPI, SQLAlchemy, React, Vite…) installs **inside the project only**
  (`backend/.venv`, `frontend/node_modules`).

### Python version choice
The default `python3` is 3.15.2, which is very new; some compiled packages (e.g.
`pydantic-core`) may not yet publish wheels for it, which would force slow or failing source
builds. The project venv will be created explicitly with
`/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13` (3.13.2, mature wheel
support). Tests will be run on 3.13; 3.14 is a fallback. No new Python install is needed.

### Disk space — needs a decision before real footage is imported
- Free space on the data volume: **~69–70 GiB**. With the required **60 GB** safety floor,
  only **~9 GB** is usable for recordings — less than one hour of 1080p60 gameplay.
- `df` excludes APFS *purgeable* space, so Finder may show a higher "available" figure; the
  app (like `df`) counts only space that is guaranteed free.
- Phase 1 **development and automated tests are unaffected** (synthetic clips are a few MB).
- Options for the owner:
  1. **External APFS SSD** for `RIFTWIRE_DATA_DIR` (recommended; 1–2 TB keeps the internal
     SSD free and makes the 60 GB floor easy).
  2. Free internal space (e.g. old recordings, caches) to raise headroom.
  3. Temporarily lower `RIFTWIRE_MIN_FREE_DISK_BYTES` (not recommended; macOS degrades badly
     when the system disk is nearly full).

## Blockers
1. **Approval to install FFmpeg** (`brew install ffmpeg`) — the only missing dependency.
2. **Go-ahead for implementation** from the owner.
3. **Data location decision** (before importing real footage, not before development):
   internal SSD (~9 GB usable above the floor) vs. external APFS SSD.

## Decisions pending
- Project location on the Mac (proposed: `~/Claude/riftwire`; currently only
  `~/Claude/riftwire-env` exists there).
- Data location (see "Disk space" above).

## Next steps (after approval)
1. Scaffold backend + config + logging; tests.
2. Database models + Alembic initial migration; tests.
3. Media service (ffprobe/ffmpeg); tests with synthetic clips.
4. Ingestion service (streaming, hashing, dedupe, safety); tests.
5. Persistent job queue + worker (claims, leases, retries, recovery); tests incl. worker kill.
6. Thumbnails; tests.
7. REST API `/api/v1`; tests.
8. React dashboard; Vitest tests; production build.
9. Live acceptance run; `docs/PHASE1_REPORT.md`; remaining docs.

## Review notes for external reviewers
- Source of truth is this repository's `main` branch.
- Every claim of "working" must be backed by a test or a recorded acceptance step.
