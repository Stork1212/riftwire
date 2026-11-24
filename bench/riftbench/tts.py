"""Text-to-speech backends for synthetic speech.

* ``SayTTS``: macOS built-in ``say`` (no download, no rights issues). Clips are cached per
  (voice, text) so a 60-minute set needs only one synthesis per unique word.
* ``ToneTTS``: deterministic tone "speech" for tests on non-macOS machines (not intelligible;
  used only to validate dataset construction, never for accuracy numbers).
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

from riftbench.audio import ffmpeg, to_wav16k


class TTS:
    name = "base"

    def __init__(self, cache: Path):
        self.cache = cache
        self.cache.mkdir(parents=True, exist_ok=True)

    def _key(self, voice: str, text: str, trimmed: bool) -> Path:
        h = hashlib.sha1(f"{self.name}|{voice}|{text}|{trimmed}".encode()).hexdigest()[:16]
        return self.cache / f"{h}.wav"

    def clip(self, text: str, voice: str, trimmed: bool) -> Path:
        out = self._key(voice, text, trimmed)
        if not out.exists():
            tmp = out.with_suffix(".tmp.wav")
            self._synth(text, voice, tmp, trimmed)
            tmp.replace(out)
        return out

    def _synth(self, text: str, voice: str, out: Path, trimmed: bool) -> None:
        raise NotImplementedError

    def voices(self) -> list[str]:
        return ["default"]


class SayTTS(TTS):
    name = "say"
    PREFERRED = ("Samantha", "Daniel", "Karen", "Alex", "Moira", "Tessa", "Fred")

    @staticmethod
    def available() -> bool:
        return shutil.which("say") is not None

    def voices(self) -> list[str]:
        out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True,
                             timeout=30).stdout
        en = [ln.split()[0] for ln in out.splitlines() if " en_" in ln or " en-" in ln]
        picked = [v for v in self.PREFERRED if v in en]
        return picked[:3] or en[:3] or ["default"]

    def _synth(self, text: str, voice: str, out: Path, trimmed: bool) -> None:
        aiff = out.with_suffix(".aiff")
        args = ["say", "-o", str(aiff)]
        if voice != "default":
            args += ["-v", voice]
        subprocess.run([*args, text], check=True, timeout=120)
        to_wav16k(aiff, out, trim_silence=trimmed)
        aiff.unlink(missing_ok=True)


class ToneTTS(TTS):
    name = "tone"

    def _synth(self, text: str, voice: str, out: Path, trimmed: bool) -> None:
        # ~70 ms per character, distinct pitch per word; deterministic.
        dur = max(0.12, 0.07 * len(text))
        freq = 200 + (int(hashlib.sha1(text.encode()).hexdigest(), 16) % 600)
        ffmpeg("-f", "lavfi", "-i", f"sine=frequency={freq}:duration={dur:.3f}",
               "-ac", "1", "-ar", "16000", "-sample_fmt", "s16", str(out))
