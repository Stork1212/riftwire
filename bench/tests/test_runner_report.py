from __future__ import annotations

from riftbench import report
from riftbench.runner import parse_time_output

MAC = """        2.31 real         1.90 user         0.40 sys
           812345344  maximum resident set size
                   0  average shared memory size
          1012345678  peak memory footprint
"""
GNU = "\tMaximum resident set size (kbytes): 204800\n"


def test_parse_time_output_macos_and_gnu():
    assert parse_time_output(MAC) == {"max_rss_bytes": 812345344,
                                      "peak_footprint_bytes": 1012345678}
    assert parse_time_output(GNU) == {"max_rss_bytes": 204800 * 1024}


def _run(model, ds, kind, variant, wer, rtf, med=30.0, p95=120.0, mem=900_000_000, rc=0):
    sc = {"wer": wer}
    if kind == "timing":
        sc.update(start_median_ms=med, start_p95_ms=p95, start_within_100ms=0.9)
    return {"model": model, "dataset": ds, "kind": kind, "variant": variant, "audio_s": 600,
            "returncode": rc, "rtf": rtf, "scores": sc, "peak_footprint_bytes": mem,
            "load_s": 0.5}


def test_recommend_prefers_most_accurate_passing_model():
    ev = {
        "tiny": report.evaluate_model([_run("tiny", "t", "timing", "clean", 0.06, 0.02),
                                       _run("tiny", "n", "natural", "noisy", 0.2, 0.02)]),
        "base": report.evaluate_model([_run("base", "t", "timing", "clean", 0.03, 0.04),
                                       _run("base", "n", "natural", "noisy", 0.09, 0.04)]),
        "small": report.evaluate_model([_run("small", "t", "timing", "clean", 0.02, 0.2),
                                        _run("small", "n", "natural", "noisy", 0.05, 0.2)]),
    }
    assert not ev["tiny"]["meets_all"] and ev["base"]["meets_all"]
    assert not ev["small"]["checks"]["speed"]
    rec = report.recommend(ev)
    assert rec["model"] == "base" and rec["status"] == "meets_targets"


def test_recommend_reports_when_nothing_passes_and_failures_count():
    ev = {"tiny": report.evaluate_model([_run("tiny", "t", "timing", "clean", 0.3, 0.02,
                                              med=200)]),
          "base": report.evaluate_model([_run("base", "t", "timing", "clean", 0.0, 0.0,
                                              rc=1)])}
    assert ev["base"]["checks"]["no_failures"] is False
    rec = report.recommend(ev)
    assert rec["status"] == "no_model_meets_all_targets"
    md = report.markdown({"transcription": [], "evaluation": ev, "recommendation": rec})
    assert "Recommendation" in md and "Limitations" in md
