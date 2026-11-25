"""The Short's closing outro: probed from the asset, validated, never truncated.

The approved RumbleFrame outro is 75 frames at 30 fps (2.5 s). Two approved variants
exist: PR #6 carries a quiet AAC sting and asks to keep the gameplay audio under the
bumper; the v1 launch kit (PR #11) is silent with no audio track and asks for appended
silence. Both are supported:

* **Duration** comes from the asset itself (counted video frames ÷ frame rate). An optional
  brand setting (``clip_outro_duration_seconds``) is checked against it and refuses a
  mismatch instead of trimming.
* **Audio during the outro** (``clip_outro_audio``):
  ``continue`` keeps the gameplay audio running under the outro (fading out over its last
  ``clip_outro_fade_ms``), with the outro's own audio mixed beneath at
  ``clip_outro_sting_gain_db``; ``outro`` ends the gameplay audio at the cut (optionally
  with a ``clip_outro_fade_ms`` tail fading into the outro) and plays the outro's audio, or
  silence when it has none.
* The gameplay segment's audio is never mixed, limited or faded: only the outro portion is
  built from the tail, the sting and a peak limiter (−1 dBFS), then concatenated.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from app.core.config import Settings
from app.core.errors import ServiceUnavailable
from app.services import media

OUTPUT_FPS = 30
MAX_SECONDS = 10
LIMIT = 0.891                      # −1 dBFS peak for the outro portion's mix


@dataclass(frozen=True)
class OutroAsset:
    path: Path
    sha256: str
    frames: int                    # video frames counted in the asset
    fps: Fraction
    width: int
    height: int
    has_audio: bool

    @property
    def duration(self) -> Fraction:
        return Fraction(self.frames) / self.fps

    @property
    def output_frames(self) -> int:
        return round(self.duration * OUTPUT_FPS)


_cache: dict[tuple[str, int, int], OutroAsset] = {}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while data := f.read(1024 * 1024):
            h.update(data)
    return h.hexdigest()


def load(settings: Settings) -> OutroAsset:
    """Probe and validate the configured outro (cached by path, size and mtime)."""
    path = settings.clip_outro_path
    if path is None or not path.is_file():
        raise ServiceUnavailable("Set RIFTWIRE_CLIP_OUTRO_PATH to the vertical outro MP4.",
                                 code="outro_missing")
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    asset = _cache.get(key)
    if asset is None:
        asset = _probe(settings, path)
        _cache.clear()
        _cache[key] = asset
    configured = settings.clip_outro_duration_seconds
    if configured is not None and \
            abs(asset.duration - Fraction(str(configured))) > Fraction(1, 2) / asset.fps:
        raise ServiceUnavailable(
            f"The outro is {float(asset.duration):.3f} s ({asset.frames} frames) but the "
            f"brand setting says {configured} s. Fix the setting or the asset; the outro is "
            "never trimmed.", code="outro_duration_mismatch")
    return asset


def _probe(settings: Settings, path: Path) -> OutroAsset:
    try:
        r = media.run_command([settings.ffprobe_path, "-v", "error", "-count_packets",
                               "-show_entries",
                               "stream=codec_type,width,height,r_frame_rate,avg_frame_rate,"
                               "nb_read_packets", "-of", "json", "-i", str(path)],
                              settings.ffprobe_timeout_seconds)
    except media.MediaError as exc:
        raise ServiceUnavailable("The outro could not be read.", code="outro_invalid") from exc
    if r.returncode != 0:
        raise ServiceUnavailable("The outro could not be read.", code="outro_invalid")
    try:
        streams = json.loads(r.stdout or b"{}").get("streams") or []
    except json.JSONDecodeError as exc:
        raise ServiceUnavailable("The outro could not be read.", code="outro_invalid") from exc
    video = [s for s in streams if s.get("codec_type") == "video"]
    if len(video) != 1:
        raise ServiceUnavailable("The outro must have exactly one video stream.",
                                 code="outro_invalid")
    v = video[0]
    fps = media._fraction(v.get("avg_frame_rate")) or media._fraction(v.get("r_frame_rate"))
    try:
        frames = int(v.get("nb_read_packets") or 0)
    except ValueError:
        frames = 0
    width, height = int(v.get("width") or 0), int(v.get("height") or 0)
    if fps is None or frames <= 0 or not width or not height:
        raise ServiceUnavailable("The outro's frames or frame rate could not be read.",
                                 code="outro_invalid")
    asset = OutroAsset(path=path, sha256=_sha256(path), frames=frames,
                       fps=fps.limit_denominator(1000), width=width, height=height,
                       has_audio=any(s.get("codec_type") == "audio" for s in streams))
    if asset.duration > MAX_SECONDS:
        raise ServiceUnavailable(f"The outro is longer than {MAX_SECONDS} s.",
                                 code="outro_invalid")
    return asset


def tail_ms(settings: Settings, asset: OutroAsset, remaining_ms: int) -> int:
    """How much gameplay audio after the cut the outro uses (0 when none is wanted or
    the recording ends at the cut)."""
    duration_ms = int(asset.duration * 1000)
    want = (duration_ms if settings.clip_outro_audio == "continue"
            else min(settings.clip_outro_fade_ms, duration_ms))
    return max(0, min(want, remaining_ms))


def filters(settings: Settings, asset: OutroAsset, *, outro_input: int,
            tail_input: int | None, tail_length_ms: int) -> str:
    """``[outro]`` (video) and ``[outroa]`` (audio) for the concat, exactly the asset's
    duration. ``tail_input``: the source opened at the cut, for the gameplay tail."""
    d = asset.duration
    dur = f"{float(d):.6f}"
    stereo = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
    out = (
        f"[{outro_input}:v]fps={OUTPUT_FPS},"
        "scale=1080:1920:force_original_aspect_ratio=decrease,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuv420p,"
        f"trim=end_frame={asset.output_frames},setpts=PTS-STARTPTS[outro];"
    )
    gain = f"volume={settings.clip_outro_sting_gain_db}dB,"
    if asset.has_audio:
        out += f"[{outro_input}:a]{stereo},{gain}asetpts=PTS-STARTPTS,"
    else:
        out += "anullsrc=r=48000:cl=stereo,"
    out += f"apad=whole_dur={dur},atrim=duration={dur},asetpts=PTS-STARTPTS[sting];"
    limiter = f"alimiter=limit={LIMIT}:level=0:latency=1,"
    if tail_input is not None and tail_length_ms > 0:
        tail = tail_length_ms / 1000
        fade_s = min(settings.clip_outro_fade_ms / 1000, tail)
        fade = ""
        if fade_s > 0:
            fade = f"afade=t=out:st={max(0.0, tail - fade_s):.6f}:d={fade_s:.6f},"
        out += (f"[{tail_input}:a]{stereo},asetpts=PTS-STARTPTS,atrim=duration={tail:.6f},"
                f"{fade}apad=whole_dur={dur},atrim=duration={dur}[tail];"
                "[tail][sting]amix=inputs=2:duration=first:normalize=0,")
    else:
        out += "[sting]"
    out += f"{limiter}atrim=duration={dur},asetpts=PTS-STARTPTS[outroa];"
    return out
