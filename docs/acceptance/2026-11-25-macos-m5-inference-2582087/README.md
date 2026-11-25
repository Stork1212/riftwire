# M5 native Mac run, `2582087` (2026-11-25 05:58Z): **inference verified, grounded benchmark passed**

**Setup:** Ollama 0.40.18 (Homebrew) and `qwen3.5:4b`. That is the MLX build (NVFP4),
digest `7f3251fa…`, **reused, nothing downloaded**. The server was bound to 127.0.0.1 with
cloud features off. Prompt: **`gaming.gta.v2`**. Output was unconstrained, because this
engine build has no JSON mode, and every answer was checked by RIFTWIRE's validator.

| | Result |
|---|---|
| Probe | health, model, plain text and the RIFTWIRE path PASS (the path's answer was accepted). JSON and schema modes are UNAVAILABLE (501) |
| Accepted answers | **10/10** (8 on the first try, 2 after two repairs), 0 provider errors |
| Highlight tasks | 8/8 accepted answers selected the highlight; **recall 1.0** |
| Injection trap | never selected (0/2) |
| Unsupported claims | 0 (e.g. no "explosion" for a bare loud burst) |
| Payoff kept in the clip | 2/2 |
| Padding (before ranking) | **4/10 answers added filler windows**. The model itself scored the filler 32–44 against 82–91 for the highlights |
| After the editorial ranking cut (`min_score` 60, re-scored from the recorded answers: `rescore-min60.json`) | padding **0/10**, precision 1.0, recall 1.0 |
| Speed (medians) | prompt about 525 tok/s, output about 36 tok/s; wall time 14.9 s per task (2.5 k-token prompt, up to about 1.2 k output tokens) |
| Memory | Ollama resident peak 4.2 GB; runner RSS peak 3.0 GB; minimum available 1.97 GB; free memory 57 % → 39 % during the run, back to about 60 % after shutdown; swap flat at about 4.1 GB (no growth this run) |
| Disk | 121.26 → 121.25 GiB free (60 GiB floor) |

**Limits:**
- **These are synthetic, author-written tasks.** The results show grounding, validity and
  basic discrimination, **not** highlight quality on real footage. That needs ≥ 3 authorized
  recordings with ≥ 30 labelled moments.
- **The ranking cut of 60 is a starting value.** It sits in a wide gap on these tasks
  (≤ 44 against ≥ 82), but it must be recalibrated on real footage.

Left out: `model-manifest.json` (its SHA-256 is in `summary.txt`) and `brew-info.json`.
