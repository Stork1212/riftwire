"""Placeholders for paid cloud providers (OpenAI, Anthropic). The AI budget is $0 and no
paid request is authorized (CLAUDE.md rule 15), so these refuse at construction. They
contain no network code and read no keys; a real adapter needs an owner decision, a budget,
a usage ledger and keys from the macOS Keychain."""

from __future__ import annotations

from app.ai.providers.base import ProviderDisabled


class CloudProviderStub:
    def __init__(self, name: str, *_args, **_kwargs):
        raise ProviderDisabled(f"{name} is a paid cloud provider; disabled (AI budget $0, "
                               "no paid requests authorized)")
