"""M6 stages B, C and F: candidate windows, context, and clip-boundary refinement.

Everything here is deterministic and works from stored analysis only.

**B. Seeds.** Moments worth showing the model, each carrying the ids that caused it:
* the baseline scan's ranked windows (sustained motion plus reliable speech cues);
* measured audio events: onsets and inferred loud bursts, when reliable speech is nearby
  (a loud moment with someone reacting to it);
* speech bursts: the busiest 5 s of reliable speech;
* scene-cut bursts (several cuts in a short time).

**C. Context.** Each seed becomes a window from ``pre_ms`` before to ``post_ms`` after
it. The start snaps back to the start of the transcript segment it falls in, and the end
forward to the end of its segment, so a sentence is never cut in half. Windows stay within
the clip limits; overlapping windows merge (up to the maximum length); the best-supported
windows are kept, up to ``max_windows``.

**F. Boundaries.** After the model picks a start word and an end word, the clip is
refined without inventing anything: it never starts or ends inside a word; a reaction that
continues straight after the chosen end (no pause of ``reaction_gap_ms``) is kept, for at
most ``reaction_max_ms`` (continuous commentary is not a reaction); a short
pre-roll and post-roll is added only where there is no other speech; the clip stays within
the limits. Every change is recorded.
"""

from __future__ import annotations

import re
from bisect import bisect_left
from dataclasses import dataclass, field

from app.services import highlight_scan

MIN_CLIP_MS, MAX_CLIP_MS = 8_000, 60_000
CONFIG = {
    "version": 1, "pre_ms": 12_000, "post_ms": 18_000, "min_window_ms": 20_000,
    "max_window_ms": 60_000, "speech_burst_ms": 5_000, "reaction_gap_ms": 700,
    "pre_roll_ms": 300, "post_roll_ms": 600, "nearby_speech_ms": 4_000,
    "reaction_max_ms": 5_000,
    "dead_gap_ms": 4_000,
}


@dataclass
class Seed:
    at_ms: int
    weight: float
    source: str
    ids: list[str] = field(default_factory=list)


@dataclass
class PlannedWindow:
    id: str
    start_ms: int
    end_ms: int
    reason: str
    sources: list[str] = field(default_factory=list)
    trigger_ids: list[str] = field(default_factory=list)
    weight: float = 0.0


def seeds(*, visual_series: dict, trigger_words: list[dict], audio_events: list[dict],
          visual_events: list[dict], duration_ms: int, transcript_id: str | None,
          audio_run_id: str | None, visual_run_id: str | None) -> list[Seed]:
    out: list[Seed] = []
    scan = highlight_scan.scan_signals(visual_series, trigger_words, duration_ms)
    for c in scan["items"]:
        out.append(Seed((c["start_ms"] + c["end_ms"]) // 2, float(c["score"]), "baseline_scan",
                        [f"cue:{x}" for x in c.get("speech_cues", [])]))
    starts = [w["start_ms"] for w in trigger_words]
    near = CONFIG["nearby_speech_ms"]

    def speech_near(t: int) -> list[dict]:
        i = bisect_left(starts, t - near)
        return [w for w in trigger_words[i:] if w["start_ms"] <= t + near]

    for i, e in enumerate(audio_events):
        if e["type"] not in ("onset", "loud_burst"):
            continue
        ws = speech_near(e["start_ms"])
        if not ws:
            continue
        ids = [f"a:{audio_run_id}:{i}"] + [f"w:{transcript_id}:{w['idx']}" for w in ws[:3]]
        out.append(Seed(e["start_ms"], 1.5 if e["type"] == "loud_burst" else 1.0,
                        f"audio_{e['type']}", ids))
    burst = CONFIG["speech_burst_ms"]
    if trigger_words:
        best, best_n, j = None, 0, 0
        for i, w in enumerate(trigger_words):
            while trigger_words[j]["start_ms"] < w["start_ms"] - burst:
                j += 1
            if i - j + 1 > best_n:
                best, best_n = (j, i), i - j + 1
        if best and best_n >= 8:
            a, b = best
            mid = (trigger_words[a]["start_ms"] + trigger_words[b]["end_ms"]) // 2
            out.append(Seed(mid, 1.0 + best_n / 20, "speech_burst",
                            [f"w:{transcript_id}:{trigger_words[a]['idx']}",
                             f"w:{transcript_id}:{trigger_words[b]['idx']}"]))
    for i, e in enumerate(visual_events):
        if e["type"] == "cut_burst":
            out.append(Seed(e["start_ms"], 1.0, "cut_burst", [f"v:{visual_run_id}:{i}"]))
    return out


def _overlap_of_shorter(x: tuple[int, int], y: tuple[int, int]) -> float:
    inter = min(x[1], y[1]) - max(x[0], y[0])
    shorter = min(x[1] - x[0], y[1] - y[0])
    return inter / shorter if inter > 0 and shorter > 0 else 0.0


def _union(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for a, b in sorted(s for s in spans if s[1] > s[0]):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _length(spans: list[tuple[int, int]]) -> int:
    return sum(b - a for a, b in spans)


def _intersect(x: list[tuple[int, int]], y: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out, i, j = [], 0, 0
    while i < len(x) and j < len(y):
        a, b = max(x[i][0], y[j][0]), min(x[i][1], y[j][1])
        if b > a:
            out.append((a, b))
        if x[i][1] < y[j][1]:
            i += 1
        else:
            j += 1
    return out


def _snap(t: int, segments: list[dict], *, start: bool) -> int:
    for s in segments:
        if s["start_ms"] <= t < s["end_ms"]:
            return s["start_ms"] if start else s["end_ms"]
    return t


def plan(seed_list: list[Seed], segments: list[dict], duration_ms: int,
         max_windows: int, max_window_ms: int | None = None) -> list[PlannedWindow]:
    """Context, merge and selection (stage C). Deterministic for the same inputs.
    ``max_window_ms`` is the target format's clip limit (a Short: 45 s): the model can
    only pick start and end ids inside a window, so no clip it picks can exceed it."""
    cfg = dict(CONFIG)
    if max_window_ms:
        cfg["max_window_ms"] = min(cfg["max_window_ms"], max_window_ms)
        cfg["min_window_ms"] = min(cfg["min_window_ms"], cfg["max_window_ms"])
    raw: list[PlannedWindow] = []
    for s in seed_list:
        a = max(0, _snap(max(0, s.at_ms - cfg["pre_ms"]), segments, start=True))
        b = min(duration_ms, _snap(min(duration_ms, s.at_ms + cfg["post_ms"]), segments,
                                   start=False))
        if b - a < cfg["min_window_ms"]:                     # widen around the seed
            pad = (cfg["min_window_ms"] - (b - a)) // 2
            a, b = max(0, a - pad), min(duration_ms, b + pad)
        if b - a > cfg["max_window_ms"]:            # trim around the seed, keeping it
            m = cfg["max_window_ms"]
            a = max(a, min(s.at_ms - m // 2, b - m))
            b = min(duration_ms, a + m)
            a = max(0, b - m)
        if not a <= s.at_ms <= b:
            continue                                  # never plan a window without its seed
        if b - a < MIN_CLIP_MS:
            continue
        raw.append(PlannedWindow("", a, b, "", [s.source], list(s.ids), s.weight))
    raw.sort(key=lambda w: (w.start_ms, w.end_ms))
    merged: list[PlannedWindow] = []
    for w in raw:
        last = merged[-1] if merged else None
        if last and w.start_ms < last.end_ms and \
                max(w.end_ms, last.end_ms) - last.start_ms <= cfg["max_window_ms"]:
            last.end_ms = max(last.end_ms, w.end_ms)
            last.sources = sorted(set(last.sources) | set(w.sources))
            last.trigger_ids = list(dict.fromkeys(last.trigger_ids + w.trigger_ids))
            last.weight += w.weight
        else:
            merged.append(w)
    best: list[PlannedWindow] = []
    for w in sorted(merged, key=lambda w: (-w.weight, w.start_ms)):
        if len(best) >= max_windows:
            break
        if any(_overlap_of_shorter((w.start_ms, w.end_ms), (o.start_ms, o.end_ms)) > 0.5
               for o in best):
            continue                                  # near-duplicate of a stronger window
        best.append(w)
    best.sort(key=lambda w: w.start_ms)
    for n, w in enumerate(best, 1):
        w.id = f"c:{n}"
        w.reason = (f"seeds: {', '.join(w.sources)}; support {w.weight:.2f}"
                    + (f"; ids {', '.join(w.trigger_ids[:6])}" if w.trigger_ids else ""))
    return best


def _split_dead_air(a: int, b: int, ws: list[dict], silences: list[list[int]],
                    cited: list[tuple[int, int]], gap_ms: int) -> tuple[int, int, str | None]:
    """If the clip contains a long gap with no speech that the audio measured as silence,
    keep the side holding most of the model's cited evidence (ties: the earlier side)."""
    inside = [w for w in ws if a <= w["start_ms"] and w["end_ms"] <= b]
    for x, y in zip(inside, inside[1:], strict=False):
        gap = (x["end_ms"], y["start_ms"])
        if gap[1] - gap[0] <= gap_ms:
            continue
        covered = sum(max(0, min(gap[1], e) - max(gap[0], s)) for s, e in silences)
        if covered < gap_ms:                    # the audio must confirm a long silence
            continue
        before = sum(1 for s, e in cited if (s + e) / 2 <= gap[0])
        after = sum(1 for s, e in cited if (s + e) / 2 >= gap[1])
        if before == after:                       # no side is better supported: keep both
            continue
        if before > after:
            return a, gap[0], (f"cut {gap[1] - gap[0]} ms of measured silence and what followed "
                               f"it ({before} of {before + after} cited items lie before it)")
        return gap[1], b, (f"cut what preceded {gap[1] - gap[0]} ms of measured silence "
                           f"({after} of {before + after} cited items lie after it)")
    return a, b, None


def refine(start_ms: int, end_ms: int, words: list[dict], duration_ms: int, *,
           max_ms: int = MAX_CLIP_MS, silences: list[list[int]] | None = None,
           cited: list[tuple[int, int]] | None = None) -> dict:
    """Stage F: refined clip bounds and the list of changes (nothing invented).
    ``max_ms`` is the target format's limit; ``silences`` and ``cited`` (the time spans of
    the model's evidence) allow cutting a long dead-air stretch away from the evidence."""
    cfg = CONFIG
    notes: list[str] = []
    ws = sorted(words, key=lambda w: w["start_ms"])
    a, b = start_ms, end_ms
    if silences is not None:
        na, nb, note = _split_dead_air(a, b, ws, silences, cited or [], cfg["dead_gap_ms"])
        if note and nb - na >= MIN_CLIP_MS:
            a, b = na, nb
            notes.append(note)
    for w in ws:                                    # never inside a word
        if w["start_ms"] < a < w["end_ms"]:
            a = w["start_ms"]
            notes.append("start moved to the start of a word it cut")
        if w["start_ms"] < b < w["end_ms"]:
            b = w["end_ms"]
            notes.append("end moved to the end of a word it cut")
    after = [w for w in ws if w["start_ms"] >= b]
    tail = b
    capped = False
    for w in after:                                 # keep a reaction that runs straight on
        if w["start_ms"] - tail > cfg["reaction_gap_ms"] or w["end_ms"] - a > max_ms:
            break
        if w["end_ms"] - b > cfg["reaction_max_ms"]:  # continuous talk: not a reaction
            capped = True
            break
        tail = w["end_ms"]
    if tail > b:
        notes.append(f"end extended {tail - b} ms to keep speech that continues without "
                     "a pause" + (f" (capped at {cfg['reaction_max_ms'] // 1000} s; "
                                  "the speech goes on)" if capped else ""))
        b = tail
    prev_end = max((w["end_ms"] for w in ws if w["end_ms"] <= a), default=0)
    next_start = min((w["start_ms"] for w in ws if w["start_ms"] >= b), default=duration_ms)
    pre = min(cfg["pre_roll_ms"], max(0, a - prev_end))
    post = min(cfg["post_roll_ms"], max(0, next_start - b), duration_ms - b)
    if b + post - (a - pre) > max_ms:
        pre, post = 0, 0
    if b + post - (a - pre) <= max_ms:
        a, b = a - pre, b + post
        if pre or post:
            notes.append(f"pre-roll {pre} ms and post-roll {post} ms with no transcribed "
                         "speech")
    if b - a < MIN_CLIP_MS or b - a > max_ms:    # nothing above is applied: say only that
        why = "too short" if b - a < MIN_CLIP_MS else f"over the {max_ms // 1000} s limit"
        a, b = start_ms, end_ms
        notes = [f"kept as chosen: refinement would make it {why}"]
    a, b = max(0, a), min(duration_ms, b)
    outside = [c for c in (cited or []) if c[1] <= a or c[0] >= b]
    if outside:
        notes.append(f"{len(outside)} cited item(s) lie outside the final clip")
    return {"start_ms": a, "end_ms": b, "notes": notes, "cited_outside": len(outside)}


# ------------------------------------------------------------------- editorial checks
PENALTIES = {"dead_air": 10, "unclear_boundaries": 5, "not_standalone": 5,
             "weak_support": 5}
DEAD_AIR_FRACTION = 0.4
# Factual claims a title or hook must not make unless the clip's own words say so. Both
# sides are normalised to canonical tokens first (lower case, punctuation and hyphens as
# spaces, "grand theft auto" -> "gta", "vi"/"six" -> "6", plural "s" and leak* folded), and
# a claim counts as supported only when its token sequence occurs in the clip's words.
CLAIM_PATTERNS = [re.compile(p) for p in (
    r"\bgta 6\b", r"\bleak\b", r"\brelease date\b", r"\btrailer\b", r"\brockstar\b",
    r"\bconfirm\w*\b", r"\bofficial\w*\b", r"\bworld record\b", r"\bfirst ever\b",
    r"\bnever seen before\b", r"\bsecret ending\b", r"\beaster egg\b",
)]


def _canonical(text: str) -> str:
    t = re.sub(r"[^a-z0-9]+", " ", text.lower())
    t = re.sub(r"\bgrand theft auto\b", "gta", t)
    t = re.sub(r"\bgta(6|vi|six)\b", r"gta \1", t)
    toks = []
    for tok in t.split():
        if tok in ("vi", "six"):
            tok = "6"
        elif tok.startswith("leak"):
            tok = "leak"
        elif len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
            tok = tok[:-1]
        toks.append(tok)
    return " ".join(toks)


def penalties(*, start_ms: int, end_ms: int, candidate: dict, words: list[dict],
              silences: list[list[int]], word_reliability: dict[str, dict]) -> list[dict]:
    """Deterministic penalties with their measured reasons (subtracted from the editorial
    score; recorded with the candidate)."""
    out = []
    span = max(1, end_ms - start_ms)
    clip = [(start_ms, end_ms)]
    spoken = _intersect(_union([(w["start_ms"], w["end_ms"]) for w in words]), clip)
    silent = _intersect(_union([(int(s), int(e)) for s, e in silences]), clip)
    quiet = _length(silent) - _length(_intersect(silent, spoken))
    if quiet / span > DEAD_AIR_FRACTION:
        out.append({"name": "dead_air", "points": PENALTIES["dead_air"],
                    "reason": f"{quiet / span:.0%} of the clip is measured silence without "
                              "speech"})
    if not candidate.get("clear_start_and_end", True):
        out.append({"name": "unclear_boundaries", "points": PENALTIES["unclear_boundaries"],
                    "reason": "the model reported no clear start and end"})
    if not candidate.get("standalone", True):
        out.append({"name": "not_standalone", "points": PENALTIES["not_standalone"],
                    "reason": "the model reported it needs outside context"})
    cited = [i for ref in candidate.get("evidence", []) if ref.get("basis") == "transcript"
             for i in ref.get("ids", []) if i.startswith("w:")]
    if cited and all(word_reliability.get(i, {}).get("needs_corroboration") for i in cited):
        out.append({"name": "weak_support", "points": PENALTIES["weak_support"],
                    "reason": "every transcript word it cites is low-confidence"})
    return out


def ground_text(text: str, clip_words: list[dict]) -> tuple[bool, list[str]]:
    """Whether a title/hook makes a factual claim the clip's words do not contain."""
    said = " " + _canonical(" ".join(w["w"] if "w" in w else w.get("text", "")
                                     for w in clip_words)) + " "
    claim = _canonical(text)
    bad = []
    for pattern in CLAIM_PATTERNS:
        for m in pattern.finditer(claim):
            if f" {m.group(0)} " not in said and m.group(0) not in bad:
                bad.append(m.group(0))
    return not bad, bad
