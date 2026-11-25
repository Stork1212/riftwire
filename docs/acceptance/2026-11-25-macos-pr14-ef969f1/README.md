# Native Mac verification — PR #14 head `ef969f1` (code `51c8fa4`), 2026-11-25 UTC

Owner-run on the development Mac (Apple M3 Pro, macOS 26.5.2) with the pinned script
[`verify-script.sh`](verify-script.sh) (`~/Claude/integration-e2e-ef969f1.sh`) and the
bundle `riftwire-pr14-ef969f1.bundle`. Started 04:43:23Z; **all passed**.

- **Real 60 GiB floor.** Free space was 127.57 GiB before and 127.48 GiB after; the
  evidence folder is 16 MB.
- **Isolated data folders and ports** 18930–18941.
- **Real local Whisper** (MLX `base`) and real macOS `say` speech.
- **Stand-in downloader.** No platform was contacted.
- **Approved RumbleFrame outros** extracted from PR #6 (`29f6a7b`) and PR #11 (`724d1be`),
  with SHA-256 verified.
- **The live demo** and its database were not touched: the migration ran on a `.backup`
  copy. The physical-SSD folders were not touched.

| Step | Result |
|---|---|
| `scripts/test.sh` | backend **448 passed, 3 skipped** (the existing M1 second-filesystem tests); bench 47 + 1 skip; frontend 38; ruff, `tsc`, ESLint, build OK |
| Acceptance (port 18931, floor 60 GiB) | **29/29** |
| Smoke (port 18930) | **30 PASS / 0 FAIL / 0 SKIP** |
| Alembic (empty disposable DB) | single head `0009`; upgrade → downgrade to `0006` → upgrade; 14 triggers; FK clean |
| Demo-DB copy migration | `0008` → `0009`, **no lost rows** (20 tables), 14 triggers, FK clean |
| End-to-end (port 18941) | **14/14**, listed below |
| Working tree afterwards | clean |

## End-to-end checks ([`e2e-results.json`](e2e-results.json))
1. A public link without permission is saved and explained, but not fetched: it is
   classified `public_web`, no record is written, nothing is queued, and acquisition is
   refused (403 `no_rights_record`).
2. A standing creator permission (acquire and analyse by `url_fetch`) with written
   platform-permission evidence. Both are **synthetic** test records.
3. The saved URL is queued on save with no prompt; `ACQUIRE` completes; the recording is
   READY and linked, with method `url_fetch`.
4. Acquisition history: `acquired_by_url` (stand-in, SHA-256) → `attached_to_source`
   under the creator permission. No `public_web` record.
5. Find highlights (queued scan): 2 candidates, the top one 0–25 s. This is the
   rule-based baseline.
6. Transcript evidence from **real Whisper `base`**: "Oh my God, the cops are right
   behind us. We got a problem. Hold on,".
7. A sampled frame, JPEG 480×270 at 19 s.
8. The `clips.txt` review range 18–33 s is accepted.
9. The edit-permission gate: export is refused (`use_not_in_scope`), then succeeds after
   the synthetic permission is superseded to include edit.
10. Short format: 1080×1920 at 30 fps, AAC 48 kHz, **17.50 s = 15 s + 2.5 s, 525
    frames**, A/V within 0 ms ([`e2e-media-specs.json`](e2e-media-specs.json)).
11. The approved PR #6 outro is intact: frames 1, 21, 38, 56 and 75 match the asset. The
    gameplay continues under the outro, with the sting at a −26.5 dBFS peak.
12. Red word highlights: 10 171 px under the spoken word, and 0 before speech or over the
    outro.
13. The original audio matches the source (r = 1.00).
14. A second Short with the silent v1 outro (no audio track) in `outro` mode: 17.50 s, 525
    frames, the outro matches the asset, silence during it, and the gameplay audio is
    identical.

## What this does not show
- **Real platform acquisition** is not verified; the downloader was a stand-in.
- **The footage, speech and permissions are synthetic.** They are not the owner's or a
  creator's content.
- **Highlights are the rule-based baseline,** not contextual AI.

## Files not committed
These stay on the Mac in
`~/Claude/riftwire-env/pr14-verify-ef969f1-20261125T044323Z/`: `e2e-short.mp4`,
`e2e-short-v1-silent-outro.mp4` and `demo-db-before.db`. Their SHA-256 values are at the
end of [`CHECKSUMS.sha256`](CHECKSUMS.sha256). Logs are stored as `.txt`.
