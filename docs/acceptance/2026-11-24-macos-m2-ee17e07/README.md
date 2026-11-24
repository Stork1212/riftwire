# M2 Mac verification — commit ee17e07 (dashboard layout refinement)

Run by the owner on 2026-11-24 19:10 UTC with `verify-script.sh` (pinned to
`ee17e07800bd98c1a93ca2b0fab9ea48a82d3974`, clean tree before and after, `pipefail`; stops any
leftover smoke-test instance first). Complete, unedited outputs (`*.log` stored as `*.txt`).

| Stage | Exit | Result |
|---|---|---|
| `scripts/test.sh` | 0 | ruff clean; backend **374 passed, 3 skipped** (no second filesystem), including the macOS volume-identity test, 1 third-party warning; bench 47 + 1 skip; frontend **35/35**; build OK |
| `scripts/acceptance.py --min-free-gib 60` | 0 | **28/28** |
| `scripts/smoke_test.sh` | 0 | **30 PASS, 0 FAIL, 0 SKIP** |

Free space (`/System/Volumes/Data`, APFS): 132.52 GiB before and after.
