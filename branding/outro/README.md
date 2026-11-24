# RumbleFrame YouTube outro

Approved 2.5-second closing bumper for RumbleFrame Media YouTube Shorts.

## Deliverables

- `RumbleFrame_Outro_9x16_1080x1920_30fps_2p5s.mp4` — final video bumper with a quiet, original digital tick.
- `RumbleFrame_Outro_Still_Reference.png` — editable visual reference.
- `RumbleFrame_Outro_Preview.png` — representative frame from the rendered video.

## Technical details

- Video: 1080 × 1920, 9:16, H.264 High Profile, yuv420p, 30 fps, exactly 2.5 seconds.
- Audio sting: AAC stereo at 48 kHz in the MP4; the uncompressed source is stereo PCM, 48 kHz, exactly 2.5 seconds. One short tick begins about 0.72 seconds in.
- The MP4 audio contains only the sting. Preserve the source gameplay audio under the bumper and mix the embedded sting quietly over it; never replace the clip soundtrack with the bumper audio.
- Visual: near-black background, centered white RF badge and RUMBLEFRAME wordmark, tagline, Subscribe capsule, four frame brackets, subtle movement and a short fade-in.

## Renderer handoff

When the planned rendering phase is implemented, append the MP4 after the final selected clip content in every RumbleFrame Media YouTube Short. Keep the payoff and its natural audio intact before the bumper, mix the sting under the original audio, and finish the output without adding another blank end card. Apply the outro once per final export, including retries; do not duplicate it when a render job is retried or resumed.

The asset is for the YouTube parent channel. Keep the same master identity for network consistency; game-specific channel variants can be made later with their own CTA or accent colour if the owner approves them.
