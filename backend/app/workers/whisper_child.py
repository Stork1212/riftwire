"""Transcribe ONE audio file in a fresh process (M4). Run by the Whisper venv's Python:

    <whisper python> whisper_child.py MODEL_DIR AUDIO OUT_JSON [LANGUAGE]

No RIFTWIRE imports: this runs outside the backend's environment, where ``mlx-whisper``
lives. Output (written atomically): ``{"words": [{"w", "start_ms", "end_ms", "prob",
"seg"}], "segments": [{"start_ms", "end_ms", "text"}], "text", "language",
"engine_version", "mlx_version", "mlx_peak_bytes", "load_s", "transcribe_s"}``. Times are
relative to the start of AUDIO; the caller adds the chunk offset. Peak memory is released
when the process exits.
"""

from __future__ import annotations

import json
import os
import sys
import time


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("mlx-whisper")
    except Exception:  # noqa: BLE001 - the version is informational only
        import mlx_whisper

        return str(getattr(mlx_whisper, "__version__", "unknown"))


def main(model_dir: str, audio: str, out_json: str, language: str = "en") -> int:
    # The model is the installed folder; never fall back to a hub download.
    if not os.path.isfile(os.path.join(model_dir, "weights.npz")):
        print(f"model not found at {model_dir}; nothing is downloaded", file=sys.stderr)
        return 66
    os.environ["HF_HUB_OFFLINE"] = "1"
    t0 = time.perf_counter()
    import mlx.core as mx
    import mlx_whisper
    from mlx_whisper.transcribe import ModelHolder

    ModelHolder.get_model(model_dir, mx.float16)
    t_load = time.perf_counter()
    result = mlx_whisper.transcribe(audio, path_or_hf_repo=model_dir, language=language,
                                    word_timestamps=True, fp16=True, verbose=None)
    t_done = time.perf_counter()
    words, segments = [], []
    for si, seg in enumerate(result.get("segments", [])):
        segments.append({"start_ms": round(seg["start"] * 1000, 1),
                         "end_ms": round(seg["end"] * 1000, 1),
                         "text": (seg.get("text") or "").strip()})
        for w in seg.get("words", []):
            words.append({"w": w["word"].strip(), "start_ms": round(w["start"] * 1000, 1),
                          "end_ms": round(w["end"] * 1000, 1),
                          "prob": round(w.get("probability", 0), 4), "seg": si})
    peak = None
    f = getattr(mx, "get_peak_memory", None) or getattr(getattr(mx, "metal", None),
                                                         "get_peak_memory", None)
    if f:
        peak = int(f())
    out = {"words": words, "segments": segments, "text": result.get("text", ""),
           "language": result.get("language"),
           "engine_version": f"mlx-whisper {_version()}",
           "mlx_version": getattr(mx, "__version__", None), "mlx_peak_bytes": peak,
           "load_s": round(t_load - t0, 3), "transcribe_s": round(t_done - t_load, 3)}
    tmp = out_json + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(out, fh)
    os.replace(tmp, out_json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
