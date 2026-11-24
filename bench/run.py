#!/usr/bin/env python3
"""RIFTWIRE M0 benchmark CLI.

    bench/.venv/bin/python bench/run.py plan                     # no network, no writes
    bench/.venv/bin/python bench/run.py fetch-models --models tiny,base --confirm
    bench/.venv/bin/python bench/run.py all --models tiny,base[,small]
    bench/.venv/bin/python bench/run.py report RESULTS_DIR

Downloads happen ONLY in ``fetch-models`` with ``--confirm``; every other command runs the
transcription child with HF_HUB_OFFLINE=1. Results go to
``~/Claude/riftwire-env/bench-<UTC stamp>/`` by default (outside the repo).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from riftbench import dataset, models, report, runner, signals, sysinfo  # noqa: E402
from riftbench.config import (  # noqa: E402
    FLOOR_BYTES,
    GIB,
    ROOT,
    InsufficientSpace,
    bench_dir,
    check_space,
    data_dir,
    dir_size,
    models_dir,
    require_space,
)
from riftbench.metrics import score_timing_set, wer  # noqa: E402
from riftbench.tts import SayTTS, ToneTTS  # noqa: E402

BENCH_VENV_PY = ROOT / "bench" / ".venv" / "bin" / "python"
DATASETS_EST = 300 * 1000**2      # all speech sets (16 kHz mono) + TTS cache
SIGNALS_EST = 1200 * 1000**2      # 10 min 1080p60 source + proxy


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_models(s: str) -> list[str]:
    keys = [k.strip() for k in s.split(",") if k.strip()]
    bad = [k for k in keys if k not in models.REGISTRY]
    if bad:
        raise SystemExit(f"unknown model(s): {bad}; choose from {list(models.REGISTRY)}")
    return keys


def cmd_plan(a: argparse.Namespace) -> int:
    print(f"Data dir: {data_dir()}  (models → {models_dir()}, datasets → {bench_dir()})")
    total = 0
    for k, spec in models.REGISTRY.items():
        have = "present" if not models.verify(spec) else "not downloaded"
        print(f"  whisper-{k:<5} {spec.repo:<36} {spec.params:>5} params  "
              f"{spec.total_bytes / 1e6:7.1f} MB  [{have}]")
        for f in spec.files:
            print(f"      https://huggingface.co/{spec.repo}/resolve/main/{f.name}"
                  f"  {f.size:>11,} B  sha256={f.sha256 or '(size-checked only)'}")
        total += models.missing_bytes(spec)
    venv = dir_size(ROOT / "bench" / ".venv")
    need = total + DATASETS_EST + SIGNALS_EST
    c = check_space(data_dir(), need)
    print(f"Bench venv on disk: {venv / 1e6:.0f} MB")
    print(f"Still to write: models {total / 1e6:.0f} MB + datasets ≈{DATASETS_EST / 1e6:.0f} MB"
          f" + signals ≈{SIGNALS_EST / 1e6:.0f} MB = {need / 1e9:.2f} GB")
    print(f"Free: {c.free_bytes / GIB:.1f} GiB → after: {(c.free_bytes - need) / GIB:.1f} GiB "
          f"(floor {FLOOR_BYTES / GIB:.0f} GiB) → {'OK' if c.ok else 'REFUSED'}")
    return 0 if c.ok else 1


def cmd_fetch(a: argparse.Namespace) -> int:
    if not a.confirm:
        print("Refusing to download without --confirm (run `plan` to see exact sizes).")
        return 2
    for k in parse_models(a.models):
        spec = models.REGISTRY[k]
        try:
            print(f"whisper-{k}: {models.fetch(spec)}")
        except InsufficientSpace as e:
            print(f"whisper-{k}: SKIPPED — {e}")
            return 1
    return 0


def _tts(a: argparse.Namespace):
    cache = bench_dir() / "tts-cache"
    if getattr(a, "tone", False):
        return ToneTTS(cache / "tone")
    if not SayTTS.available():
        raise SystemExit("macOS `say` not found; real datasets must be built on the Mac.")
    return SayTTS(cache / "say")


def build_datasets(a: argparse.Namespace) -> list[Path]:
    require_space(bench_dir(), DATASETS_EST, "dataset build")
    tts = _tts(a)
    sets = dataset.QUICK_SETS if a.quick else dataset.DEFAULT_SETS
    out = []
    for s in sets:
        t0 = time.perf_counter()
        d = dataset.build(s, tts, bench_dir() / "datasets" / tts.name)
        print(f"  dataset {s.name}: {time.perf_counter() - t0:.1f} s → {d}")
        out.append(d)
    return out


def score(truth: dict, hyp: dict) -> dict:
    if truth["kind"] == "timing":
        return score_timing_set(truth["words"], hyp["words"])
    return wer(truth["text"], hyp["text"])


def transcribe_all(a: argparse.Namespace, dirs: list[Path], out: Path, res: dict) -> None:
    py = a.python or str(BENCH_VENV_PY)
    for k in parse_models(a.models):
        spec = models.REGISTRY[k]
        problems = models.verify(spec)
        if problems:
            print(f"  whisper-{k}: not available ({problems}); run fetch-models first.")
            continue
        for d in dirs:
            truth = json.loads((d / "truth.json").read_text())
            hyp_path = out / "hyp" / f"{k}__{truth['name']}.json"
            hyp_path.parent.mkdir(parents=True, exist_ok=True)
            before = sysinfo.snapshot()
            r = runner.transcribe(py, spec.local_dir, d / "audio.wav", hyp_path)
            row = {"model": k, "dataset": truth["name"], "kind": truth["kind"],
                   "variant": truth["variant"], "audio_s": truth["duration_ms"] / 1000,
                   "returncode": r["returncode"], "wall_s": r["wall_s"],
                   "max_rss_bytes": r.get("max_rss_bytes"),
                   "peak_footprint_bytes": r.get("peak_footprint_bytes"),
                   "thermal_before": before.get("thermal"),
                   "thermal_after": sysinfo.snapshot().get("thermal"),
                   "swap_after": sysinfo.snapshot().get("swap")}
            if r["returncode"] == 0:
                h = r["result"]
                row.update(load_s=h["load_s"], transcribe_s=h["transcribe_s"],
                           rtf=h["transcribe_s"] / row["audio_s"],
                           mlx_peak_bytes=h.get("mlx_peak_bytes"), scores=score(truth, h))
                for key in ("mlx_version", "mlx_whisper_version", "torch_installed"):
                    res[key] = h.get(key)
                print(f"  {k:<5} {truth['name']:<18} RTF {row['rtf']:.3f}  "
                      f"WER {row['scores']['wer'] * 100:5.1f}%")
            else:
                row["stderr_tail"] = r.get("stderr_tail")
                print(f"  {k:<5} {truth['name']:<18} FAILED rc={r['returncode']}")
            res["transcription"].append(row)
            save(out, res)


def save(out: Path, res: dict) -> None:
    tmp = out / "results.json.tmp"
    tmp.write_text(json.dumps(res, indent=1, default=str))
    tmp.replace(out / "results.json")


def finalize(out: Path, res: dict) -> None:
    by_model: dict[str, list[dict]] = {}
    for r in res["transcription"]:
        by_model.setdefault(r["model"], []).append(r)
    res["evaluation"] = {m: report.evaluate_model(rs) for m, rs in by_model.items()}
    res["recommendation"] = report.recommend(res["evaluation"])
    res["disk"] = {
        "bench_venv": dir_size(ROOT / "bench" / ".venv"),
        "models": dir_size(models_dir()),
        "datasets_and_tts_cache": dir_size(bench_dir() / "datasets")
        + dir_size(bench_dir() / "tts-cache"),
        "signals_work": dir_size(bench_dir() / "signals"),
        "results": dir_size(out),
    }
    res["disk"]["total"] = sum(res["disk"].values())
    res["finished_utc"] = now()
    res["free_after_gib"] = sysinfo.snapshot().get("free_gib")
    save(out, res)
    (out / "results.md").write_text(report.markdown(res))
    print(f"\nResults: {out / 'results.md'}")
    print(json.dumps(res["recommendation"], indent=1))


def default_out() -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return ROOT.parent / "riftwire-env" / f"bench-{stamp}"


def cmd_all(a: argparse.Namespace) -> int:
    out = Path(a.out).expanduser() if a.out else default_out()
    out.mkdir(parents=True, exist_ok=True)
    res: dict = {"started_utc": now(), "machine": sysinfo.machine(), "transcription": [],
                 "args": {k: v for k, v in vars(a).items() if k != "func"}}
    save(out, res)
    if not a.skip_transcription:
        print("▸ Datasets")
        dirs = build_datasets(a)
        print("▸ Transcription")
        transcribe_all(a, dirs, out, res)
    if not a.skip_signals:
        print("▸ Signals")
        require_space(bench_dir(), SIGNALS_EST, "signals benchmark")
        spec = signals.VideoSpec(seconds=60, width=1280, height=720) if a.quick \
            else signals.VideoSpec()
        res["signals"] = signals.run(spec, bench_dir() / "signals")
        save(out, res)
    finalize(out, res)
    return 0


def cmd_report(a: argparse.Namespace) -> int:
    out = Path(a.results_dir).expanduser()
    res = json.loads((out / "results.json").read_text())
    finalize(out, res)
    return 0


def cmd_clean(a: argparse.Namespace) -> int:
    """Remove regenerable benchmark artefacts (datasets, TTS cache, signal videos)."""
    target = bench_dir()
    if not a.confirm:
        print(f"Would remove {target} ({dir_size(target) / 1e6:.0f} MB). Re-run with --confirm.")
        return 2
    shutil.rmtree(target, ignore_errors=True)
    print(f"Removed {target}. Models in {models_dir()} were kept.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan").set_defaults(func=cmd_plan)
    f = sub.add_parser("fetch-models")
    f.add_argument("--models", default="tiny,base")
    f.add_argument("--confirm", action="store_true")
    f.set_defaults(func=cmd_fetch)
    al = sub.add_parser("all")
    al.add_argument("--models", default="tiny,base")
    al.add_argument("--out")
    al.add_argument("--python", help="interpreter with mlx-whisper (default bench/.venv)")
    al.add_argument("--quick", action="store_true", help="1-minute sets, 60 s 720p video")
    al.add_argument("--tone", action="store_true", help=argparse.SUPPRESS)  # tests only
    al.add_argument("--skip-transcription", action="store_true")
    al.add_argument("--skip-signals", action="store_true")
    al.set_defaults(func=cmd_all)
    r = sub.add_parser("report")
    r.add_argument("results_dir")
    r.set_defaults(func=cmd_report)
    c = sub.add_parser("clean")
    c.add_argument("--confirm", action="store_true")
    c.set_defaults(func=cmd_clean)
    a = p.parse_args(argv)
    try:
        return a.func(a)
    except InsufficientSpace as e:
        print(f"✗ {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
