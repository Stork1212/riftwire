"""One grounded highlight evaluation: render the prompt, call the provider, validate, and
retry with the validator's errors at most ``max_repairs`` times.

The outcome records everything needed to reproduce and audit it: provider identity, prompt
id/version/SHA-256, evidence fingerprint, per-attempt metrics and errors. A rejected answer
is a failure with reasons, never a silent fallback; a deterministic baseline is reported
only under its own provider name.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from app.ai import prompts as prompt_registry
from app.ai.providers.base import Identity, Provider, ProviderError, Request
from app.ai.schemas import EvidenceBundle, HighlightEval, output_contract, response_json_schema
from app.ai.validate import Clip, validate


@dataclass
class Attempt:
    n: int
    ok: bool
    errors: list[str]
    warnings: list[str]
    metrics: dict
    response_chars: int
    response_head: str = ""          # first 4 000 characters, kept for diagnosis


@dataclass
class Outcome:
    status: str                       # accepted | failed_validation | provider_error
    identity: Identity | None
    prompt_id: str
    prompt_version: int
    prompt_sha256: str
    evidence_fingerprint: str
    value: HighlightEval | None = None
    clips: list[Clip] = field(default_factory=list)
    attempts: list[Attempt] = field(default_factory=list)
    error: str | None = None
    raw_last: str = ""

    def record(self) -> dict:
        return {
            "status": self.status,
            "provider": asdict(self.identity) if self.identity else None,
            "prompt": {"id": self.prompt_id, "version": self.prompt_version,
                       "sha256": self.prompt_sha256},
            "evidence_fingerprint": self.evidence_fingerprint,
            "attempts": [asdict(a) for a in self.attempts],
            "error": self.error,
            "result": self.value.model_dump(by_alias=True) if self.value else None,
            "clips": [asdict(c) for c in self.clips],
        }


def evaluate(bundle: EvidenceBundle, provider: Provider, prompt: prompt_registry.Prompt, *,
             target_count: int = 5, channel: str | None = None, max_repairs: int = 2,
             timeout_s: float = 300.0, identity: Identity | None = None) -> Outcome:
    gen = prompt.meta.get("generation", {})
    system, user = prompt.render(evidence=prompt_registry.render_evidence(bundle),
                                 target_count=str(target_count),
                                 channel_voice=prompt_registry.channel_voice(channel),
                                 output_contract=output_contract())
    mins = int(prompt.meta.get("defaults", {}).get("min_clip_s", 8)) * 1000
    maxs = int(prompt.meta.get("defaults", {}).get("max_clip_s", 60)) * 1000
    out = Outcome(status="provider_error", identity=identity, prompt_id=prompt.id,
                  prompt_version=prompt.version, prompt_sha256=prompt.sha256,
                  evidence_fingerprint=bundle.fingerprint())
    schema = response_json_schema()
    message = user
    for n in range(1, max_repairs + 2):
        req = Request(system=system, user=message, json_schema=schema,
                      num_ctx=int(gen.get("num_ctx", 8192)),
                      max_tokens=int(gen.get("max_tokens", 2048)),
                      temperature=float(gen.get("temperature", 0.0)), timeout_s=timeout_s)
        try:
            resp = provider.generate(req)
        except ProviderError as e:
            out.status, out.error = "provider_error", f"{e.code}: {e}"
            return out
        res = validate(resp.text, bundle, min_clip_ms=mins, max_clip_ms=maxs)
        out.raw_last = resp.text
        out.attempts.append(Attempt(n, res.ok, res.errors, res.warnings, resp.metrics,
                                    len(resp.text), resp.text[:4000]))
        if res.ok:
            out.status, out.value, out.clips = "accepted", res.value, res.clips
            return out
        message = (user + "\n\nYour previous answer was rejected for these reasons:\n- "
                   + "\n- ".join(res.errors[:15])
                   + "\nReturn one corrected JSON object that fixes every reason and follows "
                   "the output contract exactly (same keys, nothing else).")
    out.status = "failed_validation"
    out.error = "; ".join(out.attempts[-1].errors[:5])
    return out
