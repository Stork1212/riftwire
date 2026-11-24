# macOS dashboard check — 2026-11-24 (commit `0ae8c2f`, `main`)

Performed by the agent in the Claude desktop app's built-in browser **on the owner's Mac**,
against the instance left running by `scripts/smoke_test.sh` (isolated data dir, two synthetic
clips). Evidence of the launch-script checks: `2026-11-24-macos-smoke-0ae8c2f.txt` (30/30 PASS).

| Page | Observed (real data) | Result |
|---|---|---|
| Overview `/` | Recordings 2; imported duration 10s; pending 0, running 0, completed 6, failed 0; storage 67.7 GB free of 460 GB, 60.0 GB safety minimum, 7.7 GB available; worker Running, concurrency 1, heartbeat < 1 s | ✅ |
| Recordings `/recordings` | 2 rows (`smoke-chase.webm` 0:04 1280×720 509 KB; `smoke-heist.mp4` 0:06 1920×1080 4.1 MB), both Ready; both thumbnails loaded (480×270) | ✅ |
| Recording detail | `smoke-heist.mp4`: 29.97 fps (30000/1001), h264/aac, thumbnail loaded 480×270; Store & verify / Metadata / Thumbnail all Completed; reached by clicking the filename link | ✅ |
| Processing Queue `/queue` | 6 jobs (INGEST/METADATA/THUMBNAIL × 2), all Completed, 1/3 attempts | ✅ |
| System `/system` | v0.1.0; Backend Ready; Worker Running; FFmpeg/ffprobe 9.1 Ready; VideoToolbox Available; data dir = isolated smoke dir; no errors recorded | ✅ |
| Import `/import` | Page renders; file input present; Upload disabled until a file is selected | ✅ |
| Browser console | No errors on any page | ✅ |

Notes:
- Uploading through the file picker was not automated on the Mac (the built-in browser tool
  cannot choose local files); the same upload path was verified with a 111 MB file in headless
  Chrome in the dev container, and imports on the Mac were verified through the same API by the
  smoke test.
- Cosmetic observation: long filenames wrap in the Recordings table; clicking the filename text
  works (verified). No change made.
