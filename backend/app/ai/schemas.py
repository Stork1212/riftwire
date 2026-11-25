"""Versioned schemas: the evidence a model sees (``evidence.v1``) and what it may answer
(``highlight_eval.v1``).

The evidence bundle is the contract with M4's analysis-evidence layer: every item has a
stable id, and times are integer milliseconds relative to the recording. The model never
returns a time: it returns ids, and RIFTWIRE looks the times up. Unknown fields are refused.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

EVIDENCE_SCHEMA = "evidence.v1"
HIGHLIGHT_SCHEMA = "highlight_eval.v1"

CATEGORIES = (
    "funny", "unexpected", "chaos", "police_pursuit", "escape", "crash", "stunt",
    "reaction", "close_call", "skill", "mechanic", "discovery", "glitch",
    "character_interaction", "story", "other",
)
SCORE_COMPONENTS = ("hook", "entertainment", "surprise", "intensity", "standalone",
                    "pacing", "payoff")
# How a claim is supported. ``observed_in_keyframe`` is only valid when the provider was
# actually shown that keyframe (an image-capable model); never "confirmed".
BASES = ("transcript", "audio", "motion", "scene", "observed_in_keyframe", "inference")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ----------------------------------------------------------------------------- evidence.v1

class Word(_Strict):
    id: str                       # w:<transcript>:<idx>
    t0: int = Field(ge=0)         # ms, recording-relative
    t1: int = Field(ge=0)
    text: str


class Event(_Strict):
    id: str                       # a:<run>:<i> | v:<run>:<i> | sc:<run>:<i>
    t0: int = Field(ge=0)
    t1: int = Field(ge=0)
    kind: str                     # loud_burst, onset, scene_cut, motion_peak, silence, ...
    nature: Literal["measured", "inferred"]
    value: float | None = None


class Keyframe(_Strict):
    id: str                       # kf:<id>
    t: int = Field(ge=0)


class Window(_Strict):
    """A bounded candidate window offered to the model (stage B/C output)."""

    id: str                       # c:<n>
    t0: int = Field(ge=0)
    t1: int = Field(ge=0)


class EvidenceBundle(_Strict):
    schema_: Literal["evidence.v1"] = Field(default=EVIDENCE_SCHEMA, alias="schema")
    recording_id: str
    transcript_id: str | None
    duration_ms: int = Field(gt=0)
    windows: tuple[Window, ...]
    words: tuple[Word, ...]
    events: tuple[Event, ...] = ()
    keyframes: tuple[Keyframe, ...] = ()
    game: str | None = None

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    def fingerprint(self) -> str:
        text = json.dumps(self.model_dump(by_alias=True), sort_keys=True,
                          separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(text.encode()).hexdigest()


# ------------------------------------------------------------------------ highlight_eval.v1

class EvidenceRef(_Strict):
    ids: list[str] = Field(min_length=1, max_length=40)
    claim: str = Field(min_length=1, max_length=240)
    basis: Literal[BASES]  # type: ignore[valid-type]
    # Optional exact quote: must equal the cited words' text joined by spaces.
    quote: str | None = Field(default=None, max_length=300)


class Scores(_Strict):
    hook: int = Field(ge=0, le=100)
    entertainment: int = Field(ge=0, le=100)
    surprise: int = Field(ge=0, le=100)
    intensity: int = Field(ge=0, le=100)
    standalone: int = Field(ge=0, le=100)
    pacing: int = Field(ge=0, le=100)
    payoff: int = Field(ge=0, le=100)


class Candidate(_Strict):
    window_id: str
    start_id: str                 # a word or scene id inside the window: the clip starts here
    end_id: str                   # ... and ends at the end of this item
    category: Literal[CATEGORIES]  # type: ignore[valid-type]
    what_happens: str = Field(min_length=1, max_length=400)
    why_entertaining: str = Field(min_length=1, max_length=400)
    standalone: bool
    clear_start_and_end: bool
    title: str = Field(min_length=1, max_length=90)
    hook: str = Field(min_length=1, max_length=120)
    scores: Scores
    confidence: float = Field(ge=0, le=1)
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=12)
    uncertainty: str = Field(default="", max_length=300)


class HighlightEval(_Strict):
    schema_: Literal["highlight_eval.v1"] = Field(alias="schema")
    candidates: list[Candidate] = Field(max_length=20)
    rejected_window_ids: list[str] = Field(default_factory=list, max_length=200)

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @field_validator("candidates")
    @classmethod
    def _unique_windows(cls, v: list[Candidate]) -> list[Candidate]:
        ids = [c.window_id for c in v]
        if len(ids) != len(set(ids)):
            raise ValueError("each window may be selected at most once")
        return v


def response_json_schema() -> dict:
    """The JSON schema sent to providers that support constrained output (Ollama
    ``format``, OpenAI-compatible ``response_format``). Validation never relies on it."""
    return HighlightEval.model_json_schema(by_alias=True)
