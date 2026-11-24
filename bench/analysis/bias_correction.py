"""Ad-hoc M0 analysis: how much word-timing error remains after a constant per-model offset.

    python3 bench/analysis/bias_correction.py RESULTS_DIR [DATA_DIR]

Reads the stored hypotheses (RESULTS_DIR/hyp/*.json) and the dataset ground truth, and
prints the signed median bias, then the residual |error − bias| (median, p95) per model and
timing set. Stdlib only. The bias is calibrated on synthetic speech with 70–220 ms
inter-word gaps, so it must be re-measured on authorized real commentary before use.
"""

from __future__ import annotations

import json
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from riftbench.metrics import align, norm_word  # noqa: E402


def q(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[int(p * (len(xs) - 1))]


def main(results: Path, data: Path) -> None:
    print("set | model | start bias | end bias | start resid med/p95 | end resid med/p95")
    for ds in ("timing-10m-clean", "timing-10m-noisy", "timing-10m-music", "timing-60m-clean"):
        truth = json.loads((data / "bench/datasets/say" / ds / "truth.json").read_text())
        for m in ("tiny", "base", "small"):
            hp = results / "hyp" / f"{m}__{ds}.json"
            if not hp.exists():
                continue
            hw = [h for h in json.loads(hp.read_text())["words"] if norm_word(h["w"])]
            ref = [norm_word(w["w"]) for w in truth["words"]]
            hyp = [norm_word(h["w"]) for h in hw]
            if len(ref) > 3000:  # keep O(n·m) alignment bounded: first ~30 min
                hyp = hyp[: int(len(hyp) * 3000 / len(ref))]
                ref = ref[:3000]
            s, e = [], []
            for ri, hj in align(ref, hyp).pairs:
                if ri is not None and hj is not None and ref[ri] == hyp[hj]:
                    s.append(hw[hj]["start_ms"] - truth["words"][ri]["start_ms"])
                    e.append(hw[hj]["end_ms"] - truth["words"][ri]["end_ms"])
            bs, be = st.median(s), st.median(e)
            rs = [abs(x - bs) for x in s]
            re_ = [abs(x - be) for x in e]
            print(f"{ds} | {m} | {bs:.0f} | {be:.0f} | {st.median(rs):.0f}/{q(rs, .95):.0f} "
                  f"| {st.median(re_):.0f}/{q(re_, .95):.0f}")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]) if len(sys.argv) > 2
         else Path(__file__).resolve().parents[2] / "data")
