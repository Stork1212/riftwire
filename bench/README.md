# M0: transcription and signal benchmark

This harness is for Phase 2 milestone M0, approved by the owner on 2026-11-23. It measures
**transcription accuracy, word-timing error, processing speed, peak memory and disk usage**
on the owner's Mac, so the transcription engine and model for M1 can be chosen from data.
It is **not** part of the RIFTWIRE app: nothing under `backend/` imports it.

## Scope and limits (owner conditions)

- **Isolated environment:** a project-local `bench/.venv` (Python 3.13). It contains only
  the hash-pinned packages in `requirements.txt`: mlx-whisper 0.4.3 and yt-dlp 2026.11.23
  plus their dependencies, **without PyTorch** (about 121 MB download, about 426 MB
  installed). yt-dlp is installed but this harness never uses it. No system-wide installs.
- **Models:** only `mlx-community/whisper-{tiny,base,small}-mlx`, and only the two files
  each needs (`config.json`, `weights.npz`). Downloads happen only through
  `fetch-models --confirm`, after a free-space check, and each file is verified by size
  (and SHA-256 for the weights). Sizes: tiny 74.4 MB, base 143.7 MB, small 481.3 MB.
  `small` is fetched only if the floor check passes.
- **Hard 60 GiB free-space floor:** setup, model download, dataset build and the signal
  benchmark each refuse to run if they would leave less than 60 GiB plus a 1 GiB margin free.
- **Data:** synthetic and reproducible only. Speech comes from macOS `say`; video comes from
  FFmpeg test sources. No real footage, no network access during runs (`HF_HUB_OFFLINE=1`),
  no Ollama, no LLMs, no paid APIs.

## Run on the Mac

One command does everything below, with free space measured before setup and every 5 s
during the run (`free_space.csv`). A watchdog stops the whole run if free space drops below
60 GiB:

```bash
bench/run_m0.sh
```

Or step by step:

```bash
bench/setup_bench.sh                                     # creates bench/.venv (checks space)
bench/.venv/bin/python bench/run.py plan                 # exact files/sizes, no network
bench/.venv/bin/python bench/run.py fetch-models --models tiny,base --confirm
bench/.venv/bin/python bench/run.py fetch-models --models small --confirm   # refuses below floor
bench/.venv/bin/python bench/run.py all --models tiny,base,small
```

Results go to `~/Claude/riftwire-env/bench-<UTC>/` as `results.json` (raw) and `results.md`
(summary, target checks, recommendation). Each transcription runs in a **fresh process**
under `/usr/bin/time -l`, which records peak memory footprint and maximum RSS.

To remove everything afterwards:
- `bench/run.py clean --confirm` removes the datasets and signal videos.
- `rm -rf bench/.venv data/models/mlx-community--whisper-*` removes the environment and
  the models.

## What is measured

| Measurement | How |
|---|---|
| WER | Word-level Levenshtein alignment on normalized text, run on every set. |
| Word timing | `timing-*` sets: each word is synthesized separately, its silence trimmed, then the words are concatenated with seeded gaps, so every word's start and end is known to the sample. Reported: median, p95, and the share of words within 100 ms and 250 ms. |
| Speed | RTF = transcription seconds ÷ audio seconds. Model load time is reported separately. |
| Memory | Peak memory footprint (includes Metal unified memory) and max RSS, per run. |
| Robustness | `noisy` variant: pink noise at about 10 dB SNR plus loud bursts. `music` variant: a tonal bed about 15 dB below the speech. |
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
