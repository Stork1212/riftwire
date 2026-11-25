"""Accept or reject a model's answer against the evidence it was given.

Everything a model returns is untrusted. An answer is accepted only if it parses, matches
``highlight_eval.v1`` exactly, and every id, quote and boundary is grounded in the bundle.
Times are looked up from ids: the model has no way to state a timestamp. Any error rejects
the whole answer (no partial acceptance), and the error list is precise enough to feed back
in a bounded repair retry.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from pydantic import ValidationError

from app.ai.schemas import EvidenceBundle, HighlightEval

MAX_RESPONSE_CHARS = 60_000
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)
_NON_WORD = re.compile(r"[^\w']+", re.UNICODE)

# Which id prefixes may support each basis.
_BASIS_PREFIXES = {
    "transcript": ("w:",),
    "audio": ("a:",),
    "motion": ("v:",),
    "scene": ("sc:",),
    "observed_in_keyframe": ("kf:",),
    "inference": ("w:", "a:", "v:", "sc:", "kf:"),
}


@dataclass(frozen=True)
class Clip:
    window_id: str
    start_ms: int
    end_ms: int


@dataclass
class Result:
    ok: bool
    value: HighlightEval | None = None
    clips: list[Clip] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _norm(text: str) -> str:
    return " ".join(_NON_WORD.sub(" ", text.lower()).split())


def _parse(raw: str, warnings: list[str]) -> object:
    m = _FENCE.match(raw)
    if m:
        warnings.append("response was wrapped in a code fence")
        raw = m.group(1)
    return json.loads(raw)


def _contains_words(needle: list[str], hay: list[str]) -> bool:
    """Whole-word, contiguous match (``"he"`` is not in ``"the"``)."""
    n = len(needle)
    return any(hay[i:i + n] == needle for i in range(len(hay) - n + 1))


def validate(raw: str, bundle: EvidenceBundle, *, images_shown: bool = False,
             min_clip_ms: int = 8_000, max_clip_ms: int = 60_000) -> Result:
    warnings: list[str] = []
    if len(raw) > MAX_RESPONSE_CHARS:
        return Result(False, errors=[f"response too long ({len(raw)} characters)"])
    try:
        data = _parse(raw, warnings)
    except (json.JSONDecodeError, ValueError) as e:
        return Result(False, errors=[f"invalid JSON: {e}"], warnings=warnings)
    try:
        value = HighlightEval.model_validate(data)
    except ValidationError as e:
        errs = [f"schema: {'.'.join(str(p) for p in x['loc'])}: {x['msg']}"
                for x in e.errors()[:20]]
        return Result(False, errors=errs, warnings=warnings)

    words = {w.id: w for w in bundle.words}
    items: dict[str, tuple[int, int]] = {w.id: (w.t0, w.t1) for w in bundle.words}
    items.update({e.id: (e.t0, e.t1) for e in bundle.events})
    items.update({k.id: (k.t, k.t) for k in bundle.keyframes})
    windows = {w.id: w for w in bundle.windows}
    errors: list[str] = []
    clips: list[Clip] = []

    for wid in value.rejected_window_ids:
        if wid not in windows:
            errors.append(f"rejected_window_ids: unknown window {wid!r}")

    for n, c in enumerate(value.candidates):
        at = f"candidates[{n}]"
        win = windows.get(c.window_id)
        if win is None:
            errors.append(f"{at}: unknown window {c.window_id!r}")
            continue
        if c.window_id in value.rejected_window_ids:
            errors.append(f"{at}: window {c.window_id!r} is both selected and rejected")
        bounds = []
        for name, ref in (("start_id", c.start_id), ("end_id", c.end_id)):
            span = items.get(ref)
            if span is None or ref.startswith("kf:"):
                errors.append(f"{at}.{name}: {ref!r} is not a word, event or scene in the "
                              "evidence")
            elif span[1] < win.t0 or span[0] > win.t1:
                errors.append(f"{at}.{name}: {ref!r} lies outside window {win.id}")
            elif ref.startswith("w:") and (span[0] < win.t0 or span[1] > win.t1):
                # Clamping would cut the word itself: ask for a word fully inside instead.
                errors.append(f"{at}.{name}: word {ref!r} straddles the edge of window "
                              f"{win.id}; choose a word that lies fully inside it")
            else:
                bounds.append(span)
        if len(bounds) == 2:
            # The clip may not leave its window: an event or scene that only overlaps the
            # window is cut at the window edge, so a long event cannot pull footage the
            # window never offered into the clip. Words are never cut (refused above).
            start = max(bounds[0][0], win.t0)
            end = min(bounds[1][1], win.t1)
            if (start, end) != (bounds[0][0], bounds[1][1]):
                warnings.append(f"{at}: clip clamped to window {win.id} "
                                f"({win.t0}-{win.t1} ms)")
            if end <= start:
                errors.append(f"{at}: end_id ends before start_id begins")
            elif not (min_clip_ms <= end - start <= max_clip_ms):
                errors.append(f"{at}: clip length {end - start} ms is outside "
                              f"{min_clip_ms}-{max_clip_ms} ms")
            else:
                clips.append(Clip(c.window_id, start, end))

        for k, ev in enumerate(c.evidence):
            where = f"{at}.evidence[{k}]"
            unknown = [i for i in ev.ids if i not in items]
            if unknown:
                errors.append(f"{where}: unknown ids {unknown[:5]}")
                continue
            outside = [i for i in ev.ids if items[i][1] < win.t0 or items[i][0] > win.t1]
            if outside:
                errors.append(f"{where}: ids {outside[:5]} lie outside window {win.id}; "
                              "evidence must come from the candidate's own window")
            allowed = _BASIS_PREFIXES[ev.basis]
            if not all(i.startswith(allowed) for i in ev.ids):
                errors.append(f"{where}: basis {ev.basis!r} must cite only "
                              f"{'/'.join(allowed)} ids")
            if ev.basis == "observed_in_keyframe" and not images_shown:
                errors.append(f"{where}: no keyframe images were shown to this model")
            if ev.quote is not None:
                cited = [words[i].text for i in ev.ids if i in words]
                if not cited:
                    errors.append(f"{where}: a quote must cite transcript words")
                elif not _norm(ev.quote) or not _contains_words(_norm(ev.quote).split(),
                                                                 _norm(" ".join(cited)).split()):
                    errors.append(f"{where}: quote is not in the cited words")

    for i in range(len(clips)):
        for j in range(i + 1, len(clips)):
            a, b = clips[i], clips[j]
            inter = min(a.end_ms, b.end_ms) - max(a.start_ms, b.start_ms)
            union = max(a.end_ms, b.end_ms) - min(a.start_ms, b.start_ms)
            if inter > 0 and inter / union > 0.5:
                errors.append(f"candidates {a.window_id} and {b.window_id} overlap by more "
                              "than half (duplicate moment)")

    if errors:
        return Result(False, errors=errors, warnings=warnings)
    return Result(True, value=value, clips=clips, warnings=warnings)
