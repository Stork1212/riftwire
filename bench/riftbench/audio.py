"""Minimal 16 kHz mono PCM16 WAV helpers (stdlib only) plus FFmpeg wrappers."""

from __future__ import annotations

import array
import math
import subprocess
import wave
from pathlib import Path

RATE = 16000


def ffmpeg(*args: str, timeout: float = 600) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", *args], check=True,
                   timeout=timeout)


def to_wav16k(src: Path, dst: Path, trim_silence: bool = False) -> None:
    """Convert to 16 kHz mono s16; optionally trim leading/trailing silence only."""
    af = []
    if trim_silence:
        trim = "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0"
        af = ["-af", f"{trim},areverse,{trim},areverse"]
    ffmpeg("-i", str(src), *af, "-ac", "1", "-ar", str(RATE), "-sample_fmt", "s16", str(dst))


def read_pcm(path: Path) -> array.array:
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != RATE:
            raise ValueError(f"{path}: expected 16 kHz mono s16")
        a = array.array("h")
        a.frombytes(w.readframes(w.getnframes()))
        return a


def write_pcm(path: Path, samples: array.array) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(samples.tobytes())


def silence(ms: int) -> array.array:
    return array.array("h", bytes(2 * int(RATE * ms / 1000)))


def duration_ms(samples: array.array) -> float:
    return len(samples) * 1000.0 / RATE


def rms(samples: array.array) -> float:
    if not samples:
        return 0.0
    step = max(1, len(samples) // 200_000)  # sample for speed on long files
    sub = samples[::step]
    return math.sqrt(sum(x * x for x in sub) / len(sub))
