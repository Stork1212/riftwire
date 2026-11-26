# M5 native Mac run, `9f7b499` (2026-11-25 05:29Z): **inference verified; benchmark failed**

**Real local inference works.**
- **Setup:** Ollama 0.40.18 (Homebrew), `qwen3.5:4b` (MLX build: NVFP4 safetensors,
  digest `7f3251fa…`, the model downloaded at 05:13Z, **reused, nothing downloaded**),
  bound to 127.0.0.1 with cloud features off.
- **Probe:** health, model and plain-text inference all PASS. The plain-text reply was
  "OK", with no thinking tokens.
- **Constrained output is unavailable on this engine build:** JSON mode and JSON-schema
  mode both return 501.
- **Benchmark:** 26 generations with output and 0 provider errors. Speed: prompt about 735
  tokens/s, output about 36 tokens/s (medians).

**The grounded-task benchmark failed:** 2/10 answers were accepted, and both were the
empty-answer `nothing_here` task.
- Every task with a highlight failed schema validation (`window_id` missing, `description`
  added, 40+ evidence entries, nested objects). The repairs did not fix it.
- **Cause:** with output constraint unavailable, the model never saw the field list.
  Prompt v1 named the schema ("highlight_eval.v1") without spelling it out.
- **Fix:** prompt `gaming.gta.v2`, which adds the output contract generated from the
  schema. A two-level gate now reports **inference** and **benchmark** separately; this
  run's ALL PASSED reflected only the inference gate.

**Memory** (`memory-snapshots.txt`, `llm-bench.json`):
- **Before server start:** 63 % free (memory-pressure), swap used 3 334 MB.
- **Loading the model:** swap grew to 4 016 MB (the swap file went from 4 to 5 GB), and
  free memory fell to 41 %.
- **During the benchmark:** Ollama's resident size peaked at 4 453 MB; runner RSS peaked
  at 2 829 MB (Metal allocations are not all counted in RSS); minimum available memory
  was 1.8 GB; swap peaked at 4 117 MB.
- **After the server stopped:** free memory was back to 60 %, and 59 % at 30 s and 60 s.
  Ollama released its memory.
- **Swap stayed at 4 105 MB:** macOS keeps pages it has swapped out until their owners use
  them.

Files left out: `model-manifest.json` (150 KB; its SHA-256 is in `summary.txt`) and
`brew-info.json`.
