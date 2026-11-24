# RIFTWIRE — Project Status

_Last updated: 2026-11-24_

## Current phase
**Phase 2 — Sources & Signals: in progress.** Phases 2–3 are delivered as milestones M0–M7
(approved [`PHASE2_PROPOSAL.md`](PHASE2_PROPOSAL.md); phase numbering unchanged).

- **M0 (benchmark spike): complete.** The owner approved it on 2026-11-24 as a completed
  benchmarking milestone, with the documented timestamp-accuracy and memory limitations.
  It is **not** production acceptance of transcription or highlight detection. Results:
  [`BENCHMARKS.md`](BENCHMARKS.md). Tag `v0.2.0-m0`.
- **M1, Storage Architecture: merged (PR #9, 2026-11-24); physical-SSD acceptance
  pending.**
  - Merged after an independent review (five findings fixed, [`M1_REVIEW.md`](M1_REVIEW.md)
    §6) and Mac verification of the final code `53bece7`.
  - M1 is **not** declared complete and `v0.3.0-m1` is **not** tagged until the eight
    physical-SSD checks pass
    ([checklist §11](M1_STORAGE_CHECKLIST.md#11-acceptance-checks-that-depend-on-the-external-ssd-deferred)).
  - Design: [`ARCHITECTURE.md`](ARCHITECTURE.md) §19.
- **M2, Rights & Source Management: approved** (owner decisions R1–R6,
  [`M2_RIGHTS_SOURCES_PLAN.md`](M2_RIGHTS_SOURCES_PLAN.md) §12). In progress on branch
  `phase2/m2-rights-sources`.

### Merged state of `main` (2026-11-24)
| PR | Content | Reviewed head | Squash commit on `main` |
|---|---|---|---|
| #1 | Phase 1 Foundation | `4867e70` | `3c52e53` |
| #2 | Product spec + roadmap | — | `0ccf674` |
| #4 | Storage lifecycle & retention manager (D10), docs only | `f6c1219` | `1b84d80` |
| #3 | Phase 2 technical proposal (M0–M7) | `c53a9cb` | `6502a52` |
| #5 | M0 benchmark harness + results | `20184ca` | `a7554b3` |
| #9 | M1 Storage Architecture (SSD acceptance pending) | `53bece7` code (evidence head below) | squash on `main` |

Tags (both annotated, pushed by the owner): `v0.1.0` → `7ed5be7`; `v0.2.0-m0` → `0399a5a`.

### Combined verification before merging PR #5 (`main` @ `6502a52` + `20184ca`)
The merged tree was byte-identical to the verified tree (`e768cea`).

- `scripts/test.sh`: ruff clean; backend **169/169**; bench **48/48**; frontend **13/13**;
  lint, typecheck and production build OK.
- `scripts/acceptance.py`: **19/19**.
- `scripts/smoke_test.sh`: **28 passed, 0 failed, 1 skipped** (`caffeinate`, macOS only).

Both the acceptance and smoke scripts ran in the cloud container with an explicit test-only
floor of 1 GiB: the container has only about 28 GiB free, and at 60 GiB the floor correctly
refuses every import. On the Mac, these scripts passed at the 60 GiB floor (19/19 and 30/30
in Phase 1). The product floor is unchanged.

### Approved technical decisions (owner, 2026-11-24)
- **Transcription:** MLX Whisper. **base** is the provisional default; **small** is optional,
  pending chunked memory testing.
- **Timestamps:** raw word timestamps are preserved unchanged. Any correction needs
  independent validation on authorized real commentary.
- **Storage:** 60 GiB free-space floor, mandatory per volume. External storage is the
  **SanDisk 1 TB Portable SSD**; the architecture is capacity-independent.
- **Budgets:** benchmark storage 3.5 GB. Cloud AI **$0**. **Ollama deferred.**
- **Retention manager** (D10): design approved, documentation only. Not implemented
  (Phase 5).

### Storage on the Mac (measured 2026-11-24 04:33 UTC)
- **Free space** on `/System/Volumes/Data`: **62.48 GiB**, 2.48 GiB above the 60 GiB floor.
- **M0 benchmark** in `~/Claude/riftwire`:
  - `bench/.venv`: 597 MiB;
  - `data/models`: 679 MiB (whisper tiny, base, small);
  - `data/bench`: 725 MiB (datasets, TTS cache, signal videos).
  - Total about 2.0 GiB (2.06 GB), within the approved 3.5 GB. Everything is regenerable.
- `~/Claude/riftwire-env`: 18 MB (results, smoke data). `~/.cache/huggingface`: 380 KB.
- A leftover worktree folder `~/Claude/riftwire-m0` (1.9 MB, from the refused 22:40 run) is
  marked prunable. The owner may remove it with `git worktree prune` and delete the folder.
  The agent did not delete it.

### Remaining limitations (M0)
- **Synthetic data only:**
  - WER/timing come from macOS `say` speech;
  - signals come from easy hard cuts between test patterns;
  - nothing validates highlight quality.
  Authorized real gameplay remains a separate evaluation set.
- **Raw word timing fails the targets** for every model: starts are early by 51–283 ms, and
  base/small ends are early by about 110–150 ms. The constant-offset correction is
  synthetic-only and **not validated**.
- **Memory:** a single 60-minute pass with small reached a 7.2 GB footprint (MLX peak
  3.12 GB) and grew swap by 1 GiB. Chunked transcription is required before small can be
  used (M4).
- Whisper's temperature fallback causes small run-to-run WER differences (largest: tiny,
  5.8 % vs 6.5 %).
- No thermal data: `pmset` reports no CPU power status on this Mac.
- Internal headroom is only about 2.5 GiB above the floor. Real footage needs the external
  SSD (M1).

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
  1. **External APFS SSD** for the storage locations, moved with the M1 migration tool (the owner has ordered a SanDisk 1 TB Portable SSD; keeps the internal
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
- [x] Tag `v0.1.0` on `7ed5be7` (verified `main`), pushed by the owner (agent tag pushes are
      blocked by proxy policy).
- [x] Phase 2 technical implementation proposal: PR #3, approved and merged (`6502a52`).

## Phase 2 progress
- [x] Retention manager specification (D10), PR #4. Merged (`1b84d80`), documentation only.
- [x] **M0 benchmark spike**, PR #5. Merged (`a7554b3`).
  - Isolated `bench/.venv` (mlx-whisper 0.4.3, no PyTorch); hash-pinned dependencies.
  - Free-space watchdog on every volume; git-safety checks; per-volume peak estimate.
  - Official Mac run 2026-11-24 04:04 UTC: 24/24 transcription runs plus signals.
    Reproduced by the 03:47 run.
  - Approved by the owner as a completed benchmarking milestone.
- [x] Tag `v0.2.0-m0` (annotated) → `0399a5a`, verified `main`; pushed by the owner
      2026-11-24 05:32 UTC.
- [x] M1 checklist approved by the owner (2026-11-24), with decisions:
  - no exFAT override;
  - start without the SSD;
  - concurrent-safe, verified promotion;
  - migration dry-run by default, never deleting.
- [x] **M1 Storage Architecture**, merged via PR #9 (physical-SSD acceptance pending):
  - **Locations:** five configurable locations. The database is the authority, every process
    loads the layout first, and stored recording paths are unchanged.
  - **Identity:** markers plus the OS volume UUID; APFS required on macOS.
  - **Offline safety:** nothing is created or written on an absent or wrong drive; 503
    responses; readiness reports it; jobs wait or defer without consuming attempts.
  - **Reservations:** persistent and per volume (`BEGIN IMMEDIATE`, leases, reclaim). The
    60 GiB floor is mandatory outside `env=test`.
  - **Transfer:** staged copy, SHA-256 re-read from disk via a held fd, atomic `link`
    promotion. Concurrency is tested with threads and separate processes.
  - **Migration:** `scripts/migrate_storage.py`, dry run by default, resumable through a
    database journal, full re-hash, one-transaction switch, never deletes.
  - **Dashboard:** Storage page; `GET /api/v1/storage`.
  - **Verified in the cloud container:**
    - `scripts/test.sh`: backend **221** (after the final review), bench 48, frontend **17**, build;
    - acceptance **22/22** (new storage steps M1–M3, real processes);
    - smoke 28 passed, 1 macOS-only skip.
    - The real end-to-end migration of `exports` to a second filesystem was checked in a
      browser.
  - **First Mac run (owner, 2026-11-24 06:15 UTC) failed at startup.** On macOS
    `diskutil info` accepts only mount points or devices, not folders inside a drive, and
    the probe failure was fatal. Fixed: the mount point now comes from `df -P`, the
    filesystem from `mount`, and the volume UUID from `diskutil` on the mount point. A probe
    failure now leaves that location offline and never crashes startup. Tests use real macOS
    output formats.
  - **Second Mac run (owner, 2026-11-24 06:22 UTC, `29f612e`):**
    - smoke **30/30**; acceptance 21/22; pytest 207 passed, 1 failed, 2 skipped.
    - Both failures were test-side, and the product behaved correctly:
      - a simulated probe reported a different UUID for the real internal disk, which was
        correctly refused as a swapped drive;
      - M3 compared against the `/var` spelling of a path the tool had correctly stored as
        `/private/var`.
    - Fixed in the tests. The 2 skips are the real second-filesystem tests (macOS has no
      `/dev/shm`); they run on Linux and are covered by the SSD checks.
  - **Mac verification, internal disk (owner, 2026-11-24 06:28 UTC, `9fc026f`): passed.**
    - `scripts/test.sh`: backend 208 passed + 2 skipped (the real second-filesystem tests;
      macOS has no `/dev/shm`); bench 47 + 1 skipped; frontend 17/17; build OK.
    - **Acceptance 22/22 at the 60 GiB floor** (FFmpeg 9.1, VideoToolbox), including
      storage steps M1–M3.
    - Evidence: `docs/acceptance/2026-11-24-macos-m1-9fc026f-*`.
  - **Final engineering review** ([`M1_REVIEW.md`](M1_REVIEW.md)): 11 findings fixed with
    tests. Two were high severity:
    - thumbnails failed when temp is on another drive;
    - a drive unplugged mid-migration could have its folders recreated on the internal
      disk.
    Documentation inconsistencies were corrected as well.
  - **Mac verification of the reviewed commit `4703e22`** (2026-11-24, pinned script): all
    stages exit 0. Backend 218 passed + 3 skipped (2nd-filesystem), bench 47 + 1 skip,
    frontend 17, build, acceptance 22/22 at the 60 GiB floor, smoke 30/30. Evidence:
    `docs/acceptance/2026-11-24-macos-m1-4703e22/`.
  - **Independent review of `4703e22`** (2026-11-24): five findings (three high, two
    medium) and one lock-ordering gap, all confirmed in source, fixed, and covered by 18
    regression tests that fail on `4703e22`. Details: [`M1_REVIEW.md`](M1_REVIEW.md) §6.
    Container: backend 239 + 1 macOS-only skip, acceptance 22/22, smoke 28/28.
  - **Mac verification of the corrected code `53bece7`** (2026-11-24): all stages exit 0.
    Backend 239 passed + 3 skipped, bench 47 + 1, frontend 17, acceptance 22/22 at the
    60 GiB floor, smoke 30/30. Evidence: `docs/acceptance/2026-11-24-macos-m1-53bece7/`.
  - Re-review passed; merged into `main` 2026-11-24.
  - **Not yet verified:**
    - every physical-SSD check (checklist §11).

## Blockers
None for development. **M1 physical-SSD acceptance** waits for the SanDisk 1 TB SSD
(owner-formatted APFS). Real footage at scale needs the SSD.

## Decisions pending
- When the SSD arrives: approval of the physical-SSD acceptance run (checklist §11). No real
  data is moved and no drive is formatted by the agent.
- Separately and later: migrating real recordings to the SSD.
- Open questions in PRODUCT_SPEC §7 (first YouTube channel, Ollama and model size when
  un-deferred, Higgsfield use cases, brand assets).

## Next steps
1. M2 implementation on `phase2/m2-rights-sources` (draft PR), following the plan's §9
   sequence and §12 decisions.
2. When the SSD arrives: physical-SSD acceptance (checklist §11), then tag `v0.3.0-m1`
   (owner).

## Review notes for external reviewers
- Source of truth is this repository's `main` branch.
- Every claim of "working" must be backed by a test or a recorded acceptance step.
