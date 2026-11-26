# Native M5/M6 verification at `d85634e` (macOS, 2026-11-26 UTC)

Tested commit: `d85634e507444fdd21c6c7484d84fa2946b6a029` (phase2/m6-highlights after the
Codex review fixes: signal time base, AI-job supersession and manual retry, boundary words
refused at window edges, fixture fade). This folder is added in a later docs-only commit.

Apple M3 Pro, macOS 26.5, isolated data folders, real 60 GiB floor (128.6 GiB free before,
125.8 GiB after). Ollama on 127.0.0.1 only, `qwen3.5:4b` digest `7f3251fa…` (nothing
downloaded), MLX Whisper base. Synthetic footage only; nothing published.

| Check | Result |
|---|---|
| `scripts/test.sh` | backend 655 passed / 3 skipped; bench 47 / 1 skipped; frontend passed |
| acceptance | 29/29 |
| smoke | pass; instance stopped |
| Alembic (empty DB) | one head 0012, downgrade 0006 and back, 32 triggers, no FK violations |
| Demo DB migration (disposable copy) | pass, no rows lost |
| M5/M6 native gate | 6/6 |

One highlight of five requested (121.3-133.5 s, editorial score 83, no deductions), inside a
scripted reaction (informative only). Two AI batches: one failed validation after 3
attempts, one accepted first time; 313 s end to end; $0. The rendered Short (not committed:
media) is 1080x1920, 14.70 s = 12.2 s clip + 2.5 s outro; inspected by contact sheet:
35/65 layout, captions word-highlighted in sync with the speech, full RumbleFrame outro.

`CHECKSUMS.sha256` covers exactly the files committed here. Before copying, every file was
checked against the run folder's own manifest: all match, and `summary.txt` matches
without its last two lines, which the script appends after writing that manifest.
