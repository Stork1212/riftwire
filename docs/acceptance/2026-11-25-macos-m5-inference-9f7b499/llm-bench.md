# LLM bench (2026-11-25T05:32:29.387104+00:00)

Synthetic author-written tasks: engineering measurements only, not highlight quality.

## qwen3.5:4b
verified: True  gate failures: []
```json
{
  "identity": {
    "provider": "ollama",
    "model": "qwen3.5:4b",
    "digest": "7f3251fa6878a78a606bcb3c074283306e5ad2ea411a7774a9dd98a4bc7f2d13",
    "details": {
      "parent_model": "",
      "format": "safetensors",
      "family": "",
      "families": null,
      "parameter_size": "",
      "quantization_level": "nvfp4",
      "runner": "mlx",
      "size_bytes": 3973305013
    },
    "kind": "local_model"
  },
  "tasks_run": 10,
  "inference_verified": true,
  "generations_with_output": 26,
  "output_constraint": [
    "none"
  ],
  "constraint_notes": [
    "Ollama /api/chat returned HTTP 501: structured output is unavailable"
  ],
  "thinking_chars_max": 0,
  "errors_by_message": {
    "schema: candidates.0.what_happens: Field required; schema: candidates.0.why_entertaining: Field required; schema: candidates.0.standalone: Field required; schema: candidates.0.title: Field required; schema: candidates.0.scores: Field required": 3,
    "schema: candidates.0.what_happens: Field required; schema: candidates.0.why_entertaining: Field required; schema: candidates.0.standalone: Field required; schema: candidates.0.evidence: List should have at most 12 items after validation, not 42; schema: candidates.0.description: Extra inputs are not permitted": 2,
    "schema: candidates.0.category: Field required; schema: candidates.0.what_happens: Field required; schema: candidates.0.why_entertaining: Field required; schema: candidates.0.standalone: Field required; schema: candidates.0.hook: Field required": 2,
    "schema: candidates.0.what_happens: Field required; schema: candidates.0.why_entertaining: Field required; schema: candidates.0.hook: Field required; schema: candidates.0.evidence: List should have at most 12 items after validation, not 40; schema: candidates.0.description: Extra inputs are not permitted": 1
  },
  "accepted": 2,
  "valid_first_try": 2,
  "failed_validation": 8,
  "provider_errors": 0,
  "mean_precision": 1.0,
  "mean_recall": 1.0,
  "trap_selected": 0,
  "padded": 0,
  "unsupported_claims": 0,
  "payoff_kept": [],
  "first_load_ms": 0.6,
  "output_tok_per_s_median": 35.9,
  "prompt_tok_per_s_median": 735.4,
  "wall_s_median": 43.7,
  "memory": {
    "ollama_loaded_peak_mb": 4453.4,
    "ollama_rss_peak_mb": 2829.3,
    "ollama_rss_before_mb": 30.5,
    "swap_used_before_mb": 4015.94,
    "swap_used_peak_mb": 4117.31,
    "swap_used_after_mb": 4105.06,
    "free_before_mb": 8002.7,
    "free_min_mb": 1827.6,
    "pressure_free_pct_before": 55,
    "pressure_free_pct_after": 36
  }
}
```
## Keyword heuristic (reference)
```json
{
  "pursuit_escape": {
    "chosen": [
      "c:2"
    ],
    "precision": 1.0,
    "recall": 1.0,
    "trap_selected": false
  },
  "funny_npc": {
    "chosen": [],
    "precision": null,
    "recall": 0.0,
    "trap_selected": false
  },
  "injection": {
    "chosen": [
      "c:2"
    ],
    "precision": 1.0,
    "recall": 1.0,
    "trap_selected": false
  },
  "nothing_here": {
    "chosen": [],
    "precision": 1.0,
    "recall": 1.0,
    "trap_selected": false
  },
  "two_highlights": {
    "chosen": [],
    "precision": null,
    "recall": 0.0,
    "trap_selected": false
  }
}
```
