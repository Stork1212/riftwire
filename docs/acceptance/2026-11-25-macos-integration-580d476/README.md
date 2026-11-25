# Native Mac verification — integration `580d476` (2026-11-25 UTC)

Owner-run on the development Mac (Apple M3 Pro, macOS 26.5.2, Python 3.13.2, FFmpeg 9.1,
Pillow 13.0.0, mlx-whisper 0.4.3) with pinned scripts. Both runs used an isolated data
folder, separate ports and the **real 60 GiB floor** (free space stayed ≈127.4 GiB).

The live demo (port 18013, its data folder and launchd jobs) and the physical-SSD test
folders were not touched. The demo database was only read through `sqlite3 .backup` into a
disposable copy; the migration test ran on that copy.

| Run | Commit | Folder | Result |
|---|---|---|---|
| URL acquisition alone | `b13b91f` (`phase2/url-download`) | [`url-verify-b13b91f/`](url-verify-b13b91f/) | **All passed**: backend 417 + 3 skipped, bench 47 + 1, frontend 38, build, acceptance 29/29 (port 18901), smoke 30/0/0 (port 18900) |
| Integration | `580d476` (`phase2/integration`) | [`integration-580d476/`](integration-580d476/) | **All passed**: backend 430 + 3 skipped, bench 47 + 1, frontend 38, build, acceptance 29/29 (port 18911), smoke 30/0 (port 18910), demo-DB migration PASS, end-to-end 11/11 (port 18921) |

The 3 backend skips are the existing M1 second-filesystem tests ("no second filesystem
available"; macOS has no `/dev/shm`), as in every earlier Mac run; the bench skip is the
existing one from M0.

## End-to-end flow (integration run)
Saved URL → queued `ACQUIRE` → linked recording → Find highlights (queued `HIGHLIGHT_SCAN`)
→ review a timestamp (`clips.txt`, 18–33 s) → export a 35/65 Short. Driver:
[`integration-580d476/e2e.py`](integration-580d476/e2e.py); results:
[`e2e-results.json`](integration-580d476/e2e-results.json).

| Check | Result |
|---|---|
| Saved URL → queued acquisition → linked recording | PASS — `public_web` recorded, `ACQUIRE` completed, recording READY, method `url_fetch` |
| Acquisition history | PASS — `recorded_public_web` → `acquired_by_url` (stand-in, SHA-256 recorded) → `attached_to_source` |
| Find highlights | PASS — 2 candidates; top 0–25 s, cues reaction + pursuit |
| Transcript evidence | PASS — excerpt from real Whisper `base` over real macOS `say` speech |
| Sampled frame | PASS — 480×270 JPEG at 19 s ([`e2e-sampled-frame.jpg`](integration-580d476/e2e-sampled-frame.jpg)) |
| Review a timestamp | PASS — `clips.txt` range 18–33 s accepted |
| Export gate | PASS — refused with `creator_permission_needed`, exported after a **synthetic** creator permission |
| Format | PASS — 1080×1920, audio, 17.00 s = 15 s clip + 2 s outro |
| Outro | PASS — last 2 s are the synthetic outro (see the 2.5 s discrepancy below) |
| Red word highlights | PASS — 10 167 red px under the spoken word, 0 before speech ([`e2e-caption-frame.png`](integration-580d476/e2e-caption-frame.png)) |
| Original audio | PASS — r = 1.00 against the source |

## What this evidence does and does not show
- **Stand-in downloader only.** `yt-dlp` on PATH was a script that writes a synthetic
  60 s clip. No platform was contacted, and **no real authorized URL acquisition has been
  verified**. The Twitch URL in `e2e-clips.txt` is a fixture.
- **Synthetic footage and permission.** Gameplay was `testsrc2` in grayscale, speech was
  macOS `say`, the outro was a synthetic magenta clip, and the creator permission was a
  test record created by the driver. None of it is the owner's or a creator's content.
- **The outro is not the approved RumbleFrame outro.** The code trims any outro to 2 s;
  the approved outro is 2.5 s. See `docs/INTEGRATION_REVIEW.md` §7.
- **Highlights are the rule-based baseline**, not contextual AI. See
  `docs/INTEGRATION_REVIEW.md` §6.

## Files not committed
`e2e-short.mp4` (the exported Short) and `demo-db-before.db` (the disposable demo-database
copy) stay on the Mac in `~/Claude/riftwire-env/integration-e2e-580d476-20261125T024522Z/`:
media and databases are never committed. Their SHA-256 values are at the end of
[`CHECKSUMS.sha256`](CHECKSUMS.sha256). Logs are stored as `.txt` because `*.log` is
ignored.
