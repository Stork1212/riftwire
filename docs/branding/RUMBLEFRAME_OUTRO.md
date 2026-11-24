# RumbleFrame Media YouTube outro

This is the approved 2.5-second outro for the RumbleFrame Media parent channel. It is a brand asset and handoff for the planned video-rendering phase; it does not change the current Phase 1 application behavior.

## Files

- `branding/outro/RumbleFrame_Outro_9x16_1080x1920_30fps_2p5s.mp4` — final rendered bumper. H.264, 1080 × 1920, 30 fps, exactly 2.5 seconds; AAC stereo 48 kHz contains the original, brief digital tick.
- `branding/outro/RumbleFrame_Outro_Preview.jpg` — preview frame.

## Approved design

Near-black background, white RF badge and RUMBLEFRAME wordmark, “THE MOMENTS WORTH REPLAYING” tagline, outlined “SUBSCRIBE” prompt and four corner brackets. The video has a subtle camera move and quick fade-in. The tick is synchronized about 0.72 seconds after the bumper begins.

## Renderer behavior when Phase 4 is approved

1. Append the bumper after the selected clip payoff on every final RumbleFrame Media YouTube Short.
2. Keep the gameplay audio audible across the bumper; mix the embedded tick quietly over it. Do not replace or mute the clip soundtrack with the bumper's audio stream.
3. Add the bumper once to the final output, even when a job is retried or resumed. Do not append a second outro to an already-finalized render.
4. Keep the CTA and master monochrome identity as supplied. Platform or game-channel variants need a separately approved brand asset.

The asset has been inspected and its duration, dimensions, frame rate, codec and audio stream were checked with `ffprobe`. No rendering-pipeline integration has been made yet.
