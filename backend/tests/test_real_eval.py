"""The real-footage evaluation's manifest checks and pre-registered matching rule
(docs/REAL_FOOTAGE_EVAL.md §2). Pure functions: no footage, no models."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "real_eval", Path(__file__).resolve().parents[2] / "scripts" / "real_eval.py")
real_eval = importlib.util.module_from_spec(_SPEC)
sys.modules["real_eval"] = real_eval          # dataclasses resolve their module by name
_SPEC.loader.exec_module(real_eval)
Mark = real_eval.Mark


@pytest.mark.parametrize(("value", "ms"), [(12, 12_000), (1.5, 1_500), ("01:02", 62_000),
                                           ("1:02:03.25", 3_723_250), ("00:00:07.5", 7_500)])
def test_times(value, ms):
    assert real_eval.parse_time(value) == ms


@pytest.mark.parametrize("value", ["1:2", "61:00", "abc", -1, "1:02:03.4567"])
def test_bad_times(value):
    with pytest.raises(ValueError):
        real_eval.parse_time(value)


def _manifest(tmp_path, **over):
    video = tmp_path / "rec.mp4"
    video.write_bytes(b"x")
    rec = {"path": str(video), "authorized": True, "marked_before_analysis": True,
           "authorization": "I recorded this myself; evaluation only.", "basis": "ownership",
           "marked": [{"start": "00:01:00", "end": "00:01:20", "label": "chase"}]}
    rec.update(over)
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps({"recordings": [rec]}))
    return p


def test_a_complete_manifest_is_accepted(tmp_path):
    items, errors = real_eval.load_manifest(_manifest(tmp_path))
    assert errors == [] and len(items) == 1
    assert items[0].marks == [Mark(60_000, 80_000, "chase")] and items[0].request == 5


@pytest.mark.parametrize(("over", "expect"), [
    ({"authorized": False}, "'authorized' must be true"),
    ({"authorized": "yes"}, "'authorized' must be true"),
    ({"authorization": "ok"}, "owner's statement"),
    ({"basis": "public_web"}, "public availability is not a basis"),
    ({"path": "/nonexistent/x.mp4"}, "existing absolute file path"),
    ({"marked": []}, "at least one span"),
    ({"marked": [{"start": 20, "end": 10}]}, "end is not after start"),
    ({"marked": [{"start": 0, "end": 10}, {"start": 5, "end": 12}]}, "marks overlap"),
    ({"request": 0}, "integer from 1 to 20"),
    ({"basis": None}, "'basis' must be one of"),
    ({"marked_before_analysis": None}, "marked before"),
    ({"name": "../escape"}, "names the output files"),
    ({"name": "a/b"}, "names the output files"),
])
def test_unauthorized_or_malformed_recordings_are_refused(tmp_path, over, expect):
    items, errors = real_eval.load_manifest(_manifest(tmp_path, **over))
    assert items == [] and any(expect in e for e in errors), errors


def _h(rank, a, b):
    return {"rank": rank, "start_ms": a * 1000, "end_ms": b * 1000, "title": f"h{rank}"}


def test_matching_rule_overlap_of_the_shorter_span():
    marks = [Mark(60_000, 90_000, "long mark"), Mark(200_000, 210_000, "short mark"),
             Mark(400_000, 420_000, "missed")]
    hl = [_h(1, 62, 75),          # inside the long mark: 13/13 of the shorter span
          _h(2, 205, 240),        # 5 s of a 10 s mark: exactly 0.5 -> match
          _h(3, 300, 320)]        # nothing marked there -> extra
    v = real_eval.match(hl, marks)
    assert v["found"] == 2 and v["recall"] == "2/3" and v["precision"] == "2/3"
    by = {x["highlight_rank"]: x for x in v["matched"]}
    assert by[1]["overlap"] == 1.0 and by[1]["start_delta_s"] == 2.0
    assert by[2]["overlap"] == 0.5 and by[2]["end_delta_s"] == 30.0
    assert [x["highlight_rank"] for x in v["extras"]] == [3]
    assert v["missed"][0]["label"] == "missed" and v["missed"][0]["diagnosis"] == "window"


def test_matching_is_one_to_one_and_reports_near_misses():
    marks = [Mark(0, 20_000, "a")]
    hl = [_h(1, 2, 18), _h(2, 5, 15), _h(3, 18, 40)]
    v = real_eval.match(hl, marks)
    assert v["found"] == 1 and v["matched"][0]["highlight_rank"] == 1
    assert {x["highlight_rank"] for x in v["extras"]} == {2, 3}
    assert any(x["highlight_rank"] == 3 for x in v["near_misses"])   # 2/20 s: near miss


def test_missed_marks_are_diagnosed_window_model_or_ranking():
    marks = [Mark(10_000, 20_000, "no window"), Mark(100_000, 110_000, "model skipped"),
             Mark(200_000, 210_000, "ranked out")]
    windows = [{"id": "c:1", "start_ms": 95_000, "end_ms": 130_000},
               {"id": "c:2", "start_ms": 190_000, "end_ms": 230_000}]
    cands = [{"status": "rejected", "start_ms": 201_000, "end_ms": 212_000,
              "reason": "editorial score 55 below 60"}]
    v = real_eval.match([], marks, windows=windows, candidates=cands)
    assert [x["diagnosis"] for x in v["missed"]] == ["window", "model", "ranking"]
    assert "below 60" in v["missed"][2]["detail"] and v["precision"] == "0/0"


def test_report_states_counts_and_limits():
    result = {"name": "r", "requested": 5, "returned": 1, "summary": {
        "fewer_than_requested": True, "reason": "only these passed"},
        "request_status": "COMPLETE", "rejected_reasons": {}, "highlights": [],
        "match": real_eval.match([_h(1, 0, 10)], [Mark(0, 10_000, "x")]),
        "ai_runs": [{"status": "accepted", "attempts": 1}], "runtime_s": {"end_to_end": 9},
        "cost_usd": 0.0, "renders": []}
    text = real_eval.render_report([result])
    assert "found 1/1 marked moments" in text and "describe these recordings only" in text
    assert "not probabilities of going viral" in text and "Why fewer: only these passed" in text


def test_seconds_given_as_text_are_accepted():
    assert real_eval.parse_time("75") == 75_000 and real_eval.parse_time("1.5") == 1_500
    with pytest.raises(ValueError):
        real_eval.parse_time("-3")


# ---------------------------------------------- runner validation (eval track, 2026-11-25)
def test_a_recording_listed_twice_is_refused(tmp_path):
    p = _manifest(tmp_path)
    data = json.loads(p.read_text())
    data["recordings"].append({**data["recordings"][0], "name": "second"})
    p.write_text(json.dumps(data))
    items, errors = real_eval.load_manifest(p)
    assert len(items) == 1 and any("listed twice" in e for e in errors)


class _Probe:
    def __init__(self, duration="120.0", kinds=("video", "audio"), text=None):
        self.stdout = text if text is not None else json.dumps({
            "format": {"duration": duration}, "streams": [{"codec_type": k} for k in kinds]})


def _item(marks=((60_000, 80_000),)):
    return real_eval.Item(Path("/x.mp4"), "statement here", "ownership", "gta", "rf", 5,
                          [Mark(a, b) for a, b in marks], "rec")


def test_probe_checks_media_audio_and_marks_inside_the_recording():
    assert real_eval.probe_item(_item(), run=lambda *a: _Probe()) == []
    assert any("no audio" in e for e in real_eval.probe_item(
        _item(), run=lambda *a: _Probe(kinds=("video",))))
    assert any("after the recording ends" in e for e in real_eval.probe_item(
        _item(((60_000, 130_000),)), run=lambda *a: _Probe()))
    assert any("cannot read" in e for e in real_eval.probe_item(
        _item(), run=lambda *a: _Probe(text="not json")))


def test_preregistration_fixes_the_marks_before_analysis(tmp_path):
    p = _manifest(tmp_path)
    items, _ = real_eval.load_manifest(p)
    a = real_eval.preregistration(p, items)
    assert a["matching"] == {"overlap": 0.5, "cut_tolerance_ms": 1000, "duplicate_overlap": 0.5}
    items[0].marks[0].end_ms += 1
    b = real_eval.preregistration(p, items)
    assert a["recordings"][0]["marks_sha256"] != b["recordings"][0]["marks_sha256"]
    assert len(a["manifest_sha256"]) == 64


def test_setup_and_payoff_cuts_coverage_and_duplicates():
    marks = [Mark(10_000, 40_000, "chase"), Mark(100_000, 110_000, "crash")]
    hl = [_h(1, 15, 38),            # starts 5 s late (setup cut), ends 2 s early (payoff cut)
          _h(2, 99.5, 110.5),       # within tolerance on both sides
          _h(3, 16, 30)]            # inside #1: duplicate
    v = real_eval.match(hl, marks)
    by = {x["highlight_rank"]: x for x in v["matched"]}
    assert by[1]["cuts_setup"] and by[1]["setup_cut_s"] == 5.0
    assert by[1]["cuts_payoff"] and by[1]["payoff_cut_s"] == 2.0
    assert not by[2]["cuts_setup"] and not by[2]["cuts_payoff"]
    assert by[2]["extra_lead_s"] == 0.5 and by[2]["extra_tail_s"] == 0.5
    assert v["setup_cuts"] == 1 and v["payoff_cuts"] == 1
    assert v["marked_time_covered"] == "33.0/40.0 s"          # 23 s + 10 s, overlaps once
    assert v["duplicates"] == [{"ranks": [1, 3], "overlap": 1.0, "iou": 0.609}]


def test_text_review_reruns_the_claim_check_on_the_stored_text():
    def ground(text, words):
        bad = [t for t in ("leak",) if t in text.lower() and t not in
               " ".join(w["w"] for w in words)]
        return not bad, bad

    h = {"rank": 1, "title": "GTA 6 leak!", "hook": "Watch this", "text_checks": []}
    t = real_eval.text_review(h, [{"w": "watch"}, {"w": "this"}], ground)
    assert not t["title_claims_ok"] and t["title_unsupported"] == ["leak"]
    assert t["hook_claims_ok"] and t["clip_text"] == "watch this"


def test_batch_summary_reports_every_non_accepted_run():
    rows = [{"batch": 0, "status": "provider_error", "attempts": 1, "error": "timeout"},
            {"batch": 0, "status": "accepted", "attempts": 1, "error": None},
            {"batch": 1, "status": "failed_validation", "attempts": 3, "error": "bad ids"}]
    b = real_eval.batch_summary(rows)
    assert b["batches"] == 2 and b["accepted"] == 1
    assert [x["status"] for x in b["rejected"]] == ["provider_error", "failed_validation"]


def test_report_and_title_sheet_carry_the_new_sections():
    texts = [{"rank": 1, "title": "Big jump", "hook": "No way", "title_claims_ok": True,
              "hook_claims_ok": True, "title_unsupported": [], "hook_unsupported": [],
              "dropped_by_system": [], "clip_text": "no way | that", "span_s": [1.0, 20.0],
              "what_happens": "x"}]
    sheet = real_eval.title_sheet("rec", texts)
    assert "| 1 | 1.0-20.0 | Big jump | No way | ok | no way / that | |" in sheet
    result = {"name": "rec", "requested": 2, "returned": 2, "summary": None,
              "request_status": "COMPLETE", "rejected_reasons": {}, "highlights": [],
              "match": real_eval.match([_h(1, 15, 38), _h(2, 16, 30)],
                                       [Mark(10_000, 40_000, "chase")]),
              "ai_runs": [{"status": "accepted", "attempts": 1}],
              "batches": {"batches": 2, "accepted": 1, "rejected": [
                  {"batch": 1, "status": "failed_validation", "attempts": 3, "error": "x"}]},
              "text": texts, "runtime_s": {"end_to_end": 1}, "cost_usd": 0.0,
              "renders": [{"rank": 1, "checks": {}, "duration_s": 22.5, "integrated_lufs": -14,
                           "true_peak_dbfs": -1, "outro_psnr_db": 40,
                           "framing_sheet": "rec-rank1-framing.png"}]}
    text = real_eval.render_report([result])
    for want in ("setup cuts 1, payoff cuts 1", "duplicate: #1 and #2",
                 "AI batches: 1/2 accepted; batch 1 failed_validation after 3",
                 "claim check passed for 1/1", "rec-rank1-framing.png"):
        assert want in text, want
