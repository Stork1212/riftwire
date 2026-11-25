# LLM bench (2026-11-25T05:17:06.347376+00:00)

Synthetic author-written tasks: engineering measurements only, not highlight quality.

## qwen3.5:4b
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
  "accepted": 0,
  "valid_first_try": 0,
  "failed_validation": 0,
  "provider_errors": 10,
  "mean_precision": null,
  "mean_recall": null,
  "trap_selected": 0,
  "padded": 0,
  "unsupported_claims": 0,
  "payoff_kept": [],
  "first_load_ms": null,
  "output_tok_per_s_median": null,
  "prompt_tok_per_s_median": null,
  "wall_s_median": 0.0,
  "memory": {
    "ollama_loaded_peak_mb": 3785.5,
    "ollama_rss_peak_mb": 43.9,
    "ollama_rss_before_mb": 43.9,
    "swap_used_before_mb": 1829.06,
    "swap_used_peak_mb": 1829.06,
    "swap_used_after_mb": 3334.19,
    "free_before_mb": 286.3,
    "free_min_mb": 286.2
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
