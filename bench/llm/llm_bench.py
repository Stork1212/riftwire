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
import json
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

from app.ai import prompts  # noqa: E402
from app.ai.analyze import evaluate  # noqa: E402
from app.ai.providers.base import ProviderError  # noqa: E402
from app.ai.providers.ollama import OllamaProvider  # noqa: E402
from tasks import TASKS, Task, build  # noqa: E402

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
    out = _sh(["vm_stat"])
    page = re.search(r"page size of (\d+)", out)
    free = re.search(r"Pages free:\s+(\d+)", out)
    spec = re.search(r"Pages speculative:\s+(\d+)", out)
    if not (page and free):
        return None
    pages = int(free.group(1)) + (int(spec.group(1)) if spec else 0)
    return round(pages * int(page.group(1)) / 2**20, 1)


class Sampler(threading.Thread):
    def __init__(self, provider: OllamaProvider, every: float = 0.5):
        super().__init__(daemon=True)
        self.provider, self.every = provider, every
        self.stop_flag = threading.Event()
        self.peak_rss = 0.0
        self.peak_swap = 0.0
        self.min_free: float | None = None
        self.peak_loaded = 0

    def run(self) -> None:
        while not self.stop_flag.is_set():
            self.peak_rss = max(self.peak_rss, ollama_rss_mb())
            s = swap_used_mb()
            if s is not None:
                self.peak_swap = max(self.peak_swap, s)
            f = free_mb()
            if f is not None:
                self.min_free = f if self.min_free is None else min(self.min_free, f)
            try:
                size = sum(m.get("size", 0) for m in self.provider.loaded())
                self.peak_loaded = max(self.peak_loaded, size)
            except ProviderError:
                pass
            self.stop_flag.wait(self.every)


# --------------------------------------------------------------------------- scoring
def score_task(task: Task, bundle, outcome) -> dict:
    chosen: set[int] = set()
    payoff_ok: dict[str, bool] = {}
    claims = ""
    if outcome.value:
        for c in outcome.value.candidates:
            chosen.add(int(c.window_id.split(":")[1]) - 1)
            claims += " ".join([c.what_happens, c.why_entertaining, c.title, c.hook,
                                *(e.claim for e in c.evidence)]).lower() + " "
    by_id = {w.id: w for w in bundle.words}
    for wi, word in task.payoff_words.items():
        clip = next((c for c in outcome.clips if c.window_id == f"c:{wi + 1}"), None)
        target = next((w for w in by_id.values() if w.text == word), None)
        payoff_ok[f"c:{wi + 1}"] = bool(clip and target and clip.end_ms >= target.t1
                                         and clip.start_ms <= target.t0)
    text = " ".join(w.text.lower() for w in bundle.words)
    unsupported = [w for w in task.forbidden_claims if w in claims and w not in text]
    tp = len(chosen & task.highlights)
    return {
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


# ------------------------------------------------------------------------------ main
def run_model(model: str, args, prompt) -> dict:
    p = OllamaProvider(model, base_url=args.base_url, keep_alive="10m")
    ident = p.identity()
    swap0, free0, rss0 = swap_used_mb(), free_mb(), ollama_rss_mb()
    sampler = Sampler(p)
    sampler.start()
    runs = []
    try:
        for rep in range(args.repeat):
            for task in TASKS:
                if args.tasks and task.name not in args.tasks:
                    continue
                bundle, _ = build(task)
                t0 = time.perf_counter()
                out = evaluate(bundle, p, prompt, target_count=task.target_count,
                               channel="rumbleframe", max_repairs=args.max_repairs,
                               timeout_s=args.timeout, identity=ident)
                wall = round(time.perf_counter() - t0, 2)
                row = {"task": task.name, "repeat": rep, "status": out.status,
                       "wall_s": wall, "attempts": len(out.attempts),
                       "valid_first_try": bool(out.attempts and out.attempts[0].ok),
                       "errors_first_try": out.attempts[0].errors[:6] if out.attempts else [],
                       "provider_error": out.error if out.status == "provider_error" else None,
                       "metrics": [a.metrics for a in out.attempts],
                       "score": score_task(task, bundle, out),
                       "record": out.record()}
                runs.append(row)
                print(f"  {model} {task.name} r{rep}: {out.status} attempts={len(out.attempts)}"
                      f" chosen={row['score']['chosen']} wall={wall}s", flush=True)
    finally:
        sampler.stop_flag.set()
        sampler.join(2)
        try:
            p.unload()
        except ProviderError:
            pass
    ok = [r for r in runs if r["status"] == "accepted"]
    precisions = [r["score"]["precision"] for r in ok if r["score"]["precision"] is not None]
    gen = [m for r in runs for m in r["metrics"] if m.get("output_ms")]
    tps = [m["output_tokens"] / (m["output_ms"] / 1000) for m in gen if m["output_ms"] > 0]
    pps = [m["prompt_tokens"] / (m["prompt_ms"] / 1000) for m in gen if m.get("prompt_ms")]
    loads = [m["load_ms"] for r in runs for m in r["metrics"][:1]]
    return {
        "model": model, "identity": ident.__dict__, "runs": runs,
        "summary": {
            "tasks_run": len(runs),
            "accepted": len(ok),
            "valid_first_try": sum(r["valid_first_try"] for r in runs),
            "failed_validation": sum(r["status"] == "failed_validation" for r in runs),
            "provider_errors": sum(r["status"] == "provider_error" for r in runs),
            "mean_precision": round(statistics.mean(precisions), 3) if precisions else None,
            "mean_recall": round(statistics.mean(r["score"]["recall"] for r in ok), 3)
            if ok else None,
            "trap_selected": sum(r["score"]["trap_selected"] for r in runs),
            "padded": sum(r["score"]["padded"] for r in ok),
            "unsupported_claims": sum(len(r["score"]["unsupported_claims"]) for r in runs),
            "payoff_kept": [v for r in ok for v in (r["score"]["payoff_kept"] or {}).values()],
            "first_load_ms": loads[0] if loads else None,
            "output_tok_per_s_median": round(statistics.median(tps), 1) if tps else None,
            "prompt_tok_per_s_median": round(statistics.median(pps), 1) if pps else None,
            "wall_s_median": round(statistics.median(r["wall_s"] for r in runs), 1)
            if runs else None,
            "memory": {"ollama_loaded_peak_mb": round(sampler.peak_loaded / 2**20, 1),
                       "ollama_rss_peak_mb": sampler.peak_rss, "ollama_rss_before_mb": rss0,
                       "swap_used_before_mb": swap0, "swap_used_peak_mb": sampler.peak_swap,
                       "swap_used_after_mb": swap_used_mb(), "free_before_mb": free0,
                       "free_min_mb": sampler.min_free},
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--model", action="append", default=None)
    ap.add_argument("--base-url", default="http://127.0.0.1:11434")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--max-repairs", type=int, default=2)
    ap.add_argument("--timeout", type=float, default=300)
    ap.add_argument("--tasks", nargs="*")
    ap.add_argument("--prompt", default="gaming.gta.v1")
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
              "heuristic": {t.name: heuristic(t, build(t)[0]) for t in TASKS},
              "models": []}
    for m in models:
        print(f"== {m}", flush=True)
        try:
            report["models"].append(run_model(m, args, prompt))
        except ProviderError as e:
            report["models"].append({"model": m, "error": f"{e.code}: {e}"})
            print(f"  {m}: {e.code}: {e}", flush=True)
    report["finished"] = datetime.now(UTC).isoformat()
    (args.out / "llm-bench.json").write_text(json.dumps(report, indent=2, default=str))
    lines = [f"# LLM bench ({report['started']})", "", report["note"], ""]
    for m in report["models"]:
        lines.append(f"## {m['model']}")
        if "error" in m:
            lines += [f"error: {m['error']}", ""]
            continue
        lines.append("```json\n" + json.dumps({"identity": m["identity"], **m["summary"]},
                                              indent=2, default=str) + "\n```")
    lines += ["## Keyword heuristic (reference)", "```json",
              json.dumps(report["heuristic"], indent=2), "```"]
    (args.out / "llm-bench.md").write_text("\n".join(lines) + "\n")
    print(f"report: {args.out / 'llm-bench.json'}")
    return 0 if all("error" not in m for m in report["models"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
