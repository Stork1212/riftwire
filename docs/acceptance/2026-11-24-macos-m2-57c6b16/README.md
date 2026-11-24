# M2 Mac verification — commit 57c6b16 (corrections after the independent audit of 63a2e5e)

Run by the owner on 2026-11-24 19:47 UTC with `verify-script.sh` (pinned to
`57c6b1637e498d0c621d53604ec545d3636d38fb`, clean tree before and after, `pipefail`; stops any
leftover smoke-test instance first; refuses to start while another verification run is
active). Complete, unedited outputs (`*.log` stored as `*.txt`).

| Stage | Exit | Result |
|---|---|---|
| `scripts/test.sh` | 0 | ruff clean; backend **391 passed, 3 skipped** (no second filesystem), including the macOS volume-identity test and the 17 audit regression tests, 1 third-party warning; bench 47 + 1 skip; frontend **36/36**; build OK |
| `scripts/acceptance.py --min-free-gib 60` | 0 | **28/28** |
| `scripts/smoke_test.sh` | 0 | **30 PASS, 0 FAIL, 0 SKIP** |

Free space (`/System/Volumes/Data`, APFS): 132.27 GiB before, 132.26 GiB after.

**Discarded runs:** two earlier runs of the same script (19:42:26Z and 19:42:33Z) were
started 7 s apart and overlapped. They share ports 8765 (acceptance) and 8000 (smoke), so
each run's acceptance and smoke steps reached the other run's server; for example the first
failed with "a game with slug 'test-game' already exists", created by the second. Both
runs' test suites passed. They are not evidence and are not included; the script now takes
a lock so a second copy refuses to start.
