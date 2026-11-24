# RIFTWIRE — Project Status

_Last updated: 2026-11-24_

## Current phase
**Phase 1 — Foundation: complete and merged into `main`** (PR #1, squash commit `3c52e53`).
Verified on the target Mac (169/169 backend, 13/13 frontend, build, 19/19 acceptance at 60 GiB)
and accepted after two independent reviews. The roadmap and product spec (PR #2) are merged
(`0ccf674`).

**Release candidate:** `v0.1.0` = `main` @ `7ed5be7` (launch scripts and dashboard verified
on the Mac). The tag is pushed by the owner.
**Next:** review of the Phase 2 technical proposal (PR #3). **Phase 2 is not authorized.**

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
| FFmpeg / ffprobe | **9.1** at `/opt/homebrew/bin` (installed 2026-11-23, owner-approved); hwaccels `videotoolbox`; encoders `h264/hevc/prores_videotoolbox` | OK (re-inspected 23:00 UTC) |
| Existing RIFTWIRE folders | only `~/Claude/riftwire-env` (the inspection script) | No conflicts |

### Missing dependencies (resolved)

| Dependency | Needed for | Proposed install (awaiting owner approval) |
|---|---|---|
| FFmpeg + ffprobe | metadata, validation, thumbnails, all later rendering | `brew install ffmpeg` |

That was the **only** missing system dependency; it is now installed. Notes:
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
  1. **External APFS SSD** for `RIFTWIRE_DATA_DIR` (recommended; the owner has ordered a SanDisk 1 TB Portable SSD; keeps the internal
     SSD free and makes the 60 GB floor easy).
  2. Free internal space (e.g. old recordings, caches) to raise headroom.
  3. Temporarily lower `RIFTWIRE_MIN_FREE_DISK_BYTES` (not recommended; macOS degrades badly
     when the system disk is nearly full).

## Phase 1 build progress (2026-11-23)
- [x] FFmpeg 9.1 installed on the Mac by the owner (approved); VideoToolbox verified from the
      inspection report (`h264/hevc/prores_videotoolbox`).
- [x] Backend: config, JSON logging, DB + Alembic 0001, crash-safe ingestion, media service,
      persistent queue with fencing + orphan cleanup, worker process, REST API.
- [x] Dashboard: Overview, Recordings + detail, Import, Processing Queue, System.
- [x] Scripts: bootstrap, migrate, start/stop/status, test, acceptance, discover_env.
- [x] Docs: README, INSTALLATION, TESTING, TROUBLESHOOTING, PHASE1_REPORT, screenshots.
- [x] Dev-container verification: 148 backend tests (also on Python 3.11), 13 frontend
      tests, lint/typecheck, build, **19/19 acceptance steps**, real-browser UI check.
- [x] Re-verified from a **fresh GitHub clone** of `f49f7fa`: 148 + 13 tests, build, 19/19
      acceptance steps, no failures.
- [x] **macOS verification** (owner-run, 2026-11-24 01:46 UTC, commit `9d1e8c5`): macOS 26.5.2,
      M3 Pro, Python 3.13.2, FFmpeg 9.1 — ruff clean, **148/148** pytest, **13/13** Vitest, build
      OK, **19/19 acceptance at the 60 GiB floor**, VideoToolbox detected. Evidence in
      `docs/acceptance/2026-11-23-macos-m3pro-9d1e8c5*`.
- [x] Fixed the React `act(...)` test warning from the Mac log (test-only change; suite
      re-run: 148 + 13 passed, build OK).
- [x] Independent review round 1 (ChatGPT, PR #1 at `6d16b85`): 2 findings.
- [x] Fixed: recovery race (fence → terminate → transition) and crash-safe copy fallback
      (staging + verify + no-overwrite promote), plus 3 related cleanup/ordering issues.
      18 new deterministic tests; 166 + 13 tests pass; build OK; acceptance 19/19 (dev).
- [x] macOS run of `599f910`: acceptance 19/19 at 60 GiB, but pytest 165/166. A macOS-only
      zombie-process bug in the new `kill_failed` check. Fixed, with 3 tests including a
      BSD-semantics emulation that reproduces the Mac failure on any OS; 169 + 13 pass (dev).
- [x] **macOS re-run on `10ea57b`** (owner, 02:22 UTC): ruff clean, **169/169** pytest, **13/13** Vitest, build OK, **19/19 acceptance at 60 GiB**.
- [x] Replied to the review on PR #1 (implementation, tests, macOS evidence).
- [x] **Second independent review** (ChatGPT, at `4867e70`): both findings resolved; no new
      merge-blocking issues.
- [x] PR #1 squash-merged into `main` as `3c52e53` (tree identical to the reviewed head).
- [x] PR #2 (product spec + roadmap) squash-merged as `0ccf674` (no conflicts).
- [x] CLAUDE.md, ARCHITECTURE.md (§18) and this file updated for the approved roadmap.
- [x] macOS smoke test on `main` @ `0ae8c2f`: `scripts/smoke_test.sh` **30/30 PASS** (start.sh,
      status.sh, stop.sh, restart persistence, caffeinate, localhost-only, dashboard serving).
- [x] Dashboard checked in the built-in browser on the Mac: all pages render with real data,
      thumbnails load, no console errors (`docs/acceptance/2026-11-24-macos-dashboard-check.md`).
- [ ] Tag `v0.1.0` on `7ed5be7` (verified `main`). Pushing tags is blocked for the agent's
      session (proxy policy 403); the owner pushes the tag from the Mac.
- [x] Phase 2 technical implementation proposal: PR #3 (`docs/PHASE2_PROPOSAL.md`), awaiting review and approval. No Phase 2 code.

## Blockers
None. Before importing **real footage** at scale, the external APFS SSD (ordered: SanDisk 1 TB Portable SSD) is needed (only
~9 GiB is importable above the 60 GiB floor on the internal SSD).

## Decisions pending
- Approval of the Phase 2 technical proposal, including its listed dependency and
  model-download approvals.
- Open questions in PRODUCT_SPEC §7 (first YouTube channel, Ollama install and model size,
  SSD timing, Higgsfield use cases, brand assets).

## Next steps
1. Review of the Phase 2 technical proposal, PR #3 (ChatGPT + owner) → approval or amendments.
2. Owner decisions listed in the proposal (§0, §19, §22).

## Review notes for external reviewers
- Source of truth is this repository's `main` branch.
- Every claim of "working" must be backed by a test or a recorded acceptance step.
