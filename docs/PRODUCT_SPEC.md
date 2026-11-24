# RIFTWIRE — Future Product Specification (post-Phase 1)

_Status: **Approved in principle by the owner (2026-11-23), with the decisions below.** This is a
specification, not an implementation authorization. Phase 1 scope and architecture are
unchanged. Phase 2 starts only after Phase 1 is approved and merged **and** a detailed Phase 2
technical proposal is approved (see [`ROADMAP.md`](ROADMAP.md))._

## Owner decisions log (2026-11-23)

| # | Area | Decision |
|---|---|---|
| D1 | Sources & rights | Local recordings + **authorized** YouTube and Twitch sources. Track ownership, creator permission and licensing. **User must confirm source rights.** Use permitted acquisition methods only; **never bypass access restrictions.** |
| D2 | AI budget & providers | Initial recurring cloud AI budget **$0**. Local-first. **Interchangeable AI provider interface** (Ollama, OpenAI, Anthropic, other compatible). Paid providers optional, with usage tracking and configurable spending limits. **No paid API charge without explicit authorization.** Separate, versioned **highlight-detection** and **editing** prompts per game. |
| D3 | Transcription | **Smaller local Whisper models approved**, subject to disk space. Evaluate Apple Silicon-optimized implementations. Word-level timestamps for animated subtitles. **Benchmark accuracy, speed and memory before downloading larger models.** |
| D4 | Storage | Keep the **60 GiB** free-space floor. Plan for an **external APFS SSD**. The owner has ordered a **SanDisk 1 TB Portable SSD** (Pokémon Mewtwo Edition). Storage is **capacity-independent**: no size is assumed anywhere (§5.3). **Configurable locations** for recordings, temp files, AI models and rendered exports. Prefer selective acquisition where supported and permitted. |
| D5 | Publishing | **YouTube Shorts first.** Later: Instagram Reels, TikTok, Facebook Reels, X. Multiple accounts/channels; scheduling, metadata generation, thumbnails, **human approval before publication**; official APIs where available. |
| D6 | Higgsfield | **Optional premium integration.** Core clipping, editing, subtitles, branding and rendering must work without it. Local FFmpeg + Whisper for standard production. Evaluate it for advanced visual enhancements and creative assets. |
| D7 | Branding & editing | Customizable preset system. Default 1080×1920; 35 % camera / 65 % gameplay; bold white subtitles, black outline, red active word; white hook background with bold black text; configurable 2-second outro. **User-uploaded logos, fonts, outros and brand assets.** **Branding configurable independently per publishing channel.** |
| D8 | Workflow | One application: authorized source → viral moments → clips → edit → subtitles + thumbnails → ranking → approved, publish-ready content. No manual prompts, spreadsheets, batch files, Terminal commands or app-to-app transfers. |
| D9 | Process | Preserve the Phase 1 architecture. No Phase 2 implementation yet. After Phase 1 is approved and merged → a detailed Phase 2 technical proposal for approval. |
| D10 | Storage lifecycle (2026-11-23) | **Automated retention manager:** delete locally **generated** video files (and their disposable rendering intermediates) **72 h after the last confirmed, API-verified publication to all intended platforms**. Never when any upload is pending, scheduled, failed, partial or unverified; never with an active job or a manual retention lock; never original recordings; never remote content. Hourly, idempotent, crash-safe, SSD identity verified, full audit trail and dashboard controls (§6a). |

---

## 0. Goal and end-to-end workflow (D8)

```
 Authorized source ─► Rights confirmation ─► Acquisition (file / selective VOD sections)
        │                                              │
        ▼                                              ▼
   Provenance record                     Recording (Phase 1 crash-safe ingest)
                                                       │
                       Transcript (word-level) + audio/scene/platform signals
                                                       │
                     Highlight detection (local AI; game prompts) ─► Validation (evidence)
                                                       │
                          Ranked highlight table ─► Review / auto-select rules
                                                       │
            Clip extraction ─► Editing preset (channel brand kit) ─► Render (VideoToolbox) ─► QC
                                                       │
                 Thumbnails + titles + descriptions + hashtags (per channel, grounded)
                                                       │
                       Human approval ─► Schedule ─► Official API upload ─► Verify
```

Every arrow is a job on the existing persistent queue. Every screen is in the one dashboard.

| Manual step today | Replaced by |
|---|---|
| Copy/paste transcripts and prompts into ChatGPT/Claude | Versioned prompt library + provider interface, run as queue jobs (§3) |
| Spreadsheets of timestamps | Ranked highlight table; CSV/JSON/`clips.txt` export (§3.6) |
| Terminal `yt-dlp` / FFmpeg / batch files | Acquisition, render and QC jobs with progress and retries (§2, §5) |
| Manual editing in an NLE | Preset studio + automatic rendering (§4) |
| Manual uploads | Approval queue + official platform integrations (§6) |

## 1. Principles carried forward from Phase 1 (non-negotiable)

- Local-first; one heavy job and one resident ML model at a time on the 18 GB Mac.
- 60 GiB free-space floor on **every** volume RIFTWIRE writes to.
- Persistent queue, leases, fencing tokens, orphan-process cleanup, crash recovery and idempotent
  jobs apply to every new stage.
- **Never fabricate.** Every timestamp, event, quote and caption comes from the source and carries
  evidence (§3.5).
- **Rights first.** Sources are authorized, confirmed by the user and recorded (§2.2).
- **Human control.** Approval before publishing by default.
- **No paid charges without explicit authorization** (§3.2). No model download beyond what the owner
  approved (§5.2).

---

## 2. Source acquisition (D1, D4)

### 2.1 Supported sources
- **Local recordings** (Phase 1, existing) and, later, a watch folder / import-by-move.
- **Authorized YouTube and Twitch** VODs and videos.

### 2.2 Rights, permission and provenance (enforced)

A source cannot be acquired or processed until it has a **confirmed rights record**:

| Field | Purpose |
|---|---|
| `platform`, `url`, `platform_video_id`, `channel_id`, `channel_name` | identity |
| `ownership` | `own_content` · `creator_permission` · `licensed` · `other_permitted` |
| `permission_evidence` | document/link/message reference; grantor name; date |
| `license_terms` | allowed uses (clip, edit, publish), platforms, territory, attribution text, expiry |
| `confirmed_by`, `confirmed_at`, `attestation_version` | **explicit user confirmation** of the rights statement (checkbox + text version stored) |
| `valid_until` | jobs refuse expired rights |

- Rights are checked in the fenced transaction of every acquisition, clip, render and publish job.
  Missing, expired or out-of-scope rights cause a **permanent** failure with a clear event.
- The provenance chain (source → acquisition → recording → highlight → render → post) is queryable
  and shown on each clip.
- Attribution text from the licence is carried into publishing metadata automatically.

### 2.3 Permitted acquisition only — no bypassing (D1)

RIFTWIRE must **never** circumvent access restrictions. Acquisition is refused (permanent error)
when the source is DRM-protected, members-only or subscriber-only, private, age-gated, geo-blocked,
paywalled or otherwise restricted. RIFTWIRE will not use cookies, logins or proxies to get around
such restrictions, and will not attempt to defeat technical protection.

Order of preference:
1. **Original files from the creator/owner** (local import). Best quality and clearest rights.
2. **Platform-provided export/download features** available to the account owner (e.g. the
   creator's own YouTube Studio or Twitch download features), followed by local import.
3. **yt-dlp for public, unrestricted videos** that the user has confirmed rights to, only where
   the owner has accepted that this method is permitted for that source under the applicable
   platform terms. This is recorded per source as `acquisition_method_confirmed`.

> **Review note.** YouTube's and Twitch's terms restrict downloading outside their own features
> without permission. Method 3 is therefore opt-in per source, gated by the rights confirmation,
> and can be disabled globally (`RIFTWIRE_URL_ACQUISITION_ENABLED=false`). The owner remains
> responsible for having permission.

### 2.4 yt-dlp + FFmpeg (for permitted method-3 sources)
- Managed subprocess like FFmpeg: argument lists, timeouts, process-group kill, orphan cleanup.
  Pinned, project-local version.
- **Selective, timestamp-based acquisition** (D4) with `--download-sections "*START-END"`
  (requires FFmpeg). Default: padded sections, then frame-accurate trimming at render time.
  `--force-keyframes-at-cuts` is optional (slower; re-encodes).
- Flow: **probe** (metadata and chapters only) → **analysis proxy** (audio-only or low resolution)
  → **section fetch** of approved highlights at target quality. Full downloads are the exception.
- Batch jobs per (source, section, quality); retries with backoff for transient errors; permanent
  failure for restriction, removal or geo errors; progress parsed into `progress_percent`;
  configurable quality presets per channel.
- Pre-download size estimate + floor check; files land in temp and finalize through the Phase 1
  crash-safe ingest (SHA-256, dedupe, read-only originals).

## 3. AI architecture and highlight detection (D2)

### 3.1 Interchangeable AI provider interface

```
AIProvider
  id, kind: local | paid, capabilities: {text, vision, json_schema, max_context}
  estimate_cost(request) -> Money            # $0 for local providers
  generate(request: {prompt_ref, inputs, schema, model, params}) -> {output, usage, raw_ref}
```

| Adapter | Kind | Notes |
|---|---|---|
| **Ollama** (default) | local | Free; models ≤ ~8B parameters (quantized) on 18 GB; one model resident, unloaded after a job |
| OpenAI | paid | Optional |
| Anthropic | paid | Optional |
| OpenAI-compatible endpoint | local or paid | e.g. LM Studio or other local servers; classified by configuration |

- Provider and model are chosen per task in settings; every result records provider, model,
  prompt version and usage.
- Outputs must satisfy the task's JSON schema (validated; invalid output → retry or failure,
  never guessed).
- API keys are stored in the macOS Keychain; never in the repo, `.env` or logs.

### 3.2 Spending controls ($0 initial budget)
- Defaults: `ai_paid_providers_enabled = false`, `ai_monthly_budget_usd = 0`. With these defaults
  **no paid call is possible**.
- To use a paid provider the owner must explicitly enable it, set a monthly limit and, optionally,
  a per-run limit and/or per-run approval.
- **Usage ledger** (`ai_usage`): provider, model, task, job, tokens in/out, estimated and actual
  cost, timestamp. Shown in the dashboard with month-to-date spend.
- **Pre-flight check** in the job's fenced transaction: estimated cost + month-to-date ≤ limit,
  otherwise the job waits for approval (never silently proceeds).
- Response cache by input hash avoids paying twice for the same request.

### 3.3 Versioned prompt library — per game, separate families (D2)

```
prompts/
  gta6/
    highlight/   detect.v1.md  score.v1.md
    editing/     hook.v1.md  title.v1.md  description.v1.md  hashtags.v1.md  subtitle_emphasis.v1.md
  <game>/…
```
- Each file declares: game, family (`highlight` | `editing`), task, version, input and output
  schema, evaluated models, changelog. A content hash is stored with every result.
- New versions become default only after evaluation on an owner-labelled set for that game.

### 3.4 Signals (cheap → expensive)
Transcript (word-level) → audio energy/loudness peaks → scene cuts/motion → game-HUD events
detectable from frames → platform signals where permitted (chat rate, creator markers,
chapters) → local LLM over transcript windows → optional paid vision review of top-N proxies
(only within budget).

### 3.5 Highlight record and anti-fabrication validation

Required fields: `start_ms`, `end_ms`, `duration_ms` (computed), `title`, `description`,
`category`, `confidence` (0–1), `viral_potential` (0–100, labelled as an **estimate**),
`suggested_hook`, plus `evidence[]`, `prompt_version`, `provider`, `model`, `run_id`, `status`.

Validation before storage: timestamps within media and frame-snapped; evidence overlaps the range;
quotes match transcript words in range; strict schema. Failures are rejected and logged, never
repaired by guessing.

### 3.6 Ranking, review, export, hand-off
- Ranked **highlight table** (score, category, status); inline preview from `start_ms`;
  approve/reject/edit with frame stepping.
- Export **CSV**, **JSON** (with evidence) and **`clips.txt`**
  (`HH:MM:SS.mmm-HH:MM:SS.mmm<TAB>title`).
- Approved highlights, or rules (e.g. confidence ≥ X, category ∈ Y), automatically enqueue clip
  and render jobs with the channel's preset.

## 4. Editing preset studio and branding (D7)

### 4.1 Defaults
- **1080 × 1920**; camera **top 35 %** (1080×672), gameplay **bottom 65 %** (1080×1248).
  Fallback layouts when there is no camera.
- **Subtitles:** word-level, **bold white, black outline, red active word**.
- **Hook:** **white background, bold black text**.
- **Outro:** optional, configurable, **2 seconds**.

### 4.2 Controls
- Adjustable camera and gameplay crop regions (normalized coordinates); optional automatic
  facecam detection with manual override (the override is saved).
- Fonts, colours, sizes, outline, subtitle position (per-platform safe zones), words per line,
  animation, hook position/padding/duration/animation.
- **Live layout preview** on proxy frames, plus quick low-resolution preview renders.

### 4.3 Brand kits per channel (user uploads)
- Each publishing channel has its **own brand kit**: logos (PNG/SVG), fonts (TTF/OTF), outro
  clips/images, colours, watermark position and default hook/subtitle styles.
- Uploads are validated by content (fonts parsed, images decoded, outro probed with ffprobe), size
  limited, stored by content hash in the assets location, and recorded with a licence/ownership
  note.
- Presets are versioned per **game × channel**. A preset references a brand-kit version, and every
  render records the exact preset and brand-kit version (reproducible).

## 5. Processing (D3, D4, D6)

### 5.1 Rendering
Local FFmpeg; **VideoToolbox** encode by default, software x264 fallback; ASS subtitles via
libass (to be confirmed in the Homebrew build); loudness normalization; temp → validate → atomic
publish; `RENDER_QC` job (duration, resolution, fps, A/V sync, loudness, black/freeze detection,
safe zones).

### 5.2 Transcription (approved: small local models first)
- Implementations to evaluate on Apple Silicon:
  - `mlx-whisper` (MLX, `word_timestamps=True`);
  - MLX ports of WhisperX-style forced alignment, for tighter word timing;
  - `whisper.cpp` (Metal).
- Start with **small models** (tiny/base/small class). Each model download is checked against the
  disk floor of the models location. Size and version are recorded.
- **Benchmark before any larger model** (medium / large-v3-turbo):
  - WER;
  - word-boundary timing error (for active-word highlighting);
  - real-time factor;
  - peak memory.
  Use authorized sample clips only. Results are documented, and the owner approves larger
  downloads.

### 5.3 Configurable storage locations (D4)

| Setting | Default | Holds |
|---|---|---|
| `RIFTWIRE_DATA_DIR` | `./data` | DB, logs (default parent of everything) |
| `RIFTWIRE_RECORDINGS_DIR` | `$DATA/recordings` | original recordings (read-only, never auto-deleted) |
| `RIFTWIRE_TEMP_DIR` | `$DATA/temp` | uploads, downloads in progress, job temp outputs |
| `RIFTWIRE_MODELS_DIR` | `$DATA/models` | Whisper / local LLM model files |
| `RIFTWIRE_EXPORTS_DIR` | `$DATA/exports` | rendered clips, thumbnails, export files |
| `RIFTWIRE_ASSETS_DIR` | `$DATA/assets` | uploaded brand assets |

Design constraints:
- **Atomicity across volumes.** The Phase 1 finalize uses a hard link, which requires temp and
  recordings on the same volume. If they are configured on different volumes, the finalize uses a
  **staging directory on the destination volume** (`<recordings>/.staging/`), or a verified copy
  (SHA-256) followed by removal of the temp copy. The guarantees (never overwrite, never lose,
  idempotent) are preserved and tested.
- **Per-volume floor.** The 60 GiB minimum is checked on each volume written to.
- **Unmounted external SSD safety.** If a configured path is on a volume that is not mounted,
  RIFTWIRE must **not** create directories at that path (which would silently fill the internal
  disk). Readiness reports "storage offline", and jobs needing it wait as a transient condition.
- Planned layout with the **external APFS SSD** (owner's ordered drive: SanDisk 1 TB Portable
  SSD): recordings, temp, models, exports and assets on the SSD; database and logs on the
  internal SSD (default; configurable).
- **Capacity-independent.** No code, default or test assumes a drive size. Volumes are
  identified by their marker UUID, not by capacity or name. Every admission decision (import,
  acquisition, render, model download) uses the **measured** free space of the target volume
  against that volume's configurable floor (default 60 GiB). The dashboard shows capacity,
  usage per category and remaining headroom per volume, as measured. Replacing or adding a
  larger or smaller drive is only a configuration change plus the migration helper.

### 5.4 Higgsfield (D6)
- An optional **premium** plugin behind a generic "external creative generator" interface.
  Off by default and subject to the paid-provider rules (§3.2).
- Candidate uses: advanced visual enhancements and creative assets (e.g. intros, stylized
  transitions, thumbnail art). To be evaluated for quality, cost, terms and API.
- **The core pipeline never depends on it:** clipping, editing, subtitles, branding and rendering
  run entirely on local FFmpeg + Whisper.

### 5.5 Preserved infrastructure
All new stages are job types on the existing queue; Phase 1 crash-safety, fencing, orphan
cleanup, disk safeguards and local-first design are unchanged.

## 6. Publishing (D5)

- **Priority:**
  1. **YouTube Shorts** (YouTube Data API, OAuth).
  2. Then Instagram Reels, TikTok, Facebook Reels and X, through official APIs where available.
  The current API terms, quotas, app-review and account requirements must be verified when each
  platform is implemented.
- **Multiple accounts and channels:** a `channels` entity (platform, account, OAuth credential
  reference in the Keychain, brand kit, default preset, posting schedule, approval policy).
- **Generated metadata:** titles, descriptions, hashtags (editing-prompt family, per game,
  grounded in the highlight and transcript), attribution from the rights record, and thumbnails
  (frame selection plus brand-kit overlay).
- **Human approval before publication** (mandatory default): preview the vertical video and
  metadata; approve, edit or reject; then schedule.
- **Idempotent, verified uploads:** a retried publish never duplicates a post; the platform ID
  and URL are stored; processing status is polled.

## 6a. Storage lifecycle and retention manager (D10)

Generated short-form videos are disposable once they are safely published. This manager
reclaims their disk space automatically, and only when it is provably safe.

### 6a.1 What may be deleted (and nothing else)

| Deletable (when eligible) | Never deleted by this manager |
|---|---|
| Final rendered short-form videos (`generated_artifacts.kind = final_render`) | **Original source recordings** (the `recordings` location; Phase 1 rule 2 still applies) |
| Their registered **disposable rendering intermediates** (`kind = intermediate`: temp renders, audio stems, subtitle burn-in passes) | Publication records, platform IDs and URLs, analytics |
| | Thumbnails, transcripts, highlight records, timestamps, evidence |
| | Editing presets, brand kits, render configurations, prompts |
| | Any file not registered as a deletable artifact, or outside the exports/render locations |
| | **Remote published content.** The manager has no platform write or delete capability (§6a.6) |

After deletion, the artifact row stays as a lightweight record: hash, size, render
configuration, publication links and the deletion audit. The dashboard shows "file removed
after publication — metadata retained".

### 6a.2 Default policy and eligibility

Defaults: **automatic cleanup enabled, retention period 72 hours** (configurable).

An artifact is eligible **only if every condition holds** at the moment of deletion. All
conditions are re-checked inside the deletion job's fenced transaction, not only when the
artifact was listed:

1. Automatic cleanup is enabled and the artifact has **no retention lock** ("Keep forever"
   or any manual lock).
2. Every **intended** publication target (platform × channel) recorded for the artifact has
   status **published** and is **verified**:
   - **Official API, where supported:** the platform's API confirms the post exists and is
     processed/live for the stored publication ID, checked again right before deletion. For
     example, the YouTube Data API `videos.list` returns the video ID with processing
     complete.
   - **No usable official API:** the owner must confirm manually in the dashboard. Until
     then the artifact is not eligible.
3. **No** target is pending, scheduled, uploading, processing, failed, partially completed,
   unverified or awaiting retry.
4. `now ≥ last_successful_publication_at + retention_period`. The countdown starts at the
   **final** successful publication across all intended targets, never at the generation
   time. Adding a new target or re-publishing restarts the countdown.
5. **No active job** references the artifact or its highlight/render: no render, upload,
   publish, verification, QC or export job is PENDING, RUNNING or RETRY_WAIT.
6. The artifact's storage volume is **mounted and its identity verified**: the volume
   marker UUID matches the recorded one (the external SSD check). A path on an unmounted or
   different volume is never touched.
7. The file still matches its record (size and, when cheap, SHA-256 or inode/mtime
   fingerprint) and lies inside an allowed deletable location. Path containment is checked
   with the same guard as Phase 1 (`safe_join`).

Any failed condition gives the artifact a **blocking reason code** (shown in the dashboard and
dry run), for example `upload_pending:youtube`, `unverified:tiktok`, `locked`, `active_job`,
`volume_offline`, `retention_not_elapsed`.

### 6a.3 Crash-safe, idempotent deletion

- **Scheduler:** a persistent `CLEANUP_SWEEP` task runs **hourly while RIFTWIRE is active**.
  Its `next_run_at` is stored in the DB. On startup an **overdue** sweep runs immediately,
  so missed runs are recovered after a restart or sleep.
- **Sweep (light job):** lists candidates and enqueues one `DELETE_ARTIFACT` job per eligible
  artifact. It is idempotent: one active deletion job per artifact.
- **`DELETE_ARTIFACT` (fenced):**
  1. Re-verify every condition in §6a.2, including the platform API check and volume identity.
  2. In one transaction, write the intent `deleting` with a reason, then atomically rename
     the file into `<volume>/.riftwire-trash/<artifact_id>` (same volume).
  3. Unlink it, fsync the directory, and mark `deleted` with `deleted_at` and bytes reclaimed.
  The unlink in step 3 is gated (below); step 2 alone is always reversible.
- **Final gate before every permanent unlink.** Moving a file into the internal trash is
  never a deletion decision by itself. Immediately before unlinking (in the job *and* in
  recovery), one `BEGIN IMMEDIATE` transaction:
  1. re-checks **current** eligibility: every §6a.2 condition, including retention locks
     ("Keep forever" and manual locks), active jobs, publication states, cleanup still
     enabled and the retention period as configured *now*. Platform verification is reused
     only if it is fresher than a short window (default 15 min); otherwise it is redone first,
     outside the transaction;
  2. re-checks volume identity and that the trash file still matches the recorded fingerprint;
  3. only if everything passes, commits state `unlinking` (a commit point). Then it unlinks,
     fsyncs the directory and marks `deleted`.
- **Restore when blocked.** If any check fails, or cannot be completed (for example, the API
  is unreachable), the file is **restored**, not deleted. It is renamed from the trash back
  to its recorded path on the same volume with the Phase 1 no-replace promotion, and the
  state goes back to `active`. The audit log gets the blocking reason. If the original path
  is now occupied, nothing is overwritten: the file stays in the trash as
  `restore_conflict` and is shown to the owner. It is never unlinked automatically. If the
  volume is offline, nothing is touched and the job retries later.
- **Locks win races.** Setting "Keep forever" (or any lock) is a DB write serialized with
  the final gate. A lock committed before `unlinking` always blocks and restores the file.
  Once `unlinking` is committed, the decision is final. The dashboard shows "deletion in
  progress" for that brief step and rejects the lock with a clear message; it never shows
  the lock as saved.
- **Recovery:** the reconciler resumes interrupted deletions from the recorded state, never
  from guesses:
  - `deleting`, file still at its original path → full re-check, then proceed or return to
    `active`;
  - `deleting`, file in the trash → **final gate**, then unlink or restore as above;
  - `unlinking`, file in the trash → finish the unlink (the decision was committed);
  - file gone in either state → mark `deleted`.

  Re-running never deletes anything twice or anything else.
- Uses the Phase 1 queue guarantees (leases, fencing, orphan cleanup) and per-volume floor
  logic unchanged.

### 6a.4 Dashboard controls

- **Settings:** enable/disable automatic cleanup; retention period (default 72 h);
  per-channel override (optional).
- **Per video:**
  - **Keep forever** toggle, with lock reason and who set it;
  - **scheduled deletion time**, or the blocking reasons;
  - publication status per target, with verification time.
- **Storage reclaimed:** total and per period (by cleanup), alongside current usage per volume.
- **Cleanup history:** sweeps (time, mode, candidates, deleted, bytes, errors) and a
  per-artifact **audit log**:
  - which file (path, hash, size);
  - why it was eligible (publication IDs and API verification results with timestamps);
  - who/what deleted it, and when;
  - or why it was blocked.
- **Dry-run mode:** lists exactly which files *would* be deleted now and which are blocked,
  with reasons, and changes nothing. Dry run is the default view before the first real run.

### 6a.5 Configuration

`RIFTWIRE_CLEANUP_ENABLED` (default `true`), `RIFTWIRE_RETENTION_HOURS` (default `72`),
`RIFTWIRE_CLEANUP_INTERVAL_MINUTES` (default `60`); dashboard settings override, with changes
audited.

### 6a.6 Safety guarantees (tested)

- **Never deleted:** originals, metadata, or files outside registered deletable artifacts.
- **Remote content is never touched:** cleanup uses only read-only publication-verification
  calls. Publishing adapters expose `verify(publication_id)` separately from upload, and the
  cleanup code path has no access to delete or update operations (enforced by interface and
  tests).
- **Unverified means kept.** API errors, timeouts or ambiguous responses → not eligible
  (fail-safe), retried at the next sweep.
- **Wrong or offline volume means nothing is deleted.**
- Idempotent and crash-safe, with the same test approach as Phase 1: real SIGKILL at every
  deletion step.
## 7. Remaining open questions

1. Which YouTube channel(s) and account(s) come first, and who approves posts.
2. Ollama: installing it is a system-wide install (Homebrew or its app), which needs approval in
   Phase 3. Also which local LLM size is acceptable (memory/quality trade-off).
3. Arrival and setup of the external SSD (ordered: SanDisk 1 TB Portable SSD). URL acquisition and model storage at scale depend on it.
4. Higgsfield: specific use cases to evaluate first.
5. Brand assets for the first channel (logo, fonts, outro) and their licences.
6. Retention: should "Keep forever" be the default for specific channels or categories (e.g.
   best-performing clips), or always manual?
