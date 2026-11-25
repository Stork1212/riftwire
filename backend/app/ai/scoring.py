"""Editorial score (0-100) from a validated candidate's components, and the ranking cut.

Transparent and configurable (owner sprint brief §19): the overall score is a weighted sum
of the model's component scores. It is an estimated editorial-quality score, **not** a
probability of going viral, and it is kept separate from the model's confidence. Penalties
for dead air, duplicates and boundaries arrive with M6's signal evidence; until then the
only cut is ``min_score``, which drops weak candidates instead of padding the list.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.ai.schemas import SCORE_COMPONENTS, Candidate

DEFAULT_WEIGHTS: dict[str, float] = {
    "hook": 0.20, "entertainment": 0.20, "surprise": 0.15, "intensity": 0.15,
    "standalone": 0.10, "pacing": 0.10, "payoff": 0.10,
}
DEFAULT_MIN_SCORE = 60


@dataclass(frozen=True)
class Ranking:
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    min_score: int = DEFAULT_MIN_SCORE

    def __post_init__(self) -> None:
        if set(self.weights) != set(SCORE_COMPONENTS):
            raise ValueError(f"weights must cover exactly {SCORE_COMPONENTS}")
        if any(w < 0 for w in self.weights.values()) or abs(sum(self.weights.values()) - 1) > 1e-6:
            raise ValueError("weights must be non-negative and sum to 1")
        if not 0 <= self.min_score <= 100:
            raise ValueError("min_score must be 0-100")

    def score(self, c: Candidate) -> int:
        s = c.scores.model_dump()
        return round(sum(self.weights[k] * s[k] for k in SCORE_COMPONENTS))

    def rank(self, candidates: list[Candidate]) -> list[tuple[Candidate, int]]:
        """Candidates at or above ``min_score``, best first (ties keep model order)."""
        scored = [(c, self.score(c)) for c in candidates]
        kept = [x for x in scored if x[1] >= self.min_score]
        return sorted(kept, key=lambda x: -x[1])
