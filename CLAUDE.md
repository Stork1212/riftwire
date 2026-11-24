# CLAUDE.md — RIFTWIRE Engineering Instructions

This file is the standing brief for any AI or human engineer working in this repository
(Claude, ChatGPT reviewers, contributors). GitHub is the source of truth: if it is not
committed here, it is not done.

## 1. What RIFTWIRE is

A local-first, AI-powered gaming media automation platform. It turns authorized long-form
gameplay recordings (initially GTA VI) into short-form 9:16 videos and, in later phases,
publishes them to YouTube Shorts, Instagram Reels, Facebook Reels and X.

Phases (approved roadmap: [`docs/ROADMAP.md`](docs/ROADMAP.md); requirements and owner
decisions D1–D9: [`docs/PRODUCT_SPEC.md`](docs/PRODUCT_SPEC.md)):
1. **Foundation** — ingestion, metadata, thumbnails, persistent job queue, dashboard.
   **Complete (v0.1.0)**.
2. **Sources & Signals** — rights/provenance, permitted acquisition (local, authorized
   YouTube/Twitch, selective sections), configurable storage, local transcription, signals.
3. **Highlight Detection & Review** — AI provider interface ($0 budget), per-game prompts,
   evidence-backed highlights, review dashboard, exports.
4. **Editing Preset Studio & Rendering** — per-channel brand kits, 1080×1920 presets,
   subtitles, hooks, outro, VideoToolbox renders.
5. **Publishing** — YouTube Shorts first, human approval, official APIs.
6. **Analytics & Optimization**.

**Current:** Phase 1 complete. The next phase starts only after the owner approves its
detailed technical proposal. **Do not implement work from a later phase early.**

## 2. Target environment

- Apple Silicon MacBook Pro, 18 GB unified memory, 512 GB SSD (~70 GiB free at inspection), macOS.
- Apple M3 Pro, macOS 26.5. Homebrew at `/opt/homebrew`; no administrator access is assumed.
- Project Python: **3.13** (`python3.13`); the default `python3` is 3.15, too new for some wheels.
- All primary processing is local. Cloud AI is allowed later only where it clearly improves
  quality per dollar, and only within an owner-set budget (initially **$0**).
- **One heavy processing worker at a time** by default.
- **Keep ≥ 60 GB free** on the data volume (configurable `RIFTWIRE_MIN_FREE_DISK_BYTES`).
- Storage plan: a **2 TB external APFS SSD** for recordings, temp files, models and exports
  (configurable locations, Phase 2). Until then, ~9 GiB is importable on the internal SSD.

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
    Sources require a user-confirmed rights record (ownership, creator permission or
    licence). Use permitted acquisition methods only, and **never bypass access
    restrictions** (DRM, members-only, private, age/geo/login gates).
15. **$0 AI budget by default.** Local-first (Ollama, local Whisper). Paid providers are
    optional, off by default, usage-tracked and limit-enforced. **No paid API charge without
    explicit owner authorization.** API keys only in the macOS Keychain.
16. **Versioned prompts.** Separate, versioned highlight-detection and editing prompts per
    game. Every AI result records prompt version, provider, model and evidence.
17. **Optional integrations stay optional.** The core pipeline (clipping, editing, subtitles,
    branding, rendering) never depends on Higgsfield or any paid service.

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
- Adding any paid service or cloud AI call, or raising the AI budget above $0.
- Downloading AI models (any size during evaluation; larger models only after benchmarks).
- New project dependencies (pip/npm) beyond those already approved.
- Purchasing services.
- Starting a new phase (requires an approved technical proposal).

## 9. Key commands

```bash
scripts/bootstrap.sh              # create backend/.venv and frontend/node_modules
scripts/migrate.sh                # alembic upgrade head
scripts/start.sh                  # API + worker (+ built dashboard on :8000)
scripts/stop.sh
cd backend && .venv/bin/pytest    # backend tests
cd frontend && npm test && npm run build
scripts/test.sh                   # everything: lint, tests, build
backend/.venv/bin/python scripts/acceptance.py   # 19-step E2E (isolated data dir)
scripts/smoke_test.sh             # launch scripts + dashboard smoke test (isolated data dir)
```
