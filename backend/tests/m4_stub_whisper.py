"""Deterministic stand-in for the Whisper child, for M4 tests.

    python m4_stub_whisper.py MODEL_DIR AUDIO OUT_JSON

Same command line and output shape as ``bench/riftbench/transcribe_child.py``
(``{"words": [{"w", "start_ms", "end_ms", "prob"}], "text", ...}``), but it *listens*: it
decodes the tone-coded words written by :func:`tests.m4_fixtures.tone_word_audio` from the
audio file it is given (FFmpeg → 16 kHz PCM, amplitude segmentation, zero-crossing frequency;
pure Python). Times
are relative to the start of that file, as Whisper's are, so callers must add the chunk
offset themselves. A word cut by the file edge is reported with the part that is present
and a lower ``prob``, like an edge word from a real model.

Every invocation is appended to ``$RIFTWIRE_STUB_WHISPER_LOG`` (if set), so tests can count
Whisper runs. ``$RIFTWIRE_STUB_WHISPER_FAIL_AFTER=N`` makes the N+1th call in that log exit
with an error, to simulate a crash mid-transcription.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

RATE = 16_000
FRAME = 160                      # 10 ms
BASE_HZ, STEP_HZ, MAX_WORDS = 600.0, 40.0, 60
THRESHOLD = 0.05


def _pcm(audio: str) -> list[float]:
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", audio, "-ac", "1", "-ar",
                          str(RATE), "-f", "s16le", "-"], capture_output=True, check=True,
                         timeout=120).stdout
    return [int.from_bytes(raw[i:i + 2], "little", signed=True) / 32768
            for i in range(0, len(raw) - 1, 2)]


def _frequency(seg: list[float]) -> float | None:
    """Tone frequency from linearly interpolated zero crossings: exact enough to separate
    40 Hz steps even for a 30 ms fragment cut by a chunk edge, and fast in pure Python."""
    times = []
    for i in range(1, len(seg)):
        a, b = seg[i - 1], seg[i]
        if a < 0 <= b or a >= 0 > b:
            times.append(i - 1 + (a / (a - b) if a != b else 0.0))
    if len(times) < 3:
        return None
    return (len(times) - 1) / 2 / ((times[-1] - times[0]) / RATE)


def decode(samples: list[float]) -> list[dict]:
    active = [max(abs(x) for x in samples[i:i + FRAME]) >= THRESHOLD
              for i in range(0, len(samples) - FRAME + 1, FRAME)]
    total = len(active)
    words, i = [], 0
    while i < total:
        if not active[i]:
            i += 1
            continue
        j = i
        while j < total and active[j]:
            j += 1
        if j - i >= 3:                                         # >= 30 ms of tone
            # Trim the ramps (2 frames each side when present) before measuring.
            lo, hi = (i + 2 if i > 0 else i), (j - 2 if j < total else j)
            hz = _frequency(samples[lo * FRAME:max(hi, lo + 3) * FRAME])
            if hz is not None:
                k = round((hz - BASE_HZ) / STEP_HZ)
                if 0 <= k < MAX_WORDS:
                    edge = i == 0 or j == total
                    words.append({"w": f"w{k:02d}", "start_ms": i * 10.0, "end_ms": j * 10.0,
                                  "prob": 0.5 if edge else 0.95})
        i = j
    return words


def main(model_dir: str, audio: str, out_json: str) -> int:
    log = os.environ.get("RIFTWIRE_STUB_WHISPER_LOG")
    calls = 0
    if log:
        if os.path.exists(log):
            with open(log, encoding="utf-8") as f:
                calls = sum(1 for _ in f)
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"model": model_dir, "audio": audio}) + "\n")
    fail_after = os.environ.get("RIFTWIRE_STUB_WHISPER_FAIL_AFTER")
    if fail_after is not None and calls >= int(fail_after):
        print("stub whisper: simulated crash", file=sys.stderr)
        return 70
    words = decode(_pcm(audio))
    tmp = out_json + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"words": words, "text": " ".join(w["w"] for w in words),
                   "engine": "stub-tone-decoder", "mlx_peak_bytes": None}, f)
    os.replace(tmp, out_json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:4]))
