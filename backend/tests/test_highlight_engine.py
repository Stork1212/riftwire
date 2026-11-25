"""M6: candidate windows, boundary refinement, penalties, grounded titles, and the whole
workflow from an authorized recording to a rendered 35/65 Short with captions and outro."""

from __future__ import annotations

import json
import subprocess

import pytest

from app.services import ai_highlights, clips, highlight_candidates
from tests.conftest import requires_ffmpeg
from tests.test_ai_highlights import Scripted, provider, recording  # noqa: F401  (fixtures)
from tests.test_ingest import drain
from tests.test_transcripts import engine  # noqa: F401  (fixture)


# ------------------------------------------------------------------- B/C: windows
def _seed(at, w=1.0, src="baseline_scan", ids=()):
    return highlight_candidates.Seed(at, w, src, list(ids))


def test_windows_merge_overlaps_snap_to_segments_and_keep_the_best():
    segs = [{"start_ms": 0, "end_ms": 9_000}, {"start_ms": 9_500, "end_ms": 31_000},
            {"start_ms": 95_000, "end_ms": 99_000}]
    plan = highlight_candidates.plan(
        [_seed(20_000, 2.0, ids=["cue:reaction"]), _seed(24_000, 1.0, "audio_onset", ["a:r:1"]),
         _seed(100_000, 0.5, "cut_burst", ["v:r:2"]), _seed(200_000, 3.0, "speech_burst")],
        segs, 240_000, max_windows=2)
    assert [w.id for w in plan] == ["c:1", "c:2"]
    first = plan[0]
    assert first.start_ms == 0 and first.end_ms <= 60_000      # snapped to a segment start
    assert set(first.sources) == {"baseline_scan", "audio_onset"}       # merged
    assert "a:r:1" in first.trigger_ids and "cue:reaction" in first.trigger_ids
    assert plan[1].start_ms >= 188_000                         # the strongest remaining seed
    assert all(20_000 <= w.end_ms - w.start_ms <= 60_000 for w in plan)
    again = highlight_candidates.plan(
        [_seed(20_000, 2.0, ids=["cue:reaction"]), _seed(24_000, 1.0, "audio_onset", ["a:r:1"]),
         _seed(100_000, 0.5, "cut_burst", ["v:r:2"]), _seed(200_000, 3.0, "speech_burst")],
        segs, 240_000, max_windows=2)
    assert [(w.start_ms, w.end_ms) for w in again] == [(w.start_ms, w.end_ms) for w in plan]


# ----------------------------------------------------------------------- F: bounds
def _w(text, a, b):
    return {"w": text, "start_ms": a, "end_ms": b}


def test_refine_never_cuts_words_keeps_the_reaction_and_pads_only_in_silence():
    words = [_w("before", 1_000, 1_900), _w("Watch", 2_000, 2_400), _w("this", 2_400, 2_800),
             _w("NO", 12_000, 12_300), _w("WAY", 12_350, 12_800), _w("later", 20_000, 20_400)]
    r = highlight_candidates.refine(2_100, 12_100, words, 60_000)
    assert r["start_ms"] <= 2_000 and r["start_ms"] >= 1_900          # no word cut, padded
    assert r["end_ms"] >= 12_800 and r["end_ms"] <= 13_400            # kept "WAY"
    assert any("continues without a pause" in n for n in r["notes"])
    assert not any(w["start_ms"] < r["start_ms"] < w["end_ms"] or
                   w["start_ms"] < r["end_ms"] < w["end_ms"] for w in words)


def test_refine_does_not_grow_past_the_maximum():
    words = [_w(f"x{i}", i * 400, i * 400 + 350) for i in range(200)]
    r = highlight_candidates.refine(0, 59_900, words, 100_000)
    assert r["end_ms"] - r["start_ms"] <= 60_000


# ------------------------------------------------------------------ E: penalties
def test_penalties_are_measured_and_explained():
    cand = {"clear_start_and_end": False, "standalone": True,
            "evidence": [{"basis": "transcript", "ids": ["w:t:1"]}]}
    p = highlight_candidates.penalties(
        start_ms=0, end_ms=20_000, candidate=cand, words=[_w("hi", 1_000, 1_500)],
        silences=[[2_000, 15_000]], word_reliability={"w:t:1": {"needs_corroboration": True}})
    assert {x["name"] for x in p} == {"dead_air", "unclear_boundaries", "weak_support"}
    assert all(x["points"] > 0 and x["reason"] for x in p)


@pytest.mark.parametrize(("text", "spoken", "ok"), [
    ("GTA 6 leak at the docks!", "we found the docks", False),
    ("Police chase ends in the harbor", "police chase", True),
    ("The trailer was right", "did you see the trailer", True),
    ("Confirmed: secret ending", "", False),
])
def test_titles_cannot_claim_what_the_clip_does_not_say(text, spoken, ok):
    words = [{"w": x} for x in spoken.split()]
    assert highlight_candidates.ground_text(text, words)[0] is ok


# ---------------------------------------------------------- the complete workflow
class TitleClaims(Scripted):
    """Like Scripted, but the first window's title claims a GTA 6 leak (not in footage)."""

    def generate(self, request):
        r = super().generate(request)
        data = json.loads(r.text)
        for c in data.get("candidates", []):
            if c["window_id"] == "c:1":
                c["title"] = "GTA 6 leak caught on stream"
        return type(r)(text=json.dumps(data))


@requires_ffmpeg
def test_recording_to_rendered_short(db, settings, recording, provider, tmp_path):  # noqa: F811
    outro = tmp_path / "outro.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=0xFF00FF:s=1080x1920:r=30:d=2.5", "-f", "lavfi", "-i",
                    "anullsrc=r=48000:cl=stereo:d=2.5", "-c:v", "libx264", "-preset",
                    "ultrafast", "-pix_fmt", "yuv420p", "-frames:v", "75", "-c:a", "aac",
                    "-shortest", str(outro)], check=True, timeout=120)
    settings.clip_outro_path = outro
    provider["p"] = TitleClaims(scores=(88, 72, 66))
    with db.session() as s:
        ai_highlights.request(s, settings, recording.id, game="gta", channel="rumbleframe",
                              target_count=5)
    drain(db, settings, max_jobs=60)
    with db.session() as s:
        st = ai_highlights.describe(s, recording.id)
    assert st["request"]["status"] == "COMPLETE"
    hl = st["highlights"]
    assert 1 <= len(hl) <= 5
    assert st["request"]["summary"]["returned"] == len(hl)
    if len(hl) < 5:
        assert st["request"]["summary"]["fewer_than_requested"] is True
    for h in hl:
        assert h["title"] and "GTA 6" not in h["title"] and h["hook"] is not None
        assert h["window"]["reason"].startswith("seeds:")
        assert h["boundary_notes"] is not None and h["model_clip"]
    dropped = [h for h in hl + st["rejected"] if h["text_checks"]]
    assert dropped and "unsupported claim" in dropped[0]["text_checks"][0]
    top = hl[0]
    data = clips.cut(db, settings, recording.id, top["start_ms"], top["end_ms"],
                     layout="split", title=top["title"])
    out = tmp_path / "short.mp4"
    out.write_bytes(data)
    probe = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height:"
         "format=duration", "-of", "json", str(out)], check=True, capture_output=True,
        timeout=60).stdout)
    video = next(x for x in probe["streams"] if x["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1080, 1920)
    expected = (top["end_ms"] - top["start_ms"]) / 1000 + 2.5
    assert abs(float(probe["format"]["duration"]) - expected) < 0.25
    words, tr_id = clips._words(db, settings, recording, top["start_ms"],
                                top["end_ms"] - top["start_ms"])
    assert words and tr_id == top["transcript_id"]           # captions = the stored words


# ------------------------------------------- regressions from the native run at a22c2c7
def _native_words():
    """macOS speech placed in m6_native.py: reactions at 122-136 s, then 27 s of
    silence, then "Mission complete. Thanks for watching." at 165 s."""
    words = []
    for start, n in ((122_000, 11), (130_000, 9), (165_000, 5)):
        for i in range(n):
            words.append(_w(f"w{start}_{i}", start + i * 380, start + i * 380 + 330))
    return words


def test_native_regression_dead_air_is_cut_away_from_the_cited_evidence():
    # The model chose 121.62-165.78 s (44.2 s); refinement then grew it to 46.5 s, over
    # the 45 s Short limit, and 77 % of it was measured silence.
    words = _native_words()
    silences = [[138_000, 165_000]]
    cited = [(122_000, 122_330), (130_000, 133_400)]
    r = highlight_candidates.refine(121_620, 165_780, words, 180_000, max_ms=45_000,
                                    silences=silences, cited=cited)
    assert r["end_ms"] - r["start_ms"] <= 45_000
    assert r["end_ms"] < 140_000                       # the silence and what followed: cut
    assert any("measured silence" in n for n in r["notes"])
    later = highlight_candidates.refine(121_620, 165_780, words, 180_000, max_ms=45_000,
                                        silences=silences, cited=[(165_000, 166_500)])
    # Evidence after the silence leaves less than 8 s there: no split, limit still holds.
    assert not any("measured silence" in n for n in later["notes"])
    assert later["end_ms"] - later["start_ms"] <= 45_000


def test_refinement_never_exceeds_the_target_limit():
    words = _native_words()
    r = highlight_candidates.refine(121_620, 165_780, words, 180_000, max_ms=45_000)
    assert r["end_ms"] - r["start_ms"] <= 45_000


def test_windows_respect_the_short_limit():
    plan = highlight_candidates.plan([_seed(60_000, 2.0), _seed(70_000, 1.0)], [], 240_000,
                                     max_windows=4, max_window_ms=45_000)
    assert plan and all(w.end_ms - w.start_ms <= 45_000 for w in plan)


def test_duplicate_window_error_names_the_window():
    from app.ai.validate import validate
    from tests.test_ai import answer, bundle

    b = bundle()
    data = answer(b)
    data["candidates"].append(dict(data["candidates"][0]))
    res = validate(json.dumps(data), b)
    assert not res.ok
    assert any("appears 2 times" in e and "one candidate per window" in e for e in res.errors)
