# M5/M6 native run at a22c2c7 (real MLX Whisper + real Qwen3.5 4B): failed (render)

Run on the owner's Mac (Apple M3 Pro, macOS 26.5) with `~/Claude/m6-native-a22c2c7.sh` on
2026-11-25 at 19:27 UTC.

**Conditions:**
- Real 60 GiB floor and isolated data folders.
- Ollama listened on 127.0.0.1 only. The `qwen3.5:4b` digest matched `7f3251fa…`.
- Nothing was downloaded and nothing was published.

| Step | Result |
|---|---|
| `scripts/test.sh` | exit 0. Backend 584 passed + 3 skipped, bench 47 + 1 skipped, frontend, build |
| Acceptance (60 GiB floor) | 29/29 |
| Smoke | exit 0; the instance was stopped |
| Alembic | one head 0012; up/down/up; 30 triggers; FK clean |
| Demo database (disposable copy) | migrated; no rows lost |
| `scripts/m6_native.py` | **crashed while rendering** after 6 passing checks |

## What worked with real models
- **End to end:** the request completed in 226 s.
- **Runs:** 2 AI runs on the real model, each recorded at $0.
- **Batch 0 (3 windows):** **failed validation 3 times**. Each time the model selected one window twice, so ~193 s produced nothing usable.
- **Batch 1:** accepted on the first attempt in 26.6 s (559 output tokens).
- **Result:** 1 of 5 highlights was returned. The summary explains why there are fewer.
  - The highlight is **#1, 121.3–167.8 s**: "Game is Broken! Car Flying & Escape". It overlaps the scripted reaction moment (informative only).
  - Its bounds and evidence ids are all valid.

## Failures and fixes (next commit)
1. **The clip was longer than a Short can be.**
   - **What happened:**
     - The model picked 121.62–165.78 s (44.2 s).
     - Boundary refinement then added a continuing word and the padding, making it 46.5 s.
     - The 35/65 renderer correctly refused anything over 45 s.
     - The script crashed instead of reporting the failure.
   - **Fixes:**
     - Windows are now capped at the target format's limit (45 s), so the model cannot pick a longer clip.
     - Refinement never exceeds that limit.
     - A clip over the limit is rejected with its reason.
     - The script reports a refused render as a failed check.
2. **77 % of the clip was dead air.**
   - **What happened:** the model's end word was "Mission complete" at 165 s, after 27 s of measured silence. A 10-point penalty still left the clip at 75.
   - **Fix:** refinement now cuts a long stretch of **measured** silence (≥ 4 s, confirmed by the audio run). It keeps the side that holds most of the model's own cited evidence, provided that side lasts at least 8 s, and records the change.
   - **Regression tests:** both cases, built from this run's timings.
3. **Repairs without progress (latency).**
   - **What happened:** the validator's message ("each window may be selected at most once") did not say which window. The model repeated the same mistake twice.
   - **Fix:** the message now names the window and how many times it appears, and says to keep one candidate per window.
   - **Validation is unchanged.**
