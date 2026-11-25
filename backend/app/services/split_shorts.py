"""35/65 vertical Shorts layout with timed, burned-in word highlights."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH = 1080
UPPER_HEIGHT = 672
LOWER_HEIGHT = 1248
_FONT_PATHS = (
    Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
)


def _font(size: int):
    for path in _FONT_PATHS:
        if path.is_file():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size=size)


def overlays(folder: Path, words: list[dict], title: str, length_ms: int) -> tuple[Path, Path]:
    """Draw the title and a sparse PNG timeline timed to Whisper's clip-relative words."""
    header = Image.new("RGBA", (WIDTH, UPPER_HEIGHT))
    draw = ImageDraw.Draw(header)
    title = " ".join(title.split())[:80]
    if title:
        face = _font(44)
        lines: list[str] = []
        for word in title.split():
            if lines and draw.textlength(f"{lines[-1]} {word}", font=face) <= 900:
                lines[-1] += f" {word}"
            else:
                lines.append(word)
        for index, line in enumerate(lines):
            while draw.textlength(line, font=face) > 900:
                line = line[:-2] + "…"
            y = UPPER_HEIGHT - 75 - 60 * (len(lines) - index - 1)
            width = draw.textlength(line, font=face)
            draw.rounded_rectangle((540-width/2-20, y-27, 540+width/2+20, y+27),
                                   12, fill="white")
            draw.text((540, y), line, font=face, fill="black", anchor="mm")
    header_path = folder / "split-title.png"
    header.save(header_path)

    blank = folder / "caption-blank.png"
    Image.new("RGBA", (WIDTH, 145)).save(blank)
    spoken = []
    cursor = 0
    for word in words:
        token = " ".join(word["w"].split())[:32]
        start = max(cursor, round(word["start_ms"]), 0)
        end = min(length_ms, round(word["end_ms"]))
        if token and end > start:
            spoken.append((token, start, end))
            cursor = end

    timeline = ["ffconcat version 1.0"]

    def hold(path: Path, duration: int) -> None:
        if duration > 0:
            timeline.extend([f"file '{path.name}'", f"duration {duration / 1000:.3f}"])

    cursor = 0
    for index, (_, start, end) in enumerate(spoken):
        hold(blank, start - cursor)
        group = index // 4 * 4
        tokens = [item[0].upper() for item in spoken[group:group + 4]]
        path = folder / f"caption-{index}.png"
        _caption(path, tokens, index - group)
        hold(path, end - start)
        cursor = end
    hold(blank, length_ms - cursor)
    timeline.append(f"file '{blank.name}'")
    caption_path = folder / "split-captions.ffconcat"
    caption_path.write_text("\n".join(timeline) + "\n", encoding="utf-8")
    return header_path, caption_path


def _caption(path: Path, tokens: list[str], active: int) -> None:
    frame = Image.new("RGBA", (WIDTH, 145))
    draw = ImageDraw.Draw(frame)
    size = 56
    while True:
        face = _font(size)
        widths = [draw.textlength(token, font=face) for token in tokens]
        total = sum(widths) + 24 * (len(tokens) - 1)
        if total <= 900 or size <= 16:
            break
        size -= 2
    x = (WIDTH - total) / 2
    for index, (token, width) in enumerate(zip(tokens, widths, strict=True)):
        if index == active:
            draw.rounded_rectangle((x-9, 40, x+width+9, 108), 8, fill="#ec2028")
        draw.text((x, 74), token, font=face, fill="white", anchor="lm", stroke_width=3,
                  stroke_fill="#111114")
        x += width + 24
    frame.save(path)


def video_filters(facecam: bool, landscape: bool) -> str:
    upper = (
        "crop=trunc(iw*.24/2)*2:trunc(ih*.267/2)*2:0:trunc(ih*.433/2)*2,"
        if facecam else ""
    )
    # Keep the whole gameplay frame for portrait inputs. Wide recordings use a centered crop.
    bottom = (
        "scale=1080:1248:force_original_aspect_ratio=increase,crop=1080:1248"
        if landscape else
        "scale=1080:1248:force_original_aspect_ratio=decrease,pad=1080:1248:(ow-iw)/2:(oh-ih)/2"
    )
    return (
        "[0:v]fps=30,split[t][b];"
        f"[t]{upper}scale=1080:672:force_original_aspect_ratio=increase,"
        "crop=1080:672,setsar=1[top];"
        f"[b]{bottom},setsar=1[bottom];"
        "[top][bottom]vstack[panels];"
        "[panels][1:v]overlay=shortest=1[header];"
        "[2:v]fps=30[caps];[header][caps]overlay=0:633:eof_action=pass,"
        "format=yuv420p,setpts=PTS-STARTPTS[game];"
    )
