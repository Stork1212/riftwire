"""M4 evidence reliability: which transcript words downstream consumers may rely on.

The raw transcript is never changed. This layer reads the raw words and the measured audio
signal run and attaches a reliability assessment to each word, with every reason spelled
out. Consumers decide what to do with it:

* **Highlights:** a word that is not ``can_trigger`` must not create or label a highlight
  on its own.
* **Claims:** a word that is not ``can_support`` must not be cited as evidence for a
  factual claim (M6 grounding).
* **Captions:** may suppress words whose ``caption`` is ``"suppress"``, and record why.

**Classes (one per word; ``reasons`` lists every rule that applied):**

* ``suspected_hallucination``: the word lies (mostly) inside a span the audio run measured
  as silence **and** the audio under it does not rise above the local noise floor. Two
  independent measurements contradict the word. It is suppressed in captions by default
  and can neither trigger nor support.
* ``degenerate_timing``: the engine gave the word (almost) no duration, so its time
  cannot be trusted. It can neither trigger nor support; captions keep it only if asked.
* ``low_confidence``: the engine's own probability is below the profile's
  ``low_prob``. **That threshold is inherited from the pre-M4 scan and is not validated**
  (``validated: false``). It therefore never suppresses anything: the word stays in
  captions and may support a claim *together with* corroborating evidence, but it cannot
  trigger a highlight on its own.
* ``reliable``: none of the above.
* ``unassessed_audio``: no audio measurement exists (no run yet, or no audio track), so
  hallucination cannot be ruled out. The word is kept but cannot trigger on its own.

**Why relative energy, not absolute loudness:** genuine quiet commentary can sit below the
audio run's silence level (-50 dBFS) while still being well above the room's own floor,
and background music is loud everywhere. Comparing the word's peak level with the local
floor (a low percentile of the surrounding windows) separates "nothing was there" from
"something quiet was there". It does not decide whether a word over music is right; that
stays ``low_confidence`` when the engine itself is unsure.
"""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_left, bisect_right

PROFILE = {
    "name": "reliability.default", "version": 1,
    "silence_coverage": 0.8,      # share of the word inside measured silence
    "min_snr_db": 6.0,            # word peak above the local floor that counts as "sound"
    "floor_window_ms": 10_000,    # +/- window for the local floor
    "floor_percentile": 10,
    "degenerate_ms": 20,
    "low_prob": 0.5,              # inherited from the pre-M4 scan filter
    "low_prob_validated": False,
}
CLASSES = ("reliable", "low_confidence", "unassessed_audio", "degenerate_timing",
           "suspected_hallucination")


def profile_fingerprint(profile: dict = PROFILE) -> str:
    return hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()


class _Audio:
    """Fast lookups over a stored audio series (``signals.analyse_audio`` output)."""

    def __init__(self, series: dict, profile: dict):
        self.t = list(series.get("t_ms", []))
        self.step = int(series.get("step_ms", 100))
        self.rms = list(series.get("rms_db", []))
        self.silences = [tuple(s) for s in series.get("silences_ms", [])]
        self.p = profile

    def silence_fraction(self, a: int, b: int) -> float:
        span = max(1, b - a)
        covered = sum(max(0, min(b, e) - max(a, s)) for s, e in self.silences)
        if b == a:                                          # zero-length: the point itself
            return 1.0 if any(s <= a < e for s, e in self.silences) else 0.0
        return min(1.0, covered / span)

    def _range(self, a: int, b: int) -> list[float]:
        lo = max(0, bisect_right(self.t, a) - 1)
        hi = max(lo + 1, bisect_left(self.t, b))
        return self.rms[lo:hi]

    def peak(self, a: int, b: int) -> float | None:
        vals = self._range(a, max(b, a + 1))
        return max(vals) if vals else None

    def floor(self, a: int, b: int) -> float | None:
        w = self.p["floor_window_ms"]
        vals = sorted(self._range(a - w, b + w))
        if not vals:
            return None
        k = max(0, min(len(vals) - 1, int(len(vals) * self.p["floor_percentile"] / 100)))
        return vals[k]


def assess_words(words: list[dict], audio_series: dict | None, *,
                 has_audio: bool | None = True, profile: dict = PROFILE) -> list[dict]:
    """One assessment per word (same order). ``words`` items need ``idx``, ``start_ms``,
    ``end_ms`` and ``prob`` (raw values; this function copies nothing back)."""
    audio = _Audio(audio_series, profile) if audio_series else None
    out = []
    for w in words:
        a, b = int(w["start_ms"]), int(w["end_ms"])
        prob = w.get("prob")
        reasons: list[str] = []
        measured: dict = {"prob": prob, "duration_ms": b - a}
        if b - a < profile["degenerate_ms"]:
            reasons.append(f"degenerate_timing: duration {b - a} ms < "
                           f"{profile['degenerate_ms']} ms")
        if audio is None:
            reasons.append("unassessed_audio: " + ("recording has no audio track"
                                                   if has_audio is False
                                                   else "audio signals not measured"))
        else:
            frac = audio.silence_fraction(a, b)
            peak, floor = audio.peak(a, b), audio.floor(a, b)
            snr = None if peak is None or floor is None else round(peak - floor, 1)
            measured.update({"silence_fraction": round(frac, 2), "peak_rms_db": peak,
                             "floor_rms_db": floor, "snr_db": snr})
            if frac >= profile["silence_coverage"] and (snr is None
                                                        or snr < profile["min_snr_db"]):
                reasons.append(
                    f"suspected_hallucination: {frac:.0%} of the word lies in measured "
                    f"silence and its level is {snr if snr is not None else 'n/a'} dB above "
                    f"the local floor (< {profile['min_snr_db']} dB)")
        if prob is None or prob < profile["low_prob"]:
            reasons.append(f"low_confidence: engine probability "
                           f"{'unknown' if prob is None else round(prob, 3)} < "
                           f"{profile['low_prob']} (threshold not validated)")
        kinds = [r.split(":", 1)[0] for r in reasons]
        cls = next((c for c in reversed(CLASSES) if c in kinds), "reliable")
        out.append({
            "idx": w.get("idx"), "class": cls, "reasons": reasons, "measured": measured,
            "can_trigger": cls == "reliable",
            "can_support": cls in ("reliable", "low_confidence", "unassessed_audio"),
            "needs_corroboration": cls in ("low_confidence", "unassessed_audio"),
            "caption": "suppress" if cls == "suspected_hallucination" else "show",
        })
    return out


def summary(assessments: list[dict]) -> dict:
    counts = {c: 0 for c in CLASSES}
    for a in assessments:
        counts[a["class"]] += 1
    return {"profile": PROFILE["name"], "profile_version": PROFILE["version"],
            "profile_fingerprint": profile_fingerprint(), "counts": counts}


def for_recording(session, recording, words: list[dict]) -> list[dict]:
    """Assess ``words`` of a recording against its current audio signal run (if any)."""
    from app.models import SignalKind
    from app.services import signals

    run = signals.current(session, recording.id, SignalKind.AUDIO) \
        if recording.has_audio is not False else None
    return assess_words(words, signals.series(run) if run else None,
                        has_audio=recording.has_audio)


def caption_words(assessments: list[dict], words: list[dict], *, suppress: bool = True
                  ) -> tuple[list[dict], list[dict]]:
    """Split words into those to caption and those suppressed (with reasons)."""
    keep, dropped = [], []
    for w, a in zip(words, assessments, strict=True):
        if suppress and a["caption"] == "suppress":
            dropped.append({"idx": w.get("idx"), "w": w.get("w"), "start_ms": w["start_ms"],
                            "reasons": a["reasons"]})
        else:
            keep.append(w)
    return keep, dropped
