# CLAUDE.md — RIFTWIRE Engineering Instructions

This file is the standing brief for any AI or human engineer working in this repository
(Claude, ChatGPT reviewers, contributors). GitHub is the source of truth: if it is not
committed here, it is not done.

## 1. What RIFTWIRE is

A local-first, AI-powered gaming media automation platform. It turns authorized long-form
gameplay recordings (initially GTA VI) into short-form 9:16 videos and, in later phases,
publishes them to YouTube Shorts, Instagram Reels, Facebook Reels and X.

Phases:
1. **Foundation** (current) — ingestion, metadata, thumbnails, persistent job queue, dashboard.
2. Analysis — transcription, scene/audio analysis, highlight candidates.
3. Editing — vertical reframing, subtitles, hooks, rendering (VideoToolbox).
4. Publishing — official platform APIs, scheduling, human approval gates.
5. Analytics — performance tracking feeding back into highlight selection.

**Do not implement work from a later phase early.** No AI clipping, auto-editing or social
publishing in Phase 1.

## 2. Target environment

- Apple Silicon MacBook Pro, 18 GB unified memory, 512 GB SSD (~70 GiB free at inspection), macOS.
- Apple M3 Pro, macOS 26.5. Homebrew at `/opt/homebrew`; no administrator access is assumed.
- Project Python: **3.13** (`python3.13`); the default `python3` is 3.15, too new for some wheels.
- All primary processing is local. Cloud AI is allowed later only where it clearly improves
  quality per dollar.
- **One heavy processing worker at a time** by default.
- **Keep ≥ 60 GB free** on the data volume (configurable `RIFTWIRE_MIN_FREE_DISK_BYTES`).

## 3. Stack (Phase 1)

| Layer | Choice |
|---|---|
| Backend | Python 3.11+, FastAPI, Uvicorn, Pydantic v2, pydantic-settings |
| Database | SQLite (WAL) via SQLAlchemy 2.x, migrations with Alembic |
| Queue | SQLite-backed job table with leases; separate worker process |
| Media | FFmpeg / ffprobe via `subprocess` (argument lists, never a shell) |
| Frontend | React + TypeScript + Vite; polling, no WebSockets yet |
| Tests | pytest + HTTPX (backend), Vitest + Testing Library (frontend) |
| Lint | Ruff (Python), `tsc --noEmit` + ESLint (frontend) |

Not allowed without an approved design change: Docker, Kubernetes, Redis, PostgreSQL, cloud
hosting, message brokers, extra microservices.

## 4. Non-negotiable engineering rules

1. **Reliability first.** Every stage observable (logs + `system_events`), testable,
   recoverable, idempotent.
2. **Never delete original recordings automatically.** Not on failure, not on cleanup.
3. **Never load whole videos into memory.** Stream in chunks.
4. **Atomic file writes.** Write to `data/temp/` on the same volume, validate, then rename.
   Never overwrite an existing managed file.
5. **Validate media by content** (ffprobe + first-frame decode), not by extension alone.
6. **No shell interpolation of user input.** `subprocess.run([...], timeout=...)`, capture
   stderr, no `shell=True`.
7. **User filenames are metadata only.** Stored names are generated (`<uuid>.<ext>`). Guard
   against path traversal on every path built from data.
8. **UTC everywhere**, timezone-aware datetimes; store rational frame rates exactly
   (`fps_num/fps_den`).
9. **Schema changes only through Alembic migrations.** The app never auto-creates tables.
10. **Jobs are idempotent.** Re-running a completed job must not duplicate rows or corrupt
    files. One job per (recording, job_type).
11. **Permanent errors are not retried** (invalid/corrupt media); transient errors retry with
    exponential backoff up to `max_attempts`.
12. **Never fabricate data.** No mock data or invented statistics in the running app; later
    phases must ground every highlight, timestamp and caption in actual footage.
13. **Bind to `127.0.0.1` only.** No permissive CORS. No secrets in code, logs or commits.
14. **Respect content rights.** Only authorized footage; official APIs for publishing.

## 5. Coding standards

### Python
- Python ≥ 3.11, full type hints, `from __future__ import annotations`.
- Ruff clean (`ruff check backend`), line length 100.
- Layering: `api/` (HTTP only) → `services/` (business logic) → `models/` / `db/`.
  Routes contain no business logic; services contain no FastAPI imports.
- Settings only via `app.core.config.get_settings()`; no `os.environ` reads elsewhere.
- Logging via `app.core.logging.get_logger(...)` with structured `extra=` fields
  (`job_id`, `recording_id`, `component`). No `print`.
- Keep DB transactions short; never hold a transaction open while FFmpeg runs.
- Raise typed errors from `app.core.errors`; the API maps them to consistent JSON:
  `{"error": {"code": "...", "message": "...", "details": {...}}}`.

### TypeScript / React
- Strict TypeScript; no `any` without a comment explaining why.
- API calls live in `src/services/`; shared types in `src/types/` mirroring backend schemas.
- Components are function components; pages compose components; no data fetching inside
  presentational components.
- Every list/page has loading, error and empty states.

### API
- Versioned under `/api/v1`. Lists return `{items, total, limit, offset}`.
- `/health` = liveness; `/health/ready` = dependency readiness (DB, migrations, FFmpeg, dirs).
- Do not expose absolute filesystem paths from media endpoints.

## 6. Testing rules

- Every feature ships with tests; run the full suite before every commit.
- Use temporary directories and an isolated SQLite DB per test.
- Media fixtures are generated synthetically with FFmpeg (`testsrc`, `sine`). Never commit
  real recordings; never use the owner's footage as fixtures without permission.
- Required coverage areas are listed in `docs/ARCHITECTURE.md` §13.
- **Do not claim a feature works unless it has been executed and verified.** Reports must
  separate *verified* from *implemented but untested*.

## 7. Git workflow

- `main` is always releasable: tests and builds pass.
- Work on short-lived branches (`phase1/<topic>`), merge via PR when possible.
- Conventional commit messages: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`.
- Never commit: `data/`, `*.db`, media files, `.env`, `node_modules/`, `.venv/`, logs.
- Update `docs/PROJECT_STATUS.md` at the end of every working session.

## 8. Approval gates (ask the owner first)

- System-wide installs (Homebrew packages, global npm/pip).
- Anything destructive: deleting files outside `data/temp/`, rewriting git history,
  force-pushing, dropping tables.
- Architecture changes to this document or `docs/ARCHITECTURE.md`.
- Adding any paid service or cloud AI call.
- Starting a new phase.

## 9. Key commands (once implemented)

```bash
scripts/bootstrap.sh              # create backend/.venv and frontend/node_modules
scripts/migrate.sh                # alembic upgrade head
scripts/start.sh                  # API + worker (+ built dashboard on :8000)
scripts/stop.sh
cd backend && .venv/bin/pytest    # backend tests
cd frontend && npm test && npm run build
```
