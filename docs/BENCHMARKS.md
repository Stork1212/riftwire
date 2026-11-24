# M0 benchmark results (Phase 2)

**Status: M0 approved by the owner (2026-11-24) as a completed benchmarking milestone**,
with the timestamp-accuracy and memory limitations documented below. This is **not**
production acceptance of transcription or highlight detection. Merged as PR #5, tag
`v0.2.0-m0`. Next: M1 ([checklist](M1_STORAGE_CHECKLIST.md)), which starts only after owner
approval.

| | |
|---|---|
| Machine | Apple M3 Pro, 18 GB unified memory, macOS 26.5.2, Python 3.13.2, FFmpeg 9.1 |
| Engine | mlx-whisper 0.4.3, mlx 0.32.4, no PyTorch (isolated `bench/.venv`) |
| Run | **2026-11-24 04:04–04:13 UTC**, launcher `6d5db07`, clean checkout, git tree unchanged afterwards, one volume watched (114 samples) |
| Raw data | [`benchmarks/m0-20261124T040419Z/`](benchmarks/m0-20261124T040419Z/): `results.json` (24 runs + signals), `results.md` (generated report), `free_space.csv` |
| Reproducibility | An earlier transcription-only run (03:47 UTC, [`m0-20261124T034744Z`](benchmarks/m0-20261124T034744Z/)) gave the same numbers. The largest WER difference was tiny on natural-clean, 5.79 % vs 6.54 %, from Whisper's temperature fallback. All timing biases and residuals were identical. |
| Datasets | Synthetic, from macOS `say`, dataset v2. 121 min per model: timing sets 1/10/60 min (clean, noisy at ~10 dB SNR, music bed) and natural-sentence sets 10 min (clean, noisy, music) |

## 1. What was measured (verified on the Mac)

All 24 runs (3 models × 8 sets) finished with exit 0. **0 TTS-artifact words** appeared in
any transcript (the v1 "capital I" bug is fixed).

### Accuracy: WER on natural speech (target ≤ 5 % clean, ≤ 12 % noisy)

| model | clean | noisy | music bed | verdict |
|---|---|---|---|---|
| tiny | 6.54 % | 6.98 % | 3.46 % | ❌ clean over target (5.79 % in the earlier run) |
| base | 1.43 % | 3.83 % | 3.69 % | ✅ |
| small | **0.69 %** | **2.06 %** | **0.17 %** | ✅ best |

### Speed: real-time factor (target ≤ 0.15; lower is faster)

| model | RTF range | 60 min of audio takes |
|---|---|---|
| tiny | 0.006–0.016 | ≈ 20 s |
| base | 0.009–0.018 | ≈ 31 s |
| small | 0.023–0.034 | ≈ 81 s |

Model load is 0.02–0.10 s after the first run. All models are far inside the speed target.

### Word timing (target: start error median ≤ 80 ms, p95 ≤ 250 ms)

Raw Whisper word timestamps **fail the target for every model**:

| model | start median / p95 | signed start bias | signed end bias |
|---|---|---|---|
| tiny | 114–128 / 290–351 ms | −51 to −111 ms | +32 to +75 ms |
| base | 276–283 / 428–488 ms | −276 to −283 ms | −139 to −147 ms |
| small | 251–256 / 411–500 ms | −250 to −256 ms | −109 to −122 ms |

The cause is that word starts absorb the pause before the word (negative = early). The
offset is **almost constant per model**. After subtracting each model's median offset
(analysis of the same hypotheses with [`bench/analysis/bias_correction.py`](../bench/analysis/bias_correction.py)),
the residual error is:

| model | start residual median / p95 | end residual median / p95 |
|---|---|---|
| tiny | 69–101 / 277–370 ms ❌ | 51–87 / 235–267 ms |
| base | 59–62 / 189–237 ms ✅ | 47–49 / 141–156 ms |
| small | 56–64 / 213–243 ms ✅ | 42–45 / 138–170 ms |

**Caveat:** the offset depends on the pauses between words. The synthetic sets use
70–220 ms gaps, so the correction must be **re-measured on authorized real commentary**
before it is relied on. Without correction, base and small end words 110–150 ms early, which
would clip word tails at cut points.

### Memory (proposal target: peak RSS ≤ 3 GB)

| model | peak RSS | MLX peak allocation | process peak footprint |
|---|---|---|---|
| tiny | 0.75 GB | 0.71 GB (10 min) · 2.64 GB (60 min) | 4.23 GB |
| base | 0.87 GB | 0.96 GB (10 min) · 2.75 GB (60 min) | 4.76 GB |
| small | 1.22 GB | 1.67 GB (10 min) · **3.12 GB (60 min)** | **7.22 GB** |

RSS passes for all models, but **RSS undercounts on Apple Silicon** because it excludes
Metal memory. MLX's own peak grows with the input length: a 60-minute file is processed in
one piece. The footprint includes MLX's buffer cache. In the 03:47 run, **macOS swap grew
from 5.0 to 6.0 GiB** during small's 60-minute pass; this Mac was already using 4.5 of
5.0 GiB of swap before that run. In the final run, swap stayed at 6.0 GiB (5.6–5.7 GiB used)
throughout.

### Signals (synthetic 10-minute 1080p60 video, VideoToolbox)

| step | result | speed |
|---|---|---|
| Build the synthetic source (54 segments, 600 s) | 434 MB file | 173 s (test setup, not a pipeline step) |
| Proxy (640 px, 30 fps, VideoToolbox) | 57 MB | 19.2 s = **31× realtime** |
| Scene-cut detection on the proxy | **53/53 cuts found, 0 false (recall 100 %, precision 100 %)**, median error < 1 ms | 2.4 s = 253× realtime |
| Loudness-burst detection (EBU R128) | **23/23 bursts found, 0 false**, median error 24 ms | 0.53 s = 1,142× realtime |

These are hard cuts between visually distinct test patterns, the easiest case. In
container tests, low-contrast cuts between two similar bar patterns were sometimes missed.
Real gameplay will be harder, and that is evaluated with authorized footage later.
VideoToolbox produced 5.8 Mbit/s against a 12 Mbit/s target on this synthetic content.

### Disk (measured at the end of the final run)

| item | size |
|---|---|
| `bench/.venv` | 603 MB |
| models (tiny + base + small) | 699 MB |
| speech datasets + TTS cache | 261 MB |
| signals source + proxy | 491 MB |
| results | 3 MB |
| **total benchmark footprint** | **2.06 GB** |
| Hugging Face cache / temp area | 0 (redirected; empty after the run) |
| `~/.cache/huggingface` left by the superseded old-launcher runs | 380 KB (owner-checked; metadata only) |

**Budget:** the total, **2.06 GB**, was 0.16 GB over the budget approved at the time
(1.9 GB). The owner has since approved up to 3.5 GB. The launcher compared
the budget only against each run's new writes (1.74 GB planned in the final run) and not
against what earlier runs had already installed. That is fixed in the launcher: the budget
now covers existing usage plus the run's peak. Everything is regenerable and can be removed
with `bench/run.py clean --confirm` plus `rm -rf bench/.venv data/models/mlx-community--whisper-*`.

**Free space** on `/System/Volumes/Data`: 62.96 GiB at start, **minimum 62.48 GiB**. The
floor was never breached in any run.

## 2. Not measured

- **Thermal state:** `pmset -g therm` reported no CPU power status on this Mac, so there is no
  thermal data.
- **Real gameplay:** synthetic speech and test patterns cannot validate highlight quality.
  Authorized real gameplay stays a separate evaluation set (owner condition 4), including
  re-measuring the word-timing offset on real commentary.
- **Ollama / local LLM:** deferred by the owner (condition 3).

## 3. Recommendation (owner direction, 2026-11-24)

**Engine: `mlx-whisper`.**

- **Provisional default model: `base`.** WER is well inside target (natural speech: 1.4 %
  clean, 3.8 % noisy, 3.7 % music bed). It needs about half of small's memory (MLX peak
  0.96 GB per 10 min of audio), runs 2.2–2.6× faster (60 min of audio in about 31 s), and
  causes no swap growth.
- **Higher-quality option: `small`** (0.7 % / 2.1 % / 0.2 %, still about 30–44× faster than
  realtime). It stays optional until **chunked memory testing** shows that small runs in
  bounded memory without swap growth: about 10-minute windows with overlap and a capped MLX
  buffer cache, while the app and FFmpeg are running. A single 60-minute pass reached
  3.12 GB MLX peak, a 7.2 GB footprint, and +1 GiB of swap.
- **tiny is not recommended**: clean WER is over target (5.8–6.5 %), and its timing is the
  noisiest.

**Word timestamps: raw values are the source of truth.**

- Store Whisper's raw word timestamps unchanged.
- Any correction (constant per-model offset, energy-based onset refinement, cut-point
  padding) is a separate, versioned layer, stored next to the raw values and evaluated
  independently.
- The offsets measured here come from synthetic speech with 70–220 ms gaps between words.
  They are **not validated on real gameplay** and must not be applied as if they were.
- Until a correction is validated on authorized real commentary, cut points should use the
  raw timestamps with conservative padding, especially after a word's end, since raw ends
  run 110–150 ms early.

The mechanical report ([`results.md`](benchmarks/m0-20261124T040419Z/results.md)) says "no
model meets all targets; small fails the fewest (timing)". That is correct for the raw
targets. Every model fails raw word timing; nothing else above changes that verdict.

**Storage budget:** the owner approved up to **3.5 GB** for the benchmark, subject to the
unchanged 60 GiB floor and margin. The measured total is 2.06 GB. The launcher's default
budget is now 3.5 GB, and it counts what is already on disk.

## 4. Earlier runs (superseded)

- 03:24 UTC, old launcher `aed7a45`: interrupted after 18 of 21 runs. It revealed the
  "capital I" harness bug and the timing bias.
- 03:40 UTC, old launcher: stopped after 4 runs.
- 03:40:56 UTC, new launcher in a fresh worktree: refused at the plan step, because
  re-downloading everything would have broken the floor.
- 03:47 UTC, launcher `6d5db07`: all 24 transcription runs completed; signals were refused
  by the floor. These numbers are kept as a reproducibility check.
- 04:15 UTC: an attempt to resume signals from a checkout without `--signals-only` started
  a full plan instead, which was refused by the floor. Nothing was written. The signals had
  already been measured in the 04:04 run.

Only the 04:04 run's numbers are used above; the 03:47 run is cited only as a reproducibility check.
