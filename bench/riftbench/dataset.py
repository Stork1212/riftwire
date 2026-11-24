"""Synthetic, reproducible speech datasets with ground truth.

* **timing** sets: each word is synthesized alone, its leading/trailing silence trimmed, and
  words are concatenated with seeded gaps. The exact start/end of every word is therefore
  known (sample-accurate) → word-timing error.
* **natural** sets: whole sentences synthesized with natural prosody → WER (word timings not
  known exactly, so timing is not scored on these).
* Variants: ``clean``, ``noisy`` (pink noise at ~10 dB SNR plus loud bursts, game-like) and
  ``music`` (tonal bed at ~15 dB below speech).

Limitations (stated in the report): synthetic TTS is cleaner and more regular than real
commentary; these sets measure engine accuracy/timing/speed, not highlight usefulness.
Authorized real gameplay is a separate evaluation set.
"""

from __future__ import annotations

import array
import json
import random
from dataclasses import dataclass
from pathlib import Path

from riftbench.audio import (
    RATE,
    duration_ms,
    ffmpeg,
    read_pcm,
    rms,
    silence,
    write_pcm,
)
from riftbench.tts import TTS

CORPUS = Path(__file__).resolve().parents[1] / "corpus" / "en_gameplay.txt"


def load_corpus(path: Path = CORPUS) -> list[str]:
    return [ln.strip() for ln in path.read_text().splitlines()
            if ln.strip() and not ln.startswith("#")]


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    kind: str          # "timing" | "natural"
    minutes: float
    variant: str       # "clean" | "noisy" | "music"
    seed: int = 7


DEFAULT_SETS = (
    DatasetSpec("timing-1m-clean", "timing", 1, "clean"),
    DatasetSpec("timing-10m-clean", "timing", 10, "clean"),
    DatasetSpec("timing-10m-noisy", "timing", 10, "noisy"),
    DatasetSpec("timing-10m-music", "timing", 10, "music"),
    DatasetSpec("timing-60m-clean", "timing", 60, "clean"),
    DatasetSpec("natural-10m-clean", "natural", 10, "clean"),
    DatasetSpec("natural-10m-noisy", "natural", 10, "noisy"),
)
QUICK_SETS = (
    DatasetSpec("timing-1m-clean", "timing", 1, "clean"),
    DatasetSpec("natural-1m-clean", "natural", 1, "clean"),
)


def build(spec: DatasetSpec, tts: TTS, out_dir: Path) -> Path:
    """Build (or reuse) a dataset; returns the directory containing audio.wav + truth.json."""
    d = out_dir / spec.name
    truth_path = d / "truth.json"
    if truth_path.exists() and (d / "audio.wav").exists():
        return d
    d.mkdir(parents=True, exist_ok=True)
    rng = random.Random(f"{spec.name}:{spec.seed}")
    sentences = load_corpus()
    voices = tts.voices()
    target_ms = spec.minutes * 60_000
    pcm = array.array("h")
    words: list[dict] = []
    texts: list[str] = []
    pcm.extend(silence(500))
    while duration_ms(pcm) < target_ms:
        sent = rng.choice(sentences)
        voice = rng.choice(voices)
        texts.append(sent)
        if spec.kind == "timing":
            for i, w in enumerate(sent.split()):
                clip = read_pcm(tts.clip(w, voice, trimmed=True))
                start = duration_ms(pcm)
                pcm.extend(clip)
                words.append({"w": w, "start_ms": round(start, 1),
                              "end_ms": round(duration_ms(pcm), 1), "voice": voice})
                if i < len(sent.split()) - 1:
                    pcm.extend(silence(rng.randint(70, 220)))
        else:
            pcm.extend(read_pcm(tts.clip(sent, voice, trimmed=True)))
        pcm.extend(silence(rng.randint(450, 900)))
    clean = d / "clean.wav"
    write_pcm(clean, pcm)
    audio = d / "audio.wav"
    snr_db = _apply_variant(spec, clean, audio, rng)
    if audio != clean and clean.exists() and spec.variant != "clean":
        clean.unlink()
    truth = {
        "name": spec.name, "kind": spec.kind, "variant": spec.variant, "tts": tts.name,
        "voices": voices, "seed": spec.seed, "duration_ms": round(duration_ms(pcm), 1),
        "text": " ".join(texts), "words": words if spec.kind == "timing" else None,
        "snr_db": snr_db,
    }
    truth_path.write_text(json.dumps(truth))
    return d


def _apply_variant(spec: DatasetSpec, clean: Path, out: Path, rng: random.Random) -> float | None:
    if spec.variant == "clean":
        clean.replace(out)
        return None
    speech_rms = rms(read_pcm(clean))
    dur = duration_ms(read_pcm(clean)) / 1000
    if spec.variant == "noisy":
        noise = clean.with_name("noise.wav")
        ffmpeg("-f", "lavfi", "-i", f"anoisesrc=color=pink:amplitude=0.5:seed={spec.seed}:"
               f"duration={dur:.3f}", "-ac", "1", "-ar", str(RATE), "-sample_fmt", "s16",
               str(noise))
        target = speech_rms / (10 ** (10 / 20))  # ~10 dB SNR
        gain = target / max(1.0, rms(read_pcm(noise)))
        # Loud "explosion" bursts every ~20–40 s (game-like interference).
        bursts, t = [], 5.0
        while t < dur - 1:
            bursts.append(t)
            t += rng.uniform(20, 40)
        expr = "+".join(f"between(t,{b:.2f},{b + 0.6:.2f})" for b in bursts) or "0"
        ffmpeg("-i", str(clean), "-i", str(noise),
               "-filter_complex",
               f"[1]volume={gain:.4f},volume='1+3*({expr})':eval=frame[n];"
               "[0][n]amix=inputs=2:normalize=0:duration=first",
               "-ac", "1", "-ar", str(RATE), "-sample_fmt", "s16", str(out))
        noise.unlink(missing_ok=True)
        return 10.0
    if spec.variant == "music":
        bed = clean.with_name("bed.wav")
        ffmpeg("-f", "lavfi", "-i",
               f"aevalsrc='0.3*sin(2*PI*220*t)+0.2*sin(2*PI*277.18*t)+0.2*sin(2*PI*329.63*t)':"
               f"s={RATE}:d={dur:.3f}", "-ac", "1", "-sample_fmt", "s16", str(bed))
        target = speech_rms / (10 ** (15 / 20))  # ~15 dB below speech
        gain = target / max(1.0, rms(read_pcm(bed)))
        ffmpeg("-i", str(clean), "-i", str(bed), "-filter_complex",
               f"[1]volume={gain:.4f}[m];[0][m]amix=inputs=2:normalize=0:duration=first",
               "-ac", "1", "-ar", str(RATE), "-sample_fmt", "s16", str(out))
        bed.unlink(missing_ok=True)
        return 15.0
    raise ValueError(spec.variant)
