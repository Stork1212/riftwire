from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from collections import namedtuple
from pathlib import Path

import pytest

from riftbench import config

spec = importlib.util.spec_from_file_location(
    "bench_run", Path(__file__).resolve().parents[1] / "run.py")
run = importlib.util.module_from_spec(spec)
sys.modules["bench_run"] = run
spec.loader.exec_module(run)


def test_plan_is_offline_and_reports_sizes(capsys, monkeypatch):
    du = namedtuple("du", "total used free")
    monkeypatch.setattr(config.shutil, "disk_usage", lambda p: du(0, 0, 70 * config.GIB))
    assert run.main(["plan"]) == 0
    out = capsys.readouterr().out
    assert "whisper-tiny" in out and "74,418,540" in out and "OK" in out


def test_plan_refuses_when_floor_would_break(capsys, monkeypatch):
    du = namedtuple("du", "total used free")
    monkeypatch.setattr(config.shutil, "disk_usage", lambda p: du(0, 0, 61 * config.GIB))
    assert run.main(["plan"]) == run.EXIT_NO_SPACE == 10
    assert "REFUSED" in capsys.readouterr().out


def test_plan_reports_peak_and_enforces_budget(capsys, monkeypatch):
    du = namedtuple("du", "total used free")
    monkeypatch.setattr(config.shutil, "disk_usage", lambda p: du(0, 0, 500 * config.GIB))
    assert run.main(["plan", "--budget-gb", "1.9"]) == run.EXIT_OVER_BUDGET == 11
    out = capsys.readouterr().out
    assert "PEAK" in out and "exceeds the approved budget" in out
    assert run.main(["plan", "--budget-gb", "4"]) == 0


def test_fetch_distinguishes_floor_skip_from_real_failure(capsys, monkeypatch):
    def no_space(spec):
        raise config.InsufficientSpace("floor")

    def network(spec):
        raise OSError("connection reset")

    monkeypatch.setattr(run.models, "fetch", no_space)
    assert run.main(["fetch-models", "--models", "small", "--confirm"]) == 10
    monkeypatch.setattr(run.models, "fetch", network)
    assert run.main(["fetch-models", "--models", "small", "--confirm"]) == 1
    assert "FAILED — OSError" in capsys.readouterr().out


def test_fetch_requires_confirm(capsys):
    assert run.main(["fetch-models", "--models", "tiny"]) == 2


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
def test_all_quick_pipeline_without_models(tmp_path, monkeypatch):
    du = namedtuple("du", "total used free")
    real = shutil.disk_usage
    monkeypatch.setattr(config.shutil, "disk_usage",
                        lambda p: du(*real(p)[:2], 500 * config.GIB))
    small = run.signals.VideoSpec(seconds=20, width=320, height=180, fps=30)
    monkeypatch.setattr(run.signals, "VideoSpec", lambda **k: small)
    out = tmp_path / "res"
    assert run.main(["all", "--quick", "--tone", "--out", str(out)]) == 0
    res = json.loads((out / "results.json").read_text())
    assert res["transcription"] == []  # no models downloaded in tests
    assert res["recommendation"]["status"] == "no_results"
    assert res["signals"]["scene_cuts"]["truth"] > 0
    assert (out / "results.md").read_text().startswith("# RIFTWIRE M0")

