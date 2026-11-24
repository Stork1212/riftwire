"""Local speech-based clip suggestions and on-demand, rights-gated MP4 cuts."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from uuid import uuid4

from app.core.config import REPO_ROOT, Settings
from app.core.errors import ConflictError, NotFoundError, ServiceUnavailable, ValidationFailed
from app.db.session import Database
from app.models import Recording, RecordingStatus
from app.services import media, reservations, rights
from app.services.locations import get_layout, require_no_maintenance

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


def _transcribe(settings: Settings, source: Path, folder: Path, start_ms: int,
                length_ms: int) -> list[dict]:
    python, model = settings.clip_whisper_python, settings.clip_whisper_model
    if not python or not python.is_file() or not model or not (model / "weights.npz").is_file():
        raise ServiceUnavailable("Local Whisper is unavailable. Configure the benchmark model.")
    audio, transcript = folder / "audio.wav", folder / "words.json"
    _run([settings.ffmpeg_path, "-nostdin", "-v", "error", "-ss", f"{start_ms / 1000:.3f}",
          "-i", str(source), "-t", f"{length_ms / 1000:.3f}", "-vn", "-ac", "1", "-ar",
          "16000", str(audio)],
         settings.ffmpeg_timeout_seconds, "Could not extract the recording's audio.")
    _run([str(python), str(REPO_ROOT / "bench/riftbench/transcribe_child.py"),
          str(model), str(audio), str(transcript)], _TIMEOUT,
         "Local Whisper could not transcribe the recording.")
    return json.loads(transcript.read_text())["words"]


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
    with _workspace(db, settings) as folder:
        words = _transcribe(settings, source, folder, 0, ANALYSIS_SECONDS * 1000)
        picks = _rank(words, min(recording.duration_ms, ANALYSIS_SECONDS * 1000))
        with db.session() as session:
            rights.fence(session, decision)
        return picks


def cut(db: Database, settings: Settings, recording_id: str, start_ms: int, end_ms: int,
        *, subtitles: bool = False) -> bytes:
    length = end_ms - start_ms
    if start_ms < 0 or not 0 < length <= MAX_CLIP_MS:
        raise ValidationFailed("Choose a clip between 0 and 30 seconds long.")
    recording, source, decision = _recording(db, settings, recording_id, "edit", length)
    if end_ms > recording.duration_ms:
        raise ValidationFailed("The clip ends past the recording.")
    analyze = None
    if subtitles:
        if recording.has_audio is False:
            raise ValidationFailed("This recording has no audio to transcribe.")
        _, _, analyze = _recording(db, settings, recording_id, "analyze")
    with _workspace(db, settings) as folder:
        subtitle_input: list[str] = []
        subtitle_output: list[str] = []
        if subtitles:
            words = _transcribe(settings, source, folder, start_ms, length)
            captions = _captions(words, length)
            if not captions:
                raise ValidationFailed("No speech was detected in this clip.")
            srt = folder / "clip.srt"
            srt.write_text(captions, encoding="utf-8")
            subtitle_input = ["-i", str(srt)]
            subtitle_output = ["-map", "1:0", "-c:s", "mov_text", "-disposition:s:0", "default"]
        output = folder / "clip.mp4"
        _run([settings.ffmpeg_path, "-nostdin", "-v", "error", "-ss", f"{start_ms / 1000:.3f}",
              "-i", str(source), *subtitle_input, "-t", f"{length / 1000:.3f}",
              "-map", "0:v:0", "-map", "0:a:0?", *subtitle_output,
              "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease:force_divisible_by=2",
              "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-maxrate", "4M",
              "-bufsize", "8M", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
              str(output)],
             settings.ffmpeg_timeout_seconds, "Could not cut the requested MP4.")
        data = output.read_bytes()
        with db.session() as session:
            rights.fence(session, decision)
            if analyze is not None:
                rights.fence(session, analyze)
        return data
