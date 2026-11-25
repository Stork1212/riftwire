"""Editorial scoring: versioned, configurable profiles and the ranking cut.

The overall score is the weighted sum of the model's component scores (owner sprint brief
§19): an **estimated editorial-quality score from 0 to 100, not a probability of going
viral**, kept separate from the model's confidence. Profiles are TOML files under
``scoring/`` (stdlib ``tomllib``), selected per game, channel and prompt; every result
records the profile id, version and SHA-256, every candidate's score and the candidates the
cut rejected. Penalties for dead air, duplicates and boundaries arrive with M6's signals.
"""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from app.ai.schemas import SCORE_COMPONENTS, Candidate

REPO_ROOT = Path(__file__).resolve().parents[3]
SCORING_DIR = REPO_ROOT / "scoring"
MATCH_KEYS = ("game", "channel", "prompt")
DEFAULT_WEIGHTS: dict[str, float] = {
    "hook": 0.20, "entertainment": 0.20, "surprise": 0.15, "intensity": 0.15,
    "standalone": 0.10, "pacing": 0.10, "payoff": 0.10,
}
DEFAULT_MIN_SCORE = 60


class ScoringError(ValueError):
    pass


@dataclass(frozen=True)
class Ranking:
    """A scoring profile. ``id``/``version``/``sha256`` identify it in every result."""

    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    min_score: int = DEFAULT_MIN_SCORE
    id: str = "editorial.builtin"
    version: int = 0
    sha256: str | None = None
    match: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if set(self.weights) != set(SCORE_COMPONENTS):
            raise ScoringError(f"weights must cover exactly {SCORE_COMPONENTS}")
        if any(w < 0 for w in self.weights.values()) or abs(sum(self.weights.values()) - 1) > 1e-6:
            raise ScoringError("weights must be non-negative and sum to 1")
        if not 0 <= self.min_score <= 100:
            raise ScoringError("min_score must be 0-100")
        if set(self.match) - set(MATCH_KEYS):
            raise ScoringError(f"match keys must be among {MATCH_KEYS}")

    def score(self, c: Candidate) -> int:
        s = c.scores.model_dump()
        return round(sum(self.weights[k] * s[k] for k in SCORE_COMPONENTS))

    def rank(self, candidates: list[Candidate]) -> list[tuple[Candidate, int]]:
        """Candidates at or above ``min_score``, best first (ties keep model order)."""
        return self.split(candidates)[0]

    def split(self, candidates: list[Candidate]) -> tuple[list[tuple[Candidate, int]],
                                                          list[tuple[Candidate, int]]]:
        scored = [(c, self.score(c)) for c in candidates]
        kept = sorted((x for x in scored if x[1] >= self.min_score), key=lambda x: -x[1])
        rejected = [x for x in scored if x[1] < self.min_score]
        return kept, rejected

    def identity(self) -> dict:
        return {"id": self.id, "version": self.version, "sha256": self.sha256,
                "min_score": self.min_score, "weights": dict(self.weights),
                "match": dict(self.match),
                "meaning": "estimated editorial-quality score (0-100); not a probability of "
                           "going viral; separate from model confidence"}

    def record(self, candidates: list[Candidate]) -> dict:
        """Everything needed to audit the cut: the profile, every candidate's raw component
        scores, its editorial score and confidence, and why it was kept or rejected."""
        kept, rejected = self.split(candidates)

        def row(c: Candidate, s: int, reason: str | None) -> dict:
            return {"window_id": c.window_id, "editorial_score": s,
                    "confidence": c.confidence, "raw_scores": c.scores.model_dump(),
                    "title": c.title, **({"reason": reason} if reason else {})}
        return {"profile": self.identity(),
                "kept": [row(c, s, None) for c, s in kept],
                "rejected": [row(c, s, f"editorial score {s} below {self.min_score}")
                             for c, s in rejected]}


def load_file(path: Path) -> Ranking:
    raw = path.read_bytes()
    try:
        meta = tomllib.loads(raw.decode("utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ScoringError(f"{path}: {e}") from e
    for key in ("id", "version", "min_score", "weights"):
        if key not in meta:
            raise ScoringError(f"{path}: needs {key!r}")
    return Ranking(weights={k: float(v) for k, v in meta["weights"].items()},
                   min_score=int(meta["min_score"]), id=str(meta["id"]),
                   version=int(meta["version"]), sha256=hashlib.sha256(raw).hexdigest(),
                   match={k: str(v) for k, v in (meta.get("match") or {}).items()})


def resolve(*, game: str | None = None, channel: str | None = None, prompt: str | None = None,
            root: Path = SCORING_DIR) -> Ranking:
    """The most specific profile whose ``[match]`` keys all equal the request (its newest
    version). Older versions stay on disk so historical results remain reproducible."""
    request = {"game": game, "channel": channel, "prompt": prompt}
    profiles = [load_file(p) for p in sorted(root.rglob("*.toml"))]
    seen: dict[tuple[str, int], Path] = {}
    for p in profiles:
        key = (p.id, p.version)
        if key in seen:
            raise ScoringError(f"duplicate scoring profile {p.id} v{p.version}")
        seen[key] = root
    fits = [p for p in profiles if all(request.get(k) == v for k, v in p.match.items())]
    if not fits:
        raise ScoringError(f"no scoring profile matches {request}")
    best = max(len(p.match) for p in fits)
    latest: dict[str, Ranking] = {}
    for p in (p for p in fits if len(p.match) == best):    # newest version of each profile
        if p.id not in latest or p.version > latest[p.id].version:
            latest[p.id] = p
    top = list(latest.values())
    if len(top) > 1:
        raise ScoringError("ambiguous scoring profiles: "
                           + ", ".join(f"{p.id} v{p.version}" for p in top))
    return top[0]
