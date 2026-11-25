"""Rank review candidates from observed visual activity and timestamped speech.

Scores prioritize windows for review. A frame change measures activity, and does not
identify the event, speaker, or game mechanic in a window.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from statistics import median
from typing import TypedDict

_SAMPLE_HZ = 2
_GTA_CUES = {
    "reaction": re.compile(r"\b(?:oh my god|no way|what the|holy|wait|we got a problem)\b", re.I),
    "pursuit": re.compile(r"\b(?:police|cops|chase|getaway|escape)\b", re.I),
    "crash": re.compile(r"\b(?:crash|stuck|jump|blew up|make it|no damage)\b", re.I),
}


class Word(TypedDict):
    w: str
    start_ms: float
    end_ms: float
    prob: float


@dataclass(frozen=True)
class Candidate:
    start_ms: int
    end_ms: int
    peak_ms: int
    score: float
    motion: float
    speech_cues: list[str]
    excerpt: str


def frame_activity(raw: bytes, frame_size: int) -> list[float]:
    """Mean grayscale difference for adjacent frames, with fades and cuts capped."""
    if frame_size <= 0 or len(raw) % frame_size:
        raise ValueError("Incomplete grayscale video frame")
    return [
        min(45.0, sum(abs(a - b) for a, b in zip(
            raw[pos - frame_size:pos], raw[pos:pos + frame_size], strict=True)) / frame_size)
        for pos in range(frame_size, len(raw), frame_size)
    ]


def rank(activity: list[float], words: list[Word], duration_ms: int,
         limit: int = 15) -> list[Candidate]:
    if duration_ms < 15_000 or not activity or limit <= 0:
        return []
    baseline = max(1.0, median(activity))
    speech = sorted((w for w in words if w["prob"] >= .50 and w["w"].strip()),
                    key=lambda word: word["start_ms"])
    timestamps = [word["start_ms"] for word in speech]
    candidates = []
    for start in range(0, duration_ms - 14_999, 5000):
        end = min(start + 25_000, duration_ms)
        start_frame = start * _SAMPLE_HZ // 1000
        end_frame = end * _SAMPLE_HZ // 1000
        window = activity[start_frame:end_frame]
        if not window:
            continue
        # An isolated transition contributes little. Sustained activity drives the score.
        groups = [sum(window[i:i+4]) / 4 for i in range(max(0, len(window) - 3))]
        if not groups:
            continue
        peak = max(range(len(groups)), key=groups.__getitem__)
        motion = round(sum(sorted(groups, reverse=True)[:8]) / min(8, len(groups)), 2)
        first = bisect.bisect_left(timestamps, start)
        last = bisect.bisect_left(timestamps, end)
        excerpt = " ".join(w["w"].strip() for w in speech[first:last])
        labels = [label for label, pattern in _GTA_CUES.items() if pattern.search(excerpt)]
        # The words are hints only. The user confirms the event and authentic dialogue.
        score = round(min(motion / baseline, 5) + min(len(labels), 3) * .85, 3)
        candidates.append(Candidate(start, end, start + (peak + 2) * 500, score,
                                    motion, labels, excerpt[:900]))
    selected: list[Candidate] = []
    for candidate in sorted(candidates, key=lambda c: (-c.score, c.start_ms)):
        if any(candidate.start_ms < chosen.end_ms and candidate.end_ms > chosen.start_ms
               for chosen in selected):
            continue
        selected.append(candidate)
        if len(selected) >= limit:
            break
    return selected
