"""Ollama adapter: HTTP to a local Ollama server (``/api/chat``), standard library only.

* **Loopback only.** The base URL must resolve to 127.0.0.1, ::1 or ``localhost``; anything
  else is refused before a socket is opened. Environment proxies are bypassed, so a call
  can never leave the machine through ``HTTP(S)_PROXY``.
* **Constrained output.** The ``highlight_eval.v1`` JSON schema is passed as ``format``;
  acceptance still belongs to :mod:`app.ai.validate`.
* **Thinking off by default** (``think: false``), deterministic options (temperature 0,
  fixed seed), a bounded ``num_ctx`` and ``num_predict``.
* **Memory.** ``keep_alive`` defaults to ``0`` so the model is unloaded after each call;
  :meth:`unload` forces it. One model at a time is enforced by the server settings
  (``OLLAMA_MAX_LOADED_MODELS=1``) that RIFTWIRE's launch script sets.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from app.ai.providers.base import Identity, ProviderError, Request, Response

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _check_loopback(base_url: str) -> str:
    parts = urlsplit(base_url)
    if parts.scheme != "http" or parts.hostname not in LOOPBACK_HOSTS:
        raise ProviderError(f"refusing non-loopback AI endpoint {base_url!r}: local inference "
                            "only", transient=False, code="non_loopback_endpoint")
    return base_url.rstrip("/")


class OllamaProvider:
    def __init__(self, model: str, *, base_url: str = "http://127.0.0.1:11434",
                 keep_alive: str | int = 0):
        self.model = model
        self.base_url = _check_loopback(base_url)
        self.keep_alive = keep_alive

    # ------------------------------------------------------------------ transport
    def _call(self, path: str, payload: dict | None, timeout: float) -> dict:
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(self.base_url + path, data=data,
                                     method="GET" if data is None else "POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with _NO_PROXY.open(req, timeout=timeout) as r:
                body = r.read(8 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as e:
            detail = e.read(2000).decode(errors="replace")
            transient = e.code >= 500 or e.code == 429
            code = "model_missing" if e.code == 404 else "provider_http_error"
            raise ProviderError(f"Ollama {path} returned {e.code}: {detail}",
                                transient=transient, code=code) from e
        except TimeoutError as e:
            raise ProviderError(f"Ollama {path} timed out after {timeout:.0f} s",
                                transient=True, code="provider_timeout") from e
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            raise ProviderError(f"Ollama is not reachable at {self.base_url}: {e}",
                                transient=True, code="provider_unreachable") from e
        if len(body) > 8 * 1024 * 1024:
            raise ProviderError("Ollama response exceeded 8 MiB", transient=False,
                                code="response_too_large")
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            raise ProviderError(f"Ollama returned non-JSON from {path}", transient=True,
                                code="provider_bad_response") from e

    # ------------------------------------------------------------------ contract
    def health(self) -> dict:
        return self._call("/api/version", None, 5)

    def identity(self) -> Identity:
        tags = self._call("/api/tags", None, 10).get("models", [])
        entry = next((m for m in tags if m.get("name") == self.model
                      or m.get("model") == self.model), None)
        if entry is None:
            raise ProviderError(f"model {self.model!r} is not installed in Ollama",
                                transient=False, code="model_missing")
        return Identity(provider="ollama", model=self.model, digest=entry.get("digest"),
                        details=dict(entry.get("details") or {}) | {
                            "size_bytes": entry.get("size")})

    def generate(self, request: Request) -> Response:
        user: dict = {"role": "user", "content": request.user}
        if request.images_b64:
            user["images"] = list(request.images_b64)
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": request.system}, user],
            "stream": False,
            "think": request.think,
            "keep_alive": self.keep_alive,
            "options": {"num_ctx": request.num_ctx, "num_predict": request.max_tokens,
                        "temperature": request.temperature, "seed": request.seed},
        }
        if request.json_schema is not None:
            payload["format"] = request.json_schema
        r = self._call("/api/chat", payload, request.timeout_s)
        msg = r.get("message") or {}
        ns = 1_000_000
        metrics = {
            "total_ms": round(r.get("total_duration", 0) / ns, 1),
            "load_ms": round(r.get("load_duration", 0) / ns, 1),
            "prompt_tokens": r.get("prompt_eval_count", 0),
            "prompt_ms": round(r.get("prompt_eval_duration", 0) / ns, 1),
            "output_tokens": r.get("eval_count", 0),
            "output_ms": round(r.get("eval_duration", 0) / ns, 1),
            "done_reason": r.get("done_reason"),
        }
        return Response(text=msg.get("content", ""), thinking=msg.get("thinking", "") or "",
                        metrics=metrics, cost_usd=0.0)

    def loaded(self) -> list[dict]:
        """Models currently in memory (``/api/ps``), with their resident size."""
        return self._call("/api/ps", None, 10).get("models", [])

    def unload(self) -> None:
        self._call("/api/generate", {"model": self.model, "keep_alive": 0}, 30)
