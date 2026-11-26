# Native M5/M6 verification at `dcf5a94` (macOS, 2026-11-25)

Tested commit: `dcf5a9402904fe21f4fb595136cd95cc355c1b02` (phase2/m6-highlights, after the
independent-review fixes of PRs #16, #17, #19, #20, #21 and #22). This folder is added in a
later docs-only commit; the tested code is exactly `dcf5a94`.

Apple M3 Pro, macOS 26.5, isolated data folders, real 60 GiB floor (129 GiB free before and
after). Ollama on 127.0.0.1 only, `qwen3.5:4b` digest `7f3251fa…` (nothing downloaded),
MLX Whisper base. Synthetic footage only; nothing published.

| Check | Result |
|---|---|
| `scripts/test.sh` | backend 649 passed / 3 skipped; bench 47 / 1 skipped; frontend 44 |
| acceptance | 29/29 |
| smoke | pass; instance stopped |
| Alembic (empty DB) | one head 0012, downgrade 0006 and back, 32 triggers, no FK violations |
| Demo DB migration (disposable copy) | 0012, 32 triggers, no rows lost |
| M5/M6 native gate | 6/6 (stricter gate: ids map to stored times, evidence inside the clip, stored title and hook re-grounded on the clip's words) |

One highlight of five requested (121.3-133.5 s, editorial score 83, no deductions), inside a
scripted reaction (informative only). Two AI batches: one failed validation after 3
attempts, one accepted first time; 171 s end to end; $0. The rendered Short (not committed:
media) is 1080x1920, 30 fps, 14.70 s = 12.2 s clip + 2.5 s outro; inspected by contact
sheet: 35/65 layout, captions word-highlighted in sync with the speech, full RumbleFrame
outro. `CHECKSUMS.sha256` covers exactly the files committed in this folder (README.md included;
regenerated in a docs-only correction). Before regeneration it was the run folder's own
manifest, which listed files not committed here (raw logs, `acceptance.json`, port probes,
`git.txt`) and was written before the script appended its last two lines to `summary.txt`.
Integrity of the copies was checked against that original manifest before it was replaced:
`ai-runs.json`, `highlights.json` and `m6-native.json` (from the run's `m6-native/` subfolder)
match their original hashes, and `summary.txt` matches its original hash without its two
last lines ("ALL PASSED at dcf5a94…" and "Evidence: …"), which the script appended after
writing the manifest. Every other committed file matched its original
hash unchanged.
