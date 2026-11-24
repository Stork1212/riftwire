from __future__ import annotations

import hashlib
from collections import namedtuple

import pytest

from riftbench import config, models


def _spec(tmp_content=b"weights"):
    sha = hashlib.sha256(tmp_content).hexdigest()
    return models.ModelSpec("tiny", "org/fake-model", "1", (
        models.ModelFile("config.json", 2, None),
        models.ModelFile("weights.npz", len(tmp_content), sha)))


def _plenty(monkeypatch):
    du = namedtuple("du", "total used free")
    monkeypatch.setattr(config.shutil, "disk_usage", lambda p: du(0, 0, 500 * config.GIB))


def test_fetch_downloads_only_listed_files_and_verifies(monkeypatch):
    _plenty(monkeypatch)
    spec, calls = _spec(), []

    def dl(repo, name, dest):
        calls.append(name)
        (dest / name).write_bytes(b"{}" if name == "config.json" else b"weights")

    assert models.fetch(spec, dl) == "downloaded and verified"
    assert calls == ["config.json", "weights.npz"]
    assert models.fetch(spec, dl) == "already present (verified)"
    assert len(calls) == 2


def test_fetch_removes_files_that_fail_verification(monkeypatch):
    _plenty(monkeypatch)
    spec = _spec()

    def dl(repo, name, dest):
        (dest / name).write_bytes(b"{}" if name == "config.json" else b"tampered")

    with pytest.raises(RuntimeError, match="verification"):
        models.fetch(spec, dl)
    assert not any(spec.local_dir.iterdir())


def test_fetch_refuses_below_floor(monkeypatch):
    du = namedtuple("du", "total used free")
    monkeypatch.setattr(config.shutil, "disk_usage",
                        lambda p: du(0, 0, config.FLOOR_BYTES + config.MARGIN_BYTES + 1))
    with pytest.raises(config.InsufficientSpace):
        models.fetch(_spec(), lambda *a: pytest.fail("must not download"))


def test_unapproved_model_refused():
    spec = models.ModelSpec("large", "org/x", "1", ())
    with pytest.raises(PermissionError):
        models.fetch(spec, lambda *a: None)


def test_registry_is_exactly_the_approved_models():
    assert set(models.REGISTRY) == {"tiny", "base", "small"}
    for spec in models.REGISTRY.values():
        assert spec.repo.startswith("mlx-community/whisper-") and spec.repo.endswith("-mlx")
        assert sum(f.size for f in spec.files) < 500_000_000
