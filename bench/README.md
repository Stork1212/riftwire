# M0: transcription and signal benchmark

This harness is for Phase 2 milestone M0, approved by the owner on 2026-11-23. It measures
**transcription accuracy, word-timing error, processing speed, peak memory and disk usage**
on the owner's Mac, so the transcription engine and model for M1 can be chosen from data.
It is **not** part of the RIFTWIRE app: nothing under `backend/` imports it.

## Scope and limits (owner conditions)

- **Isolated environment:** a project-local `bench/.venv` (Python 3.13). It contains only
  the hash-pinned packages in `requirements.txt`: mlx-whisper 0.4.3 and yt-dlp 2026.11.23
  plus their dependencies, **without PyTorch** (about 121 MB download, about 426 MB
  estimated installed; measured 597 MB on the Mac including bytecode). yt-dlp is installed but this harness never uses it. No system-wide installs.
- **Models:** only `mlx-community/whisper-{tiny,base,small}-mlx`, and only the two files
  each needs (`config.json`, `weights.npz`). Downloads happen only through
  `fetch-models --confirm`, after a free-space check, and each file is verified by size
  (and SHA-256 for the weights). Sizes: tiny 74.4 MB, base 143.7 MB, small 481.3 MB.
  `small` is fetched only if the floor check passes.
- **Hard 60 GiB free-space floor:** the plan, setup, model download, dataset build and the
  signal benchmark each refuse to run if they would leave less than 60 GiB plus a 1 GiB
  margin free on the volume they write to. A watchdog also enforces the floor live on every
  volume.
- **Data:** synthetic and reproducible only. Speech comes from macOS `say`; video comes from
  FFmpeg test sources. No real footage, no network access during runs (`HF_HUB_OFFLINE=1`),
  no Ollama, no LLMs, no paid APIs.

## Run on the Mac

Run it from a **dedicated, detached worktree** pinned to the reviewed commit, so it can never
touch work in `~/Claude/riftwire`. The launcher also refuses to start in a tree with
uncommitted or untracked files, and checks afterwards that HEAD and the tree are unchanged.

```bash
cd ~/Claude/riftwire
git fetch origin
git worktree add --detach ../riftwire-m0 <PR #5 commit SHA>
cd ../riftwire-m0
bench/run_m0.sh            # add RIFTWIRE_BENCH_BUDGET_GB=<approved GB> if the owner raises it
```

Re-runs may also use an existing **clean** checkout of this branch that already has
`bench/.venv` and the models: nothing is downloaded again, and the plan counts only what is
still to be written. The launcher's git checks apply the same way.

`run_m0.sh` runs these steps; it stops on any failure:

1. `plan` (stdlib only, before anything is installed): a **per-volume peak** estimate. It
   refuses with exit 10 if any volume would drop below 60 GiB + 1 GiB margin, and with exit 11
   if the peak exceeds the approved budget (`RIFTWIRE_BENCH_BUDGET_GB`, default **1.9 GB**).
   If the full plan is refused, it re-plans without `small`.
2. `setup_bench.sh`: checks the venv volume and pip's temp volume, then installs.
3. Downloads tiny and base: any failure stops the run.
4. Downloads small: a **floor refusal (exit 10) is the only tolerated skip**. A network,
   checksum, HTTP or I/O error stops the run.
5. Runs the benchmark, then verifies the git tree is unchanged.

Every volume the run writes to is sampled every 5 s: the repo/venv, `RIFTWIRE_DATA_DIR`,
`TMPDIR` and `HF_HOME` (both redirected inside the data dir), and the results directory.
Samples go to `free_space.csv`. If any volume drops below 60 GiB, the watchdog stops every
child process (exit 3). Hugging Face Xet transfers and its chunk cache are disabled, and pip
runs with `--no-cache-dir`, so no cache is written outside the monitored places.

To finish **only the signals step** of an earlier run, for example after it was refused by
the floor, use the same results directory. It runs with the same plan, watchdog and git
checks, and downloads nothing:

```bash
bench/run_m0.sh --signals-only ~/Claude/riftwire-env/bench-<UTC>
```

Exit codes: 0 ok · 1 step failed · 3 floor breach · 4 git safety · 10 floor refusal ·
11 over budget.

### Storage estimate (exact parameters, conservative)

| Component | Final | Transient | Where |
|---|---|---|---|
| bench venv (no torch) | 600 MB (measured 597 MB on the Mac) | +350 MB pip unpack area | repo volume / `TMPDIR` |
| whisper tiny / base / small | 74 / 144 / 481 MB | none (same-dir `.incomplete` → rename) | data dir |
| HF metadata | ≤ 5 MB | none | `HF_HOME` in data dir |
| speech WAVs (121 min, 16 kHz mono) | 237 MB | +38 MB variant mixing | data dir |
| TTS clip cache | ≤ 70 MB | none | data dir |
| 10 min 1080p60 video at 12 Mbit/s + 640 px proxy, ×1.3 margin | 1.34 GB | none (`.tmp` → rename) | data dir |
| results | ≤ 50 MB | none | `~/Claude/riftwire-env` |
| **Total with small** | **3.00 GB** | **peak ≈ 3.35 GB** | |
| **Total without small** | **2.51 GB** | **peak ≈ 2.86 GB** | |

`plan` prints these figures from the actual registry and dataset parameters, minus anything
already on disk. Use `bench/run.py plan --models ... --budget-gb N`.

Manual step-by-step (same checks, no watchdog):

```bash
bench/.venv/bin/python bench/run.py plan --models tiny,base,small --budget-gb 1.9
bench/setup_bench.sh
bench/.venv/bin/python bench/run.py fetch-models --models tiny,base --confirm
bench/.venv/bin/python bench/run.py fetch-models --models small --confirm   # exit 10 = floor
bench/.venv/bin/python bench/run.py all --models tiny,base,small
```

Results go to `~/Claude/riftwire-env/bench-<UTC>/` as `results.json` (raw), `results.md`
(summary, target checks, recommendation), `free_space.csv` and `console.log`. Each
transcription runs in a **fresh process** under `/usr/bin/time -l`, which records peak
memory footprint and maximum RSS.

To remove everything afterwards:

```bash
cd ~/Claude/riftwire && git worktree remove --force ../riftwire-m0
```

This deletes the worktree, including its venv, models and datasets; results stay in
`riftwire-env`.

## What is measured

| Measurement | How |
|---|---|
| WER | Word-level Levenshtein alignment on normalized text, run on every set. |
| Word timing | `timing-*` sets: each word is synthesized separately (spoken lower-case, since `say` reads a lone "I" as "capital I"), its silence trimmed, then the words are concatenated with seeded gaps, so every word's start and end is known to the sample. Reported: absolute median and p95, signed bias (negative = early) for starts and ends, and the share of words within 100 ms and 250 ms. A harness check fails if any hypothesis contains a TTS artifact such as "capital". |
| Speed | RTF = transcription seconds ÷ audio seconds. Model load time is reported separately. |
| Memory | Per run: peak RSS (the proposal's target, ≤ 3 GB), process peak footprint (includes Metal buffers and MLX's cache, the strict figure) and MLX's own peak allocation. |
| Robustness | `noisy` variant: pink noise at about 10 dB SNR plus loud bursts. `music` variant: a tonal bed about 15 dB below the speech. WER targets are judged on the `natural-*` sets (clean, noisy, music). Timing-set WER is information only: isolated-word speech is unnatural. |
| Duration scaling | Timing sets of 1, 10 and 60 minutes. |
| Signals | 10 min of synthetic 1080p60 video with known hard cuts and loudness bursts: proxy throughput (VideoToolbox), scene-cut and loudness-burst recall and precision at ±250 ms. |
| Disk | Measured sizes of the venv, models, datasets, signal files and results. Free space is logged before and after each run. |

Targets (from `docs/PHASE2_PROPOSAL.md`): RTF ≤ 0.15; WER ≤ 5 % clean and ≤ 12 % noisy;
word-start error median ≤ 80 ms and p95 ≤ 250 ms; peak memory ≤ 3 GB.

**Known limitation:** synthetic TTS speech is cleaner and more regular than real gameplay
commentary. These numbers compare engines and models; they do not validate highlight
quality. Authorized real gameplay is a separate evaluation set, used later.

## Tests

`bench/tests` run without mlx. They use the backend venv and a tone-based TTS stand-in, and
are part of `scripts/test.sh`:

```bash
cd bench && ../backend/.venv/bin/pytest -q tests && ../backend/.venv/bin/ruff check .
```
