# LLM bench (2026-11-25T06:01:02.422631+00:00)

Synthetic author-written tasks: engineering measurements only, not highlight quality.

## qwen3.5:4b
inference verified: True  failures: []
benchmark passed: True  failures: []
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
  "generations_with_output": 14,
  "output_constraint": [
    "none"
  ],
  "constraint_notes": [
    "Ollama /api/chat returned HTTP 501: structured output is unavailable"
  ],
  "thinking_chars_max": 0,
  "errors_by_message": {},
  "accepted": 10,
  "accepted_with_candidates": 8,
  "highlight_tasks": 8,
  "valid_first_try": 8,
  "failed_validation": 0,
  "provider_errors": 0,
  "mean_precision": 0.8,
  "mean_recall": 1.0,
  "trap_selected": 0,
  "padded": 4,
  "unsupported_claims": 0,
  "payoff_kept": [
    true,
    true
  ],
  "first_load_ms": 0.4,
  "output_tok_per_s_median": 35.7,
  "prompt_tok_per_s_median": 525.1,
  "wall_s_median": 14.9,
  "memory": {
    "ollama_loaded_peak_mb": 4218.2,
    "ollama_rss_peak_mb": 3048.2,
    "ollama_rss_before_mb": 24.7,
    "swap_used_before_mb": 4126.19,
    "swap_used_peak_mb": 4126.19,
    "swap_used_after_mb": 4110.94,
    "free_before_mb": 7574.2,
    "free_min_mb": 1974.4,
    "pressure_free_pct_before": 57,
    "pressure_free_pct_after": 39
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
