# RIFTWIRE — Future Product Specification (post-Phase 1)

_Status: **Requirements captured for architectural review — not approved for implementation.**_
_Received from the owner 2026-11-23. Nothing in this document expands Phase 1 scope. Work starts
only after Phase 1 is approved and each phase below is individually approved
(see [`ROADMAP.md`](ROADMAP.md))._

## 0. Goal

Eliminate every manual hand-off between ChatGPT, Claude, spreadsheets, Terminal scripts, editing
tools and publishing services. One local-first system takes an authorized source (file or VOD URL)
to an approved, published short, with each stage visible, reviewable and recoverable.

| Manual step today | Replaced by |
|---|---|
| Copy/paste transcripts and prompts into ChatGPT/Claude | Versioned prompt library + AI provider adapters run as queue jobs (§3) |
| Spreadsheets of timestamps and ideas | Highlight table in the dashboard, CSV/JSON/`clips.txt` export (§3.5) |
| Terminal `yt-dlp` / FFmpeg commands | Source acquisition jobs with progress and retries (§2) |
| Manual editing in an NLE | Editing preset studio + automatic rendering (§4) |
| Manual uploads | Review/approval queue + official platform integrations (§6) |

## 1. Principles carried forward from Phase 1 (non-negotiable)

- Local-first. One heavy job at a time by default. 60 GiB free-space floor. Persistent queue,
  leases, fencing, crash recovery and idempotent jobs apply to every new stage.
- **Never fabricate.** Every timestamp, event, quote and caption must come from the source media
  and carry evidence (§3.4).
- **Rights first.** Only authorized sources; provenance is recorded and enforced (§2.2).
- **Human control.** Review and approval gates before publishing; automation increases only after
  validation.
- Paid services and model downloads are opt-in, cost-capped and approved by the owner.

---

## 2. Source acquisition

### 2.1 Supported sources
- Local files (Phase 1, existing).
- **Authorized YouTube and Twitch VOD URLs.**
- Later: a watch folder (OBS output) and import-by-move.

### 2.2 Permissions and provenance (enforced, not advisory)
Every source has a **rights record** before any download job may run:

| Field | Purpose |
|---|---|
| `source_url`, `platform`, `platform_video_id`, `channel_id/name` | identity |
| `rights_basis` | `own_channel` · `written_license` · `creator_agreement` · `platform_permission` |
| `evidence` | link or attached document reference; who approved, when |
| `scope` | allowed uses (clip, edit, publish), allowed platforms, territory |
| `valid_from` / `valid_until` | expiry; jobs refuse expired rights |
| `attribution` | required credit text, if any |

The acquisition job checks the rights record inside its fenced transaction and **fails
permanently** if rights are missing, expired or out of scope. The full chain
(source → download → recording → highlight → render → post) is queryable for any published clip.

> **Legal note for review.** YouTube's and Twitch's terms restrict downloading outside their own
> features unless you have permission. yt-dlp is an unofficial tool that can break when platforms
> change. RIFTWIRE treats URL acquisition as a convenience for content the owner is **authorized**
> to use (own channels, licensed creators). The owner should confirm the rights policy (and legal
> advice if needed) before this feature is enabled. Where a creator can provide original files,
> file import is preferred.

### 2.3 yt-dlp + FFmpeg integration (to evaluate in Phase 2)
- Run yt-dlp as a **subprocess** with argument lists, timeouts and process-group control, exactly
  like FFmpeg today (it is effectively another media tool). Pin its version; keep it project-local.
- **Selective, timestamp-based downloading** with `--download-sections "*START-END"` (FFmpeg is
  required). Option: `--force-keyframes-at-cuts` for frame-accurate edges (slower; re-encodes);
  the default is padded sections plus local trimming at render time.
- Two-pass flow so whole VODs are rarely downloaded:
  1. **Probe:** metadata, duration and chapters only (no media).
  2. **Analyse cheaply:** audio-only or low-resolution proxy download, or the platform's chat or
     markers where authorized, for highlight detection.
  3. **Fetch:** only the selected sections, at target quality, padded ±N seconds.
- **Batch extraction:** many sections and many URLs as separate idempotent jobs
  (key: source + section range + quality).
- **Retries:** transient network errors → backoff; geo, permission or removed-video errors →
  permanent.
- **Progress:** parse yt-dlp's machine-readable progress output into `progress_percent`.
- **Configurable quality:** max height, fps, codec preference (H.264/HEVC/AV1), audio-only.
  Presets per channel.
- **Disk safety:** estimate size from format metadata before download and apply the 60 GiB floor
  rule; downloads land in `temp/` and are finalized through the existing crash-safe ingest path
  (SHA-256, dedupe, read-only originals).
- Section files become **recordings** linked to their source and section range, so the whole
  Phase 1 pipeline (metadata, thumbnails, queue) applies unchanged.

## 3. AI highlight detection

### 3.1 Signals (cheap → expensive)
1. **Transcript** with word-level timestamps (§5.2).
2. **Audio:** loudness peaks, sudden changes, laughter/shout energy, music vs. speech.
3. **Video:** scene cuts, motion intensity, and on-screen events where reliably detectable (e.g.
   wanted-level HUD changes in GTA, visible from frames).
4. **Platform signals where authorized:** chat-rate spikes, creator markers, chapters.
5. **Selective vision-model review** of short low-resolution proxies of the top-N candidates only
   (cost-capped, optional).

### 3.2 Versioned, game-specific prompt library
- Stored in the repo (`prompts/<game>/<task>/vN.md`) and mirrored in the DB with a content hash.
- Each prompt declares: game, task (detect, score, title, hook), input schema, output JSON
  schema, model(s) it was evaluated on, and changelog.
- Every highlight run records **prompt id + version + hash, model, parameters, token cost**, so
  results are reproducible and comparable.
- Prompt changes are evaluated on a fixed, owner-labelled evaluation set before becoming default.

### 3.3 Structured highlight record (required fields)

| Field | Type | Rule |
|---|---|---|
| `id`, `recording_id` / `source_id` | UUID | |
| `start_ms`, `end_ms` | int | within media duration; `end > start` |
| `duration_ms` | int | `end_ms − start_ms` (computed, never model-supplied) |
| `title` | text | |
| `description` | text | must describe events supported by evidence |
| `category` | enum | e.g. police chase, stunt, funny interaction, fail, Easter egg, cinematic, mechanic showcase |
| `confidence` | 0–1 | model confidence, calibrated against reviews over time |
| `viral_potential` | 0–100 | **labelled as an estimate**; inputs recorded |
| `suggested_hook` | text | short on-screen hook |
| `evidence` | list | transcript word ranges, signal ids, frame timestamps |
| `prompt_version`, `model`, `run_id` | | provenance |
| `status` | enum | `proposed` · `approved` · `rejected` · `queued_for_clip` · `clipped` |

### 3.4 Anti-fabrication validation (automatic, before a record is stored)
- Timestamps must fall within the media and snap to real frame boundaries.
- At least one evidence item must overlap `[start, end]`. Quoted speech must match transcript
  words in that range (fuzzy match threshold), otherwise the record is rejected.
- Model output that fails the JSON schema is rejected and retried, never "repaired" by guessing.
- Rejections are logged as events with the reason. Nothing is shown as a highlight without
  passing validation.

### 3.5 Review and export
- Dashboard **highlight table**: sortable and filterable by score, category and status; inline
  preview player starting at `start_ms`; approve, reject and edit times with frame-step controls.
- Export: **CSV**, **JSON** (full records with evidence) and **`clips.txt`**, one clip per line:
  `HH:MM:SS.mmm-HH:MM:SS.mmm<TAB>title`. The range format is directly usable with
  `yt-dlp --download-sections "*START-END"`.
- **Auto-submit:** selected or approved highlights (or rules such as "confidence ≥ X and category
  in Y") enqueue clip and render jobs with a chosen preset.

## 4. Editing preset studio

### 4.1 Defaults
- Output **1080 × 1920** (9:16), 30 or 60 fps (follow the source, capped by the preset), H.264 via
  VideoToolbox, AAC, loudness-normalized for short-form platforms.
- **Split-screen layout:** streamer camera **top 35 %** (1080 × 672), gameplay **bottom 65 %**
  (1080 × 1248).
- No-camera fallback layouts: full-height gameplay crop (centered or follow-action), or blurred
  background fill.

### 4.2 Layout controls
- Adjustable **camera crop region** and **gameplay crop region** (normalized coordinates on the
  source frame), with aspect handling (fill/crop vs. fit/letterbox).
- **Optional automatic facecam detection** on sampled frames (face detection + static-rectangle
  consistency across samples) proposes the camera region; manual override always wins and is
  saved.

### 4.3 Text and branding
- **Hook:** customizable text on a **white background with bold black text**; position, padding,
  corner radius, duration and entry animation configurable.
- **Subtitles:** **word-level synchronized**, **bold white text with black outline**, **red
  highlight on the active word**. Font, size, colours, outline, position (safe zones for each
  platform's UI), words per line and animation are configurable. Implemented as generated ASS
  subtitles rendered by FFmpeg/libass (to be confirmed in the Homebrew build).
- **Optional two-second branded outro** (static or animated asset) appended per preset.
- Configurable fonts (bundled or user-supplied, licensing recorded) and colours.

### 4.4 Presets and previews
- **Reusable presets per game and publishing channel**, versioned. A render records the exact
  preset version used.
- **Live layout preview** in the dashboard before rendering: an interactive overlay on proxy
  frames for crop regions, hook and subtitle placement, plus an optional quick FFmpeg preview
  render of a few seconds at low resolution.

## 5. Processing

### 5.1 Rendering
- Local FFmpeg; **VideoToolbox** (`h264_videotoolbox` / `hevc_videotoolbox`) for encoding, with
  quality and bitrate targets per preset. Software x264 is the fallback when quality requires it.
- Render outputs go through the same temp → validate → atomic publish pattern (attempt-unique
  temp files, fenced completion).
- Post-render QC job: duration matches the highlight, resolution and fps match the preset,
  audio/video in sync, loudness within target, no black or frozen segments, subtitles within safe
  zones.

### 5.2 Transcription (to evaluate)
- Candidates on Apple Silicon:
  - `mlx-whisper` (MLX, `word_timestamps=True`);
  - WhisperX-style forced alignment ports for MLX, for more accurate word timing;
  - `whisper.cpp` (Metal).
- Evaluation on owner-provided, authorized samples: WER, word-boundary timing error,
  real-time factor on the M3 Pro, and peak memory (must fit alongside the OS within 18 GB; one
  model loaded at a time).
- **Model downloads need owner approval** (multi-GB). Models are stored under the data dir,
  outside git, and their versions are recorded on every transcript.

### 5.3 Optional external integrations
- **Higgsfield:** an optional plugin behind a generic "external generator" interface. Disabled by
  default, never required by any pipeline stage, per-job opt-in, cost-capped, with inputs and
  outputs recorded for provenance. Its API, pricing and terms are to be evaluated before
  integration.
- Cloud AI (vision and language models): a provider abstraction, a budget per run and per month,
  responses cached by input hash, and only for steps where local quality is insufficient.

### 5.4 Preserved infrastructure
- All new stages are job types on the existing SQLite queue (leases, fencing, retries, orphan
  cleanup).
- The disk floor applies to downloads, proxies and renders; renders and proxies are reproducible
  and may be pruned by policy, while originals never are.
- Memory scheduling: at most one ML model resident; heavy jobs are serialized.

## 6. Publishing

- Generate **thumbnails, titles, descriptions and hashtags** per platform, grounded in the
  highlight record and transcript, with the prompt version recorded.
- **Human review and approval before publishing** (default and mandatory until the owner relaxes
  it per channel): approve, edit or reject, with a full preview of the vertical video and
  metadata.
- **Multiple channels** through **official platform integrations** only (YouTube Data API,
  Instagram/Facebook Graph APIs for Reels, X API), scheduling, and upload verification (poll
  until processed, store platform IDs and URLs). Each platform's current API terms, quotas and
  account requirements must be verified at implementation time. OAuth tokens are stored in the
  macOS Keychain, never in the repo or logs.
- Later: analytics ingestion (views, retention, engagement) feeding back into scoring.

## 7. Open questions for the owner

1. **Rights policy** for URL sources: which bases are acceptable, and who approves.
2. **Cloud AI budget** (per month) and which steps may use it.
3. **Model downloads:** approval to download Whisper models (sizes to be listed) once Phase 2
   starts.
4. **Storage:** external APFS SSD before acquisition at scale (≈9 GiB headroom internally).
5. **Platform order** for publishing; which channels and accounts.
6. **Higgsfield:** the intended use case (e.g. AI-generated intros/B-roll?) to scope the plugin.
7. **Brand assets:** fonts, colours and outro asset, plus their licences.
