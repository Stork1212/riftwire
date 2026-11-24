# M2 Mac verification — commit 9cbe705

Run by the owner on 2026-11-24 16:22 UTC with `verify-script.sh` (pinned to
`9cbe705fdbaad7bc22d765c0fabf5fa1bf6d1905`, clean tree before and after, `pipefail`; stops any
leftover smoke-test instance first). Complete, unedited outputs (`*.log` stored as `*.txt`).
Also covers the M1 cache and marker changes merged in `8e5b5e3`.

| Stage | Exit | Result |
|---|---|---|
| `scripts/test.sh` | 0 | ruff clean; backend **365 passed, 3 skipped** (no second filesystem), including the macOS volume-identity test, 1 third-party warning; bench 47 + 1 skip; frontend 28/28; build OK |
| `scripts/acceptance.py --min-free-gib 60` | 0 | **28/28**, including M2-1 to M2-6 |
| `scripts/smoke_test.sh` | 0 | **30 PASS, 0 FAIL, 0 SKIP** |

Free space (`/System/Volumes/Data`, APFS): 131.62 GiB before, 131.59 GiB after.
