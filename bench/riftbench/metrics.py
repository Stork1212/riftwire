"""Accuracy and timing metrics (stdlib only, deterministic).

* WER via word-level Levenshtein alignment on normalized text.
* Word timing: hypothesis words aligned to reference words; for every *correctly recognized*
  aligned pair, |start error| and |end error| in ms.
"""

from __future__ import annotations

import re
import statistics
import unicodedata
from dataclasses import dataclass

_PUNCT = re.compile(r"[^\w\s]")


def normalize(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text).lower().replace("’", "'")
    text = text.replace("'", "")
    return [w for w in _PUNCT.sub(" ", text).split() if w]


def norm_word(w: str) -> str:
    n = normalize(w)
    return n[0] if n else ""


@dataclass
class Alignment:
    pairs: list[tuple[int | None, int | None]]  # (ref_idx, hyp_idx); None = ins/del
    substitutions: int
    deletions: int
    insertions: int

    @property
    def errors(self) -> int:
        return self.substitutions + self.deletions + self.insertions


def align(ref: list[str], hyp: list[str]) -> Alignment:
    """Levenshtein alignment with backtrace (O(n·m) memory-light via row storage of ops)."""
    n, m = len(ref), len(hyp)
    # dp rows of (cost); ops stored as compact bytes per row for backtrace
    prev = list(range(m + 1))
    ops = [bytearray(b"I" * (m + 1))]
    ops[0][0] = ord("S")
    for i in range(1, n + 1):
        cur = [i] + [0] * m
        row = bytearray(m + 1)
        row[0] = ord("D")
        ri = ref[i - 1]
        for j in range(1, m + 1):
            if ri == hyp[j - 1]:
                best, op = prev[j - 1], ord("M")
            else:
                best, op = prev[j - 1] + 1, ord("S")
            if prev[j] + 1 < best:
                best, op = prev[j] + 1, ord("D")
            if cur[j - 1] + 1 < best:
                best, op = cur[j - 1] + 1, ord("I")
            cur[j], row[j] = best, op
        ops.append(row)
        prev = cur
    pairs: list[tuple[int | None, int | None]] = []
    i, j, s, d, ins = n, m, 0, 0, 0
    while i > 0 or j > 0:
        op = chr(ops[i][j]) if i > 0 or j > 0 else "M"
        if i > 0 and j > 0 and op in "MS":
            pairs.append((i - 1, j - 1))
            if op == "S":
                s += 1
            i, j = i - 1, j - 1
        elif i > 0 and (j == 0 or op == "D"):
            pairs.append((i - 1, None))
            d += 1
            i -= 1
        else:
            pairs.append((None, j - 1))
            ins += 1
            j -= 1
    pairs.reverse()
    return Alignment(pairs, s, d, ins)


def wer(ref_text: str, hyp_text: str) -> dict:
    ref, hyp = normalize(ref_text), normalize(hyp_text)
    a = align(ref, hyp)
    return {"wer": a.errors / max(1, len(ref)), "ref_words": len(ref), "hyp_words": len(hyp),
            "substitutions": a.substitutions, "deletions": a.deletions,
            "insertions": a.insertions}


def _pct(xs: list[float], q: float) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def _bias(s_signed: list[float], e_signed: list[float]) -> dict:
    """Signed errors (hyp − ref): negative = early. Shows systematic offsets that a
    constant correction or padding could remove, which absolute errors hide."""
    return {
        "start_bias_median_ms": statistics.median(s_signed) if s_signed else float("nan"),
        "end_bias_median_ms": statistics.median(e_signed) if e_signed else float("nan"),
        "start_late_over_100ms": sum(x > 100 for x in s_signed) / len(s_signed)
        if s_signed else float("nan"),
        "end_early_over_100ms": sum(x < -100 for x in e_signed) / len(e_signed)
        if e_signed else float("nan"),
    }


def timing(ref_words: list[dict], hyp_words: list[dict]) -> dict:
    """ref_words: [{w,start_ms,end_ms}]; hyp_words: [{w,start_ms,end_ms}]."""
    ref = [norm_word(r["w"]) for r in ref_words]
    hyp_norm = [norm_word(h["w"]) for h in hyp_words]
    keep = [i for i, w in enumerate(hyp_norm) if w]
    hyp = [hyp_norm[i] for i in keep]
    a = align(ref, hyp)
    starts, ends, s_signed, e_signed = [], [], [], []
    for ri, hj in a.pairs:
        if ri is None or hj is None or ref[ri] != hyp[hj]:
            continue
        r, h = ref_words[ri], hyp_words[keep[hj]]
        s_signed.append(h["start_ms"] - r["start_ms"])
        e_signed.append(h["end_ms"] - r["end_ms"])
        starts.append(abs(s_signed[-1]))
        ends.append(abs(e_signed[-1]))
    n = len(starts)
    return {
        "matched_words": n, "ref_words": len(ref),
        "start_median_ms": statistics.median(starts) if starts else float("nan"),
        "start_p95_ms": _pct(starts, 0.95),
        "end_median_ms": statistics.median(ends) if ends else float("nan"),
        "end_p95_ms": _pct(ends, 0.95),
        "start_within_100ms": sum(x <= 100 for x in starts) / n if n else float("nan"),
        "start_within_250ms": sum(x <= 250 for x in starts) / n if n else float("nan"),
        **_bias(s_signed, e_signed),
    }


def score_timing_set(ref_words: list[dict], hyp_words: list[dict], block: int = 400) -> dict:
    """WER + timing for long timed sets, aligned block-wise.

    Reference words are split into blocks of ``block`` words; each hypothesis word is
    assigned to the block whose time span contains its start (boundaries at the first
    reference word of each block). Errors and timing are summed over blocks. This keeps
    alignment O(n·block) instead of O(n²) for 60-minute sets; boundary effects are bounded
    by a few words per block and identical across engines.
    """
    if len(ref_words) <= block * 2:
        t = timing(ref_words, hyp_words)
        w = wer(" ".join(r["w"] for r in ref_words), " ".join(h["w"] for h in hyp_words))
        return {**w, **t}
    bounds = [ref_words[i]["start_ms"] for i in range(0, len(ref_words), block)]
    ref_blocks = [ref_words[i:i + block] for i in range(0, len(ref_words), block)]
    hyp_blocks: list[list[dict]] = [[] for _ in ref_blocks]
    k = 0
    for h in hyp_words:
        while k + 1 < len(bounds) and h["start_ms"] >= bounds[k + 1]:
            k += 1
        hyp_blocks[k].append(h)
    tot = {"substitutions": 0, "deletions": 0, "insertions": 0, "ref_words": 0,
           "hyp_words": 0}
    starts: list[float] = []
    ends: list[float] = []
    s_signed: list[float] = []
    e_signed: list[float] = []
    for rb, hb in zip(ref_blocks, hyp_blocks, strict=True):
        w = wer(" ".join(r["w"] for r in rb), " ".join(h["w"] for h in hb))
        for key in tot:
            tot[key] += w[key]
        ref = [norm_word(r["w"]) for r in rb]
        keep = [i for i, h in enumerate(hb) if norm_word(h["w"])]
        hyp = [norm_word(hb[i]["w"]) for i in keep]
        for ri, hj in align(ref, hyp).pairs:
            if ri is not None and hj is not None and ref[ri] == hyp[hj]:
                s_signed.append(hb[keep[hj]]["start_ms"] - rb[ri]["start_ms"])
                e_signed.append(hb[keep[hj]]["end_ms"] - rb[ri]["end_ms"])
                starts.append(abs(s_signed[-1]))
                ends.append(abs(e_signed[-1]))
    n = len(starts)
    errors = tot["substitutions"] + tot["deletions"] + tot["insertions"]
    return {
        "wer": errors / max(1, tot["ref_words"]), **tot, "matched_words": n,
        "start_median_ms": statistics.median(starts) if starts else float("nan"),
        "start_p95_ms": _pct(starts, 0.95),
        "end_median_ms": statistics.median(ends) if ends else float("nan"),
        "end_p95_ms": _pct(ends, 0.95),
        "start_within_100ms": sum(x <= 100 for x in starts) / n if n else float("nan"),
        "start_within_250ms": sum(x <= 250 for x in starts) / n if n else float("nan"),
        **_bias(s_signed, e_signed),
        "aligned_in_blocks_of": block,
    }
