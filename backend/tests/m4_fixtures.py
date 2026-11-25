"""Synthetic media for M4 tests (prepared ahead of M4; no M4 code depends on it yet).

* :func:`tone_word_audio` writes speech-like audio where every "word" is a short tone burst
  whose frequency encodes its index. :mod:`tests.m4_stub_whisper` decodes it from the audio
  itself (pure Python), so a chunked transcription test exercises real audio
  extraction, chunk offsets and boundary words, deterministically and without MLX.
* :func:`marker_clip` writes a video with white flash frames and 1 kHz beeps at known times,
  optionally with **variable frame rate**, for checking signal timestamps against footage.

Everything is generated with FFmpeg (``sine``, ``color``, ``aevalsrc``); no real media.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

RATE = 16_000
BASE_HZ = 600.0
STEP_HZ = 40.0          # word k is a tone at BASE_HZ + k * STEP_HZ (k < 60 → below 3 kHz)
MAX_WORDS = 60          # shared with tests/m4_stub_whisper.py (the decoder imports these)
AMPLITUDE = 0.5
RAMP_S = 0.02
WORD_S = 0.30
GAP_S = 0.12


@dataclass(frozen=True)
class ToneWord:
    index: int
    start_ms: int
    end_ms: int

    @property
    def hz(self) -> float:
        return BASE_HZ + self.index * STEP_HZ

    @property
    def text(self) -> str:
        return f"w{self.index:02d}"


def plan_words(n: int, start_s: float = 0.5, pause_every: int = 0,
               pause_s: float = 1.0) -> list[ToneWord]:
    words, t = [], start_s
    for k in range(n):
        if pause_every and k and k % pause_every == 0:
            t += pause_s
        words.append(ToneWord(k % 60, round(t * 1000), round((t + WORD_S) * 1000)))
        t += WORD_S + GAP_S
    return words


def tone_word_audio(path: Path, words: list[ToneWord], duration_s: float,
                    ffmpeg: str = "ffmpeg") -> Path:
    """16 kHz mono 16-bit WAV with one tone burst per word (20 ms ramps, no clicks).

    Written directly with :mod:`wave`: cost is linear in the audio length (an FFmpeg
    expression with one term per word is words x samples and timed out past a few
    minutes). Deterministic; written to a temporary file and renamed, so an interrupted
    run never leaves a partial WAV. ``ffmpeg`` is accepted for compatibility."""
    import math
    import os
    import struct
    import wave

    n = int(round(duration_s * RATE))
    pcm = [0] * n
    ramp = RAMP_S * RATE
    for w in words:
        a, b = int(round(w.start_ms * RATE / 1000)), min(n, int(round(w.end_ms * RATE / 1000)))
        step = 2 * math.pi * w.hz / RATE
        for i in range(a, b):
            env = min(1.0, (i - a) / ramp, (b - i) / ramp)
            pcm[i] = int(round(32767 * AMPLITUDE * env * math.sin(step * i)))
    tmp = path.with_name(path.name + ".partial")
    with wave.open(str(tmp), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(RATE)
        f.writeframes(struct.pack(f"<{n}h", *pcm))
    os.replace(tmp, path)
    path.with_suffix(".truth.json").write_text(json.dumps(
        [{"w": w.text, "start_ms": w.start_ms, "end_ms": w.end_ms} for w in words]))
    return path


def marker_clip(path: Path, markers_s: list[float], duration_s: float, *, vfr: bool = False,
                fps: int = 30, ffmpeg: str = "ffmpeg") -> Path:
    """Black video with a single white frame and a 100 ms 1 kHz beep at each marker.

    Each marker is snapped to the nearest frame (``round(m * fps)``): that frame is white
    and the beep starts at that frame's time, so flashes and beeps always agree and an
    off-grid marker can neither vanish nor move early.

    ``vfr=True`` drops two of every three frames between 30 % and 60 % of the clip and
    writes it with variable frame rate, so frame index x fps no longer gives the time."""
    frames = sorted({int(round(m * fps)) for m in markers_s})
    flash = "+".join(f"eq(N,{k})" for k in frames) or "0"
    keep_flash = "+".join(f"eq(n,{k})" for k in frames) or "0"
    beep = "+".join(f"between(t,{k / fps:.6f},{k / fps + 0.1:.6f})" for k in frames) or "0"
    vf = f"geq=lum='if({flash},235,16)':cb=128:cr=128"          # geq: N is the frame index
    if vfr:
        a, b = duration_s * 0.3, duration_s * 0.6
        keep = f"not(between(t,{a:.3f},{b:.3f}))+not(mod(n,3))+({keep_flash})"
        vf += f",select='{keep}'"
    args = [ffmpeg, "-nostdin", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"color=c=black:s=320x180:r={fps}:d={duration_s:.3f}",
            "-f", "lavfi", "-i", f"aevalsrc='({beep})*0.6*sin(2*PI*1000*t)':s=48000:"
                                 f"d={duration_s:.3f}",
            "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest"]
    if vfr:
        args += ["-fps_mode", "vfr"]
    subprocess.run([*args, str(path)], check=True, timeout=180)
    return path
