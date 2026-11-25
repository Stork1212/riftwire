# Editorial scoring profiles

Versioned TOML profiles used by `backend/app/ai/scoring.py` to turn a validated candidate's
component scores into an **estimated editorial-quality score (0–100)**, and to drop
candidates below `min_score`. The score is **not** a probability of going viral and is kept
separate from the model's confidence.

- `[match]` may name `game`, `channel` and/or `prompt` (a prompt id). The most specific
  profile whose keys all match is used. Two equally specific matches are an error.
- Results record the profile id, version and SHA-256, the score of every candidate and the
  candidates the cut rejected, so historical results stay reproducible.
- A changed profile is a new version (or a new file); do not edit a profile in place once
  results have used it.

| id | version | match | status |
|---|---|---|---|
| `editorial.default` | 1 | (any) | provisional cut 60 (owner, 2026-11-25) |
