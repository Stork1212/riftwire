"""Whisper model registry and controlled downloads.

Only the files listed here are downloaded (config + weights), into RIFTWIRE's models
directory (not a hidden cache). Size and SHA-256 are verified; downloads are refused when
they would breach the 60 GiB free-space floor (+1 GiB margin).
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from riftbench.config import models_dir, require_space


@dataclass(frozen=True)
class ModelFile:
    name: str
    size: int
    sha256: str | None  # LFS files have a published SHA-256; small JSON files do not


@dataclass(frozen=True)
class ModelSpec:
    key: str
    repo: str
    params: str
    files: tuple[ModelFile, ...]

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def local_dir(self) -> Path:
        return models_dir() / self.repo.replace("/", "--")


# Sizes and hashes from the Hugging Face API (mlx-community, 2026-11-24). Verified on download.
REGISTRY: dict[str, ModelSpec] = {
    "tiny": ModelSpec("tiny", "mlx-community/whisper-tiny-mlx", "39M", (
        ModelFile("config.json", 262, None),
        ModelFile("weights.npz", 74_418_540,
                  "0e03a5993d6eea43b07ee2dcc772b0e4cef5bb227257dacc24bf289387d49186"),
    )),
    "base": ModelSpec("base", "mlx-community/whisper-base-mlx", "74M", (
        ModelFile("config.json", 262, None),
        ModelFile("weights.npz", 143_724_204,
                  "2f57d5f3ef473054c638961f90716f4ee415e8108de81313eccb2c5fd62eff0b"),
    )),
    "small": ModelSpec("small", "mlx-community/whisper-small-mlx", "244M", (
        ModelFile("config.json", 266, None),
        ModelFile("weights.npz", 481_307_592,
                  "55b6674c9b339702d486e2b1573839a66f8ec8f821ed2886993ef717a86b09f5"),
    )),
}
APPROVED = ("tiny", "base", "small")  # small: only if the floor check passes


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def verify(spec: ModelSpec) -> list[str]:
    """Return a list of problems (empty if the local copy is complete and verified)."""
    problems = []
    for mf in spec.files:
        p = spec.local_dir / mf.name
        if not p.is_file():
            problems.append(f"{mf.name}: missing")
            continue
        if p.stat().st_size != mf.size:
            problems.append(f"{mf.name}: size {p.stat().st_size} != {mf.size}")
            continue
        if mf.sha256 and sha256_of(p) != mf.sha256:
            problems.append(f"{mf.name}: sha256 mismatch")
    return problems


def missing_bytes(spec: ModelSpec) -> int:
    return sum(f.size for f in spec.files
               if not ((spec.local_dir / f.name).is_file()
                       and (spec.local_dir / f.name).stat().st_size == f.size))


def fetch(spec: ModelSpec, downloader=None) -> str:
    """Download exactly the registered files for ``spec``; verify; return a status string."""
    if spec.key not in APPROVED:
        raise PermissionError(f"model {spec.key!r} is not approved for download")
    if not verify(spec):
        return "already present (verified)"
    need = missing_bytes(spec)
    require_space(models_dir(), need, f"download of whisper-{spec.key}")
    spec.local_dir.mkdir(parents=True, exist_ok=True)
    if downloader is None:
        from huggingface_hub import hf_hub_download  # bench/.venv only

        def downloader(repo: str, filename: str, dest: Path) -> None:
            hf_hub_download(repo_id=repo, filename=filename, local_dir=str(dest))
    for mf in spec.files:
        p = spec.local_dir / mf.name
        if p.is_file() and p.stat().st_size == mf.size and \
                (mf.sha256 is None or sha256_of(p) == mf.sha256):
            continue
        downloader(spec.repo, mf.name, spec.local_dir)
    problems = verify(spec)
    if problems:
        for mf in spec.files:  # never leave an unverified model behind
            (spec.local_dir / mf.name).unlink(missing_ok=True)
        raise RuntimeError(f"whisper-{spec.key} failed verification: {problems}")
    # huggingface_hub may leave a small metadata cache inside local_dir; keep it out of size
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    return "downloaded and verified"
