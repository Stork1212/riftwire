# M5/M6 native run at ef218bc (real MLX Whisper + real Qwen3.5 4B): ALL PASSED

This ran on the owner's Mac (Apple M3 Pro, macOS 26.5) with `~/Claude/m6-native-ef218bc.sh`,
on 2026-11-25 at 20:48 UTC.

**Conditions:**
- Real 60 GiB floor and isolated data folders.
- Ollama listened on 127.0.0.1 only. The `qwen3.5:4b` digest matched `7f3251fa…`.
- Nothing was downloaded or published.
- The live demo, the SSD folders and PR #13 were not touched, and the working tree was clean afterwards.

| Step | Result |
|---|---|
| `scripts/test.sh` | exit 0 |
| Acceptance (60 GiB floor) | 29/29 |
| Smoke | exit 0; the instance was stopped |
| Alembic | one head 0012; up/down/up; 30 triggers; FK clean |
| Demo database (disposable copy) | migrated; no rows lost |
| `scripts/m6_native.py` | **6/6 gate checks** |

## The workflow on real models (synthetic footage, macOS speech, 180 s)
Recording → transcript → signals → windows → Qwen3.5 4B → ranked highlights → Short, **79 s end to end** (226 s at a22c2c7).

**AI runs:** 2, both **accepted on the first attempt** (47.1 s and 25.6 s; 1,244 and 580 output tokens), each recorded at $0. At a22c2c7, one batch had failed three repairs. One run like this does not show that the duplicate-window repair is fixed in general.

**Highlights:** 2 of the 5 requested were returned. The summary explains that only these passed validation, the cut and duplicate suppression.

| Rank | Span | Editorial score | Notes |
|---|---|---|---|
| #1 | 121.3–133.5 s (12.2 s) | 81 | "Car is flying! GTA glitch escape" |
| #2 | 41.4–54.0 s (12.6 s) | 72 (82 − 10, dead air) | "Cops Right Behind Us!" |

- Both overlap the scripted reaction moments. That is informative only: a single synthetic recording is not a quality measurement.
- Model confidence (0.85 and 0.95) is recorded separately. The editorial score is not a probability of going viral.

**Rendered Short of #1:**
- H.264 1080×1920 at 30 fps with AAC audio.
- 14.70 s long, which is the 12.2 s clip plus the full 2.5 s RumbleFrame outro from PR #6.
- The 35/65 layout has the title and synchronised word-highlighted captions. Its 22 caption words come from the stored transcript.
- The video file was reviewed but is not committed (media files are never committed).

**Limits:**
- Every output was synthetic. Highlight quality still needs labelled, authorized recordings.
- Latency comes from one run. It is not a benchmark.
