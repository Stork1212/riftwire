# RIFTWIRE — Roadmap

_Status: **approved in principle by the owner (2026-11-23)** with decisions D1–D9 (see
[`PRODUCT_SPEC.md`](PRODUCT_SPEC.md#owner-decisions-log-2026-11-23)). Phase 1 scope and
architecture are unchanged. **Next gate:** Phase 1 approved and merged → detailed Phase 2
technical implementation proposal → owner approval → Phase 2 implementation._

## Overview

| Phase | Name | Outcome | Status |
|---|---|---|---|
| 1 | **Foundation** | Crash-safe local ingestion, metadata, thumbnails, persistent queue, dashboard | Implemented and verified on the Mac; awaiting final review (PR #1) |
| 2 | **Sources & Signals** | Rights confirmation + provenance; permitted acquisition (local, authorized YouTube/Twitch, selective sections); configurable storage locations; small local Whisper with word timestamps + benchmark; audio/scene signals | Approved in principle; awaiting technical proposal |
| 3 | **Highlight Detection & Review** | Interchangeable AI provider interface (Ollama default; OpenAI/Anthropic optional at $0 budget), usage ledger + spending limits; per-game highlight prompts; validated, ranked records; review table; exports; auto-submit | Approved in principle |
| 4 | **Editing Preset Studio & Rendering** | Per-channel brand kits (uploaded logos/fonts/outros); 1080×1920 35/65 presets, hooks, word-level subtitles, 2 s outro, live preview, VideoToolbox renders + QC; per-game editing prompts; **artifact registry for generated files (retention foundations)** | Approved in principle |
| 5 | **Publishing** | **YouTube Shorts first**; multi-account channels, scheduling, generated metadata + thumbnails, mandatory human approval; then Instagram Reels, TikTok, Facebook Reels, X via official APIs; **storage lifecycle & retention manager (72 h after verified publication)** | Approved in principle |
| 6 | **Analytics & Optimization** | Performance ingestion, scoring feedback, prompt/model A/B evaluation | Approved in principle |

The earlier phase list in CLAUDE.md §1 (2 Analysis, 3 Editing, 4 Publishing, 5 Analytics) is
**refined, not replaced**: "Analysis" is split into Phase 2 (signals) and Phase 3 (highlight
detection). Updating CLAUDE.md §1 is an architecture-document change and will be done only once
this roadmap is approved.

## Phase 2 — Sources & Signals

**Scope**
- **Rights and provenance** (`sources`, `source_rights`): ownership / creator permission /
  licence, evidence, and **explicit user confirmation** (attestation recorded). Enforced in every
  acquisition, clip, render and publish job.
- **Permitted acquisition only:** local import (preferred); creator/owner-provided files;
  yt-dlp only for public, unrestricted sources the owner has confirmed as permitted, with
  **no bypassing** of DRM, member/subscriber-only, private, age, geo or login restrictions.
  Selective `--download-sections` by default; batch jobs, retries, progress and quality presets.
  A global switch disables URL acquisition.
- **Configurable storage locations** (recordings, temp, models, exports, assets) with per-volume
  60 GiB floor, cross-volume atomic finalize, and "storage offline" handling for an unmounted
  external SSD. Data migration helper for the planned 2 TB APFS SSD.
- Watch folder / import-by-move.
- `TRANSCRIBE` with word-level timestamps using **small local Whisper models**, plus a
  **benchmark** (WER, word-timing error, real-time factor, peak memory) of Apple Silicon
  implementations before any larger model is requested.
- `AUDIO_SIGNALS`, `SCENE_SIGNALS`, `PROXY` (VideoToolbox) jobs.

**Acceptance (examples)**
- Acquisition without a confirmed, in-scope rights record → permanent failure + event (tested).
- Restricted source (simulated) → refused; no retry, no workaround.
- A 3-hour VOD: probe plus 3 selected 60-second sections downloads < 5 % of the full size.
- Unmounted SSD path → no directories created on the internal disk; readiness reports offline.
- Crash tests extended to download, transcription and cross-volume finalize.

**Owner approvals needed before/within Phase 2:** the technical proposal itself; small-model
downloads (sizes listed in the proposal); SSD timing.

## Phase 3 — Highlight Detection & Review

**Scope**
- **AI provider interface** with adapters: **Ollama (default, local)**, OpenAI, Anthropic,
  OpenAI-compatible endpoints. Paid providers disabled by default; **$0 monthly budget**;
  usage ledger, month-to-date spend, per-month/per-run limits, pre-flight cost checks, response
  cache; keys in the macOS Keychain.
- **Per-game, versioned prompt families:** `highlight/` (detect, score) and `editing/` (hook,
  title, description, hashtags, subtitle emphasis).
- `HIGHLIGHT_DETECT` / `HIGHLIGHT_SCORE` jobs producing records with the required fields
  **plus evidence**; anti-fabrication validator; ranking.
- Ranked highlight table with preview; approve/reject/edit; CSV / JSON / `clips.txt` export;
  rule-based auto-submit to the clipping queue.

**Acceptance (examples)**
- With default settings, no code path can issue a paid API call (tested).
- 0 stored highlights without valid evidence (adversarial model-output tests).
- Top-10 precision on an owner-labelled GTA set ≥ agreed target, using local models only.
- Idempotent re-runs per prompt version + input.

**Owner approvals needed:** installing Ollama (system-wide install) and the local model size.

## Phase 4 — Editing Preset Studio & Rendering

**Scope**
- **Per-channel brand kits** with user uploads (logos, fonts, outros, colours), validated by
  content and stored by hash with licence notes.
- Versioned presets per **game × channel**: default 1080×1920, camera top 35 % / gameplay
  bottom 65 %, adjustable crops, optional facecam auto-detect with manual override, hook (white
  box, bold black text), word-level subtitles (bold white, black outline, red active word),
  configurable 2-second outro.
- Live layout preview and quick preview renders.
- `RENDER` (FFmpeg + ASS subtitles, VideoToolbox) and `RENDER_QC` jobs.
- Higgsfield evaluated as an **optional premium** plugin (off by default, paid-provider rules
  apply); the core pipeline never depends on it.

- **Retention foundations (D10):** every generated file is registered in
  `generated_artifacts` (kind `final_render` | `intermediate`, path, volume UUID, size, hash,
  render/highlight links, `retention_lock`). Intermediates are marked disposable at creation.
  Renders write only into exports/render locations, never into `recordings`. No deletion
  logic yet.

**Acceptance (examples)**
- Golden-frame tests for layouts, hooks and subtitles; active-word timing within one frame.
- Renders reproducible from recorded preset + brand-kit versions; crash/retry → identical output.
- Full pipeline renders with Higgsfield disabled/uninstalled.

## Phase 5 — Publishing

**Scope**
- **YouTube Shorts first** (YouTube Data API, OAuth via Keychain), then Instagram Reels,
  TikTok, Facebook Reels and X via official APIs (terms, quotas and app review verified at
  implementation time).
- Multiple accounts and channels, each with its own brand kit, presets, schedule and approval
  policy.
- Generated titles, descriptions, hashtags (editing prompts), attribution from rights records,
  thumbnails with brand overlay.
- **Mandatory human approval** before publication (default); scheduling; idempotent, verified
  uploads.

- **Storage lifecycle and retention manager (D10, PRODUCT_SPEC §6a):**
  - publication targets per artifact;
  - read-only `verify(publication_id)` per platform adapter;
  - persistent hourly `CLEANUP_SWEEP` with overdue recovery;
  - fenced, crash-safe `DELETE_ARTIFACT` (intent → same-volume trash → unlink), with the SSD
    identity checked before any deletion;
  - dashboard: enable/disable, retention period (default 72 h), Keep forever, scheduled
    deletion time, storage reclaimed, history and audit log, dry run.

**Acceptance (examples)**
- Nothing publishes without an approval record.
- A retried publish never creates a duplicate post; the platform ID and URL are stored.
- **Retention:**
  - 0 deletions when any target is pending, scheduled, failed, partial or unverified, when an
    active job or retention lock exists, or when the volume is offline or has the wrong
    identity. One property-test matrix covers every combination.
  - The countdown starts at the final verified publication (time-travel tests).
  - An API error means the file is kept.
  - Originals, thumbnails, transcripts and metadata are never deleted.
  - The cleanup path cannot call any platform write or delete operation.
  - Real SIGKILL at every deletion step → recovered with no double deletion.
  - An overdue sweep runs after restart.
  - Dry run changes nothing.

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
- Paid services (cloud AI, Higgsfield) are optional and off by default. The budget is $0
  until the owner sets a limit, and no paid call happens without explicit authorization.
- Acquisition only by permitted methods, with confirmed rights and no bypassing of restrictions.
- Only the retention manager (Phase 5) may delete generated video files, and only under
  PRODUCT_SPEC §6a. Original recordings are never deleted automatically.
- Each phase ends with a report like `PHASE1_REPORT.md`: verified vs. untested, acceptance
  results on the Mac.

## Next steps

1. Phase 1: independent review of PR #1 → owner approval → squash-merge.
2. Then: update `CLAUDE.md` §1 (phase list) to match this roadmap. This is an architecture
   document, approved in principle here.
3. Then: **detailed Phase 2 technical implementation proposal** (data model, job types,
   storage-location design, acquisition policy enforcement, transcription benchmark plan, test
   plan, risks) for owner approval. No Phase 2 code before that approval.

Open questions: [`PRODUCT_SPEC.md` §7](PRODUCT_SPEC.md#7-remaining-open-questions).
