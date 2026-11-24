"""Automatic creator and source matching (M2, plan §12.3).

A matcher turns an acquisition *hint* (a parsed URL, a file hash, a folder rule later)
into the creator channel it belongs to, and so to the standing licence that covers it.
The result is always ``matched`` (exactly one candidate), ``ambiguous`` (several: the
owner decides) or ``unmatched`` (rights needed). Matchers never guess.

M2 matchers work offline:
* an existing source with the same canonical URL;
* Twitch clip URLs, which carry the channel login;
* a file whose SHA-256 matches an already-imported recording that has a source.
M3 adds an official-API matcher (YouTube channel id, Twitch user id, titles, duration)
through the same interface; until then YouTube video URLs, which do not name their
channel, are ``unmatched`` unless the source already exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Creator, CreatorChannel, Recording, Source
from app.services.source_urls import ParsedSourceUrl

MATCHED = "matched"
AMBIGUOUS = "ambiguous"
UNMATCHED = "unmatched"


@dataclass(frozen=True)
class Hint:
    parsed_url: ParsedSourceUrl | None = None
    sha256: str | None = None


@dataclass(frozen=True)
class MatchResult:
    status: str
    matcher: str | None = None
    source_id: str | None = None                 # an existing source
    creator_channel_id: str | None = None
    candidates: tuple[str, ...] = field(default_factory=tuple)   # creator_channel ids
    reason: str = ""


class SourceMatcher(Protocol):
    name: str

    def match(self, s: Session, hint: Hint) -> MatchResult | None:
        """A result, or None if this matcher has nothing to say about the hint."""


class ExistingSourceMatcher:
    name = "existing_source"

    def match(self, s: Session, hint: Hint) -> MatchResult | None:
        if hint.parsed_url is None:
            return None
        src = s.scalar(select(Source).where(
            Source.url_canonical == hint.parsed_url.canonical_url))
        if src is None:
            return None
        return MatchResult(MATCHED, self.name, src.id, src.creator_channel_id,
                           (src.creator_channel_id,) if src.creator_channel_id else (),
                           "this video is already a source")


class TwitchLoginMatcher:
    name = "twitch_login"

    def match(self, s: Session, hint: Hint) -> MatchResult | None:
        p = hint.parsed_url
        if p is None or p.platform != "twitch" or not p.channel_login:
            return None
        rows = list(s.scalars(select(CreatorChannel).join(
            Creator, Creator.id == CreatorChannel.creator_id).where(
            CreatorChannel.platform == "twitch",
            func.lower(CreatorChannel.handle) == p.channel_login.lower(),
            Creator.archived_at.is_(None))))
        if not rows:
            return MatchResult(UNMATCHED, self.name,
                               reason=f"no creator has the Twitch channel '{p.channel_login}'")
        if len(rows) > 1:
            return MatchResult(AMBIGUOUS, self.name, candidates=tuple(r.id for r in rows),
                               reason="several creator channels match")
        return MatchResult(MATCHED, self.name, creator_channel_id=rows[0].id,
                           candidates=(rows[0].id,),
                           reason=f"Twitch channel '{p.channel_login}'")


class FileHashMatcher:
    name = "file_hash"

    def match(self, s: Session, hint: Hint) -> MatchResult | None:
        if not hint.sha256:
            return None
        rec = s.scalar(select(Recording).where(Recording.sha256 == hint.sha256))
        if rec is None or rec.source_id is None:
            return None
        src = s.get(Source, rec.source_id)
        return MatchResult(MATCHED, self.name, src.id, src.creator_channel_id,
                           (src.creator_channel_id,) if src.creator_channel_id else (),
                           "the same file was imported before")


MATCHERS: list[SourceMatcher] = [ExistingSourceMatcher(), FileHashMatcher(),
                                 TwitchLoginMatcher()]


def match(s: Session, hint: Hint) -> MatchResult:
    """First definitive answer wins (``matched``/``ambiguous``); otherwise the most
    informative ``unmatched``."""
    unmatched: MatchResult | None = None
    for m in MATCHERS:
        r = m.match(s, hint)
        if r is None:
            continue
        if r.status in (MATCHED, AMBIGUOUS):
            return r
        unmatched = unmatched or r
    if unmatched is not None:
        return unmatched
    if hint.parsed_url is not None and hint.parsed_url.platform == "youtube":
        return MatchResult(UNMATCHED, None, reason="YouTube links do not name their channel "
                           "offline: choose the creator channel (automatic matching via the "
                           "official API is planned for M3)")
    return MatchResult(UNMATCHED, None, reason="no creator channel matches")
