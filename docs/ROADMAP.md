# RIFTWIRE — Roadmap

_Status: **proposed, for architectural review** (2026-11-23). Phase 1 scope is unchanged.
Each phase starts only after the previous phase is verified and the owner approves it
(CLAUDE.md §8). Detailed requirements: [`PRODUCT_SPEC.md`](PRODUCT_SPEC.md)._

## Overview

| Phase | Name | Outcome | Status |
|---|---|---|---|
| 1 | **Foundation** | Crash-safe local ingestion, metadata, thumbnails, persistent queue, dashboard | Implemented and verified on the Mac; awaiting final review (PR #1) |
| 2 | **Sources & Signals** | Authorized YouTube/Twitch VOD acquisition with provenance; selective downloads; word-level transcripts; audio/scene signals | Proposed |
| 3 | **Highlight Detection & Review** | Versioned prompt library; validated, evidence-backed highlight records; review table; exports; auto-submit to clipping | Proposed |
| 4 | **Editing Preset Studio & Rendering** | 1080×1920 split-screen presets, hooks, word-level subtitles, outro, live preview, VideoToolbox renders + QC | Proposed |
| 5 | **Publishing** | Generated metadata + thumbnails, human approval, official multi-channel uploads, scheduling | Proposed |
| 6 | **Analytics & Optimization** | Performance ingestion, scoring feedback, prompt/model A/B evaluation | Proposed |

The earlier phase list in CLAUDE.md §1 (2 Analysis, 3 Editing, 4 Publishing, 5 Analytics) is
**refined, not replaced**: "Analysis" is split into Phase 2 (signals) and Phase 3 (highlight
detection). Updating CLAUDE.md §1 is an architecture-document change and will be done only once
this roadmap is approved.

## Phase 2 — Sources & Signals

**Scope**
- Rights/provenance model (`sources`, `source_rights`), enforced before any acquisition job.
- yt-dlp integration as a managed subprocess (pinned, project-local): probe, proxy/audio-only
  fetch, selective `--download-sections` fetch, batch jobs, retries, progress, quality presets,
  disk-floor estimates. Downloaded sections finalize through the Phase 1 crash-safe ingest path.
- Watch folder / import-by-move (relieves the 2× disk use of browser upload).
- `TRANSCRIBE` job with word-level timestamps (engine chosen by the evaluation below).
- `AUDIO_SIGNALS` (loudness/energy peaks) and `SCENE_SIGNALS` (cuts/motion) jobs; `PROXY` job
  (low-res, VideoToolbox).
- Data-dir migration helper for an external APFS SSD.

**Evaluation gates inside the phase:** transcription engine bake-off (`mlx-whisper`, MLX
forced-alignment port, `whisper.cpp`) on owner-authorized samples, measuring WER, word timing
error, real-time factor and peak memory. Results are documented before an engine is chosen.

**Acceptance (examples)**
- A URL without a valid rights record cannot be downloaded (permanent failure plus event).
- A 3-hour VOD: probe plus 3 selected 60-second sections downloads < 5 % of the full size.
- Killed download/transcription → recovered without duplicates (existing crash tests extended).
- Word timestamps within the agreed tolerance on the evaluation set.

**Owner approvals needed:** rights policy; model downloads; external SSD; yt-dlp use.

## Phase 3 — Highlight Detection & Review

**Scope**
- Prompt library (`prompts/<game>/<task>/vN.md`, hashed, versioned) and AI provider adapters
  (local first; cloud optional with budget caps and caching).
- `HIGHLIGHT_DETECT` and `HIGHLIGHT_SCORE` jobs producing records with the required fields
  (start/end/duration, title, description, category, confidence, estimated viral potential,
  suggested hook) **plus evidence**.
- Anti-fabrication validator (timestamps in range, evidence overlap, transcript-matched quotes,
  strict JSON schema).
- Dashboard highlight table with preview, approve/reject/edit; CSV / JSON / `clips.txt` export;
  rule-based auto-submit to the clipping queue.

**Acceptance (examples)**
- 0 stored highlights without valid evidence (property-tested with adversarial model outputs).
- Precision of the top-10 on an owner-labelled GTA set ≥ the agreed target; cost per
  recording-hour within budget.
- Re-running a detection with the same prompt version and input is idempotent (no duplicate
  records).

## Phase 4 — Editing Preset Studio & Rendering

**Scope**
- Preset model (per game and channel, versioned): layout (default cam top 35 % / gameplay
  bottom 65 %, 1080×1920), crop regions, facecam auto-detect with manual override, hook style
  (white box, bold black text), subtitles (bold white, black outline, red active word),
  fonts/colours/position/animation, optional 2-second outro.
- Live layout preview in the dashboard and quick low-res preview renders.
- `RENDER` job (FFmpeg filter graph + ASS subtitles, VideoToolbox), `RENDER_QC` job
  (duration, resolution, fps, A/V sync, loudness, black/freeze detection, safe zones).

**Acceptance (examples)**
- Golden-image tests of layouts and subtitles (frame snapshots within tolerance).
- Active-word highlight timing within one frame of the transcript word boundaries.
- Render throughput target on the M3 Pro with VideoToolbox; crash and retry produce
  identical outputs.

## Phase 5 — Publishing

**Scope**
- Generated titles, descriptions, hashtags and thumbnails (grounded, prompt-versioned).
- Review and approval queue (mandatory by default), scheduling, multi-channel accounts.
- Official integrations (YouTube Data API, Instagram/Facebook Graph API Reels, X API). OAuth
  tokens in the macOS Keychain; upload verification and post-publish status tracking.

**Acceptance (examples)**
- Nothing publishes without an approval record (unless the owner enables auto-publish per
  channel).
- Idempotent uploads: a retried publish never creates a duplicate post.

## Phase 6 — Analytics & Optimization

- Ingest platform metrics (views, retention, engagement) through official APIs.
- Feed performance back into scoring; evaluate prompts and models by A/B test on held-out sets.
- Reporting dashboard per channel, game and category.

## Cross-cutting rules (all phases)

- Every new stage is a job type on the existing queue: idempotent, fenced, recoverable, with
  orphan-process cleanup and disk-floor checks.
- One heavy job / one ML model at a time on the 18 GB Mac unless a phase proves otherwise.
- No fabricated data. Every AI output carries provenance (prompt version, model, inputs) and
  evidence.
- Paid services (cloud AI, Higgsfield) are optional, off by default, budget-capped and approved.
- Each phase ends with a report like `PHASE1_REPORT.md`: verified vs. untested, acceptance
  results on the Mac.

## Decisions requested from the owner

See [`PRODUCT_SPEC.md` §7](PRODUCT_SPEC.md#7-open-questions-for-the-owner): rights policy, cloud
AI budget, model downloads, external SSD, platform order, Higgsfield use case, brand assets.
