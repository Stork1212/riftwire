# M4 — Persistent Transcription & Multimodal Signals: implementation plan

_Status: **plan, for approval** (2026-11-25). No M4 code exists yet. The owner's sprint
brief (2026-11-25) authorizes M4 once PR #14 is merged. Until then this plan is the only
M4 work: the brief forbids dependent implementation against an unapproved baseline._

**Baseline:** `phase2/integration` @ `5ceb2e2` (PR #14). The code commit `51c8fa4` was
verified natively on the Mac on 2026-11-25. M4 starts from `main` once PR #14 is merged.
**Builds on:** [`TRANSCRIPT_ARCHITECTURE_PROPOSAL.md`](TRANSCRIPT_ARCHITECTURE_PROPOSAL.md)
(approved in principle), [`PHASE2_PROPOSAL.md`](PHASE2_PROPOSAL.md) §9–§10, the M0 results
in [`BENCHMARKS.md`](BENCHMARKS.md), and the approved decisions in CLAUDE.md §2.

## 1. Starting point (what exists today, read from the code)
- **Four independent Whisper call sites**, none of which stores its result:
  - `highlight_scan.scan_video` (the whole recording in one pass);
  - `clips.suggest` (the first 300 s);
  - `clips._render_split` (the clip range);
  - `clips.cut(subtitles=True)` (the clip range).
- **They depend on the benchmark harness.** All four run
  `bench/riftbench/transcribe_child.py` with `RIFTWIRE_CLIP_WHISPER_PYTHON` (the
  `bench/.venv` interpreter, which has `mlx-whisper`) and `RIFTWIRE_CLIP_WHISPER_MODEL`
  (MLX `base`, `weights.npz`). The application therefore depends on a file under
  `bench/`.
- **Signals are computed ad hoc.** Frame activity is computed inside
  `highlight_scan` (a 2 Hz grayscale proxy, discarded after use). No audio signals exist in
  the application. M0 validated FFmpeg scene-cut and loudness-burst detection on synthetic
  footage (`bench/riftbench/signals.py`).
- **No storage location for analysis output.** The kinds are `recordings`, `temp`,
  `models`, `exports` and `assets`.
- **A name to avoid:** `services/evidence.py` already means **rights evidence** (M2). The
  new layer is called **analysis evidence** (`services/analysis_evidence.py`) to avoid the
  clash.

## 2. Scope
**In scope:**
- the `TRANSCRIBE` job and a stored, versioned transcript;
- the `AUDIO_SIGNALS` and `VISUAL_SIGNALS` jobs;
- the analysis-evidence layer;
- moving all four consumers onto the shared transcript;
- tests, documentation, and a pinned Mac procedure.

**Out of scope:**
- **OCR** needs Apple Vision through `pyobjc-framework-Vision`, a new dependency that
  needs approval. M4 ships the interface and the evidence slot, marked "not available".
- AI providers (M5) and contextual highlights (M6).
- `small` as the default model (it is optional; chunked memory tests decide).
- Any model download.

**No new dependencies.** It uses FFmpeg, the existing `mlx-whisper` in its own venv (the
backend never imports it), the standard library, SQLAlchemy and Pillow (approved). The
backend has no NumPy, and the signal maths stays in pure Python over compact series, as
`highlight_scan` does today.

## 3. Data model (migration `0010`, additive)
All timestamps are integer **milliseconds relative to the recording**, unless named
`source_*`.

| Table | Purpose | Key columns |
|---|---|---|
| `transcription_configs` | One row per distinct configuration; immutable | `fingerprint` (SHA-256 of the canonical JSON, unique), `config_json`: engine `mlx-whisper`, engine version, model id `mlx-community/whisper-base-mlx`, **model SHA-256** (of `weights.npz` + `config.json`), language, chunk_ms, overlap_ms, audio extraction (16 kHz mono s16le), decoding options, `merge_version` |
| `transcripts` | One per (recording, config) | `id`, `recording_id`, `recording_sha256` (snapshot), `config_id`; **UNIQUE (recording_id, config_id)**; `status` (`pending`/`partial`/`complete`/`failed`); `coverage_ms`, `duration_ms`; `word_count`; `created_at`, `completed_at`; `job_id` |
| `transcript_chunks` | Resumability and coverage | `transcript_id`, `idx`, `start_ms`, `end_ms` (overlap included), `audio_sha256`, `status`, `raw_json_gz` (the child's untouched output), `elapsed_ms`, `mlx_peak_bytes` |
| `transcript_words` | **Immutable raw words** after the merge | `transcript_id`, `idx`, `start_ms`, `end_ms`, `text`, `prob` (nullable), `chunk_idx`, `segment_idx`; PK (transcript_id, idx) |
| `transcript_segments` | Segment-level timing | `transcript_id`, `idx`, `start_ms`, `end_ms`, `text`, `first_word_idx`, `last_word_idx`, `avg_logprob`, `no_speech_prob` |
| `transcript_corrections` | **Separate, versioned** timing correction | `id`, `transcript_id`, `version`, `method` (e.g. `constant_offset`), `params_json`, `validated` (bool, false by default), `validation_ref`, `created_at`. Never alters `transcript_words` |
| `signal_runs` | One per (recording, signal kind, config) | `id`, `recording_id`, `kind` (`audio`/`visual`), `config_fingerprint`, **UNIQUE (recording_id, kind, config_fingerprint)**, `status`, `series_gz` (compressed JSON series), `events_json`, `created_at` |
| `keyframes` | Representative frames | `id`, `signal_run_id`, `pts_ms`, `scene_idx`, `stored_filename` (a generated name in the thumbnails directory), `width`, `height`, `sha256` |
| `recordings.source_offset_ms` | Source-relative mapping | Nullable; `0` for whole-file imports and fetches. Section acquisition (later in M3) sets it. Source time = recording time + offset |
| `jobs.job_type` | New types | `TRANSCRIBE`, `AUDIO_SIGNALS`, `VISUAL_SIGNALS` (check constraint) |

**Why the database and not files:**
- **Scale:** words for an hour of speech are on the order of 10⁴ rows; compressed series
  are hundreds of kilobytes.
- **Safety:** SQLite gives atomicity, the existing backups, and no new storage-location
  kind.
- **Room for growth:** if the database grows past a threshold measured in M4, series move
  to an `analysis` location. That would be a recorded design change, not a default.

**Immutability:** triggers forbid `UPDATE` and `DELETE` on `transcript_words`, on
`transcript_segments`, and on `transcript_chunks` rows once `status='done'`, following the
M2 trigger pattern.

## 4. TRANSCRIBE
**Configuration and identity:**
- **Defaults:** MLX Whisper `base` (the provisional default), chunk 120 s, overlap 4 s,
  language `en`, with an override per source.
- **Settings:** `RIFTWIRE_TRANSCRIBE_CHUNK_SECONDS`, `RIFTWIRE_TRANSCRIBE_OVERLAP_SECONDS`
  and `RIFTWIRE_WHISPER_PYTHON`/`MODEL`. The old `CLIP_WHISPER_*` names stay as
  deprecated aliases.
- **The model is verified at the start of each job:** its SHA-256 must match the
  configuration; otherwise the job fails permanently with `model_changed`. **No
  downloads, ever.** A missing model gives a clear `model_missing` error.

**Pipeline (one heavy job; it respects `RIFTWIRE_WORKER_CONCURRENCY=1`):**
1. **Fence `analyze`** (M2 rules, using the recording's actual method).
2. **Reserve temporary space:** 16 kHz mono s16le is about 115 MB per hour, plus a
   margin.
3. **Extract each chunk's audio** with FFmpeg (`-ss`/`-t` on the source, as
   argument lists). Its SHA-256 is recorded.
4. **Transcribe in a child process.** One child per job runs
   `backend/app/transcription/child.py`, which **moves out of `bench/`** and has no
   application imports. It is run by the Whisper venv's Python and writes each finished
   chunk to its own JSON file atomically. The parent stores the chunk row
   (`raw_json_gz`, status `done`) in a short transaction after each chunk.
5. **Resume.** A crash, timeout, sleep or cancel loses at most the chunk in progress. On
   retry the job skips chunks already `done`, whose audio SHA-256 matches.
6. **Merge deterministically** (`merge_version=1`) and write the words and segments in
   one transaction.
7. **Set the status:** `complete`, or `partial` with the coverage if the input was
   truncated.
8. **Fence again; release the reservation.**

**Merge rule (deterministic, tested):**
- **Cut point:** within each overlap, take the lowest-energy 200 ms window in the
  overlap, using the audio signal when present and otherwise the overlap's midpoint.
- **Assignment:** chunk A keeps the words whose midpoint is before the cut; chunk B keeps
  the words whose midpoint is at or after it.
- **Duplicate check:** if a kept word from A and one from B overlap by ≥ 50 % in time and
  have the same normalised text, keep the higher `prob`; on a tie, keep A's.
- **Validation:**
  - times are clamped to `[0, duration]`;
  - starts must be monotonic, otherwise the merge fails loudly;
  - the result does not depend on the order in which chunks finished.

**Idempotency and regeneration:**
- **Re-queueing is a no-op.** A `TRANSCRIBE` job for (recording, config) that is already
  `complete` returns the existing transcript, without running Whisper.
- **A new configuration** (model, chunking, language or merge version) creates a new
  transcript beside the old one. The API offers
  `POST /recordings/{id}/transcripts {config?}` for explicit regeneration.
- **Nothing is deleted;** old transcripts stay for provenance.
- **The current transcript** is the one matching the current default configuration.

**Rights and storage:**
- The job type maps to use `analyze` in `JOB_USE`; the test that forbids unmapped job
  types covers it.
- Reservations cover temporary audio and keyframes.
- An offline location pauses the job (`storage_offline`), as for the M1 jobs.

## 5. AUDIO_SIGNALS (measured vs inferred)
- **Input:** a single FFmpeg pass over the audio. `astats` (RMS and peak per 100 ms) and
  `ebur128` (momentary and short-term LUFS at 10 Hz), plus `silencedetect`.

**Measured series (stored in `series_gz`):**
- `rms_db`, `peak_db` and `lufs_m` at 10 Hz;
- silence spans;
- onsets: a rise of ≥ X dB within 500 ms, with X configurable;
- **speech activity**, from transcript word coverage when a transcript exists, otherwise
  from energy;
- **speaking pace**: words per second over a rolling 5 s window, from the transcript.

**Inferred events (stored in `events_json`):**
- **Each event is marked `"kind":"inferred"`,** with its method, thresholds and a
  heuristic confidence. Examples: `loud_burst`, `possible_shouting` (high energy during
  speech with a pace change), `possible_laughter` (rapid periodic energy bursts with no
  words, a heuristic).
- **What they are not:** none of these is a gunshot, an explosion or an exciting moment.
  The API and the evidence bundle keep `measured` and `inferred` apart.

## 6. VISUAL_SIGNALS
- **Proxy decode:** FFmpeg decodes a proxy (160 px grayscale; frame rate configurable,
  default 4 Hz) with `showinfo` **presentation timestamps**, so variable-frame-rate
  footage keeps its real frame times. Frame index × fps is never assumed.

**Measured series:**
- frame difference (motion intensity);
- the scene-change score (FFmpeg `scdet`, the method validated in M0);
- the scene list with durations;
- rapid-change bursts.

**Keyframes:**
- **Selection:** one per scene (the frame at the scene's motion median), plus the peaks.
- **Format:** JPEG at 480 px width, stored with generated names and reserved space.
- **Capped per hour,** so a two-hour recording stays a few megabytes.

**"Potential camera change"** is an inferred event (a scene cut with low motion before and
after), labelled as inferred. **No semantic visual claims** come from motion.

## 7. Analysis-evidence layer (`services/analysis_evidence.py`)
`window(recording_id, start_ms, end_ms, *, transcript=current, corrected=False) ->
EvidenceBundle` (a pydantic model, `schema="evidence.v1"`).

**Contents:**
- recording metadata: id, SHA-256, duration, fps or VFR, source id, source offset;
- the transcript reference: id, config fingerprint and correction version;
- words and segments with **stable ids** (`w:<transcript>:<idx>`,
  `seg:<transcript>:<idx>`);
- audio and visual measurements, downsampled to the window;
- events (`a:<run>:<i>`, `v:<run>:<i>`), each marked measured or inferred;
- scenes (`sc:<run>:<i>`) and keyframe references (`kf:<id>`);
- OCR: `{"available": false}`.

**Fingerprint:** the SHA-256 of the canonical bundle, recorded by every consumer.

**Traceability:** every id resolves back to a row, and every time is in recording time,
with the source time derived. M6 validates the model's evidence references against these
ids.

**Size control:** `max_words` and `max_events` keep bundles small enough for a model
prompt. Whole videos are never passed to a model.

## 8. Moving the consumers onto the shared transcript
| Consumer | Change |
|---|---|
| `highlight_scan` | Reads `analysis_evidence` (words + motion series) instead of running Whisper and decoding frames. The job depends on `TRANSCRIBE` + `VISUAL_SIGNALS` and queues them if missing. Its results record `transcript_id` and `signal_run_ids` |
| `clips.suggest` | Reads the words in the range |
| `clips._render_split` (captions) and `cut(subtitles=True)` | Read the words in the clip range, re-based to clip time. Every export records `transcript_id` and the correction version in its provenance event |
| `bench/` | Unchanged; the benchmark keeps its own harness |

**The rule:** no application module may launch Whisper except `TRANSCRIBE`. A test
asserts this by patching the child launcher and failing on any other caller. The four
`_transcribe` paths are deleted.

**Behaviour while the transcript is missing:** export returns `409 transcript_pending`
and queues `TRANSCRIBE`, rather than transcribing inline. Exports are faster afterwards,
and the scan excerpt and the captions are identical by construction.

## 9. Acceptance gate → tests (all synthetic or generated; none on owner footage)
| Gate item | Test |
|---|---|
| One transcript serves many consumers | Scan + suggest + two exports → **one** Whisper invocation (counted by the stub child), and the same `transcript_id` everywhere |
| No unnecessary rerun | Re-queue `TRANSCRIBE` → no child launched; a new configuration → a second transcript and a second run |
| Interrupted transcription resumes | Kill the child after chunk *k* (a stub hook); the retry runs only chunks > *k*; the final words equal those of an uninterrupted run |
| Boundary words reconciled | Synthetic speech placed across each boundary (stub and real) → no duplicates or losses; the merge is invariant to completion order |
| Raw timestamps unchanged | Triggers refuse UPDATE/DELETE; applying a correction leaves the raw rows' bytes identical; `corrected=True` returns shifted copies |
| Captions and evidence share the transcript | The export provenance `transcript_id` equals the scan result's `transcript_id`; the caption text equals the stored words in range |
| Signal timestamps match the footage | Generated VFR and CFR clips with markers at known times (a tone and a flash) → audio onset and scene cut within ±1 proxy frame / ±50 ms; keyframe `pts_ms` matches |
| Storage and rights enforced | Revoked mid-job → no transcript rows committed past the fence, a permanent failure with provenance; floor breach → 507 / retry; offline location → paused |
| Rendering still works | The existing outro, caption and split tests pass, now fed from the stored transcript; E2E 17.5 s / 525 frames unchanged |
| Migration | Populated `0009` DB → `0010` → `0009` → `0010`; the 14 existing triggers plus the new ones; FK clean |

## 10. Native Mac verification (pinned script, like PR #14's)
- **Environment:** the real 60 GiB floor, isolated data and ports, real MLX `base`
  (already installed), and macOS `say` speech over a generated 30-minute and a 2-hour
  synthetic recording.
- **Transcription measurements:**
  - wall time per hour of audio;
  - MLX peak per chunk;
  - process footprint;
  - **swap before and after** (`sysctl vm.swapusage`);
  - database growth;
  - resume after a `kill -9` mid-run;
  - word-boundary counts.
- **Signal measurements:** VFR timing accuracy on the generated markers.
- **Expectations, to be verified rather than assumed:**
  - M0 measured `base` at RTF 0.009–0.018, about 31 s per hour, so about 1 minute for
    2 hours plus extraction;
  - MLX peak about 1 GB per 10-minute input, so 120 s chunks should stay well under it.
- **Reporting:** results go to `docs/BENCHMARKS.md` (M4 section) with the commit SHA. M4
  is **not** declared complete on synthetic container tests alone; the Mac run is the
  gate.

## 11. Approvals needed
| Item | Status |
|---|---|
| `mlx-whisper` and MLX `base` | Already approved (D3, M0) and installed on the Mac |
| OCR through Apple Vision (`pyobjc-framework-Vision`) | **Needs approval**; deferred, with a stub only |
| `small` as default | Not proposed. It is optional after the chunked memory test, which M4's chunking enables |
| Live-demo database migration to `0010` | **Needs approval**; it will not be done by the agent |

## 12. Risks
- **Timing bias.** Raw `base` word timings are about 280 ms early (M0). The correction
  layer exists, but stays `validated=false` until it is measured on authorized real
  commentary. Captions therefore keep their current behaviour.
- **Chunk-edge recognition.** Whisper may mishear a word cut at a chunk edge; the overlap
  and the energy-based cut reduce this. The rate is measured in the boundary tests.
- **Database growth on very long recordings.** It is measured, with a documented
  threshold for moving series to files.
- **Heuristic labels.** `possible_laughter` and `possible_shouting` are heuristics, with
  no ground truth yet. They are inferred and must never be presented as detected events.
