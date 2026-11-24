from __future__ import annotations

from collections import namedtuple
from pathlib import Path

from riftbench import config, estimate, models


def test_estimate_covers_every_component_and_peak_exceeds_final(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    comps = estimate.components(["tiny", "base", "small"], tmp_path / "out")
    names = " | ".join(c.name for c in comps)
    for part in ("venv", "pip", "HF", "whisper-tiny", "whisper-base", "whisper-small",
                 "datasets", "TTS", "signals", "results"):
        assert part in names
    by = {c.name: c for c in comps}
    assert by["model whisper-small"].final_bytes == models.REGISTRY["small"].total_bytes
    # 10 min of 1080p60 at 12 Mbit/s plus proxy, with margin: > 1 GB
    assert by["signals video + proxy"].final_bytes > 1_000_000_000
    # 111 minutes of 16 kHz mono s16 ≈ 213 MB
    assert 200e6 < by["speech datasets (WAV)"].final_bytes < 240e6
    vols = estimate.by_volume(comps)
    assert sum(v.peak for v in vols) > sum(v.final for v in vols)


def test_by_volume_groups_paths_on_separate_devices(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    comps = [estimate.Component("x", a, 1), estimate.Component("y", b / "new" / "dir", 2)]
    vols = estimate.by_volume(comps)
    assert len(vols) == 1 and vols[0].final == 3  # same device in a tmp dir
    du = namedtuple("du", "total used free")
    monkeypatch.setattr(config.shutil, "disk_usage",
                        lambda p: du(0, 0, config.FLOOR_BYTES + config.MARGIN_BYTES + 3))
    assert estimate.check(vols[0])[0] is True
    vols[0].components.append(estimate.Component("z", a, 1))
    assert estimate.check(vols[0])[0] is False


def test_existing_parent_for_missing_path(tmp_path):
    assert estimate._existing_parent(tmp_path / "no" / "such") == Path(tmp_path)
