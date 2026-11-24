"""Turn raw results into a summary table, target checks and a recommendation.

Targets come from docs/PHASE2_PROPOSAL.md (M0). The recommendation is mechanical and
explained; the owner and an independent reviewer make the decision.
"""

from __future__ import annotations

import math

GB = 1000**3
TARGETS = {
    "rtf_max": 0.15,              # transcribe seconds / audio seconds (lower is faster)
    "wer_clean_max": 0.05,
    "wer_noisy_max": 0.12,
    "start_median_ms_max": 80,
    "start_p95_ms_max": 250,
    "peak_memory_bytes_max": 3 * GB,  # proposal: peak RSS ≤ 3 GB
}
TTS_ARTIFACTS = ("capital",)  # words macOS `say` may insert; must never appear in hypotheses


def _fmt(x: float | None, pct: bool = False, nd: int = 3) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{x * 100:.1f}%" if pct else f"{x:.{nd}f}" if isinstance(x, float) else str(x)


def peak_memory(run: dict) -> int | None:
    """Process peak footprint (includes Metal buffers and MLX's cache): the strict figure."""
    return run.get("peak_footprint_bytes") or run.get("max_rss_bytes")


def tts_artifacts(runs: list[dict]) -> int:
    return sum(r.get("tts_artifact_words", 0) for r in runs)


def evaluate_model(runs: list[dict]) -> dict:
    """runs: results for one model across datasets (each with dataset meta + scores)."""
    ok = [r for r in runs if r.get("returncode") == 0]
    rtfs = [r["rtf"] for r in ok if r.get("rtf") is not None]
    # WER is judged on natural speech; timing sets (isolated words) are for timing only.
    nat = [r for r in ok if r["kind"] == "natural"]
    clean = [r["scores"]["wer"] for r in nat if r["variant"] == "clean"]
    noisy = [r["scores"]["wer"] for r in nat if r["variant"] != "clean"]
    t_runs = [r for r in ok if r["kind"] == "timing" and r["variant"] == "clean"]
    med = max((r["scores"]["start_median_ms"] for r in t_runs), default=None)
    p95 = max((r["scores"]["start_p95_ms"] for r in t_runs), default=None)
    rss = max((r.get("max_rss_bytes") or 0 for r in ok), default=0) or None
    mem = max((peak_memory(r) or 0 for r in ok), default=0) or None
    mlx = max((r.get("mlx_peak_bytes") or 0 for r in ok), default=0) or None
    s = {
        "runs": len(runs), "failed": len(runs) - len(ok),
        "rtf_worst": max(rtfs) if rtfs else None,
        "wer_clean_worst": max(clean) if clean else None,
        "wer_noisy_worst": max(noisy) if noisy else None,
        "start_median_ms_worst": med, "start_p95_ms_worst": p95,
        "start_bias_median_ms": max((r["scores"]["start_bias_median_ms"] for r in t_runs),
                                    default=None, key=lambda x: abs(x)),
        "end_bias_median_ms": max((r["scores"]["end_bias_median_ms"] for r in t_runs),
                                  default=None, key=lambda x: abs(x)),
        "peak_rss_bytes": rss, "peak_footprint_bytes": mem, "mlx_peak_bytes": mlx,
        "tts_artifact_words": tts_artifacts(runs),
    }
    checks = {
        "speed": s["rtf_worst"] is not None and s["rtf_worst"] <= TARGETS["rtf_max"],
        "wer_clean": s["wer_clean_worst"] is not None
        and s["wer_clean_worst"] <= TARGETS["wer_clean_max"],
        "wer_noisy": s["wer_noisy_worst"] is None
        or s["wer_noisy_worst"] <= TARGETS["wer_noisy_max"],
        "timing": med is not None and p95 is not None
        and med <= TARGETS["start_median_ms_max"] and p95 <= TARGETS["start_p95_ms_max"],
        "memory": rss is not None and rss <= TARGETS["peak_memory_bytes_max"],
        "no_failures": s["failed"] == 0,
        "harness_clean": s["tts_artifact_words"] == 0,
    }
    return {**s, "checks": checks, "meets_all": all(checks.values())}


ORDER = ("tiny", "base", "small")


def recommend(per_model: dict[str, dict]) -> dict:
    passing = [m for m in ORDER if m in per_model and per_model[m]["meets_all"]]
    if passing:
        # Most accurate passing model (accuracy drives highlight evidence quality);
        # speed is already within target for all of them.
        best = min(passing, key=lambda m: (per_model[m]["wer_noisy_worst"] or 0,
                                           per_model[m]["wer_clean_worst"] or 0))
        why = (f"{best} meets every M0 target and has the lowest WER among passing models "
               f"({', '.join(passing)}).")
        return {"model": best, "engine": "mlx-whisper", "status": "meets_targets",
                "reason": why}
    tested = [m for m in ORDER if m in per_model]
    if not tested:
        return {"model": None, "engine": None, "status": "no_results", "reason": "No runs."}
    best = min(tested, key=lambda m: (sum(not v for v in per_model[m]["checks"].values()),
                                      per_model[m]["wer_noisy_worst"] or 1.0,
                                      per_model[m]["wer_clean_worst"] or 1.0))
    fails = [k for k, v in per_model[best]["checks"].items() if not v]
    return {"model": best, "engine": "mlx-whisper", "status": "no_model_meets_all_targets",
            "reason": f"No model met all targets; {best} fails the fewest ({', '.join(fails)}). "
                      "Owner decision required before M1."}


def markdown(results: dict) -> str:
    lines = ["# RIFTWIRE M0 benchmark results", ""]
    m = results.get("machine", {})
    lines += [f"- Machine: {m.get('chip')} · {m.get('memory_gb')} GB · macOS {m.get('macos')}",
              f"- Python {m.get('python')} · {m.get('ffmpeg')}",
              f"- Engine: mlx-whisper {results.get('mlx_whisper_version')} · "
              f"mlx {results.get('mlx_version')} · torch installed: "
              f"{results.get('torch_installed')}",
              f"- Started {results.get('started_utc')} · finished {results.get('finished_utc')}",
              "", "## Transcription", "",
              "| model | dataset | audio min | RTF | WER | start med ms | start p95 ms "
              "| start bias ms | end bias ms | ≤100 ms | RSS GB | footprint GB | MLX GB |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results.get("transcription", []):
        sc = r.get("scores", {})
        def gb(x):
            return _fmt(x / GB if x else None, nd=2)
        lines.append(
            f"| {r['model']} | {r['dataset']} | {r['audio_s'] / 60:.1f} | {_fmt(r.get('rtf'))} "
            f"| {_fmt(sc.get('wer'), pct=True)} | {_fmt(sc.get('start_median_ms'), nd=0)} "
            f"| {_fmt(sc.get('start_p95_ms'), nd=0)} "
            f"| {_fmt(sc.get('start_bias_median_ms'), nd=0)} "
            f"| {_fmt(sc.get('end_bias_median_ms'), nd=0)} "
            f"| {_fmt(sc.get('start_within_100ms'), pct=True)} "
            f"| {gb(r.get('max_rss_bytes'))} | {gb(r.get('peak_footprint_bytes'))} "
            f"| {gb(r.get('mlx_peak_bytes'))} |"
            if r.get("returncode") == 0 else
            f"| {r['model']} | {r['dataset']} | — | FAILED (rc {r.get('returncode')}) "
            "| | | | | | | | | |")
    lines += ["", "## Targets per model", "",
              "| model | speed | WER clean | WER noisy | timing | memory (RSS) | no failures "
              "| harness clean | all |",
              "|---|---|---|---|---|---|---|---|---|"]
    for name, ev in results.get("evaluation", {}).items():
        c = ev["checks"]
        mark = {True: "✅", False: "❌"}
        lines.append(f"| {name} | " + " | ".join(mark[c[k]] for k in (
            "speed", "wer_clean", "wer_noisy", "timing", "memory", "no_failures",
            "harness_clean"))
            + f" | {mark[ev['meets_all']]} |")
    rec = results.get("recommendation", {})
    lines += ["", f"**Recommendation:** `{rec.get('engine')}` / `{rec.get('model')}` "
              f"({rec.get('status')}). {rec.get('reason')}", ""]
    sig = results.get("signals")
    if sig:
        lines += ["## Signals (synthetic video)", "",
                  f"- Source: {sig['video']['spec']} · encoder {sig['encoder']} · "
                  f"build {sig['video']['build_s']} s",
                  f"- Proxy: {sig['proxy']['wall_s']} s ({sig['proxy']['realtime_x']}× realtime)"]
        for k in ("scene_cuts", "loudness_bursts"):
            s = sig[k]
            lines.append(f"- {k}: recall {_fmt(s['recall'], pct=True)}, precision "
                         f"{_fmt(s['precision'], pct=True)}, median error "
                         f"{_fmt(s['median_error_ms'], nd=0)} ms, {s['realtime_x']}× realtime")
        lines.append("")
    d = results.get("disk", {})
    if d:
        lines += ["## Disk usage", ""] + [f"- {k}: {v / GB:.2f} GB" for k, v in d.items()
                                         if isinstance(v, int)] + [""]
    lines += ["## Limitations", "",
              "- Speech is synthesized with macOS `say`: cleaner and more regular than real "
              "commentary. Results measure engine accuracy, timing, speed and memory, not "
              "highlight usefulness.",
              "- Word-timing ground truth exists only for the `timing-*` sets (words "
              "synthesized individually and concatenated with known gaps). WER targets are "
              "judged on the `natural-*` sets; timing-set WER is shown for information.",
              "- Memory target (proposal): peak RSS ≤ 3 GB. On Apple Silicon RSS excludes "
              "Metal allocations, so the process peak footprint (includes MLX's buffer cache) "
              "and MLX's own peak are reported too.",
              "- Decoding uses Whisper's temperature fallback, so repeated runs can differ "
              "slightly.",
              "- Authorized real gameplay remains a separate evaluation set (owner condition 4).",
              ""]
    return "\n".join(lines)
