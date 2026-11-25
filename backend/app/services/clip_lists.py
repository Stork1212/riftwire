"""Validate a clips.txt list and map its source clock onto a linked recording."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.errors import ValidationFailed
from app.services.source_urls import SourceUrlError, parse_source_url

_STAMP = r"(\d{1,3}:[0-5]\d:[0-5]\d)"
_ROW = re.compile(rf"{_STAMP}\s*-\s*{_STAMP}\s+(.{{1,120}})")


@dataclass(frozen=True)
class ClipRange:
    line: int
    label: str
    start_ms: int
    end_ms: int
    from_ms: int
    to_ms: int


def _seconds(timestamp: str) -> int:
    hours, minutes, seconds = map(int, timestamp.split(":"))
    return 3600 * hours + 60 * minutes + seconds


def parse(text: str, source_url: str | None, offset_ms: int,
          duration_ms: int) -> tuple[str | None, list[ClipRange]]:
    expected_url = source_url
    url = None
    items: list[ClipRange] = []
    seen = set()
    for line, row in enumerate(text.splitlines(), 1):
        row = row.strip()
        if not row or row.startswith("#"):
            continue
        if row.startswith("URL="):
            if url is not None or items:
                raise ValidationFailed(f"Line {line}: put a single URL header before the ranges.")
            try:
                url = parse_source_url(row.removeprefix("URL=")).canonical_url
            except SourceUrlError as exc:
                raise ValidationFailed(f"Line {line}: {exc}") from exc
            if url != expected_url:
                raise ValidationFailed(f"Line {line}: the URL does not match the linked source.")
            continue
        match = _ROW.fullmatch(row)
        if match is None:
            raise ValidationFailed(f"Line {line}: enter HH:MM:SS-HH:MM:SS followed by a label.")
        start, end, label = match.groups()
        start_ms, end_ms = _seconds(start) * 1000, _seconds(end) * 1000
        if end_ms <= start_ms:
            raise ValidationFailed(f"Line {line}: the end timestamp must follow the start.")
        if (start_ms, end_ms) in seen:
            raise ValidationFailed(f"Line {line}: this timestamp range is already listed.")
        seen.add((start_ms, end_ms))
        origin = offset_ms if url else 0
        relative_start, relative_end = start_ms - origin, end_ms - origin
        if relative_start < 0 or relative_end > duration_ms:
            raise ValidationFailed(f"Line {line}: the range falls outside this recording.")
        items.append(ClipRange(line, label, start_ms, end_ms, relative_start, relative_end))
        if len(items) > 100:
            raise ValidationFailed("A timestamp list can contain up to 100 ranges.")
    if not items:
        raise ValidationFailed("Add at least one timestamp range.")
    return url, items
