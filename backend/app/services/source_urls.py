"""Offline validation and canonicalisation of YouTube and Twitch video URLs (M2, plan §4).

Pure string parsing: no DNS, no HTTP, no platform API. The result identifies a video; it
says nothing about rights, which are checked separately (``services/rights.py``).

* Only ``https`` (or no scheme) and an allow-listed host are accepted. Credentials, ports,
  IP-literal hosts, punycode/IDN hosts and over-long URLs are refused.
* Every accepted URL maps to exactly one canonical URL, which is the deduplication key for
  sources: tracking parameters are dropped; a start time is kept separately.
* Channel, playlist-only and live-channel URLs are refused with a clear reason: a source is
  one recorded video.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

MAX_URL_LENGTH = 2048

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
YOUTU_BE = "youtu.be"
TWITCH_HOSTS = {"twitch.tv", "www.twitch.tv", "m.twitch.tv"}
TWITCH_CLIPS = "clips.twitch.tv"

YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
TWITCH_VOD = re.compile(r"^[0-9]{1,20}$")
TWITCH_LOGIN = re.compile(r"^[A-Za-z0-9_]{4,25}$")
TWITCH_SLUG = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
TIME_HMS = re.compile(r"^(?:(\d{1,3})h)?(?:(\d{1,4})m)?(?:(\d{1,6})s?)?$")

# Twitch paths that are not channel logins.
TWITCH_RESERVED = {"videos", "directory", "settings", "subscriptions", "inventory",
                   "wallet", "downloads", "p", "search", "turbo", "jobs", "drops"}
YT_CHANNEL_PREFIXES = ("@", "channel", "c", "user")


class SourceUrlError(ValueError):
    """The URL is not an acceptable video URL. ``code`` is stable for the API/UI."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ParsedSourceUrl:
    platform: str               # "youtube" | "twitch"
    media: str                  # "video" (YouTube) | "vod" | "clip" (Twitch)
    video_id: str               # YouTube id, Twitch VOD number or clip slug
    canonical_url: str
    start_ms: int | None = None
    channel_login: str | None = None   # Twitch clip URLs carry the channel login


def _start_ms(value: str | None) -> int | None:
    if not value:
        return None
    m = TIME_HMS.match(value.strip().lower())
    if not m or not any(m.groups()):
        return None
    h, mi, s = (int(g) if g else 0 for g in m.groups())
    total = (h * 3600 + mi * 60 + s) * 1000
    return total if 0 <= total <= 48 * 3600 * 1000 else None


def _host(netloc: str) -> str:
    if "@" in netloc:
        raise SourceUrlError("credentials_in_url", "Links containing a user name or "
                             "password are not accepted.")
    if netloc.startswith("["):
        raise SourceUrlError("ip_address_host", "IP-address links are not accepted.")
    host, sep, port = netloc.rpartition(":") if ":" in netloc else (netloc, "", "")
    if sep:
        raise SourceUrlError("port_not_allowed", "Links with a port number are not accepted.")
    host = host.lower().rstrip(".")
    if not host.isascii() or "xn--" in host:
        raise SourceUrlError("unsupported_host", "Internationalised or look-alike host "
                             "names are not accepted.")
    if re.fullmatch(r"[0-9.]+", host):
        raise SourceUrlError("ip_address_host", "IP-address links are not accepted.")
    return host


def parse_source_url(raw: str) -> ParsedSourceUrl:
    """Validate and canonicalise ``raw``. Raises ``SourceUrlError``."""
    if not isinstance(raw, str):
        raise SourceUrlError("invalid_url", "A URL is required.")
    url = raw.strip()
    if not url:
        raise SourceUrlError("invalid_url", "A URL is required.")
    if len(url) > MAX_URL_LENGTH:
        raise SourceUrlError("url_too_long", "The link is too long.")
    if any(c.isspace() for c in url) or any(ord(c) < 32 for c in url):
        raise SourceUrlError("invalid_url", "The link contains spaces or control characters.")
    if "://" not in url:
        if ":" in url.split("/", 1)[0] and not url.lower().startswith(("youtu", "www.",
                                                                       "m.", "clips.")):
            raise SourceUrlError("unsupported_scheme", "Only https links are accepted.")
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme.lower() != "https":
        raise SourceUrlError("unsupported_scheme", "Only https links are accepted.")
    host = _host(parts.netloc)
    segs = [s for s in parts.path.split("/") if s]
    query = parse_qs(parts.query, keep_blank_values=False)

    if host in YOUTUBE_HOSTS or host == YOUTU_BE:
        return _youtube(host, segs, query)
    if host in TWITCH_HOSTS or host == TWITCH_CLIPS:
        return _twitch(host, segs, query)
    raise SourceUrlError("unsupported_host", "Only YouTube and Twitch video links are "
                         "accepted.")


def _first(query: dict[str, list[str]], key: str) -> str | None:
    vals = query.get(key)
    return vals[0] if vals else None


def _youtube(host: str, segs: list[str], query: dict[str, list[str]]) -> ParsedSourceUrl:
    vid: str | None = None
    if host == YOUTU_BE:
        vid = segs[0] if len(segs) == 1 else None
    elif segs == ["watch"]:
        vid = _first(query, "v")
    elif len(segs) == 2 and segs[0] in ("shorts", "live"):
        vid = segs[1]
    elif segs and (segs[0].startswith("@") or segs[0] in YT_CHANNEL_PREFIXES[1:]):
        raise SourceUrlError("channel_url", "This is a channel link: paste a link to one "
                             "video.")
    elif segs == ["playlist"]:
        raise SourceUrlError("playlist_url", "This is a playlist link: paste a link to one "
                             "video.")
    if vid is None:
        raise SourceUrlError("not_a_video", "This YouTube link does not point to a video.")
    if not YT_ID.match(vid):
        raise SourceUrlError("invalid_video_id", "The YouTube video id is not valid.")
    start = _start_ms(_first(query, "t") or _first(query, "start"))
    return ParsedSourceUrl("youtube", "video", vid,
                           f"https://www.youtube.com/watch?v={vid}", start)


def _twitch(host: str, segs: list[str], query: dict[str, list[str]]) -> ParsedSourceUrl:
    if host == TWITCH_CLIPS:
        if len(segs) == 1 and TWITCH_SLUG.match(segs[0]):
            return ParsedSourceUrl("twitch", "clip", segs[0],
                                   f"https://clips.twitch.tv/{segs[0]}")
        raise SourceUrlError("not_a_video", "This Twitch clip link is not valid.")
    if len(segs) == 2 and segs[0] == "videos":
        if not TWITCH_VOD.match(segs[1]):
            raise SourceUrlError("invalid_video_id", "The Twitch video number is not valid.")
        return ParsedSourceUrl("twitch", "vod", segs[1],
                               f"https://www.twitch.tv/videos/{segs[1]}",
                               _start_ms(_first(query, "t")))
    if len(segs) == 3 and segs[1] == "clip" and TWITCH_LOGIN.match(segs[0]):
        if not TWITCH_SLUG.match(segs[2]):
            raise SourceUrlError("invalid_video_id", "The Twitch clip id is not valid.")
        return ParsedSourceUrl("twitch", "clip", segs[2],
                               f"https://clips.twitch.tv/{segs[2]}",
                               channel_login=segs[0].lower())
    if len(segs) == 1 and segs[0].lower() not in TWITCH_RESERVED \
            and TWITCH_LOGIN.match(segs[0]):
        raise SourceUrlError("live_channel_url", "This is a Twitch channel (live) link: paste "
                             "a link to a recorded video or clip.")
    raise SourceUrlError("not_a_video", "This Twitch link does not point to a recorded "
                         "video or clip.")
