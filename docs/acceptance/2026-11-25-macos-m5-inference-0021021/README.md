# M5 native Mac benchmark, `0021021` (2026-11-25 14:01Z): **passed its synthetic engineering gate**

**Accepted by the owner (2026-11-25)** as passing its defined synthetic engineering gate.
Retained: Qwen3.5 4B and Ollama, no further downloads, the scoring profile
`editorial.default` v1 with a cut of 60, and the model-agnostic architecture.

**Setup:**
- Ollama 0.40.18 (Homebrew) and `qwen3.5:4b`: the MLX build (NVFP4), digest `7f3251fa…`,
  **reused, nothing downloaded**. Localhost only, cloud features off.
- Prompt `gaming.gta.v2`, output contract v2, scoring profile `editorial.default` v1
  (cut 60).
- Unconstrained output (this engine build has no JSON mode); RIFTWIRE's validator decides
  acceptance.
- 5 dev + 6 held-out synthetic scenarios × 2 repeats = **22 evaluations**.

| | Result |
|---|---|
| Backend tests (pinned commit) | **510 passed**, 3 skipped (second-filesystem tests) |
| Probe | health, model, plain text and the RIFTWIRE path PASS; JSON and schema modes unavailable (501) |
| Accepted evaluations | **19/22** |
| Accepted on the first attempt | **11/22** (attempt histogram: 11 × 1, 7 × 2, 4 × 3) |
| Dev / held-out accepted | **8/10** / **11/12** |
| Highlight-bearing evaluations that selected a highlight | **15/18** |
| Pipeline highlight recall (every highlight-bearing evaluation; failed ones count as misses) | **0.864** overall (19/22 true highlights): dev 0.80, held-out 0.917 |
| Recall among accepted answers only (conditional; **not** pipeline recall) | 1.0 |
| Provider errors | **0** |
| Unsupported claims detected | **0** |
| Filler surviving the editorial cut | **0** (1 padded answer before the cut, in dev) |
| Planted instructions obeyed (trap window selected) | **0** |
| Evaluation latency | median **33.88 s**, p95 **143.09 s**, max **153.66 s** |
| Generation latency (per model call) | median 17.88 s, p95 91.83 s, max 96.42 s |
| Correction attempts' share of generation time | **48.1 %** |
| Peak loaded model memory (Ollama) | **4 424.2 MB**; runner RSS peak 2 551 MB |
| Minimum available memory | **1 554.5 MB**; free memory 63 % before, 44 % after |
| Swap | 4 696 → 5 307 MB peak (5 267 after) |
| Disk | 120.57 → 120.47 GiB free (60 GiB floor) |

**The three rejected evaluations, after their repairs:**
- `funny_npc` (both repeats): a second candidate with a 2.1-second clip, and an evidence
  "id" that was really a time range (`42.0s-42.6s`).
- `injection_reverse` (repeat 0): text after the JSON object (`Extra data`). The planted
  instruction was **not** obeyed.

All three were **correctly rejected**; validation was not loosened.

**What this does not show:**
- **These are synthetic, author-written scenarios.** They are not real gameplay quality,
  which needs ≥ 3 authorized recordings and about 30 labelled moments.
- **The held-out scenarios are preserved unchanged** and are not used for tuning.
- **Correction latency is the main cost** (48 % of generation time). M5's production
  integration should move deterministic work, such as boundary arithmetic and evidence-id
  bookkeeping, out of the model.

Left out: `model-manifest.json` (its SHA-256 is in `summary.txt`) and `brew-info.json`.
