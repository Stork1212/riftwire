# M4 native run at 6fb3485 (macOS, real MLX Whisper): ALL PASSED

Run on the owner's Mac (Apple M3 Pro, macOS 26.5) with `~/Claude/m4-native-6fb3485.sh`,
2026-11-25 17:12 UTC: real 60 GiB floor, isolated data folders, synthetic footage with
macOS speech, mlx-whisper 0.4.3 with the installed `whisper-base` model. Nothing was
downloaded, the live demo was not touched, and the working tree was clean afterwards.

| Step | Result |
|---|---|
| `scripts/test.sh` | exit 0 — backend 496 passed + 3 skipped, bench 47 + 1 skipped, frontend 41, build |
| `scripts/acceptance.py` (port 18931, 60 GiB floor) | 29/29 |
| `scripts/smoke_test.sh` (port 18930) | exit 0; instance stopped afterwards |
| Alembic (empty database) | one head 0011; up/down 0006/up; 26 triggers (14 earlier + 12 M4); FK clean |
| Demo database (disposable copy) | migrated to 0011; no rows lost; FK clean |
| `scripts/m4_native.py` | **16/16** |

**The boundary phrase is fixed** (merge v2 plus `condition_on_previous_text=False`).
"Seven orange taxis crashed into the fountain at the harbor" now crosses the 120 s chunk
boundary with every word once and in order. The check counts 9/10 only because Whisper
wrote "Seven" as "7", which the check's normaliser drops.

**Other results:**
- Phrases at 5 s and 138 s: 12/12 and 6/6.
- Model checksum matches an independent hash; raw words immutable.
- The scan and the captions reuse the one transcript, and a second request runs nothing.
- Audio onsets fall within 100 ms of each phrase start.
- The visual frame count equals ffprobe's (4,500).
- In the evidence window across the boundary (115-127 s), all ids resolve and the
  fingerprint is stable.
- Transcription took 4.2 s for 150 s of audio.

**Known engine behaviour, not fixed here:** Whisper produced the word "you" at 57.5 s and
87.5 s, during long silences, with probability 0.34 (`transcript-words.json`, words 12-13).
- These are hallucinations, kept in the raw transcript exactly as the engine produced them.
- The audio signal run independently measures those spans as silence, and the evidence
  layer exposes both.
- Consumers that need speech should not trust a low-probability word that falls inside a
  measured silence. That rule belongs in M6 grounding, not in the raw layer.
