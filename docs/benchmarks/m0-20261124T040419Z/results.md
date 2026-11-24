# RIFTWIRE M0 benchmark results

- Machine: Apple M3 Pro · 18.0 GB · macOS 26.5.2
- Python 3.13.2 · ffmpeg version 9.1
- Engine: mlx-whisper 0.4.3 · mlx 0.32.4 · torch installed: False
- Started 2026-11-24T04:04:22Z · finished 2026-11-24T04:13:17Z

## Transcription

| model | dataset | audio min | RTF | WER | start med ms | start p95 ms | start bias ms | end bias ms | ≤100 ms | RSS GB | footprint GB | MLX GB |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| tiny | timing-1m-clean | 1.0 | 0.016 | 1.0% | 121 | 318 | -104 | 45 | 37.9% | 0.44 | 0.85 | 0.33 |
| tiny | timing-10m-clean | 10.1 | 0.006 | 10.9% | 127 | 315 | -110 | 36 | 34.9% | 0.54 | 1.87 | 0.71 |
| tiny | timing-10m-noisy | 10.0 | 0.007 | 12.2% | 114 | 351 | -51 | 75 | 44.1% | 0.55 | 1.85 | 0.70 |
| tiny | timing-10m-music | 10.1 | 0.006 | 1.5% | 123 | 319 | -105 | 46 | 37.0% | 0.55 | 1.82 | 0.71 |
| tiny | timing-60m-clean | 60.0 | 0.006 | 5.8% | 128 | 290 | -111 | 32 | 35.2% | 0.75 | 4.23 | 2.64 |
| tiny | natural-10m-clean | 10.0 | 0.010 | 6.5% | — | — | — | — | — | 0.61 | 2.12 | 0.71 |
| tiny | natural-10m-noisy | 10.0 | 0.009 | 7.0% | — | — | — | — | — | 0.62 | 2.22 | 0.70 |
| tiny | natural-10m-music | 10.0 | 0.009 | 3.5% | — | — | — | — | — | 0.61 | 2.14 | 0.71 |
| base | timing-1m-clean | 1.0 | 0.018 | 0.0% | 280 | 488 | -280 | -147 | 3.8% | 0.57 | 1.24 | 0.61 |
| base | timing-10m-clean | 10.1 | 0.010 | 2.7% | 276 | 430 | -276 | -141 | 4.4% | 0.67 | 2.50 | 0.96 |
| base | timing-10m-noisy | 10.0 | 0.009 | 8.5% | 283 | 447 | -283 | -146 | 5.2% | 0.66 | 2.37 | 0.96 |
| base | timing-10m-music | 10.1 | 0.009 | 1.1% | 276 | 428 | -276 | -139 | 4.7% | 0.67 | 2.33 | 0.96 |
| base | timing-60m-clean | 60.0 | 0.009 | 2.1% | 281 | 433 | -281 | -144 | 2.9% | 0.87 | 4.76 | 2.75 |
| base | natural-10m-clean | 10.0 | 0.014 | 1.4% | — | — | — | — | — | 0.75 | 3.04 | 0.96 |
| base | natural-10m-noisy | 10.0 | 0.014 | 3.8% | — | — | — | — | — | 0.71 | 2.64 | 0.96 |
| base | natural-10m-music | 10.0 | 0.018 | 3.7% | — | — | — | — | — | 0.77 | 3.04 | 0.96 |
| small | timing-1m-clean | 1.0 | 0.033 | 0.0% | 252 | 500 | -252 | -122 | 7.7% | 0.96 | 2.57 | 1.47 |
| small | timing-10m-clean | 10.1 | 0.023 | 3.6% | 251 | 455 | -250 | -109 | 5.5% | 1.07 | 4.77 | 1.67 |
| small | timing-10m-noisy | 10.0 | 0.024 | 3.7% | 254 | 411 | -254 | -117 | 6.0% | 1.07 | 4.69 | 1.67 |
| small | timing-10m-music | 10.1 | 0.024 | 1.0% | 256 | 424 | -256 | -112 | 5.8% | 1.09 | 4.55 | 1.67 |
| small | timing-60m-clean | 60.0 | 0.023 | 1.1% | 254 | 417 | -254 | -115 | 5.4% | 1.18 | 7.22 | 3.12 |
| small | natural-10m-clean | 10.0 | 0.032 | 0.7% | — | — | — | — | — | 1.21 | 5.81 | 1.67 |
| small | natural-10m-noisy | 10.0 | 0.032 | 2.1% | — | — | — | — | — | 1.16 | 5.20 | 1.67 |
| small | natural-10m-music | 10.0 | 0.032 | 0.2% | — | — | — | — | — | 1.22 | 5.72 | 1.67 |

## Targets per model

| model | speed | WER clean | WER noisy | timing | memory (RSS) | no failures | harness clean | all |
|---|---|---|---|---|---|---|---|---|
| tiny | ✅ | ❌ | ✅ | ❌ | ✅ | ✅ | ✅ | ❌ |
| base | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ | ❌ |
| small | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ | ❌ |

**Recommendation:** `mlx-whisper` / `small` (no_model_meets_all_targets). No model met all targets; small fails the fewest (timing). Owner decision required before M1.

## Signals (synthetic video)

- Source: {'seconds': 600, 'width': 1920, 'height': 1080, 'fps': 60, 'seed': 11} · encoder h264_videotoolbox · build 172.77 s
- Proxy: 19.21 s (31.2× realtime)
- scene_cuts: recall 100.0%, precision 100.0%, median error 0 ms, 252.6× realtime
- loudness_bursts: recall 100.0%, precision 100.0%, median error 24 ms, 1142.5× realtime

## Disk usage

- bench_venv: 0.60 GB
- models: 0.70 GB
- datasets_and_tts_cache: 0.26 GB
- signals_work: 0.49 GB
- results: 0.00 GB
- total: 2.06 GB

## Limitations

- Speech is synthesized with macOS `say`: cleaner and more regular than real commentary. Results measure engine accuracy, timing, speed and memory, not highlight usefulness.
- Word-timing ground truth exists only for the `timing-*` sets (words synthesized individually and concatenated with known gaps). WER targets are judged on the `natural-*` sets; timing-set WER is shown for information.
- Memory target (proposal): peak RSS ≤ 3 GB. On Apple Silicon RSS excludes Metal allocations, so the process peak footprint (includes MLX's buffer cache) and MLX's own peak are reported too.
- Decoding uses Whisper's temperature fallback, so repeated runs can differ slightly.
- Authorized real gameplay remains a separate evaluation set (owner condition 4).
