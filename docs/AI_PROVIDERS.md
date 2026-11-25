# AI providers, prompts and local models

_Status: **M5 preparation** on branch `phase2/m5-ai-prep`, which depends on PR #14. The
modules exist and are tested against a loopback stub server. They are **not yet wired into
the job queue**; M5 does that after the M4 gate. Real local inference is measured with
`bench/llm` on the Mac. None has run at the time of writing._

## Architecture (`backend/app/ai/`)
| Module | Role |
|---|---|
| `schemas.py` | `evidence.v1`: what a model sees, with every item carrying a stable id and recording-relative times in ms. `highlight_eval.v1`: what a model may answer. Strict; unknown fields are refused; **no time field exists**. |
| `validate.py` | Accepts an answer only if it is valid JSON, matches the schema, cites ids that exist, gives quotes found in the cited words, picks boundaries inside the window with a clip length of 8–60 s, cites keyframes only when images were shown, has no duplicate moments, and fits the size cap. Times are looked up from ids. Any error rejects the whole answer. |
| `prompts.py` | Versioned registry over `prompts/`: TOML front matter, `## system` and `## user` sections, `{{placeholders}}`, and a SHA-256 per file. Evidence text is JSON-quoted with `<` and `>` escaped, so it cannot close the `<EVIDENCE>` block. |
| `analyze.py` | One evaluation: render → provider → validate, with at most 2 repair retries that feed back the validator's errors. The outcome records the provider identity, prompt id/version/SHA, evidence fingerprint and per-attempt metrics. It ends in `accepted`, `failed_validation` or `provider_error`; there is no silent fallback. |
| `providers/base.py` | The engine-neutral contract: `identity()`, `health()`, `generate(Request)`, `unload()`. |
| `providers/ollama.py` | Ollama `/api/chat`, standard library only. **Loopback only**: anything else is refused before a socket opens, and environment proxies are bypassed. `think: false`, temperature 0 with a fixed seed, **repetition penalties forced off** (the model ships `presence_penalty 1.5`, which penalizes the repeated keys of JSON), bounded `num_ctx` and `num_predict`, and `keep_alive: 0`. **Output constraint** `structured="auto"` sends the JSON schema as `format`; if the engine answers `501 structured output is unavailable`, it retries without `format` and records `output_constraint: "none"` plus the engine's reason. `"schema"` refuses instead; `"none"` never constrains. Engine errors keep their code and message (`structured_output_unavailable`, `model_missing`, `provider_unreachable`, …). |
| `providers/cloud.py` | OpenAI and Anthropic placeholders that **refuse at construction** ($0 budget). They contain no network code and read no keys. |

**Replacing or upgrading a model is configuration only:** the model tag (and later the
engine) is a setting. Results always record which model, digest and prompt produced them,
so old and new results stay distinguishable.

## Local engine: Ollama (installation approved by the owner, 2026-11-25)
- **Install:** `brew install ollama`, the Homebrew formula (MIT, Apple Silicon bottles).
  It is **not** installed as a login service. RIFTWIRE's scripts start `ollama serve`
  for the duration of a run and stop it afterwards, so no model runs in the background.
- **Server settings** used by RIFTWIRE (environment variables of `ollama serve`):

| Setting | Value | Why |
|---|---|---|
| `OLLAMA_HOST` | `127.0.0.1:11434` | Localhost only (also Ollama's default) |
| `OLLAMA_MODELS` | `<models location>/ollama` (now `~/Claude/riftwire/data/models/ollama`) | Weights live in RIFTWIRE's models location, under its floor checks, not in `~/.ollama` |
| `OLLAMA_NO_CLOUD` | `1` | Disables Ollama cloud models and web search |
| `OLLAMA_MAX_LOADED_MODELS` | `1` | Never two models in memory at once |
| `OLLAMA_NUM_PARALLEL` | `1` | One request at a time |
| `OLLAMA_KEEP_ALIVE` | `0` | Unload after each response unless a batch asks otherwise |
| `OLLAMA_CONTEXT_LENGTH` | `8192` | A bounded context; the server default is 4096 |

- **Heavy workloads:** Whisper (M4) and the language model never run at the same time.
  The Mac benchmark script refuses to start while a Whisper or FFmpeg job is running. M5
  adds a memory and swap check before every AI job (`RETRY_WAIT` under pressure).

## First model: Qwen3.5 4B (download approved by the owner, 2026-11-25)
| | |
|---|---|
| Tag | `qwen3.5:4b` (Ollama library) |
| What Ollama 0.40 actually installed on the Mac | **MLX build:** safetensors, **NVFP4**, runner `mlx`, 4.5 B parameters including vision; digest `7f3251fa6878…` (4.0 GB listed; 3.70 GiB on disk, 733 layers). On Apple Silicon, Ollama 0.40 picks the MLX variant when one exists; the GGUF Q4_K_M build (llama.cpp runner) is a different tag. The plan's "Q4_K_M" expectation was wrong |
| Licence | Apache 2.0 (Ollama page, and the Qwen3.5 family release). The Mac script saves `ollama show --license` and fails if it is not Apache 2.0 |
| Context | 256K native; RIFTWIRE uses 8 192 |
| Inputs | Text and image. Keyframe input is a later option; the first benchmark is text-only |
| Storage | The models location; the script checks the 60 GiB floor plus headroom before and after the pull |

**Model choice:** RIFTWIRE is not bound to this model. If the 4B is insufficient on the
benchmark, the next candidates are `qwen3.5:9b` and then `gemma4:e4b`
([plan §5](M5_M6_AI_HIGHLIGHTS_PLAN.md)). Either one is downloaded **only after** a
recommendation backed by benchmark evidence.

## First native run (2026-11-25 05:13Z, `c7179ad`): no inference verified
- **Result:** all 10 benchmark requests failed with `provider_error`, and none were
  accepted. The script still printed "ALL PASSED". That was a gate bug, now fixed.
- **Root cause, from Ollama's own log:**
  - the request reached the MLX runner, which logged `Structured output is unavailable`,
    because `libollama_xgrammar.dylib` is missing from the Homebrew 0.40.18 bottle;
  - it answered `501 Not Implemented` to every `/api/chat` carrying `format`.
- **It was not a model-quality problem:** the model loaded in 2.3 s, and no token was ever
  generated.
- **Fixes:**
  - the engine's message is kept (`structured_output_unavailable: … HTTP 501: structured
    output is unavailable`);
  - `structured="auto"` falls back explicitly to unconstrained output, which the
    validator checks as before;
  - repetition penalties are forced off;
  - the benchmark gate fails on any provider error, on zero accepted answers, or on no
    generated tokens;
  - `bench/llm/probe.py` isolates health → model → plain text → JSON mode → schema mode →
    RIFTWIRE path.
- **Swap went from 1 829 to 3 334 MB** (the swap file grew from 3 to 4 GB):
  - when the model loaded, Ollama logged `free="3.7 GiB"` against 3.78 GB of weights, so
    macOS swapped **other processes'** pages out;
  - Ollama stopped its runner afterwards (`stopping mlx runner subprocess`, `ollama ps`
    empty), and the memory-pressure free figure rose from 40 % to 44 %;
  - macOS keeps swapped pages in swap until their owners touch them, so "used" does not fall
    at once. That alone does not show that Ollama held memory. The next run samples swap
    and available memory every 0.25 s on a thread that no longer blocks on `/api/ps`
    (it blocked for the whole load the first time), and again after unload, after server
    shutdown, and 30 s and 60 s later.
- **Side effect:** on first start, `ollama serve` created an identity key in
  `~/.ollama/id_ed25519` (used for ollama.com accounts). Cloud features are off. The key
  is left in place and noted.

## Benchmark (`bench/llm/llm_bench.py`)
**What it measures:**
- the model's identity and digest;
- the first load time;
- prompt and output tokens per second, and wall time per evaluation;
- memory: Ollama's resident size, runner RSS, swap before, peak and after, and minimum free
  memory;
- on 5 synthetic tasks: first-try schema validity, repair count, grounding errors,
  selection against the author's labels, the injection trap, the empty-is-correct case,
  padding, payoff retention, and unsupported claims (for example "explosion" from a bare
  loud burst).

A keyword heuristic runs on the same tasks as a reference.

**These tasks are synthetic and author-labelled.** They measure engineering behaviour
only. Highlight **quality** needs ≥ 3 authorized recordings with ≥ 30 human-labelled
moments.

**Not yet:** wiring into the queue, the `AI_ANALYZE` job, `ai_runs` storage, memory-aware
scheduling, and the dashboard (M5/M6).
