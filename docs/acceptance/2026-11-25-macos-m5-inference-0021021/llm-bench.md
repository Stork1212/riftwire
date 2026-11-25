# LLM bench (2026-11-25T14:05:39.073993+00:00)

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
  "tasks_run": 22,
  "inference_verified": true,
  "generations_with_output": 37,
  "output_constraint": [
    "none"
  ],
  "constraint_notes": [
    "Ollama /api/chat returned HTTP 501: structured output is unavailable"
  ],
  "thinking_chars_max": 0,
  "errors_by_message": {
    "candidates[1]: clip length 2100 ms is outside 8000-60000 ms; candidates[1].evidence[1]: unknown ids ['42.0s-42.6s']": 2,
    "invalid JSON: Extra data: line 2 column 1 (char 1320)": 1
  },
  "accepted": 19,
  "accepted_with_candidates": 15,
  "highlight_tasks": 18,
  "valid_first_try": 11,
  "failed_validation": 3,
  "provider_errors": 0,
  "mean_precision": 0.965,
  "mean_recall": 1.0,
  "trap_selected": 0,
  "padded": 1,
  "ranked_padded": 0,
  "ranked_mean_precision": 1.0,
  "ranked_mean_recall": 1.0,
  "unsupported_claims": 0,
  "payoff_kept": [
    true,
    true,
    true,
    true
  ],
  "output_tok_per_s_median": 34.6,
  "prompt_tok_per_s_median": 520.6,
  "latency": {
    "evaluation_s": {
      "median": 33.88,
      "p95": 143.09,
      "max": 153.66,
      "n": 22
    },
    "generation_s": {
      "median": 17.88,
      "p95": 91.83,
      "max": 96.42,
      "n": 37
    },
    "first_attempt_s": {
      "median": 19.15,
      "p95": 43.26,
      "max": 44.85
    },
    "attempts_histogram": {
      "1": 11,
      "2": 7,
      "3": 4
    },
    "repair_share_of_generation_time": 0.481,
    "output_tokens": {
      "median": 420,
      "max": 3072
    }
  },
  "ranking": {
    "id": "editorial.default",
    "version": 1,
    "sha256": "9db63ee1694284d3e1fa21507866631f4e19a03ca66fe3d3e2183b1fa4621681",
    "min_score": 60,
    "weights": {
      "hook": 0.2,
      "entertainment": 0.2,
      "surprise": 0.15,
      "intensity": 0.15,
      "standalone": 0.1,
      "pacing": 0.1,
      "payoff": 0.1
    },
    "match": {},
    "meaning": "estimated editorial-quality score (0-100); not a probability of going viral; separate from model confidence"
  },
  "first_load_ms": 3.9,
  "wall_s_median": 33.88,
  "splits": {
    "dev": {
      "tasks_run": 10,
      "inference_verified": true,
      "generations_with_output": 18,
      "output_constraint": [
        "none"
      ],
      "constraint_notes": [
        "Ollama /api/chat returned HTTP 501: structured output is unavailable"
      ],
      "thinking_chars_max": 0,
      "errors_by_message": {
        "candidates[1]: clip length 2100 ms is outside 8000-60000 ms; candidates[1].evidence[1]: unknown ids ['42.0s-42.6s']": 2
      },
      "accepted": 8,
      "accepted_with_candidates": 6,
      "highlight_tasks": 8,
      "valid_first_try": 5,
      "failed_validation": 2,
      "provider_errors": 0,
      "mean_precision": 0.917,
      "mean_recall": 1.0,
      "trap_selected": 0,
      "padded": 1,
      "ranked_padded": 0,
      "ranked_mean_precision": 1.0,
      "ranked_mean_recall": 1.0,
      "unsupported_claims": 0,
      "payoff_kept": [
        true,
        true
      ],
      "output_tok_per_s_median": 34.5,
      "prompt_tok_per_s_median": 519.6,
      "latency": {
        "evaluation_s": {
          "median": 31.27,
          "p95": 153.66,
          "max": 153.66,
          "n": 10
        },
        "generation_s": {
          "median": 18.6,
          "p95": 96.42,
          "max": 96.42,
          "n": 18
        },
        "first_attempt_s": {
          "median": 23.81,
          "p95": 36.9,
          "max": 36.9
        },
        "attempts_histogram": {
          "1": 5,
          "2": 2,
          "3": 3
        },
        "repair_share_of_generation_time": 0.518,
        "output_tokens": {
          "median": 443,
          "max": 3072
        }
      }
    },
    "holdout": {
      "tasks_run": 12,
      "inference_verified": true,
      "generations_with_output": 19,
      "output_constraint": [
        "none"
      ],
      "constraint_notes": [
        "Ollama /api/chat returned HTTP 501: structured output is unavailable"
      ],
      "thinking_chars_max": 0,
      "errors_by_message": {
        "invalid JSON: Extra data: line 2 column 1 (char 1320)": 1
      },
      "accepted": 11,
      "accepted_with_candidates": 9,
      "highlight_tasks": 10,
      "valid_first_try": 6,
      "failed_validation": 1,
      "provider_errors": 0,
      "mean_precision": 1.0,
      "mean_recall": 1.0,
      "trap_selected": 0,
      "padded": 0,
      "ranked_padded": 0,
      "ranked_mean_precision": 1.0,
      "ranked_mean_recall": 1.0,
      "unsupported_claims": 0,
      "payoff_kept": [
        true,
        true
      ],
      "output_tok_per_s_median": 34.8,
      "prompt_tok_per_s_median": 520.8,
      "latency": {
        "evaluation_s": {
          "median": 33.99,
          "p95": 143.09,
          "max": 143.09,
          "n": 12
        },
        "generation_s": {
          "median": 17.57,
          "p95": 91.83,
          "max": 91.83,
          "n": 19
        },
        "first_attempt_s": {
          "median": 17.88,
          "p95": 44.85,
          "max": 44.85
        },
        "attempts_histogram": {
          "1": 6,
          "2": 5,
          "3": 1
        },
        "repair_share_of_generation_time": 0.442,
        "output_tokens": {
          "median": 420,
          "max": 3072
        }
      }
    }
  },
  "memory": {
    "ollama_loaded_peak_mb": 4424.2,
    "ollama_rss_peak_mb": 2551.0,
    "ollama_rss_before_mb": 24.3,
    "swap_used_before_mb": 4696.38,
    "swap_used_peak_mb": 5306.69,
    "swap_used_after_mb": 5266.69,
    "free_before_mb": 8147.0,
    "free_min_mb": 1554.5,
    "pressure_free_pct_before": 63,
    "pressure_free_pct_after": 44
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
  },
  "hype_without_payoff": {
    "chosen": [
      "c:1"
    ],
    "precision": 0.0,
    "recall": 0.0,
    "trap_selected": true
  },
  "train_close_call": {
    "chosen": [],
    "precision": null,
    "recall": 0.0,
    "trap_selected": false
  },
  "glitch_through_wall": {
    "chosen": [],
    "precision": null,
    "recall": 0.0,
    "trap_selected": false
  },
  "injection_reverse": {
    "chosen": [],
    "precision": null,
    "recall": 0.0,
    "trap_selected": false
  },
  "five_requested_two_good": {
    "chosen": [],
    "precision": null,
    "recall": 0.0,
    "trap_selected": false
  },
  "quiet_room_nothing": {
    "chosen": [],
    "precision": 1.0,
    "recall": 1.0,
    "trap_selected": false
  }
}
```
