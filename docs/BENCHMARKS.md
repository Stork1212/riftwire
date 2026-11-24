# M0 benchmark results (Phase 2)

**Status: transcription complete and verified. Signals step pending.** The 60 GiB floor
correctly refused the signals step. This is a draft for independent review. **No M1 work
starts until the owner approves.**

| | |
|---|---|
| Machine | Apple M3 Pro, 18 GB unified memory, macOS 26.5.2, Python 3.13.2, FFmpeg 9.1 |
| Engine | mlx-whisper 0.4.3, mlx 0.32.4, no PyTorch (isolated `bench/.venv`) |
| Run | 2026-11-24 03:47–03:54 UTC, launcher `6d5db07`, clean checkout, one volume watched |
| Raw data | [`benchmarks/m0-20261124T034744Z/`](benchmarks/m0-20261124T034744Z/): `results.json` (all 24 runs), `free_space.csv`, generated report |
| Datasets | Synthetic, from macOS `say`, dataset v2. 121 min per model: timing sets 1/10/60 min (clean, noisy at ~10 dB SNR, music bed) and natural-sentence sets 10 min (clean, noisy, music) |

## 1. What was measured (verified on the Mac)

All 24 runs (3 models × 8 sets) finished with exit 0. **0 TTS-artifact words** appeared in
any transcript (the v1 "capital I" bug is fixed).

### Accuracy: WER on natural speech (target ≤ 5 % clean, ≤ 12 % noisy)

| model | clean | noisy | music bed | verdict |
|---|---|---|---|---|
| tiny | 5.79 % | 6.98 % | 3.46 % | ❌ clean slightly over target |
| base | 1.43 % | 3.83 % | 3.63 % | ✅ |
| small | **0.69 %** | **2.06 %** | **0.17 %** | ✅ best |

### Speed: real-time factor (target ≤ 0.15; lower is faster)

| model | RTF range | 60 min of audio takes |
|---|---|---|
| tiny | 0.006–0.016 | ≈ 21 s |
| base | 0.009–0.018 | ≈ 33 s |
| small | 0.023–0.035 | ≈ 84 s |

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
| base | 0.81 GB | 0.96 GB (10 min) · 2.75 GB (60 min) | 4.77 GB |
| small | 1.22 GB | 1.67 GB (10 min) · **3.12 GB (60 min)** | **7.03 GB** |

RSS passes for all models, but **RSS undercounts on Apple Silicon** because it excludes
Metal memory. MLX's own peak grows with the input length: a 60-minute file is processed in
one piece. The footprint includes MLX's buffer cache. During small's 60-minute run, **macOS
swap grew from 5.0 to 6.0 GiB**; this Mac was already using 4.5 of 5.0 GiB of swap before the
run started.

### Disk (measured)

| item | size |
|---|---|
| `bench/.venv` | 597 MB |
| models (tiny + base + small) | 679 MB |
| speech datasets + TTS cache | 226 MB + 29 MB |
| Hugging Face cache / temp area | 0 (redirected; empty after the run) |
| `~/.cache/huggingface` left by the superseded old-launcher runs | 380 KB (owner-checked; metadata only) |
| results | < 1 MB |

Free space on `/System/Volumes/Data`: 63.10 GiB at start, **minimum 61.97 GiB**. The floor
was never breached. The drop exceeds the benchmark's own writes mainly because swap grew by
1 GB. The signals step needs about 1.34 GB, and the floor plus margin refused it (correct
behaviour).

## 2. Not yet measured

- **Signals** (VideoToolbox proxy throughput, scene-cut and loudness-burst recall/precision on
  a 10-minute 1080p60 synthetic video). Resume with
  `bench/run_m0.sh --signals-only ~/Claude/riftwire-env/bench-20261124T034744Z` once free
  space allows (≥ about 62.3 GiB).
- **Thermal state:** `pmset -g therm` reported no CPU power status on this Mac, so there is no
  thermal data.
- **Real gameplay:** synthetic speech cannot validate highlight quality. It stays a separate
  evaluation set (owner condition 4).

## 3. Recommendation (for independent review)

**Engine: `mlx-whisper`. Model: `small`**, with three conditions for M1:

1. **Chunked transcription.** Process long recordings in about 10-minute windows with
   overlap. This keeps MLX's peak at about 1.7 GB and avoids the 7 GB footprint and swap
   growth seen on the single 60-minute pass. Also cap MLX's buffer cache.
2. **Word-timing correction.** Apply per-model offset calibration and/or energy-based onset
   refinement, re-measured on authorized real commentary. Add asymmetric padding at cut
   points, since word ends are early.
3. **Fallback: `base`.** Use it if chunked `small` still causes memory pressure or swap growth
   while the app and FFmpeg are running. It needs about half the memory, is about 2.3–2.6× faster, and
   its WER is still well inside target (1.4 % / 3.8 %).

Why small: it is the most accurate model (0.7 % clean, 2.1 % noisy, 0.2 % with music bed).
It is still about 29–43× faster than realtime, and accuracy drives the evidence quality of
transcripts and highlights. tiny is not recommended: its clean WER is over target and its
timing is the noisiest after correction.

The mechanical report ([`transcription_report.md`](benchmarks/m0-20261124T034744Z/transcription_report.md))
says "no model meets all targets; small fails the fewest (timing)". That is correct for the
raw targets. The recommendation above depends on the timing correction, and the owner must
accept that.

## 4. Earlier runs (superseded)

- 03:24 UTC, old launcher `aed7a45`: interrupted after 18 of 21 runs. It revealed the
  "capital I" harness bug and the timing bias.
- 03:40 UTC, old launcher: stopped after 4 runs.
- 03:40:56 UTC, new launcher in a fresh worktree: refused at the plan step, because
  re-downloading everything would have broken the floor.

None of these numbers are used above.
