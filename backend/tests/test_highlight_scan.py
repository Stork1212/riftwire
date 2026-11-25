import pytest

from app.services.highlight_scan import frame_activity, rank


def test_frame_activity_caps_scene_changes_and_rejects_partial_frames():
    assert frame_activity(bytes([0, 0, 20, 20, 255, 255]), 2) == [20, 45]
    with pytest.raises(ValueError, match="Incomplete"):
        frame_activity(bytes([0, 0, 20]), 2)


def test_rank_can_select_a_silent_visual_moment():
    samples = [3.0] * 160
    samples[90:103] = [40.0] * 13
    candidates = rank(samples, [], 80_000, limit=2)
    assert candidates[0].start_ms <= 45_000 < candidates[0].end_ms
    assert candidates[0].speech_cues == []
    assert candidates[0].end_ms <= candidates[1].start_ms or (
        candidates[1].end_ms <= candidates[0].start_ms
    )


def test_confirmed_reaction_words_are_used_as_evidence():
    words = [
        {"w": "no", "start_ms": 51_000, "end_ms": 51_090, "prob": .99},
        {"w": "way", "start_ms": 51_100, "end_ms": 51_340, "prob": .9},
        {"w": "crash", "start_ms": 51_400, "end_ms": 51_500, "prob": .1},
    ]
    [candidate] = rank([2.0] * 160, words, 80_000, limit=1)
    assert candidate.speech_cues == ["reaction"]
    assert candidate.excerpt == "no way"
