"""M2: offline URL validation and canonicalisation (plan §4). No network is used."""

from __future__ import annotations

import socket

import pytest

from app.services.source_urls import (
    MAX_URL_LENGTH,
    ParsedSourceUrl,
    SourceUrlError,
    parse_source_url,
)

YT = "dQw4w9WgXcQ"
YT_CANON = f"https://www.youtube.com/watch?v={YT}"

VALID: list[tuple[str, tuple[str, str, str, str, int | None, str | None]]] = [
    # (input, (platform, media, id, canonical, start_ms, channel_login))
    (f"https://www.youtube.com/watch?v={YT}", ("youtube", "video", YT, YT_CANON, None, None)),
    (f"https://youtube.com/watch?v={YT}", ("youtube", "video", YT, YT_CANON, None, None)),
    (f"https://m.youtube.com/watch?v={YT}", ("youtube", "video", YT, YT_CANON, None, None)),
    (f"https://music.youtube.com/watch?v={YT}", ("youtube", "video", YT, YT_CANON, None, None)),
    (f"https://WWW.YouTube.com/watch?v={YT}", ("youtube", "video", YT, YT_CANON, None, None)),
    (f"https://www.youtube.com./watch?v={YT}", ("youtube", "video", YT, YT_CANON, None, None)),
    (f"www.youtube.com/watch?v={YT}", ("youtube", "video", YT, YT_CANON, None, None)),
    (f"youtube.com/watch?v={YT}", ("youtube", "video", YT, YT_CANON, None, None)),
    (f"  https://youtu.be/{YT}  ", ("youtube", "video", YT, YT_CANON, None, None)),
    (f"youtu.be/{YT}", ("youtube", "video", YT, YT_CANON, None, None)),
    (f"https://youtu.be/{YT}?si=abc123", ("youtube", "video", YT, YT_CANON, None, None)),
    (f"https://www.youtube.com/shorts/{YT}", ("youtube", "video", YT, YT_CANON, None, None)),
    (f"https://www.youtube.com/shorts/{YT}/", ("youtube", "video", YT, YT_CANON, None, None)),
    (f"https://www.youtube.com/live/{YT}?feature=share",
     ("youtube", "video", YT, YT_CANON, None, None)),
    (f"https://www.youtube.com/watch?v={YT}&list=PL123&index=4&feature=share",
     ("youtube", "video", YT, YT_CANON, None, None)),
    (f"https://www.youtube.com/watch?feature=share&v={YT}",
     ("youtube", "video", YT, YT_CANON, None, None)),
    (f"https://www.youtube.com/watch?v={YT}&t=90",
     ("youtube", "video", YT, YT_CANON, 90_000, None)),
    (f"https://youtu.be/{YT}\n", ("youtube", "video", YT, YT_CANON, None, None)),
    (f"https://www.youtube.com/watch?v={YT}&t=90s",
     ("youtube", "video", YT, YT_CANON, 90_000, None)),
    (f"https://youtu.be/{YT}?t=1m30s", ("youtube", "video", YT, YT_CANON, 90_000, None)),
    (f"https://youtu.be/{YT}?t=1h2m3s", ("youtube", "video", YT, YT_CANON, 3_723_000, None)),
    (f"https://www.youtube.com/watch?v={YT}&start=5",
     ("youtube", "video", YT, YT_CANON, 5_000, None)),
    (f"https://www.youtube.com/watch?v={YT}&t=garbage",
     ("youtube", "video", YT, YT_CANON, None, None)),
    ("https://www.twitch.tv/videos/2201234567",
     ("twitch", "vod", "2201234567", "https://www.twitch.tv/videos/2201234567", None, None)),
    ("https://twitch.tv/videos/2201234567?filter=archives&sort=time",
     ("twitch", "vod", "2201234567", "https://www.twitch.tv/videos/2201234567", None, None)),
    ("https://m.twitch.tv/videos/2201234567",
     ("twitch", "vod", "2201234567", "https://www.twitch.tv/videos/2201234567", None, None)),
    ("https://www.twitch.tv/videos/2201234567?t=1h2m3s",
     ("twitch", "vod", "2201234567", "https://www.twitch.tv/videos/2201234567", 3_723_000,
      None)),
    ("twitch.tv/videos/42",
     ("twitch", "vod", "42", "https://www.twitch.tv/videos/42", None, None)),
    ("https://clips.twitch.tv/FunnyClipSlug-AbC_123",
     ("twitch", "clip", "FunnyClipSlug-AbC_123", "https://clips.twitch.tv/FunnyClipSlug-AbC_123",
      None, None)),
    ("https://www.twitch.tv/SomeStreamer/clip/FunnyClipSlug-AbC_123?filter=clips",
     ("twitch", "clip", "FunnyClipSlug-AbC_123", "https://clips.twitch.tv/FunnyClipSlug-AbC_123",
      None, "somestreamer")),
]


@pytest.mark.parametrize("url,expected", VALID)
def test_valid_urls_are_canonicalised(url, expected):
    p = parse_source_url(url)
    assert p == ParsedSourceUrl(*expected)


def test_variants_share_one_canonical_url():
    variants = [u for u, e in VALID if e[3] == YT_CANON]
    assert len(variants) >= 15
    assert {parse_source_url(u).canonical_url for u in variants} == {YT_CANON}
    clip = {parse_source_url(u).canonical_url for u, e in VALID if e[1] == "clip"}
    assert len(clip) == 1


INVALID: list[tuple[object, str]] = [
    ("", "invalid_url"),
    ("   ", "invalid_url"),
    (None, "invalid_url"),
    (f"http://www.youtube.com/watch?v={YT}", "unsupported_scheme"),
    (f"ftp://www.youtube.com/watch?v={YT}", "unsupported_scheme"),
    ("javascript:alert(1)", "unsupported_scheme"),
    (f"mailto:x@youtube.com?v={YT}", "unsupported_scheme"),
    (f"data:text/html,https://youtu.be/{YT}", "unsupported_scheme"),
    (f"https://user:pass@www.youtube.com/watch?v={YT}", "credentials_in_url"),
    (f"https://www.youtube.com@evil.example/watch?v={YT}", "credentials_in_url"),
    (f"https://www.youtube.com:8443/watch?v={YT}", "port_not_allowed"),
    (f"https://142.250.80.46/watch?v={YT}", "ip_address_host"),
    (f"https://[2607:f8b0::200e]/watch?v={YT}", "ip_address_host"),
    (f"https://www.xn--yutube-wqf.com/watch?v={YT}", "unsupported_host"),
    (f"https://www.yоutube.com/watch?v={YT}", "unsupported_host"),       # Cyrillic о
    (f"https://youtube.com.evil.example/watch?v={YT}", "unsupported_host"),
    (f"https://evilyoutube.com/watch?v={YT}", "unsupported_host"),
    (f"https://vimeo.com/{YT}", "unsupported_host"),
    (f"https:///watch?v={YT}", "unsupported_host"),
    ("https://www.youtube.com/watch?v=short", "invalid_video_id"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQX", "invalid_video_id"),
    ("https://www.youtube.com/watch?v=dQw4w9Wg%3CQ", "invalid_video_id"),
    ("https://youtu.be/dQw4w9Wg/XcQ", "not_a_video"),
    ("https://www.youtube.com/watch", "not_a_video"),
    ("https://www.youtube.com/", "not_a_video"),
    ("https://www.youtube.com/playlist?list=PL1234567890", "playlist_url"),
    ("https://www.youtube.com/@RumbleFrame", "channel_url"),
    ("https://www.youtube.com/@RumbleFrame/videos", "channel_url"),
    ("https://www.youtube.com/channel/UC1234567890abcdefghij", "channel_url"),
    ("https://www.youtube.com/c/SomeName", "channel_url"),
    ("https://www.youtube.com/user/SomeName", "channel_url"),
    ("https://www.twitch.tv/somestreamer", "live_channel_url"),
    ("https://www.twitch.tv/somestreamer/videos", "not_a_video"),
    ("https://www.twitch.tv/videos/abc", "invalid_video_id"),
    ("https://www.twitch.tv/videos/", "not_a_video"),
    ("https://www.twitch.tv/directory", "not_a_video"),
    ("https://www.twitch.tv/somestreamer/clip/bad%2Fslug", "invalid_video_id"),
    ("https://clips.twitch.tv/", "not_a_video"),
    ("https://clips.twitch.tv/a/b", "not_a_video"),
    (f"https://www.youtube.com/watch?v={YT} extra", "invalid_url"),
    (f"https://www.youtube.com/watch?v={YT}\n&t=5", "invalid_url"),
    ("https://youtu.be/\x00" + YT, "invalid_url"),
    ("https://www.youtube.com/watch?v=" + YT + "&x=" + "a" * MAX_URL_LENGTH, "url_too_long"),
]


@pytest.mark.parametrize("url,code", INVALID)
def test_invalid_urls_are_refused_with_a_stable_code(url, code):
    with pytest.raises(SourceUrlError) as e:
        parse_source_url(url)  # type: ignore[arg-type]
    assert e.value.code == code, str(e.value)
    assert str(e.value)                                  # human-readable message


def test_parser_never_touches_the_network(monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("network used")
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    for url, _ in VALID:
        parse_source_url(url)
    for url, _ in INVALID:
        with pytest.raises(SourceUrlError):
            parse_source_url(url)  # type: ignore[arg-type]
