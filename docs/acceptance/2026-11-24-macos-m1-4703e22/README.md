# M1 Mac verification — commit 4703e22

Run by the owner on 2026-11-24 14:14 UTC with `verify-script.sh` (pinned to
`4703e2201e2dcbbc845a876f7c235c3c5011f52e`, clean tree checked, `pipefail`, per-stage exit codes).
Complete, unedited outputs. The script wrote `*.log`; they are stored here as `*.txt`
(`test`, `acceptance`, `smoke`, `git`) because the repository ignores `*.log`.

| Stage | Exit | Result |
|---|---|---|
| `scripts/test.sh` | 0 | ruff clean; backend 218 passed, 3 skipped (no second filesystem), 1 third-party warning; bench 47 passed, 1 skipped; frontend 17/17; build OK |
| `scripts/acceptance.py --min-free-gib 60` | 0 | 22/22 |
| `scripts/smoke_test.sh` | 0 | 30 PASS, 0 FAIL, 0 SKIP |

Free space on `/System/Volumes/Data` (APFS): 69.80 GiB before and after. Working tree clean
after the run (`tree-after.txt` is empty).
