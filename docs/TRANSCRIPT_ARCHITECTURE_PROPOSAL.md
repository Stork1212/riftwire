# Proposal: one shared, timestamped transcript per recording

_Status: **approved in principle by the owner for M4** (2026-11-25). MLX Whisper `base`
is the provisional default. Nothing here is implemented, and it will not be in PR #14: the
detailed M4 implementation plan follows after the integration PR is reviewed and merged. It answers the inconsistency found in the integration run
([`INTEGRATION_REVIEW.md`](INTEGRATION_REVIEW.md) §8)._

## Problem
Today four call sites each run Whisper on their own audio window, and none keeps the
result:
- the highlight scan;
- clip suggestions;
- the 35/65 Short;
- subtitled cuts.

As a result:
- **The same speech is transcribed differently.** In the Mac run, the scan read "this get
  is insane" and the Short's captions "Getaway is insane". Whisper's output depends on the
  surrounding audio it is given.
- **Work is repeated.** Every export re-transcribes its range, and a scan re-transcribes
  the whole recording. That costs time on the one heavy worker.
- **Raw word timestamps are not preserved,** which the approved M0 decision requires.
  Nothing is auditable afterwards: a caption cannot be traced to the transcript it came
  from.

## Proposal
**1. A `TRANSCRIBE` job per recording**, queued after `METADATA`. It is one heavy job and
respects the one-heavy-worker rule.
- **Chunking:** the recording is transcribed in fixed chunks of about 30 s, with a few
  seconds of overlap, using MLX Whisper `base` (the approved default).
- **Merging:** overlapping words are merged by timestamp. M0 measured the memory needed
  for full-length passes, which is why it runs in chunks.
- **Rights:** the job's use is `analyze`, so the M2 rights checks apply as they do for the
  highlight scan.
- **Recovery:** it is resumable by chunk, so a crash keeps the finished chunks.

**2. A stored, immutable transcript artefact.**
- **`transcripts` table:** one row per *(recording, engine, engine version, model, model
  checksum, parameters, chunking version)*. It records the status, coverage in
  milliseconds and the creation time.
- **Word storage, either:**
  - a `transcript_words` table (transcript id, index, start_ms, end_ms, text, probability,
    chunk); or
  - a compressed JSON file in the managed `analysis` location, written atomically, with
    its SHA-256 in the row.

  Both keep the raw word timestamps **unchanged**.
- **Idempotency:** the identity key includes the recording's SHA-256 and the model. A
  re-run with the same inputs is a no-op, and a new model creates a new transcript beside
  the old one.

**3. Timestamp correction is a separate, versioned layer** (approved M0 decision).
- Any offset or alignment correction is stored as its own record, referencing the raw
  transcript and carrying a correction version.
- It is never written over the raw words.
- It is only enabled after validation on authorized real commentary.

**4. Consumers read slices; they never transcribe.**
- **Highlight scan, clip suggestions, captions, subtitles:** they call
  `transcripts.words(recording_id, start_ms, end_ms, *, corrected=False)`.
- **Missing transcript:** a consumer waits on, or queues, the `TRANSCRIBE` job instead of
  running Whisper itself.
- **Recorded on every result:**
  - every highlight candidate and export records the transcript id (and correction
    version, if any) it used;
  - later AI stages (M5–M6) also record the prompt version, provider and model
    (rule 16).

**5. Captions use the same words as the evidence.** A Short's captions are the stored
words inside the clip range, re-based to clip time. The highlight excerpt and the captions
therefore always match.

## Cost and risks
- **Time:** about one `base` pass per recording; M0 measured about 31 s per 60 minutes of
  audio on the M3 Pro. Exports become faster.
- **Space:** words are small, a few hundred kilobytes per hour of continuous speech before
  compression. Storage goes through the
  normal reservations and the per-volume floor.
- **Risk:** chunk-boundary words. They are handled by overlap and merge, with tests on
  synthetic speech placed across a boundary.
- **Risk:** transcription quality is unchanged by this design. It removes inconsistency
  and duplication; it does not make `base` more accurate.

## Migration and tests (when approved)
- An Alembic migration for `transcripts` (and `transcript_words` if chosen), with `0009`
  as the base.
- Tests:
  - idempotent re-run;
  - a new model creates a second transcript;
  - crash mid-chunk resumes;
  - the merge is correct across a boundary;
  - consumers never call Whisper (the existing call sites are removed);
  - captions equal the stored words for the range.
- The E2E check gains "the scan excerpt and the captions come from the same transcript
  id".
