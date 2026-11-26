# M5 native Mac run, `c7179ad` (2026-11-25 05:13Z): **NOT VERIFIED**

**No local inference happened.** All 10 benchmark requests failed with `provider_error`, and
none were accepted. The script printed "ALL PASSED" because its gate only treated a missing
model as an error. The gate was fixed in the next commit.

**Root cause** (`ollama-serve.txt`, `llm-bench.json`):
- Homebrew Ollama 0.40.18 ran `qwen3.5:4b` as its MLX build (safetensors, NVFP4, runner
  `mlx`).
- Its runner logged `Structured output is unavailable … libollama_xgrammar.dylib: no such
  file or directory`.
- It returned `501 {"error":"structured output is unavailable"}` to every `/api/chat`
  request that carried a JSON-schema `format`.
- The model loaded; no token was generated.

**Kept:** the environment, the test run (489 passed, 3 skipped), Ollama's log (with the
generated public key redacted), the model identity and licence (Apache 2.0), the swap
readings and the benchmark JSON.

**Left out:**
- `pull.log`: a 112 KB progress spinner;
- `model-manifest.json`: 150 KB; its SHA-256 is `3b6225ff…` in `summary.txt`;
- `brew-info.json`.

The analysis is in [`../../AI_PROVIDERS.md`](../../AI_PROVIDERS.md), under "First native
run".
