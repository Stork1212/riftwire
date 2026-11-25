"""Local-model benchmark for RIFTWIRE's grounded highlight task (M5 preparation).

    backend/.venv/bin/python bench/llm/llm_bench.py --model qwen3.5:4b --out DIR [--repeat 2]

Talks only to a loopback Ollama server. For each model it records identity (digest,
quantisation), load time, prompt and output speed, memory (Ollama's resident size, runner
RSS, swap) and, on the synthetic tasks in ``tasks.py``, structured-output validity, grounding
errors, selection against the author's labels, injection resistance, padding and
over-claiming. A keyword heuristic runs on the same tasks as a reference.

Results are synthetic engineering measurements, not highlight-quality evidence.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import platform
import re
import statistics
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "backend"))
sys.path.insert(0, str(HERE))

from app.ai import prompts, scoring  # noqa: E402
from app.ai.analyze import evaluate  # noqa: E402
from app.ai.providers.base import ProviderError  # noqa: E402
from app.ai.providers.ollama import OllamaProvider  # noqa: E402
from app.ai.scoring import Ranking  # noqa: E402
from tasks import ALL_TASKS, Task, build  # noqa: E402

TASKS_BY_NAME = {t.name: t for t in ALL_TASKS}
MIN_ACCEPTANCE = 0.8

_CUES = re.compile(r"\b(?:oh my god|no way|what the|holy|wait|we got a problem|police|cops|"
                   r"chase|getaway|escape|crash|stuck|jump|blew up|make it|no damage)\b", re.I)


# ---------------------------------------------------------------------------- memory
def _sh(args: list[str]) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def swap_used_mb() -> float | None:
    m = re.search(r"used = ([\d.]+)M", _sh(["sysctl", "-n", "vm.swapusage"]))
    return float(m.group(1)) if m else None


def ollama_rss_mb() -> float:
    total = 0
    for line in _sh(["ps", "-axo", "rss=,comm="]).splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and "ollama" in parts[1]:
            total += int(parts[0])
    return round(total / 1024, 1)


def free_mb() -> float | None:
    """Available memory: free + inactive + speculative + purgeable pages. ("Pages free"
    alone is near zero on a healthy Mac, because macOS keeps memory in use as cache.)"""
    out = _sh(["vm_stat"])
    page = re.search(r"page size of (\d+)", out)
    if not page:
        return None
    pages = 0
    for label in ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable"):
        m = re.search(label + r":\s+(\d+)", out)
        pages += int(m.group(1)) if m else 0
    return round(pages * int(page.group(1)) / 2**20, 1)


def pressure_free_pct() -> int | None:
    m = re.search(r"free percentage: (\d+)%", _sh(["memory_pressure", "-Q"]))
    return int(m.group(1)) if m else None


class Sampler(threading.Thread):
    """OS memory sampling on its own thread, every 0.25 s. It never calls Ollama: in the
    first Mac run the sampler's /api/ps call blocked for the whole 2.3 s model load, so the
    swap peak during loading was never sampled. Ollama's resident size is sampled by a
    second thread with a short timeout."""

    def __init__(self, provider: OllamaProvider, every: float = 0.25):
        super().__init__(daemon=True)
        self.provider, self.every = provider, every
        self.stop_flag = threading.Event()
        self.peak_rss = 0.0
        self.peak_swap = 0.0
        self.min_free: float | None = None
        self.peak_loaded = 0
        self.timeline: list[tuple[float, float | None, float | None]] = []
        self._t0 = time.perf_counter()
        self._ps = threading.Thread(target=self._poll_loaded, daemon=True)

    def _poll_loaded(self) -> None:
        while not self.stop_flag.is_set():
            try:
                size = sum(m.get("size", 0) for m in self.provider.loaded())
                self.peak_loaded = max(self.peak_loaded, size)
            except ProviderError:
                pass
            self.stop_flag.wait(1.0)

    def run(self) -> None:
        self._ps.start()
        while not self.stop_flag.is_set():
            self.peak_rss = max(self.peak_rss, ollama_rss_mb())
            s, f = swap_used_mb(), free_mb()
            if s is not None:
                self.peak_swap = max(self.peak_swap, s)
            if f is not None:
                self.min_free = f if self.min_free is None else min(self.min_free, f)
            if len(self.timeline) < 20_000:
                self.timeline.append((round(time.perf_counter() - self._t0, 2), s, f))
            self.stop_flag.wait(self.every)


# --------------------------------------------------------------------------- scoring
def score_task(task: Task, bundle, outcome, ranking: Ranking | None = None) -> dict:
    ranking = ranking or Ranking()
    chosen: set[int] = set()
    ranked: set[int] = set()
    editorial: dict[str, int] = {}
    payoff_ok: dict[str, bool] = {}
    claims = ""
    if outcome.value:
        for c in outcome.value.candidates:
            chosen.add(int(c.window_id.split(":")[1]) - 1)
            editorial[c.window_id] = ranking.score(c)
            claims += " ".join([c.what_happens, c.why_entertaining, c.title, c.hook,
                                *(e.claim for e in c.evidence)]).lower() + " "
        ranked = {int(c.window_id.split(":")[1]) - 1
                  for c, _ in ranking.rank(list(outcome.value.candidates))}
    by_id = {w.id: w for w in bundle.words}
    for wi, word in task.payoff_words.items():
        clip = next((c for c in outcome.clips if c.window_id == f"c:{wi + 1}"), None)
        target = next((w for w in by_id.values() if w.text == word), None)
        payoff_ok[f"c:{wi + 1}"] = bool(clip and target and clip.end_ms >= target.t1
                                         and clip.start_ms <= target.t0)
    text = " ".join(w.text.lower() for w in bundle.words)
    unsupported = [w for w in task.forbidden_claims if w in claims and w not in text]
    tp = len(chosen & task.highlights)
    rtp = len(ranked & task.highlights)
    return {
        # After the editorial ranking cut (app.ai.scoring, min_score): what M6 would keep.
        "ranked": sorted(f"c:{i + 1}" for i in ranked),
        "editorial_scores": editorial,
        "ranked_precision": round(rtp / len(ranked), 3) if ranked else (
            1.0 if not task.highlights else None),
        "ranked_recall": round(rtp / len(task.highlights), 3) if task.highlights else 1.0,
        "ranked_padded": len(ranked - task.highlights) > 0,
        "chosen": sorted(f"c:{i + 1}" for i in chosen),
        "expected": sorted(f"c:{i + 1}" for i in task.highlights),
        # Precision is undefined when nothing was chosen but highlights existed (None).
        "precision": round(tp / len(chosen), 3) if chosen else (1.0 if not task.highlights
                                                                else None),
        "recall": round(tp / len(task.highlights), 3) if task.highlights else 1.0,
        "trap_selected": bool(chosen & task.must_not),
        "padded": len(chosen - task.highlights) > 0,
        "empty_correct": (not task.highlights and not chosen) if not task.highlights else None,
        "payoff_kept": payoff_ok or None,
        "unsupported_claims": unsupported,
    }


def heuristic(task: Task, bundle) -> dict:
    chosen = set()
    for wi, w in enumerate(bundle.windows):
        text = " ".join(x.text for x in bundle.words if w.t0 <= x.t0 <= w.t1)
        if len(_CUES.findall(text)) >= 2:
            chosen.add(wi)
    tp = len(chosen & task.highlights)
    return {"chosen": sorted(f"c:{i + 1}" for i in chosen),
            "precision": round(tp / len(chosen), 3) if chosen else (
                1.0 if not task.highlights else None),
            "recall": round(tp / len(task.highlights), 3) if task.highlights else 1.0,
            "trap_selected": bool(chosen & task.must_not)}


def _mean(values: list) -> float | None:
    vals = [v for v in values if v is not None]
    return round(statistics.mean(vals), 3) if vals else None


# ------------------------------------------------------------------------------ main
def _pct(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile (no interpolation): honest for small samples."""
    if not values:
        return None
    v = sorted(values)
    rank = math.ceil(len(v) * q)                  # 1-based nearest rank
    return round(v[min(len(v), max(1, rank)) - 1], 2)


def latency(runs: list[dict]) -> dict:
    walls = [r["wall_s"] for r in runs]
    gens = [m["total_ms"] / 1000 for r in runs for m in r["metrics"] if m.get("total_ms")]
    first = [r["metrics"][0]["total_ms"] / 1000 for r in runs
             if r["metrics"] and r["metrics"][0].get("total_ms")]
    repair_s = sum(m["total_ms"] / 1000 for r in runs for m in r["metrics"][1:]
                   if m.get("total_ms"))
    return {
        "evaluation_s": {"median": _pct(walls, 0.5), "p95": _pct(walls, 0.95),
                         "max": max(walls) if walls else None, "n": len(walls)},
        "generation_s": {"median": _pct(gens, 0.5), "p95": _pct(gens, 0.95),
                         "max": round(max(gens), 2) if gens else None, "n": len(gens)},
        "first_attempt_s": {"median": _pct(first, 0.5), "p95": _pct(first, 0.95),
                            "max": round(max(first), 2) if first else None},
        "attempts_histogram": dict(sorted(collections.Counter(
            r["attempts"] for r in runs).items())),
        "repair_share_of_generation_time": round(repair_s / sum(gens), 3) if gens else None,
        "output_tokens": {"median": _pct([m.get("output_tokens", 0) for r in runs
                                          for m in r["metrics"]], 0.5),
                          "max": max((m.get("output_tokens", 0) for r in runs
                                      for m in r["metrics"]), default=None)},
    }


def _pipeline_recall(runs: list[dict]) -> float | None:
    found = total = 0
    for r in runs:
        truth = TASKS_BY_NAME[r["task"]].highlights
        if not truth:
            continue
        total += len(truth)
        if r["status"] == "accepted":
            found += round(r["score"]["ranked_recall"] * len(truth))
    return round(found / total, 3) if total else None


def summarize(runs: list[dict]) -> dict:
    ok = [r for r in runs if r["status"] == "accepted"]
    precisions = [r["score"]["precision"] for r in ok if r["score"]["precision"] is not None]
    gen = [m for r in runs for m in r["metrics"] if m.get("output_ms")]
    tps = [m["output_tokens"] / (m["output_ms"] / 1000) for m in gen if m["output_ms"] > 0]
    pps = [m["prompt_tokens"] / (m["prompt_ms"] / 1000) for m in gen if m.get("prompt_ms")]
    errors = collections.Counter(r["record"]["error"] for r in runs if r["status"] != "accepted")
    generated = [m for r in runs for m in r["metrics"] if m.get("output_tokens", 0) > 0]
    return {
        "tasks_run": len(runs),
        "inference_verified": bool(generated),
        "generations_with_output": len(generated),
        "output_constraint": sorted({str(m.get("output_constraint")) for m in generated}),
        "constraint_notes": sorted({m["constraint_note"] for m in generated
                                    if m.get("constraint_note")}),
        "thinking_chars_max": max((m.get("thinking_chars", 0) for m in generated), default=0),
        "errors_by_message": dict(errors.most_common()),
        "accepted": len(ok),
        "accepted_with_candidates": sum(bool(r["score"]["chosen"]) for r in ok),
        "highlight_tasks": sum(bool(TASKS_BY_NAME[r["task"]].highlights) for r in runs),
        "valid_first_try": sum(r["valid_first_try"] for r in runs),
        "failed_validation": sum(r["status"] == "failed_validation" for r in runs),
        "provider_errors": sum(r["status"] == "provider_error" for r in runs),
        "mean_precision": round(statistics.mean(precisions), 3) if precisions else None,
        # Conditional: averaged over ACCEPTED answers only. Not pipeline recall.
        "mean_recall": round(statistics.mean(r["score"]["recall"] for r in ok), 3)
        if ok else None,
        # Pipeline-level: over every highlight-bearing evaluation, a failed or rejected
        # evaluation counting as zero highlights found.
        "pipeline_highlight_recall": _pipeline_recall(runs),
        "highlight_evaluations_with_highlight_selected": sum(
            1 for r in runs if TASKS_BY_NAME[r["task"]].highlights
            and r["status"] == "accepted" and r["score"]["recall"] > 0),
        "trap_selected": sum(r["score"]["trap_selected"] for r in runs),
        "padded": sum(r["score"]["padded"] for r in ok),
        "ranked_padded": sum(r["score"]["ranked_padded"] for r in ok),
        "ranked_mean_precision": _mean([r["score"]["ranked_precision"] for r in ok]),
        "ranked_mean_recall": _mean([r["score"]["ranked_recall"] for r in ok]),
        "unsupported_claims": sum(len(r["score"]["unsupported_claims"]) for r in runs),
        "payoff_kept": [v for r in ok for v in (r["score"]["payoff_kept"] or {}).values()],
        "output_tok_per_s_median": round(statistics.median(tps), 1) if tps else None,
        "prompt_tok_per_s_median": round(statistics.median(pps), 1) if pps else None,
        "latency": latency(runs),
    }


def run_model(model: str, args, prompt) -> dict:
    p = OllamaProvider(model, base_url=args.base_url, keep_alive="10m")
    ident = p.identity()
    ranking = scoring.resolve(game="gta", channel="rumbleframe", prompt=prompt.id)
    swap0, free0, rss0, press0 = swap_used_mb(), free_mb(), ollama_rss_mb(), pressure_free_pct()
    sampler = Sampler(p)
    sampler.start()
    runs = []
    split = getattr(args, "split", "all")
    try:
        for rep in range(args.repeat):
            for task in ALL_TASKS:
                if args.tasks and task.name not in args.tasks:
                    continue
                if split != "all" and task.split != split:
                    continue
                bundle, _ = build(task)
                t0 = time.perf_counter()
                out = evaluate(bundle, p, prompt, target_count=task.target_count,
                               channel="rumbleframe", max_repairs=args.max_repairs,
                               timeout_s=args.timeout, identity=ident, ranking=ranking,
                               game="gta", keep_alive_during="10m", unload_after=False)
                wall = round(time.perf_counter() - t0, 2)
                row = {"task": task.name, "split": task.split, "repeat": rep,
                       "status": out.status, "wall_s": wall, "attempts": len(out.attempts),
                       "valid_first_try": bool(out.attempts and out.attempts[0].ok),
                       "errors_by_attempt": [a.errors[:4] for a in out.attempts],
                       "provider_error": out.error if out.status == "provider_error" else None,
                       "metrics": [a.metrics for a in out.attempts],
                       "score": score_task(task, bundle, out, ranking),
                       "record": out.record()}
                runs.append(row)
                why = f" error={out.error}" if out.status != "accepted" else ""
                print(f"  {model} [{task.split}] {task.name} r{rep}: {out.status} "
                      f"attempts={len(out.attempts)} chosen={row['score']['chosen']} "
                      f"kept={row['score']['ranked']} wall={wall}s{why}", flush=True)
    finally:
        sampler.stop_flag.set()
        sampler.join(2)
        try:
            p.unload()
        except ProviderError:
            pass
    loads = [m["load_ms"] for r in runs for m in r["metrics"][:1]]
    summary = summarize(runs)
    summary.update({
        "ranking": ranking.identity(),
        "first_load_ms": loads[0] if loads else None,
        "wall_s_median": summary["latency"]["evaluation_s"]["median"],
        "splits": {sp: summarize([r for r in runs if r["split"] == sp])
                   for sp in sorted({r["split"] for r in runs})},
        "memory": {"ollama_loaded_peak_mb": round(sampler.peak_loaded / 2**20, 1),
                   "ollama_rss_peak_mb": sampler.peak_rss, "ollama_rss_before_mb": rss0,
                   "swap_used_before_mb": swap0, "swap_used_peak_mb": sampler.peak_swap,
                   "swap_used_after_mb": swap_used_mb(), "free_before_mb": free0,
                   "free_min_mb": sampler.min_free,
                   "pressure_free_pct_before": press0,
                   "pressure_free_pct_after": pressure_free_pct(),
                   "timeline_s_swapmb_availmb": sampler.timeline},
    })
    return {"model": model, "identity": ident.__dict__, "runs": runs, "summary": summary}


def gate(model_report: dict) -> list[str]:
    """Reasons a model run does NOT verify local inference (empty list = verified).

    Zero accepted answers, any provider error, or no generation with output tokens is a
    failure, whatever else passed. (The first Mac run reported ALL PASSED with 10/10
    provider errors because only a missing model counted as an error.)"""
    if "error" in model_report:
        return [f"model error: {model_report['error']}"]
    s = model_report["summary"]
    reasons = []
    if not s.get("inference_verified"):
        reasons.append("no generation produced output tokens")
    if s.get("provider_errors"):
        reasons.append(f"{s['provider_errors']} provider error(s): "
                       + "; ".join(list(s.get("errors_by_message", {}))[:3]))
    if not s.get("accepted"):
        reasons.append("zero accepted answers")
    return reasons


def _split_failures(s: dict, label: str) -> list[str]:
    reasons = []
    rate = s["accepted"] / s["tasks_run"] if s.get("tasks_run") else 0.0
    if rate < MIN_ACCEPTANCE:
        reasons.append(f"{label}: acceptance {s['accepted']}/{s['tasks_run']} below "
                       f"{MIN_ACCEPTANCE:.0%}")
    if s.get("highlight_tasks") and not s.get("accepted_with_candidates"):
        reasons.append(f"{label}: no accepted answer selected a highlight")
    if s.get("ranked_padded"):
        reasons.append(f"{label}: {s['ranked_padded']} answer(s) still padded after the cut")
    rec, rrec = s.get("mean_recall"), s.get("ranked_mean_recall")
    if rec is not None and rec < 1.0:
        reasons.append(f"{label}: the model missed real highlights (recall {rec})")
    if rec is not None and rrec is not None and rrec < rec:
        reasons.append(f"{label}: the cut dropped real highlights (recall {rec} -> {rrec})")
    if s.get("trap_selected"):
        reasons.append(f"{label}: injection trap selected {s['trap_selected']} time(s)")
    if s.get("unsupported_claims"):
        reasons.append(f"{label}: {s['unsupported_claims']} unsupported claim(s)")
    return reasons


def benchmark_gate(model_report: dict) -> list[str]:
    """Reasons the grounded-task benchmark fails, given that inference works.

    Evaluated on every split separately (the dev scenarios that prompt v2 and the cut were
    developed against, and the held-out ones), so passing cannot come from tuning to the
    dev set alone. Accepting "nothing" proves little (second Mac run), so the gate also
    requires accepted answers that select highlights, nothing padded after the ranking cut,
    no real highlight dropped by it, no trap and no unsupported claim."""
    if "error" in model_report:
        return [f"model error: {model_report['error']}"]
    s = model_report["summary"]
    splits = s.get("splits") or {"all": s}
    reasons = []
    for label, sub in splits.items():
        reasons += _split_failures(sub, label)
    return reasons


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--model", action="append", default=None)
    ap.add_argument("--base-url", default="http://127.0.0.1:11434")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--max-repairs", type=int, default=2)
    ap.add_argument("--timeout", type=float, default=300)
    ap.add_argument("--tasks", nargs="*")
    ap.add_argument("--split", choices=["all", "dev", "holdout"], default="all")
    ap.add_argument("--prompt", default="gaming.gta.v2")
    args = ap.parse_args()
    models = args.model or ["qwen3.5:4b"]
    args.out.mkdir(parents=True, exist_ok=True)
    prompt = prompts.load(args.prompt)
    report = {"schema": "riftwire.llm_bench.v1", "started": datetime.now(UTC).isoformat(),
              "host": {"platform": platform.platform(), "machine": platform.machine(),
                       "python": platform.python_version()},
              "prompt": {"id": prompt.id, "version": prompt.version, "sha256": prompt.sha256},
              "note": "Synthetic author-written tasks: engineering measurements only, not "
                      "highlight quality.",
              "heuristic": {t.name: heuristic(t, build(t)[0]) for t in ALL_TASKS},
              "models": []}
    for m in models:
        print(f"== {m}", flush=True)
        try:
            report["models"].append(run_model(m, args, prompt))
        except ProviderError as e:
            report["models"].append({"model": m, "error": f"{e.code}: {e}"})
            print(f"  {m}: {e.code}: {e}", flush=True)
    report["finished"] = datetime.now(UTC).isoformat()
    for m in report["models"]:
        m["gate_failures"] = gate(m)
        m["verified"] = not m["gate_failures"]
        m["benchmark_failures"] = benchmark_gate(m) if m["verified"] else ["inference not verified"]
        m["benchmark_passed"] = not m["benchmark_failures"]
    (args.out / "llm-bench.json").write_text(json.dumps(report, indent=2, default=str))
    lines = [f"# LLM bench ({report['started']})", "", report["note"], ""]
    for m in report["models"]:
        lines.append(f"## {m['model']}")
        if "error" in m:
            lines += [f"error: {m['error']}", ""]
            continue
        lines.append(f"inference verified: {m['verified']}  failures: {m['gate_failures']}")
        lines.append(f"benchmark passed: {m['benchmark_passed']}  failures: "
                     f"{m['benchmark_failures']}")
        summ = {k: v for k, v in m["summary"].items() if k != "memory"}
        mem = {k: v for k, v in m["summary"]["memory"].items() if k != "timeline_s_swapmb_availmb"}
        lines.append("```json\n" + json.dumps({"identity": m["identity"], **summ, "memory": mem},
                                              indent=2, default=str) + "\n```")
    lines += ["## Keyword heuristic (reference)", "```json",
              json.dumps(report["heuristic"], indent=2), "```"]
    (args.out / "llm-bench.md").write_text("\n".join(lines) + "\n")
    print(f"report: {args.out / 'llm-bench.json'}")
    for m in report["models"]:
        print(f"INFERENCE {m['model']}: " + ("VERIFIED" if m["verified"] else
              "NOT VERIFIED - " + "; ".join(m["gate_failures"])))
        print(f"BENCHMARK {m['model']}: " + ("PASSED" if m["benchmark_passed"] else
              "FAILED - " + "; ".join(m["benchmark_failures"])))
    # 0 = inference verified and benchmark passed; 3 = inference verified, benchmark failed;
    # 1 = inference not verified.
    if not report["models"] or not all(m["verified"] for m in report["models"]):
        return 1
    return 0 if all(m["benchmark_passed"] for m in report["models"]) else 3


if __name__ == "__main__":
    raise SystemExit(main())
