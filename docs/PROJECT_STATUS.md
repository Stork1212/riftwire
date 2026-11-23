# RIFTWIRE — Project Status

_Last updated: 2026-11-23_

## Current phase
**Phase 1 — Foundation: planning.** Architecture **approved in principle** (2026-11-23,
after external review) subject to three requirements, now written into
`docs/ARCHITECTURE.md` (§6, §7, §16). No application code has been committed.

## Completed
- [x] GitHub repository `Stork1212/riftwire` connected; `main` verified (initial commit `faf765e`).
- [x] Phase 1 requirements received and reviewed.
- [x] Partial environment inspection (see below).
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

## Environment (development Mac)
| Item | Status |
|---|---|
| Machine | Apple Silicon MacBook Pro (arm64) — verified |
| Memory / SSD | 18 GB / 512 GB — owner-reported |
| Homebrew | Present, per-user install at `~/.homebrew` — verified (folder exists) |
| macOS version | **not yet verified** |
| Free disk space | **not yet verified** |
| Python ≥ 3.11 | **not yet verified** (macOS system Python 3.9 is not sufficient) |
| Git | **not yet verified** |
| FFmpeg / ffprobe | **not yet verified** |
| Node.js / npm | **not yet verified** |

The AI agent's connection to the Mac provides file access via a sandbox, not a macOS shell
(and Terminal is click-only under computer use), so the inspection must be run once by the
owner:

```bash
bash ~/Claude/riftwire-env/discover_env.sh ~/Claude/riftwire-env/environment.txt
```

The agent can read `environment.txt` directly and will record the results here.

## Blockers
1. **Environment inspection** — owner runs the command above (requirement 3).
2. **Go-ahead for implementation** from the owner after reviewing the inspection results.
3. **Possible installs** (per-user Homebrew, needs owner approval): `ffmpeg`, `python@3.12`,
   `node` — only those found missing.

## Decisions pending
- Project location on the Mac (proposed: `~/Claude/riftwire` or a dedicated dev folder).
- Data location: internal SSD with 60 GB floor (default) vs. external APFS SSD.

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
