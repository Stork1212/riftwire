#!/usr/bin/env python3
"""Scan a local video and export candidate evidence and a RIFTWIRE clips.txt list."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.highlight_scan import frame_activity, rank  # noqa: E402
from app.services.source_urls import parse_source_url  # noqa: E402

CHUNK_SECONDS = 300
FLOOR = 60 * 1024**3


def clock(ms: int) -> str:
    minutes, seconds = divmod(ms // 1000, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--words", type=Path, help="Timestamped Whisper JSON for this video.")
    parser.add_argument("--whisper-python", type=Path)
    parser.add_argument("--whisper-model", type=Path)
    parser.add_argument("--url")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.words and (args.whisper_python or args.whisper_model):
        parser.error("Choose either a transcript file or a local Whisper run.")
    if bool(args.whisper_python) != bool(args.whisper_model):
        parser.error("Supply both --whisper-python and --whisper-model.")
    source_url = parse_source_url(args.url).canonical_url if args.url else None
    args.out.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(args.out).free < FLOOR + 64 * 1024**2:
        parser.error("The output volume must have at least 60 GiB free plus working space.")
    video = args.video.resolve(strict=True)
    duration = float(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(video),
    ], text=True, timeout=30))
    duration_ms = int(duration * 1000)
    words = json.loads(args.words.read_text())["words"] if args.words else []
    activity: list[float] = [0.0]
    prior_frame = b""
    with TemporaryDirectory(prefix=".scan-", dir=args.out) as temp:
        work = Path(temp)
        for begin in range(0, (duration_ms + 999)//1000, CHUNK_SECONDS):
            length = min(CHUNK_SECONDS, duration - begin)
            demux = ["ffmpeg", "-nostdin", "-v", "error", "-ss", str(begin),
                     "-i", str(video), "-t", str(length)]
            raw = subprocess.check_output(
                demux + ["-an", "-vf", "fps=2,scale=96:54,format=gray",
                         "-f", "rawvideo", "-"], timeout=240)
            if raw:
                activity.extend(frame_activity(prior_frame + raw, 96 * 54))
                prior_frame = raw[-96 * 54:]
            if args.whisper_python:
                audio, transcript = work / "audio.wav", work / "words.json"
                subprocess.run(demux + ["-y", "-vn", "-ac", "1", "-ar", "16000", str(audio)],
                               check=True, timeout=120)
                subprocess.run([
                    str(args.whisper_python.resolve()),
                    str(ROOT / "bench/riftbench/transcribe_child.py"),
                    str(args.whisper_model.resolve()), str(audio), str(transcript),
                ], check=True, timeout=180)
                for word in json.loads(transcript.read_text())["words"]:
                    word["start_ms"] += begin * 1000
                    word["end_ms"] += begin * 1000
                    words.append(word)
            print(f"Scanned {clock(min((begin + CHUNK_SECONDS)*1000, duration_ms))} "
                  f"of {clock(duration_ms)}", flush=True)

    ranked = rank(activity, words, duration_ms)
    report = {
        "source_url": source_url, "duration_ms": duration_ms,
        "coverage_ms": min(duration_ms, len(activity) * 500),
        "speech_available": bool(words),
        "scoring": "GTA signal shortlist. Visual activity plus speech cues. Human review needed.",
        "candidates": [asdict(candidate) for candidate in ranked],
    }
    lines = [f"URL={source_url}"] if source_url else []
    lines.extend(f"{clock(c.start_ms)}-{clock(c.end_ms)} candidate_{index:02}"
                 for index, c in enumerate(ranked, 1))
    (args.out / "clips.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (args.out / "candidates.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
