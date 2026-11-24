"""Runs ONE transcription in a fresh process (so peak memory is per run and fully released).

    bench/.venv/bin/python -m riftbench.transcribe_child MODEL_DIR AUDIO OUT_JSON

Writes {load_s, transcribe_s, text, words[{w,start_ms,end_ms,prob}], mlx_peak_bytes, ...}.
Only imports mlx/mlx_whisper here, so the rest of the harness runs without them.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time


def main(model_dir: str, audio: str, out_json: str, language: str = "en") -> None:
    t0 = time.perf_counter()
    import mlx.core as mx
    import mlx_whisper
    from mlx_whisper.transcribe import ModelHolder

    t_import = time.perf_counter()
    ModelHolder.get_model(model_dir, mx.float16)  # load once; transcribe() reuses it
    t_load = time.perf_counter()
    result = mlx_whisper.transcribe(audio, path_or_hf_repo=model_dir, language=language,
                                    word_timestamps=True, fp16=True, verbose=None)
    t_done = time.perf_counter()
    words = [{"w": w["word"].strip(), "start_ms": round(w["start"] * 1000, 1),
              "end_ms": round(w["end"] * 1000, 1), "prob": round(w.get("probability", 0), 4)}
             for seg in result.get("segments", []) for w in seg.get("words", [])]
    peak = None
    for fn in ("get_peak_memory",):
        f = getattr(mx, fn, None) or getattr(getattr(mx, "metal", None), fn, None)
        if f:
            peak = int(f())
            break
    out = {
        "import_s": round(t_import - t0, 3), "load_s": round(t_load - t_import, 3),
        "transcribe_s": round(t_done - t_load, 3), "text": result.get("text", ""),
        "language": result.get("language"), "words": words, "mlx_peak_bytes": peak,
        "mlx_version": getattr(mx, "__version__", None),
        "mlx_whisper_version": mlx_whisper.__version__,
        "torch_installed": importlib.util.find_spec("torch") is not None,
    }
    with open(out_json, "w") as f:
        json.dump(out, f)


if __name__ == "__main__":
    main(*sys.argv[1:])
