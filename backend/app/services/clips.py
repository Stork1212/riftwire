"""Local speech-based clip suggestions and on-demand, rights-gated MP4 cuts."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from typing import Literal
from uuid import uuid4

from sqlalchemy import select

from app.core.config import Settings
from app.core.errors import (
    ConflictError,
    NotFoundError,
    PermanentJobError,
    ServiceUnavailable,
    ValidationFailed,
)
from app.core.logging import get_logger
from app.db.session import Database
from app.models import Job, JobType, Recording, RecordingStatus
from app.services import media, outro, reservations, rights, split_shorts, transcripts
from app.services.locations import get_layout, require_no_maintenance

log = get_logger(__name__)

ANALYSIS_SECONDS = 300
MAX_CLIP_MS = 30_000
WORK_BYTES = 32 * 1024 * 1024
_TIMEOUT = 180
_work_lock = Lock()
_HOOK = re.compile(r"\b(clutch|won|winner|win|insane|watch|final|wait|nice|no way|let.s go)\b",
                   re.IGNORECASE)


@contextmanager
def _workspace(db: Database, settings: Settings) -> Iterator[Path]:
    if not _work_lock.acquire(blocking=False):
        raise ConflictError("A clip operation is already running. Try again when it finishes.")
    try:
        require_no_maintenance(settings)
        layout = get_layout(settings)
        layout.require_online("temp")
        path = layout.path("temp")
        volume = reservations.volume_for_path(layout, path)
        reservation = reservations.reserve_on_volume(
            db, volume, path, max(settings.min_free_disk_bytes, layout.floor_for_volume(volume)),
            "clips", f"clips:{uuid4()}", WORK_BYTES,
            lease_seconds=max(int(_TIMEOUT + 2 * settings.ffmpeg_timeout_seconds) + 30,
                              settings.reservation_lease_seconds))
        try:
            with TemporaryDirectory(prefix="clip-work-", dir=path) as folder:
                yield Path(folder)
        finally:
            reservations.release(db, reservation)
    finally:
        _work_lock.release()


def _recording(db: Database, settings: Settings, recording_id: str, use: str,
               clip_ms: int | None = None) -> tuple[Recording, Path, rights.Decision]:
    with db.session() as session:
        recording = session.get(Recording, recording_id)
        if recording is None:
            raise NotFoundError("Recording not found")
        decision = rights.evaluate_recording(session, recording_id, use, clip_ms=clip_ms)
        if not decision.allowed:
            raise decision.refuse()
        if recording.status != RecordingStatus.READY.value or not recording.duration_ms:
            raise ConflictError("Wait for the recording to finish processing.")
        get_layout(settings).require_online("recordings")
        return recording, settings.managed_path(recording.file_path), decision


def _run(args: list[str], timeout: float, error: str) -> None:
    try:
        result = media.run_command(args, timeout)
    except media.MediaError as exc:
        raise ServiceUnavailable(error) from exc
    if result.returncode != 0:
        raise ServiceUnavailable(error)


def _rank(words: list[dict], duration_ms: int) -> list[dict]:
    candidates = []
    next_start = 0
    for i, word in enumerate(words):
        start = max(0, round(word["start_ms"]) - 1200)
        if start < next_start:
            continue
        next_start = start + 4_000
        end = min(start + 20_000, duration_ms)
        speech = []
        for following in words[i:]:
            if following["end_ms"] > end:
                break
            if following["w"] and following["prob"] >= 0.35:
                speech.append(following)
        if len(speech) < 6:
            continue
        end = min(end, round(speech[-1]["end_ms"]) + 800)
        if end - start < 5_000:
            continue
        transcript = " ".join(w["w"] for w in speech)
        score = min(len(speech), 50) + 7 * len(_HOOK.findall(transcript))
        candidates.append((score, start, end, transcript))
    picks: list[dict] = []
    for _, start, end, transcript in sorted(candidates, key=lambda item: -item[0]):
        if any(start < pick["end_ms"] and end > pick["start_ms"] for pick in picks):
            continue
        picks.append({"start_ms": start, "end_ms": end, "transcript": transcript})
        if len(picks) == 3:
            break
    return picks


def _words(db: Database, settings: Settings, recording: Recording, start_ms: int,
           length_ms: int) -> tuple[list[dict], str | None]:
    """Words of ``[start_ms, start_ms + length_ms)`` from the recording's shared transcript
    (M4), re-based to clip time, and the transcript's id. Nothing here runs Whisper: if the
    current transcript does not exist yet, the TRANSCRIBE job is queued and the caller gets
    ``409 transcript_pending`` to retry shortly."""
    if recording.has_audio is False:
        return [], None
    with db.session() as session:
        tr = transcripts.current(session, settings, recording.id)
        if tr is not None:
            return transcripts.words_for_clip(session, tr.id, start_ms, length_ms), tr.id
    try:
        with db.session() as session:            # committed before we answer 409
            transcripts.request(session, settings, recording.id)
    except PermanentJobError as e:
        raise ServiceUnavailable(str(e), code="whisper_unavailable") from e
    raise ConflictError("The recording's transcript is being produced. Try again shortly.",
                        code="transcript_pending")


def _srt_time(ms: int) -> str:
    seconds, fraction = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02},{fraction:03}"


def _captions(words: list[dict], length_ms: int) -> str:
    cues: list[str] = []
    group: list[str] = []
    start = end = 0
    for word in words:
        text = " ".join(word["w"].split())
        left = max(0, min(length_ms, round(word["start_ms"])))
        right = max(0, min(length_ms, round(word["end_ms"])))
        if not text or right <= left:
            continue
        if group and left > start and (len(group) >= 7 or left - end > 650 or right - start > 2800):
            cues.append(f"{_srt_time(start)} --> {_srt_time(min(end, left))}\n{' '.join(group)}")
            group = []
        if not group:
            start = end = left
        group.append(text)
        end = max(end, right)
    if group:
        cues.append(f"{_srt_time(start)} --> {_srt_time(end)}\n{' '.join(group)}")
    return "".join(f"{index}\n{cue}\n\n" for index, cue in enumerate(cues, 1))


def suggest(db: Database, settings: Settings, recording_id: str) -> list[dict]:
    recording, source, decision = _recording(db, settings, recording_id, "analyze")
    if recording.has_audio is False:
        return []
    words, _ = _words(db, settings, recording, 0, ANALYSIS_SECONDS * 1000)
    picks = _rank(words, min(recording.duration_ms, ANALYSIS_SECONDS * 1000))
    with db.session() as session:
        rights.fence(session, decision)
    return picks


def highlight_frame(db: Database, settings: Settings, recording_id: str, index: int) -> bytes:
    """A sampled frame from the completed candidate, under current analyze rights."""
    _, source, decision = _recording(db, settings, recording_id, "analyze")
    with db.session() as session:
        raw = session.scalar(select(Job.result_json).where(
            Job.recording_id == recording_id, Job.job_type == JobType.HIGHLIGHT_SCAN.value))
    items = json.loads(raw)["items"] if raw else []
    if not 0 <= index < len(items):
        raise NotFoundError("Highlight candidate not found.")
    seek = items[index]["peak_ms"] / 1000
    try:
        result = media.run_command([
            settings.ffmpeg_path, "-nostdin", "-v", "error", "-ss", f"{seek:.3f}",
            "-i", str(source), "-frames:v", "1",
            "-vf", "scale=480:-2", "-c:v", "mjpeg", "-f", "image2pipe", "-",
        ], settings.ffprobe_timeout_seconds)
    except media.MediaError as exc:
        raise ServiceUnavailable("Could not extract the highlight preview.") from exc
    if result.returncode or not result.stdout:
        raise ServiceUnavailable("Could not extract the highlight preview.")
    with db.session() as session:
        rights.fence(session, decision)
    return result.stdout


ClipLayout = Literal["original", "split", "split_facecam"]


def outro_tail(db: Database, settings: Settings, recording: Recording, end_ms: int,
               length: int) -> tuple[int, rights.Decision | None, str]:
    """Source audio after the cut that the outro may use, within the rights scope.

    In ``continue`` mode (and ``outro`` mode with a fade) the outro plays gameplay audio
    from *after* ``end_ms``. That audio is source footage used in the export, so it counts
    toward the rights-checked length: ``edit`` is evaluated again for ``length + tail``. If
    that is refused (for example a licence's ``max_clip_seconds`` is reached), the tail is
    dropped: the gameplay audio ends at the cut and the outro still plays whole. Returns
    ``(tail_ms, decision covering the tail or None, reason)``; the decision is fenced again
    before the export is returned (independent review of PR #14, 2026-11-25)."""
    if recording.has_audio is False:
        return 0, None, "recording_has_no_audio"
    wanted = outro.tail_ms(settings, outro.load(settings),
                           max(0, (recording.duration_ms or end_ms) - end_ms))
    if wanted == 0:
        return 0, None, "no_tail_wanted_or_recording_ends_at_the_cut"
    with db.session() as session:
        decision = rights.evaluate_recording(session, recording.id, "edit",
                                             clip_ms=length + wanted)
    if decision.allowed:
        return wanted, decision, "within_rights_scope"
    return 0, None, f"tail_dropped:{decision.reason}"


def _render_split(settings: Settings, source: Path, folder: Path, output: Path,
                  start_ms: int, length: int, recording: Recording, facecam: bool,
                  title: str, tail: int = 0, words: list[dict] | None = None) -> None:
    """One FFmpeg pass: the 35/65 gameplay segment with its original audio untouched, then
    the whole outro (``services/outro.py``): ``length`` + the outro's own duration.
    ``tail``: milliseconds of source audio after the cut that the outro may use, already
    checked against the rights scope by :func:`outro_tail` (0 = none)."""
    asset = outro.load(settings)
    has_audio = recording.has_audio is not False
    words = list(words or []) if has_audio else []
    header, captions = split_shorts.overlays(folder, words, title, length)
    duration = f"{length / 1000:.3f}"
    end_ms = start_ms + length
    tail = tail if has_audio else 0
    audio = ("[0:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
             if has_audio else "anullsrc=r=48000:cl=stereo,")
    audio += (f"apad=whole_dur={duration},atrim=duration={duration},"
              "asetpts=PTS-STARTPTS[gamea];")
    layout = split_shorts.video_filters(
        facecam, (recording.width or 0) >= (recording.height or 0))
    filters = (
        layout + audio +
        outro.filters(settings, asset, outro_input=3, tail_input=4 if tail else None,
                      tail_length_ms=tail) +
        "[game][gamea][outro][outroa]concat=n=2:v=1:a=1[v][a]"
    )
    tail_args = (["-ss", f"{end_ms / 1000:.3f}", "-t", f"{tail / 1000:.3f}", "-i", str(source)]
                 if tail else [])
    _run([settings.ffmpeg_path, "-nostdin", "-v", "error", "-ss", f"{start_ms / 1000:.3f}",
          "-t", duration, "-i", str(source), "-loop", "1", "-framerate", "30", "-i",
          str(header), "-f", "concat", "-safe", "0", "-i", str(captions), "-i", str(asset.path),
          *tail_args,
          "-filter_complex_threads", "2", "-filter_complex", filters, "-map", "[v]",
          "-map", "[a]", "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
          "-maxrate", "4M", "-bufsize", "8M", "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
          "-movflags", "+faststart", str(output)],
         settings.ffmpeg_timeout_seconds, "Could not render the 35/65 Short.")


def cut(db: Database, settings: Settings, recording_id: str, start_ms: int, end_ms: int,
        *, subtitles: bool = False, layout: ClipLayout = "original", title: str = "") -> bytes:
    length = end_ms - start_ms
    split = layout != "original"
    maximum = 45_000 if split else MAX_CLIP_MS
    if start_ms < 0 or not 0 < length <= maximum:
        raise ValidationFailed(f"Choose a clip up to {maximum // 1000} seconds long.")
    recording, source, decision = _recording(db, settings, recording_id, "edit", length)
    if end_ms > recording.duration_ms:
        raise ValidationFailed("The clip ends past the recording.")
    tail, tail_decision, tail_reason = (outro_tail(db, settings, recording, end_ms, length)
                                        if split else (0, None, "not_a_short"))
    log.info("clip_outro_tail", extra={"recording_id": recording_id, "component": "clips",
                                       "tail_ms": tail, "tail_reason": tail_reason})
    analyze = None
    if subtitles or (split and recording.has_audio is not False):
        if recording.has_audio is False:
            raise ValidationFailed("This recording has no audio to transcribe.")
        _, _, analyze = _recording(db, settings, recording_id, "analyze")
    words: list[dict] = []
    transcript_id = None
    if subtitles or split:
        words, transcript_id = _words(db, settings, recording, start_ms, length)
    log.info("clip_transcript", extra={"recording_id": recording_id, "component": "clips",
                                       "transcript_id": transcript_id, "words": len(words)})
    with _workspace(db, settings) as folder:
        output = folder / "clip.mp4"
        if split:
            _render_split(settings, source, folder, output, start_ms, length, recording,
                          layout == "split_facecam", title, tail, words)
        else:
            subtitle_input: list[str] = []
            subtitle_output: list[str] = []
            if subtitles:
                captions = _captions(words, length)
                if not captions:
                    raise ValidationFailed("No speech was detected in this clip.")
                srt = folder / "clip.srt"
                srt.write_text(captions, encoding="utf-8")
                subtitle_input = ["-i", str(srt)]
                subtitle_output = ["-map", "1:0", "-c:s", "mov_text", "-disposition:s:0", "default"]
            _run([settings.ffmpeg_path, "-nostdin", "-v", "error", "-ss", f"{start_ms / 1000:.3f}",
                  "-i", str(source), *subtitle_input, "-t", f"{length / 1000:.3f}",
                  "-map", "0:v:0", "-map", "0:a:0?", *subtitle_output,
                  "-vf",
                  "scale=1920:1080:force_original_aspect_ratio=decrease:force_divisible_by=2",
                  "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-maxrate", "4M",
                  "-bufsize", "8M", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
                  str(output)],
                 settings.ffmpeg_timeout_seconds, "Could not cut the requested MP4.")
        data = output.read_bytes()
        with db.session() as session:
            rights.fence(session, decision)
            if tail_decision is not None:
                rights.fence(session, tail_decision)
            if analyze is not None:
                rights.fence(session, analyze)
        return data
