from __future__ import annotations

import itertools
import json
import shutil

import pytest

from riftbench import dataset, signals
from riftbench.audio import duration_ms, read_pcm
from riftbench.tts import ToneTTS

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")


@pytest.mark.parametrize("variant", ["clean", "noisy", "music"])
def test_timing_dataset_has_exact_word_ground_truth(tmp_path, variant):
    spec = dataset.DatasetSpec(f"t-{variant}", "timing", 0.25, variant)
    d = dataset.build(spec, ToneTTS(tmp_path / "cache"), tmp_path / "ds")
    truth = json.loads((d / "truth.json").read_text())
    pcm = read_pcm(d / "audio.wav")
    assert abs(duration_ms(pcm) - truth["duration_ms"]) < 1
    words = truth["words"]
    assert words and all(w["end_ms"] > w["start_ms"] for w in words)
    assert all(b["start_ms"] >= a["end_ms"] + 69 for a, b in itertools.pairwise(words))
    assert truth["text"].split() == [w["w"] for w in words]
    # deterministic: same seed → same truth, reused without rebuilding
    assert dataset.build(spec, ToneTTS(tmp_path / "cache"), tmp_path / "ds") == d


def test_natural_dataset_has_text_only(tmp_path):
    spec = dataset.DatasetSpec("n", "natural", 0.2, "clean")
    truth = json.loads((dataset.build(spec, ToneTTS(tmp_path / "c"), tmp_path / "ds")
                        / "truth.json").read_text())
    assert truth["words"] is None and len(truth["text"].split()) > 5


def test_signals_detects_known_cuts_and_bursts(tmp_path):
    r = signals.run(signals.VideoSpec(seconds=30, width=320, height=180, fps=30), tmp_path)
    assert r["scene_cuts"]["truth"] >= 5 and r["loudness_bursts"]["truth"] >= 3
    assert r["scene_cuts"]["precision"] >= 0.9 and r["scene_cuts"]["recall"] >= 0.7
    assert r["loudness_bursts"]["recall"] >= 0.9
    assert r["loudness_bursts"]["median_error_ms"] <= 150


def test_match_tolerance():
    m = signals.match([1.0, 5.0], [1.2, 5.4, 9.0])
    assert m["true_positives"] == 1 and m["precision"] == 1 / 3
