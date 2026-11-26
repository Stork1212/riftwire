"""Re-score a recorded ``llm-bench.json`` with the current scoring and ranking code, without
running a model again. Model answers are re-validated against the same synthetic evidence;
nothing is regenerated, so the result is a re-analysis of recorded outputs.

    backend/.venv/bin/python bench/llm/rescore.py llm-bench.json [--min-score 60]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "backend"))
sys.path.insert(0, str(HERE))

from app.ai.scoring import Ranking  # noqa: E402
from app.ai.validate import validate  # noqa: E402
from llm_bench import TASKS_BY_NAME, _mean, score_task  # noqa: E402
from tasks import build  # noqa: E402


class _Outcome:
    def __init__(self, value, clips):
        self.value, self.clips = value, clips


def rescore(report: dict, ranking: Ranking) -> dict:
    out = {}
    for m in report["models"]:
        rows = []
        for r in m.get("runs", []):
            result = r["record"].get("result")
            if result is None:
                continue
            task = TASKS_BY_NAME[r["task"]]
            bundle, _ = build(task)
            res = validate(json.dumps(result), bundle)
            if not res.ok:
                rows.append({"task": r["task"], "repeat": r["repeat"], "revalidated": False,
                             "errors": res.errors[:3]})
                continue
            sc = score_task(task, bundle, _Outcome(res.value, res.clips), ranking)
            rows.append({"task": r["task"], "repeat": r["repeat"], "revalidated": True, **sc})
        ok = [x for x in rows if x["revalidated"]]
        out[m["model"]] = {
            "min_score": ranking.min_score, "rows": rows,
            "padded_before_cut": sum(x["padded"] for x in ok),
            "padded_after_cut": sum(x["ranked_padded"] for x in ok),
            "precision_before": _mean([x["precision"] for x in ok]),
            "precision_after": _mean([x["ranked_precision"] for x in ok]),
            "recall_after": _mean([x["ranked_recall"] for x in ok]),
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("report", type=Path)
    ap.add_argument("--min-score", type=int, default=Ranking().min_score)
    args = ap.parse_args()
    result = rescore(json.loads(args.report.read_text()), Ranking(min_score=args.min_score))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
