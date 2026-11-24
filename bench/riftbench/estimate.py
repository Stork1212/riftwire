"""Disk-usage estimate per component and per volume, with the **peak**, not just the final size.

Every component is mapped to the directory it is written to. Components are grouped by
volume (``st_dev``), and each volume is checked against the 60 GiB floor + 1 GiB margin.
Components that are written and then removed (pip's unpack area, per-variant mixing
temporaries, ``say`` AIFF files) count only toward the peak.

The figures are conservative upper bounds derived from the actual run parameters:
* WAV: 16 kHz mono s16 = 32,000 B/s.
* Video: (video + audio bitrate) × duration × 1.3. VideoToolbox can overshoot its target.
* Models: exact registry sizes. Downloads go to ``<file>.incomplete`` in the same directory,
  then are renamed, so a file never exists twice.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from riftbench import config, dataset, models, signals
from riftbench.config import FLOOR_BYTES, MARGIN_BYTES, ROOT, bench_dir, dir_size, models_dir

WAV_BPS = 32_000
VENV_INSTALLED = 600 * 1000**2      # measured on the Mac: 597 MB incl. bytecode (no torch)
PIP_TRANSIENT = 350 * 1000**2       # 121 MB wheels + unpack/compile area, freed after setup
RESULTS = 50 * 1000**2              # results.json, per-run hypotheses, logs
HF_METADATA = 5 * 1000**2           # huggingface_hub metadata under HF_HOME / local_dir/.cache


@dataclass
class Component:
    name: str
    path: Path
    final_bytes: int
    transient_bytes: int = 0   # extra bytes that exist only while the step runs
    note: str = ""


@dataclass
class VolumeNeed:
    device: int
    mount_hint: Path
    components: list[Component] = field(default_factory=list)

    @property
    def final(self) -> int:
        return sum(c.final_bytes for c in self.components)

    @property
    def peak(self) -> int:
        # Conservative: everything persistent at once plus the largest transient spike.
        return self.final + max((c.transient_bytes for c in self.components), default=0)


def _existing_parent(p: Path) -> Path:
    p = p.expanduser().absolute()
    while not p.exists():
        p = p.parent
    return p


def _corpus_tokens() -> tuple[int, float]:
    sents = dataset.load_corpus()
    tokens = {w for s in sents for w in s.split()}
    avg_chars = sum(len(s) for s in sents) / len(sents)
    return len(tokens), avg_chars


def dataset_bytes(sets=dataset.DEFAULT_SETS) -> tuple[int, int, int]:
    """(final dataset WAVs, TTS cache, largest transient) in bytes."""
    final = sum(int(s.minutes * 60 * WAV_BPS * 1.02) for s in sets)  # + leading silence
    # Mixing a variant needs clean.wav + noise/bed.wav + audio.wav at the same time.
    transient = max((int(s.minutes * 60 * WAV_BPS) * 2 for s in sets
                     if s.variant != "clean"), default=0)
    n_tokens, avg_chars = _corpus_tokens()
    voices = 3
    word_cache = n_tokens * voices * int(0.8 * WAV_BPS)              # ≤ 0.8 s per word
    sent_cache = len(dataset.load_corpus()) * voices * int(avg_chars * 0.09 * WAV_BPS)
    aiff_tmp = int(10 * 22_050 * 2)                                   # one ≤10 s AIFF at a time
    return final, int((word_cache + sent_cache) * 1.5), max(transient, aiff_tmp)


def signals_bytes(spec: signals.VideoSpec | None = None) -> int:
    spec = spec or signals.VideoSpec()
    src = (12_000_000 + 128_000) / 8 * spec.seconds
    proxy = (1_500_000 + 96_000) / 8 * spec.seconds
    return int((src + proxy) * 1.3)


def components(model_keys: list[str], out_dir: Path, quick: bool = False,
               signals_only: bool = False) -> list[Component]:
    venv = ROOT / "bench" / ".venv"
    tmp = Path(os.environ.get("TMPDIR", "/tmp"))
    hf_home = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
    venv_missing = 0 if (venv / "bin" / "python").exists() else VENV_INSTALLED
    sets = dataset.QUICK_SETS if quick else dataset.DEFAULT_SETS
    ds_final, tts_cache, ds_transient = dataset_bytes(sets)
    sig_spec = signals.VideoSpec(seconds=60, width=1280, height=720) if quick \
        else signals.VideoSpec()
    comps = [
        Component("bench venv (installed)", venv, venv_missing,
                  note="0 if already installed"),
        Component("pip download/unpack area", tmp, 0,
                  PIP_TRANSIENT if venv_missing else 0, note="freed after setup"),
        Component("HF metadata/cache", hf_home, HF_METADATA,
                  note="HF_HOME set inside the data dir by run_m0.sh; Xet chunk cache off"),
    ]
    for k in model_keys:
        spec = models.REGISTRY[k]
        comps.append(Component(f"model whisper-{k}", models_dir(), models.missing_bytes(spec)))
    if signals_only:
        sig_spec = signals.VideoSpec(seconds=60, width=1280, height=720) if quick \
            else signals.VideoSpec()
        return [Component("signals video + proxy", bench_dir(), signals_bytes(sig_spec)),
                Component("results", out_dir, RESULTS)]
    comps += [
        Component("speech datasets (WAV)", bench_dir(), ds_final),
        Component("TTS clip cache", bench_dir(), tts_cache, ds_transient,
                  note="transient: variant mixing / AIFF"),
        Component("signals video + proxy", bench_dir(), signals_bytes(sig_spec)),
        Component("results", out_dir, RESULTS),
    ]
    return comps


def by_volume(comps: list[Component]) -> list[VolumeNeed]:
    vols: dict[int, VolumeNeed] = {}
    for c in comps:
        p = _existing_parent(c.path)
        dev = p.stat().st_dev
        vols.setdefault(dev, VolumeNeed(dev, p)).components.append(c)
    return list(vols.values())


def free_bytes(path: Path) -> int:
    return config.shutil.disk_usage(_existing_parent(path)).free


def check(vol: VolumeNeed) -> tuple[bool, int]:
    free = free_bytes(vol.mount_hint)
    return free - vol.peak >= FLOOR_BYTES + MARGIN_BYTES, free


def existing_usage() -> int:
    return dir_size(ROOT / "bench" / ".venv") + dir_size(models_dir()) + dir_size(bench_dir())
