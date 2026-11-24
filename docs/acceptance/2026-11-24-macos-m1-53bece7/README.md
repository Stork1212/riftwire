# M1 Mac verification — commit 53bece7 (final code for re-review)

Run by the owner on 2026-11-24 14:57 UTC with `verify-script.sh` (pinned to
`53bece76d6bf6e224203a7f8696d77a3cffd7f06`, clean tree before and after, `pipefail`).
Complete, unedited outputs (`*.log` stored as `*.txt`). The leftover smoke instance from an
earlier run was stopped first.

| Stage | Exit | Result |
|---|---|---|
| `scripts/test.sh` | 0 | ruff clean; backend **239 passed, 3 skipped** (no second filesystem), incl. the macOS identity test, 1 third-party warning; bench 47 + 1 skip; frontend 17/17; build OK; **74 s** (pytest 48.5 s: speed regression fixed) |
| `scripts/acceptance.py --min-free-gib 60` | 0 | **22/22** (25 s) |
| `scripts/smoke_test.sh` | 0 | **30 PASS, 0 FAIL, 0 SKIP** |

Free space (`/System/Volumes/Data`, APFS): 135.87 GiB before, 132.02 GiB after. All of
RIFTWIRE's test data on the Mac totals 52 MB, so the change came from other system
activity. It stayed far above the 60 GiB floor.
