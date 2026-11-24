from __future__ import annotations

import math

from riftbench.metrics import align, normalize, score_timing_set, timing, wer


def test_normalize_strips_case_and_punctuation():
    assert normalize("Hello, WORLD! It's fine.") == ["hello", "world", "its", "fine"]


def test_wer_perfect_and_errors():
    assert wer("the car jumps", "The car jumps.")["wer"] == 0
    r = wer("the red car jumps high", "the car jumps very high")
    assert (r["substitutions"], r["deletions"], r["insertions"]) == (0, 1, 1)
    assert math.isclose(r["wer"], 2 / 5)


def test_align_substitution():
    a = align(["a", "b", "c"], ["a", "x", "c"])
    assert a.substitutions == 1 and a.errors == 1


def _words(ws, offset=0.0):
    return [{"w": w, "start_ms": i * 500 + offset, "end_ms": i * 500 + 300 + offset}
            for i, w in enumerate(ws)]


def test_timing_errors_only_on_matched_words():
    ref = _words(["the", "police", "chase", "begins"])
    hyp = _words(["the", "police", "case", "begins"], offset=40)
    t = timing(ref, hyp)
    assert t["matched_words"] == 3
    assert t["start_median_ms"] == 40 and t["start_within_100ms"] == 1.0


def test_block_scoring_matches_direct_for_long_sets():
    ws = [f"w{i % 37}" for i in range(2000)]
    ref = _words(ws)
    hyp = _words(ws, offset=20)
    del hyp[500]
    s = score_timing_set(ref, hyp, block=300)
    assert s["deletions"] == 1 and s["substitutions"] == 0 and s["insertions"] == 0
    assert s["start_median_ms"] == 20 and s["matched_words"] == 1999
