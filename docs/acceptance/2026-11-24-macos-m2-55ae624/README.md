# M2 Mac verification — commit 55ae624 (corrections after the independent review)

Run by the owner on 2026-11-24 17:12 UTC with `verify-script.sh` (pinned to
`55ae624aa50dd52b3df634b73c6ed6b735dbd15d`, clean tree before and after, `pipefail`; stops any
leftover smoke-test instance first). Complete, unedited outputs (`*.log` stored as `*.txt`).

| Stage | Exit | Result |
|---|---|---|
| `scripts/test.sh` | 0 | ruff clean; backend **374 passed, 3 skipped** (no second filesystem), including the macOS volume-identity test, 1 third-party warning; bench 47 + 1 skip; frontend **35/35**; build OK |
| `scripts/acceptance.py --min-free-gib 60` | 0 | **28/28** |
| `scripts/smoke_test.sh` | 0 | **30 PASS, 0 FAIL, 0 SKIP** |

Free space (`/System/Volumes/Data`, APFS): 131.55 GiB before and after.
