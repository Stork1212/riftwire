# M1 Mac verification — commit 25a2734 (independent-review corrections)

Run by the owner on 2026-11-24 14:39 UTC with `verify-script.sh` (pinned to
`25a27341b68cdbef78e2b1025e7fa4771cee133f`, clean tree before and after, `pipefail`).
Complete, unedited outputs (`*.log` stored as `*.txt`).

| Stage | Exit | Result |
|---|---|---|
| `scripts/test.sh` | 0 | backend 237 passed, 3 skipped (no second filesystem), incl. the macOS identity test; bench 47 + 1 skip; frontend 17/17; build OK. **472 s** (was 61 s): speed regression, fixed in the next commit |
| `scripts/acceptance.py --min-free-gib 60` | 0 | 22/22 (45 s, was 24 s) |
| `scripts/smoke_test.sh` | 1 | **Not a test result.** Port 8000 was still held by the smoke instance left running by the previous verification run (by design, for its dashboard check) |

Superseded by the Mac run of the commit with the root-keyed identity cache.
