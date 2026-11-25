# Native Mac verification — PR #14 head `7911092` (2026-11-25 UTC): **all passed**

`7911092` adds the independent-review fix: the outro's post-cut source-audio tail is checked
against the rights scope and dropped when it would exceed it. The full 2.5 s outro is kept.
The runs were owner-run on the development Mac (Apple M3 Pro, macOS 26.5.2).

**Run 1: full verification, 15:05Z** ([`verify/`](verify/summary.txt)), with
`integration-e2e-7911092.sh`:
- Real 60 GiB floor: free space went from 120.52 to 120.45 GiB.
- Isolated data folders and ports.
- Real local Whisper (MLX `base`) and macOS speech, with a stand-in downloader.
- Both approved outros, taken from PR #6 and PR #11 and checked by SHA-256.

| Step | Result |
|---|---|
| `scripts/test.sh` | backend **460 passed, 3 skipped** (the M1 second-filesystem tests); bench 47 + 1; frontend 38; ruff, `tsc`, ESLint, build OK |
| Acceptance (port 18931) | **29/29** |
| Alembic (empty disposable DB) | single head `0009`; upgrade, downgrade and upgrade again: OK |
| Demo-DB copy | `0008` → `0009`, no lost rows, 14 triggers, FK clean (the live demo was not touched) |
| End-to-end (port 18941) | **14/14**: both Shorts 17.50 s / 525 frames; PR #6 outro frames 1–75 intact; original audio r = 1.00; red word highlights correct |
| Smoke (port 18930) | **did not run**: the port was still held by the smoke instance left by the 04:43Z run (`smoke_test.sh` leaves its instance up for a dashboard check) |

**Run 2: smoke re-run, 15:13Z** ([`smoke-rerun/`](smoke-rerun/summary.txt)), with
`pr14-smoke-7911092.sh`:
- **Cleanup first:** three leftover smoke instances, all isolated RIFTWIRE test instances
  under `riftwire-env/smoke-data-*` (02:45Z, 02:47Z, 04:46Z), were stopped with
  `scripts/stop.sh`. Nothing else was touched.
- **Smoke at `7911092`:** port 18930, real floor: **30 PASS / 0 FAIL / 0 SKIP**.
- **Afterwards:** the new instance was stopped, and the working tree stayed clean.

**Not committed** (on the Mac in `pr14-verify-7911092-20261125T150543Z/`): `e2e-short.mp4`,
`e2e-short-v1-silent-outro.mp4` and `demo-db-before.db`. Their SHA-256 values are in
[`CHECKSUMS.sha256`](CHECKSUMS.sha256).

**What this does not show:**
- **No real platform acquisition:** only the stand-in downloader ran.
- **The footage, speech and permissions are synthetic.**
- **Highlights are the rule-based baseline,** not contextual AI.
- **The restricted-licence tail behaviour is covered by the 11 regression tests in the
  suite,** which passed here. The end-to-end run uses an unrestricted permission.
