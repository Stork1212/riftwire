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
    """16 kHz mono WAV with one tone burst per word (gentle 20 ms ramps, no clicks)."""
    terms = []
    for w in words:
        a, b = w.start_ms / 1000, w.end_ms / 1000
        env = (f"between(t,{a:.3f},{b:.3f})*min(1,min((t-{a:.3f})/0.02,({b:.3f}-t)/0.02))")
        terms.append(f"{env}*0.5*sin(2*PI*{w.hz:.1f}*t)")
    expr = "+".join(terms) or "0"
    subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"aevalsrc='{expr}':s={RATE}:d={duration_s:.3f}", "-ac", "1",
                    str(path)], check=True, timeout=120)
    path.with_suffix(".truth.json").write_text(json.dumps(
        [{"w": w.text, "start_ms": w.start_ms, "end_ms": w.end_ms} for w in words]))
    return path


def marker_clip(path: Path, markers_s: list[float], duration_s: float, *, vfr: bool = False,
                fps: int = 30, ffmpeg: str = "ffmpeg") -> Path:
    """Black video with a single white frame and a 100 ms 1 kHz beep at each marker.

    ``vfr=True`` drops two of every three frames between 30 % and 60 % of the clip and
    writes it with variable frame rate, so frame index x fps no longer gives the time."""
    flash = "+".join(f"between(t,{m:.3f},{m + 1 / fps - 1e-3:.3f})" for m in markers_s) or "0"
    beep = "+".join(f"between(t,{m:.3f},{m + 0.1:.3f})" for m in markers_s) or "0"
    vf = f"geq=lum='if({flash.replace('(t,', '(T,')},235,16)':cb=128:cr=128"   # geq: T is time
    if vfr:
        a, b = duration_s * 0.3, duration_s * 0.6
        keep = f"not(between(t,{a:.3f},{b:.3f}))+not(mod(n,3))+({flash})"
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
