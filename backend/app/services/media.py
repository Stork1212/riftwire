"""FFmpeg / ffprobe integration.

* Arguments are always passed as lists (never a shell); paths given to FFmpeg are
  internal absolute paths, never user-supplied names.
* Every child runs in its own process group so the whole group can be killed on
  timeout, cancellation, or lease loss.
* stderr is captured (bounded) for troubleshooting.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from app.core.logging import get_logger

log = get_logger("media")

MOV_FAMILY = "mov"
MKV_FAMILY = "matroska"
EXT_FAMILY = {"mp4": MOV_FAMILY, "mov": MOV_FAMILY, "mkv": MKV_FAMILY, "webm": MKV_FAMILY}
STDERR_LIMIT = 8000
KILL_GRACE_SECONDS = 5.0


# --------------------------------------------------------------------------- errors

class MediaError(Exception):
    """Base media failure."""

    permanent = False


class InvalidMedia(MediaError):
    """The file is not a supported, decodable video. Never retried."""

    permanent = True


class MediaToolTimeout(MediaError):
    permanent = False


class MediaToolMissing(MediaError):
    permanent = False


class CommandCancelled(MediaError):
    permanent = False


# --------------------------------------------------------------------- process control

@dataclass
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: str


def process_start_signature(pid: int) -> str | None:
    """Start time of ``pid`` as reported by ``ps`` (stable for the process lifetime).

    Used to tell a genuine orphan apart from an unrelated process that reused the PID.
    """
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    sig = " ".join(out.stdout.split())
    return sig or None


def process_command_name(pid: int) -> str | None:
    try:
        out = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    name = out.stdout.strip()
    return os.path.basename(name) if name else None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_process_group(pid: int, grace: float = KILL_GRACE_SECONDS) -> None:
    """SIGTERM the process group led by ``pid``; SIGKILL it after ``grace`` seconds."""
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        # Reap if it is our own child (avoids a zombie keeping the group "alive").
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        time.sleep(0.05)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def kill_orphan_if_matches(pid: int, started_at: str | None,
                           allowed_names: tuple[str, ...] = ("ffmpeg", "ffprobe")) -> str:
    """Kill a recorded child of a dead worker only if it is provably the same process.

    Returns one of: "not_running", "killed", "pid_reused", "kill_failed" (still alive after
    SIGKILL and the grace period — the caller must not treat the job as free).
    """
    if not pid_alive(pid):
        return "not_running"
    sig = process_start_signature(pid)
    name = process_command_name(pid)
    if started_at is None or sig != started_at or name not in allowed_names:
        return "pid_reused"
    kill_process_group(pid)
    deadline = time.monotonic() + KILL_GRACE_SECONDS + 2
    while pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if pid_alive(pid) and process_start_signature(pid) == started_at:
        return "kill_failed"
    return "killed"


def _scrub(text: str, path: Path) -> str:
    """Remove internal filesystem paths from tool output shown to users."""
    return text.replace(str(path), "<input>").replace(str(path.parent), "<dir>")


def run_command(args: list[str], timeout: float, *,
                on_spawn: Callable[[int, str | None], None] | None = None,
                should_cancel: Callable[[], bool] | None = None,
                poll_interval: float = 0.25) -> CommandResult:
    """Run a media tool in its own process group with timeout and cancellation."""
    if not args or not isinstance(args, list):
        raise ValueError("args must be a non-empty list")
    try:
        proc = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError as e:
        raise MediaToolMissing(f"executable not found: {args[0]}") from e

    try:
        if on_spawn:
            on_spawn(proc.pid, process_start_signature(proc.pid))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                kill_process_group(proc.pid)
                proc.communicate()
                raise MediaToolTimeout(f"{os.path.basename(args[0])} timed out after {timeout}s")
            if should_cancel and should_cancel():
                kill_process_group(proc.pid)
                proc.communicate()
                raise CommandCancelled(f"{os.path.basename(args[0])} cancelled")
            try:
                out, err = proc.communicate(timeout=min(poll_interval, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        if proc.poll() is None:
            kill_process_group(proc.pid)
            proc.communicate()
        raise
    stderr = err.decode("utf-8", "replace")[-STDERR_LIMIT:]
    return CommandResult(proc.returncode, out, stderr)


# --------------------------------------------------------------------------- tools

@dataclass
class ToolStatus:
    available: bool
    path: str | None
    version: str | None
    hwaccels: list[str] = field(default_factory=list)


def tool_status(executable: str, timeout: float = 10.0) -> ToolStatus:
    path = shutil.which(executable)
    if not path:
        return ToolStatus(False, None, None)
    try:
        r = run_command([path, "-hide_banner", "-version"], timeout)
    except MediaError:
        return ToolStatus(False, path, None)
    first = r.stdout.decode("utf-8", "replace").splitlines()[:1]
    version = first[0].split(" Copyright")[0].strip() if first else None
    hw: list[str] = []
    if os.path.basename(path).startswith("ffmpeg"):
        try:
            h = run_command([path, "-hide_banner", "-hwaccels"], timeout)
            hw = [ln.strip() for ln in h.stdout.decode().splitlines()[1:] if ln.strip()]
        except MediaError:
            pass
    return ToolStatus(r.returncode == 0, path, version, hw)


# ------------------------------------------------------------------------ metadata

@dataclass
class MediaInfo:
    container_format: str
    family: str
    major_brand: str | None
    duration_ms: int | None
    width: int | None
    height: int | None
    fps_num: int | None
    fps_den: int | None
    is_variable_frame_rate: bool | None
    video_codec: str | None
    audio_codec: str | None
    has_audio: bool


def _fraction(v: str | None) -> Fraction | None:
    if not v:
        return None
    v = v.replace(":", "/")  # ffprobe writes aspect ratios as "4:3"
    if v in ("0/0", "0/1", "N/A"):
        return None
    try:
        f = Fraction(v)
    except (ValueError, ZeroDivisionError):
        return None
    return f if f > 0 else None


def _rotation(stream: dict) -> int:
    rot = 0
    tags = stream.get("tags") or {}
    if "rotate" in tags:
        try:
            rot = int(float(tags["rotate"]))
        except ValueError:
            rot = 0
    for sd in stream.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                rot = int(float(sd["rotation"]))
            except (TypeError, ValueError):
                pass
    return rot % 360


def parse_probe(data: dict) -> MediaInfo:
    """Turn ffprobe JSON into MediaInfo. Raises InvalidMedia if unsupported."""
    fmt = data.get("format") or {}
    format_name = fmt.get("format_name") or ""
    names = set(format_name.split(","))
    if "mov" in names or "mp4" in names:
        family = MOV_FAMILY
    elif "matroska" in names or "webm" in names:
        family = MKV_FAMILY
    else:
        raise InvalidMedia(f"unsupported container: {format_name or 'unknown'}")

    streams = data.get("streams") or []
    video = [s for s in streams if s.get("codec_type") == "video"
             and not (s.get("disposition") or {}).get("attached_pic")]
    if not video:
        raise InvalidMedia("no video stream found")
    v = video[0]
    audio = [s for s in streams if s.get("codec_type") == "audio"]

    width, height = v.get("width"), v.get("height")
    if width and height:
        sar = _fraction(v.get("sample_aspect_ratio"))
        if sar and sar != 1:
            width = int(round(width * sar))
        if _rotation(v) in (90, 270):
            width, height = height, width

    r = _fraction(v.get("r_frame_rate"))
    avg = _fraction(v.get("avg_frame_rate"))
    is_vfr: bool | None = None
    fps: Fraction | None = None
    if r and avg:
        is_vfr = abs(r - avg) / r > Fraction(1, 100)
        fps = avg if is_vfr else r
    else:
        fps = avg or r
    if fps is not None:
        fps = fps.limit_denominator(1_000_000)

    dur = None
    for candidate in (fmt.get("duration"), v.get("duration")):
        try:
            if candidate not in (None, "N/A"):
                dur = int(round(float(candidate) * 1000))
                break
        except ValueError:
            continue

    tags = fmt.get("tags") or {}
    return MediaInfo(
        container_format=format_name,
        family=family,
        major_brand=(tags.get("major_brand") or "").strip() or None,
        duration_ms=dur,
        width=width,
        height=height,
        fps_num=fps.numerator if fps else None,
        fps_den=fps.denominator if fps else None,
        is_variable_frame_rate=is_vfr,
        video_codec=v.get("codec_name"),
        audio_codec=audio[0].get("codec_name") if audio else None,
        has_audio=bool(audio),
    )


def probe(ffprobe: str, path: Path, timeout: float, **kw) -> MediaInfo:
    r = run_command([ffprobe, "-v", "error", "-print_format", "json", "-show_format",
                     "-show_streams", "-i", str(path)], timeout, **kw)
    if r.returncode != 0:
        raise InvalidMedia("ffprobe could not read the file: "
                           + _scrub(r.stderr.strip(), path)[:500])
    try:
        data = json.loads(r.stdout or b"{}")
    except json.JSONDecodeError as e:
        raise InvalidMedia("ffprobe returned unreadable output") from e
    return parse_probe(data)


def _decodes_frame(ffmpeg: str, path: Path, seek: float, timeout: float, **kw) -> tuple[bool, str]:
    args = [ffmpeg, "-v", "error", "-nostdin"]
    if seek > 0:
        args += ["-ss", f"{seek:.3f}"]
    args += ["-i", str(path), "-map", "0:v:0", "-frames:v", "1", "-f", "framecrc", "-"]
    r = run_command(args, timeout, **kw)
    frames = [ln for ln in r.stdout.decode("utf-8", "replace").splitlines()
              if ln and not ln.startswith("#")]
    return (r.returncode == 0 and bool(frames)), _scrub(r.stderr.strip(), path)[:500]


def verify_decodable(ffmpeg: str, path: Path, timeout: float,
                     duration_ms: int | None = None, **kw) -> None:
    """Decode a frame at the start and near the end of the file.

    The end check catches truncated recordings (e.g. a capture that was cut off) whose
    container still claims the full duration. Raises InvalidMedia on failure.
    """
    ok, err = _decodes_frame(ffmpeg, path, 0.0, timeout, **kw)
    if not ok:
        raise InvalidMedia("no decodable video frame at start: " + (err or "empty"))
    if duration_ms and duration_ms > 1500:
        end = max(0.0, duration_ms / 1000 - min(1.0, duration_ms / 10000))
        ok, err = _decodes_frame(ffmpeg, path, end, timeout, **kw)
        if not ok:
            raise InvalidMedia("file appears truncated or corrupt near the end: "
                               + (err or "no frame"))


def choose_extension(info: MediaInfo, original_ext: str) -> str:
    """Pick the stored extension from the *content*, keeping the original if consistent."""
    if EXT_FAMILY.get(original_ext) == info.family:
        if info.family == MOV_FAMILY and original_ext in ("mp4", "mov"):
            return original_ext
        if info.family == MKV_FAMILY:
            return original_ext
    if info.family == MOV_FAMILY:
        return "mov" if (info.major_brand or "").startswith("qt") else "mp4"
    return "mkv"


# ----------------------------------------------------------------------- thumbnails

JPEG_MAGIC = b"\xff\xd8\xff"


def is_valid_jpeg(path: Path) -> bool:
    try:
        if path.stat().st_size < 100:
            return False
        with open(path, "rb") as f:
            return f.read(3) == JPEG_MAGIC
    except OSError:
        return False


def thumbnail_seek_seconds(duration_ms: int | None) -> float:
    if not duration_ms or duration_ms < 2000:
        return 0.0
    return round(min(duration_ms * 0.1, 30_000) / 1000, 3)


def render_thumbnail(ffmpeg: str, src: Path, dst: Path, *, seek: float, width: int,
                     height: int, timeout: float, **kw) -> None:
    """Write one JPEG frame to ``dst``: square pixels, aspect preserved, fits WxH."""
    vf = (f"scale=trunc(iw*sar/2)*2:ih,setsar=1,"
          f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2")
    args = [ffmpeg, "-v", "error", "-nostdin", "-y"]
    if seek > 0:
        args += ["-ss", f"{seek:.3f}"]
    args += ["-i", str(src), "-map", "0:v:0", "-frames:v", "1", "-vf", vf,
             "-c:v", "mjpeg", "-q:v", "3", "-f", "image2", str(dst)]
    r = run_command(args, timeout, **kw)
    if r.returncode != 0 or not is_valid_jpeg(dst):
        raise MediaError("thumbnail extraction failed: " + _scrub(r.stderr.strip(), src)[:500])
