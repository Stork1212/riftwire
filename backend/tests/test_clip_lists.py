import pytest

from app.core.errors import ValidationFailed
from app.services.clip_lists import parse

YOUTUBE = "https://www.youtube.com/watch?v=GMZzFJsf844"


def test_source_timecodes_map_to_recording_time_with_subsecond_offset():
    url, items = parse(
        "URL=https://youtu.be/GMZzFJsf844?t=717\n"
        "00:12:01-00:12:15 clutch\n00:11:57-00:12:01 setup",
        YOUTUBE, 716_500, 20_000,
    )
    assert url == YOUTUBE
    assert [(r.label, r.from_ms, r.to_ms) for r in items] == [
        ("clutch", 4500, 18500), ("setup", 500, 4500),
    ]


def test_headerless_list_uses_the_recording_clock():
    url, items = parse("00:00:00-00:00:02 setup", YOUTUBE, 716_500, 2000)
    assert url is None
    assert items[0].to_ms == 2000


@pytest.mark.parametrize(("text", "error"), [
    ("URL=https://youtu.be/dQw4w9WgXcQ\n00:00:00-00:00:02 setup",
     "does not match the linked source"),
    ("00:00:02-00:00:01 reverse", "must follow"),
    ("00:00:00-00:00:03 long", "outside this recording"),
    ("00:00:00-00:00:01 first\n00:00:00-00:00:01 duplicate", "already listed"),
    ("00:00:00-00:00:01", "followed by a label"),
])
def test_reports_unsafe_or_invalid_ranges(text, error):
    with pytest.raises(ValidationFailed, match=error):
        parse(text, YOUTUBE, 0, 2000)
