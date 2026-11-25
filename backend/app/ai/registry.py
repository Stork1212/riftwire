"""Builds the configured local AI provider and checks machine resources before a heavy
AI workload (docs/M5_M6_AI_HIGHLIGHTS_PLAN.md §2).

Only local providers exist: Ollama on a loopback address (enforced by the adapter). The
cloud placeholders refuse to construct; there is no paid path and no hidden fallback."""

from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from app.ai.providers.base import Identity, Provider, ProviderError, Request, Response
from app.core.config import Settings


class AIDisabled(Exception):
    pass


def build(settings: Settings) -> Provider:
    if settings.ai_provider == "none":
        raise AIDisabled("Local AI is turned off (RIFTWIRE_AI_PROVIDER=none).")
    from app.ai.providers.ollama import OllamaProvider

    return OllamaProvider(settings.ai_model, base_url=settings.ai_base_url, keep_alive=0)


def available_memory_mb() -> int | None:
    """Memory the OS can hand to a new process now (MB), or ``None`` if unknown.
    macOS: free + inactive + speculative pages (``vm_stat``; purgeable pages are already
    counted among those and would be double-counted); Linux: ``MemAvailable``."""
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5,
                                 check=True).stdout
            size = int(re.search(r"page size of (\d+) bytes", out).group(1))
            pages = 0
            for key in ("Pages free", "Pages inactive", "Pages speculative"):
                m = re.search(rf"^{key}:\s+(\d+)\.", out, re.MULTILINE)
                pages += int(m.group(1)) if m else 0
            return pages * size // (1024 * 1024)
        meminfo = Path("/proc/meminfo")
        if meminfo.is_file():
            m = re.search(r"^MemAvailable:\s+(\d+) kB", meminfo.read_text(), re.MULTILINE)
            return int(m.group(1)) // 1024 if m else None
    except (OSError, subprocess.SubprocessError, AttributeError, ValueError):
        return None
    return None


_live: set[threading.Thread] = set()
_live_lock = threading.Lock()


def calls_in_flight() -> int:
    """Generation calls still running in this process, including ones abandoned by a
    stopped job (an HTTP call cannot be interrupted; it ends at the provider's timeout).
    Heavy jobs wait while this is non-zero, so two workloads never share the memory."""
    with _live_lock:
        for t in [t for t in _live if not t.is_alive()]:
            _live.discard(t)
        return len(_live)


class Cancellable:
    """Wraps a provider so a blocking ``generate`` can be abandoned when the job is
    cancelled or loses its lease: the call runs in a worker thread (bounded by the
    provider's own HTTP timeout), the model is asked to unload, and the thread is joined
    for up to ``join_s``. A thread still alive after that stays counted by
    :func:`calls_in_flight`, which keeps heavy jobs from starting beside it."""

    def __init__(self, inner: Provider, should_stop, poll_s: float = 0.25,
                 join_s: float = 30.0):
        self.inner, self.should_stop, self.poll_s = inner, should_stop, poll_s
        self.join_s = join_s

    def identity(self) -> Identity:
        return self.inner.identity()

    def health(self) -> dict:
        return self.inner.health()

    def unload(self) -> None:
        if hasattr(self.inner, "unload"):
            self.inner.unload()

    def generate(self, request: Request) -> Response:
        box: dict = {}

        def call() -> None:
            try:
                box["value"] = self.inner.generate(request)
            except BaseException as e:  # noqa: BLE001 - re-raised in the caller
                box["error"] = e

        t = threading.Thread(target=call, name="ai-generate", daemon=True)
        with _live_lock:
            _live.add(t)
        t.start()
        while t.is_alive():
            if self.should_stop():
                try:
                    self.unload()
                except ProviderError:
                    pass
                t.join(self.join_s)
                raise ProviderError("the job was cancelled during generation"
                                    + ("" if not t.is_alive() else
                                       " (the call is still finishing)"),
                                    transient=True, code="cancelled")
            t.join(self.poll_s)
            time.sleep(0)
        if "error" in box:
            raise box["error"]
        return box["value"]
