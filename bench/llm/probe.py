"""Staged isolation of local inference, from the simplest request to RIFTWIRE's own path.

    backend/.venv/bin/python bench/llm/probe.py --model qwen3.5:4b --out DIR

1. server health          GET  /api/version
2. model availability     GET  /api/tags (+ /api/show: format, quantisation)
3. plain-text inference   POST /api/chat, think=false, no format
4. engine and residency   GET  /api/ps while the model is loaded
5. JSON mode              POST /api/chat, format="json"
6. JSON-schema mode       POST /api/chat, format=<small schema>
7. RIFTWIRE provider      app.ai.analyze.evaluate on one synthetic task (structured=auto)

Each step records PASS / FAIL / UNAVAILABLE with the engine's own message. Steps 1-3 and a
step 7 that reaches validation (not a provider error) are required; 5-6 are capabilities of
the engine build and are reported, not required. Loopback only; nothing is downloaded.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "backend"))
sys.path.insert(0, str(HERE))

from app.ai import prompts  # noqa: E402
from app.ai.analyze import evaluate  # noqa: E402
from app.ai.providers.base import ProviderError  # noqa: E402
from app.ai.providers.ollama import OllamaProvider  # noqa: E402
from tasks import TASKS, build  # noqa: E402

REQUIRED = ("health", "model_available", "plain_text", "provider_path")


def chat(p: OllamaProvider, content: str, fmt=None, timeout: float = 180) -> dict:
    body = {"model": p.model, "stream": False, "think": False, "keep_alive": "2m",
            "messages": [{"role": "user", "content": content}],
            "options": {"temperature": 0, "seed": 7, "num_predict": 64, "num_ctx": 2048,
                        "presence_penalty": 0, "repeat_penalty": 1}}
    if fmt is not None:
        body["format"] = fmt
    return p._call("/api/chat", body, timeout)


def run(model: str, base_url: str) -> list[dict]:
    p = OllamaProvider(model, base_url=base_url, keep_alive="2m", structured="auto")
    steps: list[dict] = []

    def step(name: str, fn) -> dict:
        t0 = time.perf_counter()
        try:
            status, detail = fn()
        except ProviderError as e:
            status = "UNAVAILABLE" if e.code == "structured_output_unavailable" else "FAIL"
            detail = {"code": e.code, "message": str(e)}
        except Exception as e:  # noqa: BLE001 - a probe reports every failure
            status, detail = "FAIL", {"code": type(e).__name__, "message": str(e)}
        row = {"step": name, "status": status, "seconds": round(time.perf_counter() - t0, 2),
               "detail": detail}
        steps.append(row)
        print(f"{status:11} {name:16} {json.dumps(detail, default=str)[:400]}", flush=True)
        return row

    step("health", lambda: ("PASS", p.health()))

    def available():
        ident = p.identity()
        show = p._call("/api/show", {"model": model}, 30)
        return "PASS", {"digest": ident.digest, "details": ident.details,
                        "capabilities": show.get("capabilities"),
                        "parameters": show.get("parameters")}
    step("model_available", available)

    def plain():
        r = chat(p, "Reply with exactly one word: OK")
        text = (r.get("message") or {}).get("content", "")
        thinking = (r.get("message") or {}).get("thinking", "") or ""
        ok = bool(text.strip())
        return ("PASS" if ok else "FAIL"), {
            "content": text[:200], "thinking_chars": len(thinking),
            "output_tokens": r.get("eval_count"), "load_ms": round(
                r.get("load_duration", 0) / 1e6), "total_ms": round(
                r.get("total_duration", 0) / 1e6), "done_reason": r.get("done_reason")}
    step("plain_text", plain)
    step("engine_residency", lambda: ("PASS", {"loaded": p.loaded()}))

    def json_mode():
        r = chat(p, 'Return a JSON object with key "answer" set to "OK".', fmt="json")
        text = (r.get("message") or {}).get("content", "")
        return "PASS", {"parsed": json.loads(text)}
    step("json_mode", json_mode)

    def schema_mode():
        schema = {"type": "object", "properties": {"answer": {"type": "string"}},
                  "required": ["answer"], "additionalProperties": False}
        r = chat(p, 'Return {"answer": "OK"}.', fmt=schema)
        return "PASS", {"parsed": json.loads((r.get("message") or {}).get("content", ""))}
    step("json_schema_mode", schema_mode)

    def provider_path():
        task = TASKS[0]
        bundle, _ = build(task)
        out = evaluate(bundle, p, prompts.load("gaming.gta.v2"),
                       target_count=task.target_count, channel="rumbleframe",
                       max_repairs=1, timeout_s=300)
        detail = {"outcome": out.status, "error": out.error,
                  "attempts": [{"ok": a.ok, "errors": a.errors[:4], "metrics": a.metrics}
                               for a in out.attempts],
                  "raw_head": out.raw_last[:600]}
        # Reaching validation proves inference through RIFTWIRE's path; acceptance is the
        # benchmark's question.
        return ("FAIL" if out.status == "provider_error" else "PASS"), detail
    step("provider_path", provider_path)
    try:
        p.unload()
    except ProviderError:
        pass
    return steps


def main() -> int:
    ap = argparse.ArgumentParser(description="Staged local-inference probe")
    ap.add_argument("--model", default="qwen3.5:4b")
    ap.add_argument("--base-url", default="http://127.0.0.1:11434")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    steps = run(args.model, args.base_url)
    ok = all(next((s["status"] == "PASS" for s in steps if s["step"] == r), False)
             for r in REQUIRED)
    (args.out / "probe.json").write_text(json.dumps(
        {"model": args.model, "required": REQUIRED, "passed": ok, "steps": steps},
        indent=2, default=str))
    print("PROBE", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
