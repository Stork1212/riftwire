# M4 native run at 6476044 (macOS, real MLX Whisper): one genuine failure

Run on the owner's Mac (Apple M3 Pro, macOS 26.5) with `~/Claude/m4-native-6476044.sh`,
2026-11-25 16:07 UTC: real 60 GiB floor, isolated data folders, synthetic footage with
macOS speech, mlx-whisper 0.4.3 with the installed `whisper-base` model. Nothing was
downloaded.

| Step | Result |
|---|---|
| `scripts/test.sh` | exit 0 |
| `scripts/acceptance.py` (port 18931, 60 GiB floor) | 29/29 |
| `scripts/smoke_test.sh` (port 18930) | exit 0; instance stopped afterwards |
| Alembic (empty database) | one head 0010; up/down 0006/up; 22 triggers (8 transcript); FK clean |
| Demo database (disposable copy) | migrated; no rows lost |
| `scripts/m4_native.py` | **10/11 — FAIL** |

**The failure (`phrase@117.5s`).** The phrase placed across the 120 s chunk boundary came
out as "the police fountain helicopter is right above us take the next exit now at the
harbor" (`transcript-words.json`, words 12-27):

- **Chunk 0 (0-124 s):** Whisper repeated the *first* phrase over the boundary phrase.
  Those words have probabilities of 0.0-0.17 and collapsed, zero-length timestamps at
  119.98 s. This is the known repetition failure when decoding is conditioned on
  previous text after a long silence.
- **Merge v1:** gave each word to the chunk whose core contains its midpoint. It
  therefore kept chunk 0's copy and dropped chunk 1's correct words before 120 s.

**Fix (in the next commit; a new transcription configuration):**
- **Decoding:** `condition_on_previous_text=False`, recorded in the configuration
  fingerprint.
- **Merge v2:** the overlap zone goes to the chunk whose words there are clearly better.
  Degenerate words count as 0. Otherwise the core edge stands.
- **Regression test:** the failure is a unit test built from this run's stored words
  (`test_merge_v2_gives_the_overlap_to_the_chunk_with_real_words`).
- **Raw output:** each chunk's raw engine output is still stored untouched.

**What passed natively:**
- The other two phrases: 12/12 and 6/6 words.
- Boundary words: none duplicated, all in order.
- The model checksum matches an independent hash.
- Raw words cannot be changed.
- The highlight scan and the captions reused the one transcript; asking for it again
  ran nothing.
- Speed: 4.3 s for 150 s of audio in two chunks (2.48 s and 1.68 s).
