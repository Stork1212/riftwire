"""Job handlers. Each is idempotent and writes to the database only through fenced
transactions (via the JobContext / queue.complete)."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import (
    ConflictError,
    DependencyPendingJobError,
    MemoryPressureJobError,
    NotFoundError,
    PayloadTooLarge,
    PermanentJobError,
    RightsRefused,
    RightsRefusedJobError,
    ServiceUnavailable,
    StorageOffline,
    StorageOfflineJobError,
    SupersededJobError,
    TransientJobError,
    ValidationFailed,
)
from app.core.logging import get_logger
from app.db.session import Database
from app.models import (
    Job,
    JobType,
    Recording,
    RecordingStatus,
    Severity,
    SignalKind,
    TranscriptChunk,
)
from app.services import (
    evidence_reliability,
    highlight_scan,
    media,
    queue,
    reservations,
    signals,
    transcripts,
    url_download,
)
from app.services.acquisition import AcquisitionUnavailable
from app.services.events import record_event
from app.services.ingest import FinalizeError, finalize_recording
from app.services.locations import get_layout
from app.services.storage import fsync_dir, safe_join, sha256_file

log = get_logger("handlers")

# Storage locations each job type reads or writes. While any is offline the job is not
# started (claim loop) and an attempt that loses its drive mid-way is deferred, not failed.
REQUIRED_KINDS: dict[JobType, tuple[str, ...]] = {
    JobType.INGEST: ("temp", "recordings"),
    JobType.METADATA: ("recordings",),
    # Thumbnails are written next to their final (internal) location, never via temp, so a
    # temp location on another drive can never make the final rename cross devices.
    JobType.THUMBNAIL: ("recordings",),
    # The download and the import's .part file are both on temp; INGEST then moves it.
    JobType.ACQUIRE: ("temp",),
    JobType.HIGHLIGHT_SCAN: ("recordings", "temp"),
    JobType.TRANSCRIBE: ("recordings", "temp"),
    # Signals read the recording and stream FFmpeg's metadata through temp; keyframes go
    # to the internal thumbnails folder.
    JobType.AUDIO_SIGNALS: ("recordings", "temp"),
    JobType.VISUAL_SIGNALS: ("recordings", "temp"),
    # Reads stored analysis only; writes database rows.
    JobType.AI_HIGHLIGHTS: (),
}

THUMB_TMP_PREFIX = ".job-"           # thumbnails/.job-<job>-<token>.tmp.jpg
THUMB_RESERVE_BYTES = 4 * 1024 * 1024


def require_storage(ctx: JobContext) -> None:
    try:
        get_layout(ctx.settings).require_online(*REQUIRED_KINDS[ctx.claimed.job_type])
    except StorageOffline as e:
        raise StorageOfflineJobError(e.message) from e


class JobContext:
    """Everything a handler needs; enforces fencing for progress and child tracking."""

    def __init__(self, db: Database, settings: Settings, claimed: queue.ClaimedJob):
        self.db = db
        self.settings = settings
        self.claimed = claimed
        self.lost = threading.Event()      # lease lost (fence failed)
        self.cancel = threading.Event()    # shutdown requested
        self._last_progress = -1
        self._last_progress_ts = 0.0

    @property
    def job_id(self) -> str:
        return self.claimed.id

    def should_stop(self) -> bool:
        return self.lost.is_set() or self.cancel.is_set()

    def check(self) -> None:
        if self.lost.is_set():
            raise queue.LeaseLost(self.job_id)
        if self.cancel.is_set():
            raise media.CommandCancelled("shutdown")

    def progress(self, percent: int) -> None:
        now = time.monotonic()
        if percent == self._last_progress or (now - self._last_progress_ts < 0.5
                                              and percent < 100):
            return
        try:
            queue.set_progress(self.db, self.job_id, self.claimed.token, percent)
        except queue.LeaseLost:
            self.lost.set()
            raise
        self._last_progress, self._last_progress_ts = percent, now

    def _on_spawn(self, pid: int, started_at: str | None) -> None:
        try:
            queue.set_process(self.db, self.job_id, self.claimed.token, pid, started_at)
        except queue.LeaseLost:
            self.lost.set()  # run_command sees should_stop() and kills the group

    def run(self, args: list[str], timeout: float) -> media.CommandResult:
        try:
            return media.run_command(args, timeout, on_spawn=self._on_spawn,
                                     should_cancel=self.should_stop)
        finally:
            if not self.lost.is_set():
                try:
                    queue.set_process(self.db, self.job_id, self.claimed.token, None, None)
                except queue.LeaseLost:
                    self.lost.set()

    # Thin wrappers so handlers get fenced child tracking automatically.
    def probe(self, path: Path) -> media.MediaInfo:
        return media.probe(self.settings.ffprobe_path, path,
                           self.settings.ffprobe_timeout_seconds,
                           on_spawn=self._on_spawn, should_cancel=self.should_stop)


def _load_recording(ctx: JobContext) -> Recording:
    with ctx.db.session() as s:
        rec = s.get(Recording, ctx.claimed.recording_id)
        if rec is None:
            raise PermanentJobError("recording no longer exists")
        s.expunge(rec)
        return rec


# ----------------------------------------------------------------------------- INGEST

def handle_ingest(ctx: JobContext) -> None:
    require_storage(ctx)
    rec = _load_recording(ctx)
    try:
        outcome = finalize_recording(ctx.settings, rec, attempt_token=ctx.claimed.token,
                                     progress=ctx.progress, should_stop=ctx.should_stop,
                                     db=ctx.db,
                                     holder=f"job:{ctx.job_id}:{ctx.claimed.token}")
    except StorageOffline as e:
        raise StorageOfflineJobError(e.message) from e
    except FinalizeError as e:
        if e.permanent:
            raise PermanentJobError(str(e)) from e
        raise
    except InterruptedError:
        ctx.check()
        raise
    ctx.check()

    def apply(s: Session) -> None:
        r = s.get(Recording, rec.id)
        r.staging_filename = None
        if r.status in (RecordingStatus.STAGED.value, RecordingStatus.FAILED.value):
            r.status = RecordingStatus.PROCESSING.value
            r.error_message = None
        record_event(s, Severity.INFO, "worker", "recording_stored",
                     f"Recording stored and verified ({outcome})",
                     job_id=ctx.job_id, recording_id=rec.id)

    queue.complete(ctx.db, ctx.claimed, apply)


# --------------------------------------------------------------------------- METADATA

def handle_metadata(ctx: JobContext) -> None:
    require_storage(ctx)
    rec = _load_recording(ctx)
    path = ctx.settings.managed_path(rec.file_path)
    if not path.exists():
        raise PermanentJobError("recording file is missing")
    ctx.progress(10)
    try:
        info = ctx.probe(path)
    except media.InvalidMedia as e:
        raise PermanentJobError(str(e)) from e
    ctx.check()

    def apply(s: Session) -> None:
        r = s.get(Recording, rec.id)
        r.container_format = info.container_format
        r.duration_ms = info.duration_ms
        r.width, r.height = info.width, info.height
        r.fps_num, r.fps_den = info.fps_num, info.fps_den
        r.is_variable_frame_rate = info.is_variable_frame_rate
        r.video_codec, r.audio_codec = info.video_codec, info.audio_codec
        r.has_audio = info.has_audio
        r.status = RecordingStatus.READY.value
        r.error_message = None

    queue.complete(ctx.db, ctx.claimed, apply)


# -------------------------------------------------------------------------- THUMBNAIL

def thumbnail_path(settings: Settings, recording_id: str) -> Path:
    return safe_join(settings.thumbnails_dir, f"{recording_id}.jpg")


def handle_thumbnail(ctx: JobContext) -> None:
    require_storage(ctx)
    rec = _load_recording(ctx)
    s = ctx.settings
    dst = thumbnail_path(s, rec.id)

    def mark_ready(sess: Session) -> None:
        sess.get(Recording, rec.id).thumbnail_status = "READY"

    if media.is_valid_jpeg(dst):  # idempotent: never regenerate a valid thumbnail
        queue.complete(ctx.db, ctx.claimed, mark_ready)
        return

    if s.env == "test" and s.test_slow_ffmpeg_seconds > 0:
        ctx.run([s.ffmpeg_path, "-v", "error", "-nostdin", "-re", "-f", "lavfi", "-i",
                 f"testsrc2=size=64x64:duration={s.test_slow_ffmpeg_seconds}",
                 "-f", "null", "-"], timeout=s.test_slow_ffmpeg_seconds + 30)
        ctx.check()

    src = s.managed_path(rec.file_path)
    tmp = safe_join(s.thumbnails_dir,
                    f"{THUMB_TMP_PREFIX}{ctx.job_id}-{ctx.claimed.token}.tmp.jpg")
    kw: dict = {"on_spawn": ctx._on_spawn, "should_cancel": ctx.should_stop}
    errors = []
    # Floor check for the (internal) thumbnails volume, like every other write.
    layout = get_layout(s)
    res = reservations.reserve_on_volume(
        ctx.db, reservations.volume_for_path(layout, s.thumbnails_dir), s.thumbnails_dir,
        s.min_free_disk_bytes, "thumbnails", f"job:{ctx.job_id}:{ctx.claimed.token}",
        THUMB_RESERVE_BYTES, lease_seconds=s.reservation_lease_seconds)
    try:
        for seek in dict.fromkeys([media.thumbnail_seek_seconds(rec.duration_ms), 0.0]):
            try:
                media.render_thumbnail(s.ffmpeg_path, src, tmp, seek=seek,
                                       width=s.thumbnail_width, height=s.thumbnail_height,
                                       timeout=s.ffmpeg_timeout_seconds, **kw)
                break
            except (media.MediaToolTimeout, media.CommandCancelled, media.MediaToolMissing):
                raise
            except media.MediaError as e:
                errors.append(f"seek {seek}s: {e}")
        else:
            raise media.MediaError("; ".join(errors))
        ctx.check()

        def publish(sess: Session) -> None:
            os.replace(tmp, dst)  # atomic; inside the fenced transaction
            mark_ready(sess)

        queue.complete(ctx.db, ctx.claimed, publish)
    finally:
        tmp.unlink(missing_ok=True)
        reservations.release(ctx.db, res)


def handle_acquire(ctx: JobContext) -> None:
    """Fetch the source's saved URL into a linked recording (``url_download.acquire``).

    Retried: download and network failures, a full disk. Permanent, and never retried:
    rights refusals (recorded with provenance by the worker), URL fetching switched off,
    no downloader installed, a file over the size limit, the bytes already belonging to
    another source. Idempotent across attempts and crashes: the workspace is the job id
    (cleared first), a finished acquisition is returned as is, and identical bytes
    imported by a crashed attempt are linked rather than imported twice."""
    require_storage(ctx)
    ctx.check()
    try:
        result = url_download.acquire(
            ctx.db, ctx.settings, ctx.claimed.source_id, actor="system",
            should_cancel=ctx.should_stop, on_spawn=ctx._on_spawn, progress=ctx.progress,
            workspace=ctx.job_id)
    except RightsRefused as e:
        raise RightsRefusedJobError(e.message) from e
    except StorageOffline as e:
        raise StorageOfflineJobError(e.message) from e
    except (AcquisitionUnavailable, ValidationFailed, PayloadTooLarge, ConflictError,
            NotFoundError) as e:
        raise PermanentJobError(f"{e.code}: {e.message}") from e
    except ServiceUnavailable as e:
        if e.code in ("downloader_missing", "ffmpeg_missing"):
            raise PermanentJobError(f"{e.code}: {e.message}") from e
        raise                                   # the platform/network: retry with backoff
    finally:
        if not ctx.lost.is_set():
            try:
                queue.set_process(ctx.db, ctx.job_id, ctx.claimed.token, None, None)
            except queue.LeaseLost:
                ctx.lost.set()
    ctx.check()

    def link_job(s: Session) -> None:
        s.get(Job, ctx.job_id).recording_id = result.recording_id

    queue.complete(ctx.db, ctx.claimed, link_job)


_PENDING = object()


def _shared_transcript(ctx: JobContext, recording: Recording):
    """``(transcript_id, words)`` from the current-configuration transcript (M4), or
    ``_PENDING`` after queueing it. No local Whisper installed: ``(None, [])`` and the scan
    runs without speech, as before M4. Transcription failed for good: a permanent error."""
    if recording.has_audio is False:
        return None, []
    try:
        transcripts.require_engine(ctx.settings)
    except PermanentJobError:
        log.info("no local Whisper configured; scanning without speech",
                 extra={"job_id": ctx.job_id, "recording_id": recording.id,
                        "component": "highlight_scan"})
        return None, []
    with ctx.db.session() as s:
        tr = transcripts.current(s, ctx.settings, recording.id)
        if tr is not None:
            ws = transcripts.words(s, tr.id)
            return tr.id, [{"idx": w["idx"], "w": w["w"], "start_ms": w["start_ms"],
                            "end_ms": w["end_ms"], "prob": w["prob"] or 0.0} for w in ws]
        _fail_if(transcripts.failed_for_current_config(s, ctx.settings, recording.id),
                 "shared transcript")
        transcripts.request(s, ctx.settings, recording.id)
    return _PENDING


def _shared_audio(ctx: JobContext, recording: Recording):
    """``(state, signal_run_id, series)`` of the recording's audio measurement (word
    reliability), or ``_PENDING`` after queueing it. A measurement that failed for the
    current configuration does not fail the scan: ``state == "failed"`` and the scan uses
    the documented fallback (``evidence_reliability.triggering``)."""
    with ctx.db.session() as s:
        state, run_id, series = evidence_reliability.audio_basis(s, recording)
        if state != "pending":
            return state, run_id, series
        signals.request(s, ctx.settings, recording.id, SignalKind.AUDIO)
    return _PENDING


def _shared_visual(ctx: JobContext, recording: Recording):
    """``(signal_run_id, motion series)`` from the current visual signal run, or
    ``_PENDING`` after queueing it."""
    with ctx.db.session() as s:
        run = signals.current(s, recording.id, SignalKind.VISUAL)
        if run is not None:
            return run.id, signals.series(run)
        _fail_if(signals.failed_for_current_config(s, recording.id, SignalKind.VISUAL),
                 "visual signals")
        signals.request(s, ctx.settings, recording.id, SignalKind.VISUAL)
    return _PENDING


def _fail_if(error: str | None, what: str) -> None:
    """A dependency that failed for the current configuration fails this job too (with its
    reason) instead of being waited for forever. One that failed for an older
    configuration is re-armed by the caller's request."""
    if error is not None:
        raise PermanentJobError(f"The {what} could not be produced: {error}")


# ------------------------------------------------------------------------- TRANSCRIBE
def handle_transcribe(ctx: JobContext) -> None:
    """The only place RIFTWIRE runs Whisper (M4). Resumable by chunk: every finished chunk
    is stored in its own fenced transaction and never transcribed again."""
    from app.ai import registry

    if registry.calls_in_flight():          # one heavy workload at a time (M5)
        raise MemoryPressureJobError("an earlier local AI call is still finishing")
    require_storage(ctx)
    recording = _load_recording(ctx)
    if recording.status != RecordingStatus.READY.value or not recording.duration_ms:
        raise PermanentJobError("Wait for the recording to finish processing.")
    settings = ctx.settings
    with queue.fenced(ctx.db, ctx.job_id, ctx.claimed.token) as s:
        tr, chunks = transcripts.prepare(s, settings, recording, ctx.job_id)
        tr_id, done = tr.id, tr.status == "COMPLETE"
        pending = [c.idx for c in chunks if c.status != "DONE"]
        total = len(chunks)
    if done:
        queue.complete(ctx.db, ctx.claimed)
        return
    if recording.has_audio is False:
        def silent(session: Session) -> None:
            transcripts.finalize_silent(session, tr_id)
            queue.wake_dependents(session, recording.id)

        queue.complete(ctx.db, ctx.claimed, silent)
        return
    source = settings.managed_path(recording.file_path)
    if not source.is_file():
        raise PermanentJobError("Recording file is missing.")
    layout = get_layout(settings)
    temp = layout.path("temp")
    volume = layout.location("temp").volume_uuid
    chunk_s = settings.transcribe_chunk_seconds + 2 * settings.transcribe_overlap_seconds
    budget = 32_000 * chunk_s + 16 * 1024 * 1024      # one chunk's 16 kHz PCM + output
    # Renewed between chunks: it must outlive one chunk's extraction and transcription.
    lease = max(600, settings.reservation_lease_seconds, settings.ffmpeg_timeout_seconds
                + settings.transcribe_child_timeout_seconds + 120)
    reservation = reservations.reserve_on_volume(
        ctx.db, volume, temp, layout.floor_for_volume(volume), "transcribe",
        f"job:{ctx.job_id}:{ctx.claimed.token}", budget, lease_seconds=lease)
    try:
        with TemporaryDirectory(prefix="transcribe-work-", dir=temp) as folder:
            for n, idx in enumerate(pending):
                ctx.check()
                require_storage(ctx)
                reservations.renew(ctx.db, layout, reservation, budget, lease_seconds=lease)
                with ctx.db.session() as s:
                    chunk = s.get(TranscriptChunk, (tr_id, idx))
                    s.expunge(chunk)
                raw, audio_sha, elapsed = transcripts.transcribe_chunk(
                    settings, source, Path(folder), chunk, ctx.run)
                with queue.fenced(ctx.db, ctx.job_id, ctx.claimed.token) as s:
                    transcripts.store_chunk(s, tr_id, idx, raw, audio_sha, elapsed)
                ctx.progress(min(98, (total - len(pending) + n + 1) * 98 // max(1, total)))
        ctx.check()
        require_storage(ctx)

        def apply(session: Session) -> None:
            t = transcripts.finalize(session, tr_id)
            woken = queue.wake_dependents(session, recording.id)
            record_event(session, Severity.INFO, "worker", "transcript_completed",
                         f"Transcript complete: {t.word_count} words; {woken} waiting "
                         f"job(s) resumed.",
                         job_id=ctx.job_id, recording_id=recording.id)

        queue.complete(ctx.db, ctx.claimed, apply)
    except (media.MediaError, OSError):
        require_storage(ctx)
        raise
    finally:
        reservations.release(ctx.db, reservation)


# ------------------------------------------------------------------ AUDIO/VISUAL_SIGNALS
KEYFRAME_PREFIX = "kf-"


def _analysis_timeout(settings: Settings, duration_ms: int) -> float:
    """A whole-recording FFmpeg pass: the base timeout plus four times real time. It is a
    hang guard, not a budget: software decoding of 4K60 HEVC can run slower than real
    time, so one times real time would fail such sources every attempt."""
    return settings.ffmpeg_timeout_seconds + 4 * duration_ms / 1000


def handle_signals(ctx: JobContext, kind: SignalKind) -> None:
    """Measure audio or visual signals in one FFmpeg pass and store them (M4). Idempotent:
    a complete run for the current configuration is never measured again."""
    require_storage(ctx)
    recording = _load_recording(ctx)
    if recording.status != RecordingStatus.READY.value or not recording.duration_ms:
        raise PermanentJobError("Wait for the recording to finish processing.")
    s = ctx.settings
    with queue.fenced(ctx.db, ctx.job_id, ctx.claimed.token) as sess:
        run = signals.prepare(sess, recording, kind, ctx.job_id)
        run_id, done = run.id, run.status == "COMPLETE"
    if done:
        queue.complete(ctx.db, ctx.claimed)
        return
    if kind == SignalKind.AUDIO and recording.has_audio is False:
        raise PermanentJobError("This recording has no audio track.")
    source = s.managed_path(recording.file_path)
    if not source.is_file():
        raise PermanentJobError("Recording file is missing.")
    ctx.progress(5)
    with TemporaryDirectory(prefix="signals-work-", dir=get_layout(s).path("temp")) as wd:
        series, events, scenes = signals.measure(
            kind, s.ffmpeg_path, source, recording.duration_ms, ctx.run,
            _analysis_timeout(s, recording.duration_ms), work=Path(wd),
            ffprobe=s.ffprobe_path)
    ctx.progress(60)
    ctx.check()
    plan = signals.keyframe_plan(scenes, recording.duration_ms) if kind == SignalKind.VISUAL \
        else []
    staged: list[tuple[Path, Path]] = []
    keyframes: list[dict] = []
    res = None
    try:
        if plan:
            layout = get_layout(s)
            res = reservations.reserve_on_volume(
                ctx.db, reservations.volume_for_path(layout, s.thumbnails_dir),
                s.thumbnails_dir, s.min_free_disk_bytes, "keyframes",
                f"job:{ctx.job_id}:{ctx.claimed.token}",
                signals.KEYFRAME_RESERVE_BYTES * len(plan),
                lease_seconds=s.reservation_lease_seconds)
        cfg = signals.VISUAL_CONFIG
        for n, k in enumerate(plan):
            ctx.check()
            name = f"{KEYFRAME_PREFIX}{uuid.uuid4()}.jpg"
            tmp = safe_join(s.thumbnails_dir,
                            f"{THUMB_TMP_PREFIX}{ctx.job_id}-{ctx.claimed.token}.kf{n}.tmp.jpg")
            staged.append((tmp, safe_join(s.thumbnails_dir, name)))
            media.render_thumbnail(s.ffmpeg_path, source, tmp, seek=k["pts_ms"] / 1000,
                                   width=cfg["keyframe_width"], height=cfg["keyframe_height"],
                                   timeout=s.ffmpeg_timeout_seconds,
                                   on_spawn=ctx._on_spawn, should_cancel=ctx.should_stop)
            w, h = media.jpeg_size(tmp)
            keyframes.append({**k, "stored_filename": name, "width": w, "height": h,
                              "sha256": sha256_file(tmp)})
            ctx.progress(60 + 35 * (n + 1) // len(plan))
        ctx.check()
        require_storage(ctx)

        def publish(sess: Session) -> None:
            for tmp, final in staged:
                os.link(tmp, final)            # never overwrites; inside the fenced txn
            signals.finish(sess, run_id, series, events, keyframes)
            queue.wake_dependents(sess, recording.id)
            record_event(sess, Severity.INFO, "worker", f"{kind.value}_signals_completed",
                         f"{kind.value.capitalize()} signals measured: "
                         f"{sum(e['kind'] == 'measured' for e in events)} measured and "
                         f"{sum(e['kind'] == 'inferred' for e in events)} inferred events, "
                         f"{len(keyframes)} keyframes.",
                         job_id=ctx.job_id, recording_id=recording.id)

        try:
            queue.complete(ctx.db, ctx.claimed, publish)
        except BaseException:
            for _, final in staged:            # the transaction rolled back: unpublish
                final.unlink(missing_ok=True)
            raise
        if staged:
            fsync_dir(s.thumbnails_dir)
    finally:
        for tmp, _ in staged:
            tmp.unlink(missing_ok=True)
        if res is not None:
            reservations.release(ctx.db, res)


def handle_highlight_scan(ctx: JobContext) -> None:
    """Rank review candidates from the stored analysis (M4): the shared transcript's words
    and the visual signal run's motion series. The scan itself decodes nothing and runs no
    Whisper; missing inputs are queued and this job waits without spending an attempt."""
    require_storage(ctx)
    recording = _load_recording(ctx)
    if recording.status != RecordingStatus.READY.value or not recording.duration_ms:
        raise PermanentJobError("Wait for the recording to finish processing.")
    transcript = _shared_transcript(ctx, recording)
    visual = _shared_visual(ctx, recording)
    audio = _shared_audio(ctx, recording) if transcript != (None, []) else \
        ("unused", None, None)
    pending = [n for n, v in (("transcript", transcript), ("visual signals", visual),
                              ("audio signals", audio)) if v is _PENDING]
    if pending:
        raise DependencyPendingJobError(f"waiting for the shared {' and '.join(pending)}")
    transcript_id, words = transcript
    run_id, series = visual
    audio_state, audio_run, audio_series = audio
    ctx.progress(50)
    # Evidence reliability: only words that may trigger a highlight on their own feed
    # the speech cues (raw transcript unchanged; reasons are in the evidence layer).
    assessed = evidence_reliability.assess_words(words, audio_series,
                                                 audio_state=audio_state)
    triggering = evidence_reliability.triggering(words, assessed, audio_state)
    if audio_state == "failed":
        log.warning("audio signals failed; speech cues use the pre-M4 word filter",
                    extra={"job_id": ctx.job_id, "recording_id": recording.id,
                           "component": "highlight_scan"})
    result = highlight_scan.scan_signals(series, triggering, recording.duration_ms)
    result["transcript_id"] = transcript_id
    result["signal_run_ids"] = {"visual": run_id,
                                **({"audio": audio_run} if audio_run else {})}
    result["word_reliability"] = {
        **evidence_reliability.summary(assessed, audio_state),
        "fallback": "pre-M4 word filter (audio signals failed)"
        if audio_state == "failed" else None}
    ctx.check()
    require_storage(ctx)

    def apply(session: Session) -> None:
        session.get(Job, ctx.job_id).result_json = json.dumps(result, ensure_ascii=False)
        record_event(session, Severity.INFO, "worker", "highlights_scanned",
                     f"Found {len(result['items'])} candidates for review.",
                     job_id=ctx.job_id, recording_id=recording.id)

    queue.complete(ctx.db, ctx.claimed, apply)


# ------------------------------------------------------------------------ AI_HIGHLIGHTS

class _Superseded(Exception):
    pass


def handle_ai_highlights(ctx: JobContext) -> None:
    """M5: evaluate candidate windows with the local model, batch by batch, from stored
    analysis only. Each batch's run is stored in its own fenced transaction, so a retry
    (provider down, worker restart, lost lease) resumes at the first unfinished batch of
    the windows pinned on the request's first run. One heavy workload at a time is the
    worker's single-job loop; the model is unloaded after every batch.

    A newer request supersedes the one in progress: the job stops at the next batch (or
    at finalization) without finishing the old request, and waits briefly, without
    spending an attempt, to pick up the new one."""
    from app.ai import prompts as prompt_registry
    from app.ai import registry, scoring
    from app.models import HighlightRequest, RequestStatus
    from app.services import ai_highlights

    recording = _load_recording(ctx)
    if recording.status != RecordingStatus.READY.value or not recording.duration_ms:
        raise PermanentJobError("Wait for the recording to finish processing.")
    settings = ctx.settings
    with ctx.db.session() as s:
        req = ai_highlights.active_request(s, recording.id)
        req_id = req.id if req is not None else None
    if req_id is None:                        # nothing to do (all requests finished)
        queue.complete(ctx.db, ctx.claimed)
        return
    if registry.calls_in_flight():
        raise MemoryPressureJobError("an earlier local AI call is still finishing")
    transcript = _shared_transcript(ctx, recording)
    visual = _shared_visual(ctx, recording)
    audio = _shared_audio(ctx, recording) if transcript != (None, []) else \
        ("unused", None, None)
    pending = [n for n, v in (("transcript", transcript), ("visual signals", visual),
                              ("audio signals", audio)) if v is _PENDING]
    if pending:
        raise DependencyPendingJobError(f"waiting for the shared {' and '.join(pending)}")
    transcript_id, words = transcript
    audio_state, audio_run, audio_series = audio

    def superseded() -> None:
        # Never finalize a superseded request. The job is still needed if a newer request
        # is waiting (request() does not re-arm a running job): wait, no attempt spent.
        with ctx.db.session() as s:
            newer = ai_highlights.active_request(s, recording.id)
        if newer is None:
            queue.complete(ctx.db, ctx.claimed)
            return
        raise SupersededJobError("superseded by a newer highlight request; "
                                 "starting it next")

    with queue.fenced(ctx.db, ctx.job_id, ctx.claimed.token) as s:
        r = s.get(HighlightRequest, req_id)
        if r.status == RequestStatus.SUPERSEDED.value:
            planned = None
        else:
            planned = ai_highlights.pinned_plan(r)
            if planned is None:   # first run: plan once, reuse on every retry
                assessed = evidence_reliability.assess_words(words, audio_series,
                                                             audio_state=audio_state)
                triggering = evidence_reliability.triggering(words, assessed, audio_state)
                planned = ai_highlights.windows(
                    s, recording, transcript_id=transcript_id, trigger_words=triggering,
                    audio_run_id=audio_run, visual_run_id=visual[0],
                    max_windows=settings.ai_max_windows)
                ai_highlights.pin_plan(r, planned)
            if r.status == RequestStatus.PENDING.value:
                r.status = RequestStatus.RUNNING.value
            done = ai_highlights.finished_batches(s, req_id)
    if planned is None:
        return superseded()
    per = settings.ai_windows_per_call
    batches = [planned[i:i + per] for i in range(0, len(planned), per)]
    todo = [n for n in range(len(batches)) if n not in done]
    if todo:
        free = registry.available_memory_mb()
        if free is not None and free < settings.ai_min_available_memory_mb:
            raise MemoryPressureJobError(
                f"memory_pressure: {free} MB available, {settings.ai_min_available_memory_mb}"
                " MB needed before loading the local model")
        try:
            provider = registry.Cancellable(registry.build(settings), ctx.should_stop)
        except registry.AIDisabled as e:
            raise PermanentJobError(str(e)) from e
    ctx.progress(5)
    try:
        for k, n in enumerate(todo):
            ctx.check()
            with ctx.db.session() as s:
                r = s.get(HighlightRequest, req_id)
                was_superseded = r.status == RequestStatus.SUPERSEDED.value
                if not was_superseded:
                    bundle, meta = ai_highlights.bundle_for(s, settings, recording,
                                                            batches[n], r.game)
                    s.expunge(r)
            if was_superseded:
                raise _Superseded()
            try:
                outcome = ai_highlights.evaluate_batch(bundle, provider, r, settings)
            except (prompt_registry.PromptError, scoring.ScoringError) as e:
                raise PermanentJobError(f"the request's prompt or scoring is no longer "
                                        f"available: {e}") from e
            if outcome.status == "provider_error" and outcome.error_code == "cancelled":
                ctx.check()                    # raises: cancellation or lost lease
            with queue.fenced(ctx.db, ctx.job_id, ctx.claimed.token) as s:
                ai_highlights.store_run(s, s.get(HighlightRequest, req_id), n, outcome,
                                        bundle, meta, settings)
            if outcome.status == "provider_error":
                if outcome.error_transient is False:
                    raise PermanentJobError(f"local AI provider refused: {outcome.error}")
                raise TransientJobError(f"local AI provider error on batch {n + 1}: "
                                        f"{outcome.error}")
            ctx.progress(5 + 90 * (k + 1) // max(1, len(todo)))
        ctx.check()

        def apply(session: Session) -> None:
            r = session.get(HighlightRequest, req_id)
            if r.status == RequestStatus.SUPERSEDED.value:     # superseded at the last step
                raise _Superseded()
            summary = ai_highlights.finalize(session, r)
            record_event(session, Severity.INFO, "worker", "ai_highlights_completed",
                         f"AI highlights: {summary['returned']} of {summary['requested']} "
                         f"requested ({summary['candidates_validated']} validated "
                         f"candidates, {summary['batches']} batches).",
                         job_id=ctx.job_id, recording_id=recording.id)

        queue.complete(ctx.db, ctx.claimed, apply)
    except _Superseded:
        return superseded()


HANDLERS: dict[JobType, Callable[[JobContext], None]] = {
    JobType.INGEST: handle_ingest,
    JobType.METADATA: handle_metadata,
    JobType.THUMBNAIL: handle_thumbnail,
    JobType.ACQUIRE: handle_acquire,
    JobType.HIGHLIGHT_SCAN: handle_highlight_scan,
    JobType.TRANSCRIBE: handle_transcribe,
    JobType.AUDIO_SIGNALS: lambda ctx: handle_signals(ctx, SignalKind.AUDIO),
    JobType.VISUAL_SIGNALS: lambda ctx: handle_signals(ctx, SignalKind.VISUAL),
    JobType.AI_HIGHLIGHTS: lambda ctx: handle_ai_highlights(ctx),
}
