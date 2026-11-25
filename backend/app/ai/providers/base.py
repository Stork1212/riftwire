"""The provider contract shared by every engine (Ollama, OpenAI-compatible servers, future
engines). Engines differ only in transport; RIFTWIRE records identity and metrics for each
call so results stay reproducible when a model is upgraded or replaced."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class ProviderError(Exception):
    """A call failed. ``transient`` failures may be retried by the queue."""

    def __init__(self, message: str, *, transient: bool, code: str = "provider_error"):
        super().__init__(message)
        self.transient = transient
        self.code = code


class ProviderDisabled(ProviderError):
    def __init__(self, message: str):
        super().__init__(message, transient=False, code="provider_disabled")


@dataclass(frozen=True)
class Identity:
    provider: str                 # "ollama", "openai_compat", "heuristic"
    model: str                    # e.g. "qwen3.5:4b"
    digest: str | None            # engine-reported content digest of the weights
    details: dict = field(default_factory=dict)   # quantisation, family, parameter size
    kind: str = "local_model"     # "local_model" | "deterministic"


@dataclass(frozen=True)
class Request:
    system: str
    user: str
    json_schema: dict | None = None
    images_b64: tuple[str, ...] = ()
    num_ctx: int = 8192
    max_tokens: int = 2048
    temperature: float = 0.0
    seed: int = 7
    timeout_s: float = 300.0
    think: bool = False
    # Repetition penalties are forced off: model defaults (qwen3.5 ships presence_penalty
    # 1.5) penalize tokens already used, which corrupts JSON that repeats keys.
    presence_penalty: float = 0.0
    repeat_penalty: float = 1.0
    # Per-request override of the provider's keep_alive (e.g. keep the model loaded across
    # the repair attempts of one evaluation, then unload).
    keep_alive: str | int | None = None


@dataclass(frozen=True)
class Response:
    text: str
    thinking: str = ""
    metrics: dict = field(default_factory=dict)   # tokens, durations (ms), load time
    cost_usd: float = 0.0                          # local engines: always 0


class Provider(Protocol):
    def identity(self) -> Identity: ...
    def health(self) -> dict: ...
    def generate(self, request: Request) -> Response: ...
    def unload(self) -> None: ...
