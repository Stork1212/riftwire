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

Phases 2 and 3 are delivered as milestones **M0–M7** ([`docs/PHASE2_PROPOSAL.md`](docs/PHASE2_PROPOSAL.md),
approved): M0–M4 belong to Phase 2, M5–M7 to Phase 3. Phase numbering is unchanged.

**Current:** Phase 2 in progress.
- **M0 (benchmark spike):** complete (v0.2.0-m0); results in
  [`docs/BENCHMARKS.md`](docs/BENCHMARKS.md).
- **M1 (Storage Architecture):** merged (PR #9) after independent review and Mac
  verification. Its **physical-SSD acceptance gate stays open** until the SanDisk SSD
  arrives ([`docs/M1_STORAGE_CHECKLIST.md`](docs/M1_STORAGE_CHECKLIST.md) §11); no
  `v0.3.0-m1` tag until then.
- **M2 (Rights & Source Management):** approved (decisions R1–R6, `docs/M2_RIGHTS_SOURCES_PLAN.md`
  §12); in progress.
- Each milestone needs owner approval before it starts. **Do not implement work from a later
  milestone or phase early.**

## 2. Target environment

- Apple Silicon MacBook Pro, 18 GB unified memory, 512 GB SSD (~70 GiB free at inspection), macOS.
- Apple M3 Pro, macOS 26.5. Homebrew at `/opt/homebrew`; no administrator access is assumed.
- Project Python: **3.13** (`python3.13`); the default `python3` is 3.15, too new for some wheels.
- All primary processing is local. Cloud AI is allowed later only where it clearly improves
  quality per dollar, and only within an owner-set budget (initially **$0**).
- **One heavy processing worker at a time** by default.
- **Keep ≥ 60 GiB free on every volume RIFTWIRE writes to** (`RIFTWIRE_MIN_FREE_DISK_BYTES`).
  The floor is mandatory; only isolated test runs may lower it, explicitly. Every file write
  goes through the persistent per-volume reservations (`services/reservations.py`); a
  location is resolved only through the loaded layout and never falls back to another
  location (ARCHITECTURE §19).
- Storage plan: an **external APFS SSD** for recordings, temp files, models and exports
  (configurable locations, Phase 2). Ordered: **SanDisk 1 TB Portable SSD**. Storage logic is
  **capacity-independent**: never assume a drive size; use measured free space and per-volume
  floors. Until it arrives, ~9 GiB is importable on the internal SSD.

### Approved technical decisions (owner, 2026-11-24, after M0)
- **Transcription engine: MLX Whisper (`mlx-whisper`).** **`base` is the provisional
  default.** `small` is an optional higher-quality model, pending chunked memory testing.
  `tiny` is not used.
- **Raw word timestamps are preserved unchanged.** Any timestamp correction is a separate,
  versioned layer and needs independent validation on authorized real commentary.
- **60 GiB floor mandatory per volume.** Benchmark storage budget: **3.5 GB**.
- **Cloud AI budget: $0.** **Ollama remains deferred.**
- **External storage: SanDisk 1 TB Portable SSD**, capacity-independent architecture.
- M0 is a completed benchmarking milestone, **not** production acceptance of transcription
  or highlight detection.

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
   Generated videos may be deleted automatically **only** by the retention manager (Phase 5)
   under `docs/PRODUCT_SPEC.md` §6a: verified publication to every intended target, 72 h
   default, no active job or lock (re-checked at a final gate before every unlink, restoring
   the file if blocked), SSD identity verified, and never remote content.
3. **Never load whole videos into memory.** Stream in chunks.
4. **Atomic file writes.** Write a temporary file on the **same volume** as the destination,
   validate it, then promote it without overwriting (`link(2)`). Across volumes, use
   `services/transfer.py` (staged copy on the destination, verify, no-overwrite promote).
   Never overwrite an existing managed file. Never create folders under a configured
   location that is offline, and never use `mkdir(parents=True)` below a location root.
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
backend/.venv/bin/python scripts/acceptance.py   # 28-step E2E (isolated data dir)
scripts/smoke_test.sh             # launch scripts + dashboard smoke test (isolated data dir)
bench/run_m0.sh                   # M0 benchmark on the Mac (isolated bench/.venv; see bench/README.md)
```
