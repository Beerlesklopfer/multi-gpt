"""Auswahl des Adapters anhand ``Provider.kind``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import ProviderAdapter, ProviderError

if TYPE_CHECKING:
    from multigpt.chat.models import Provider


def _adapter_classes() -> dict[str, type[ProviderAdapter]]:
    # Spät importiert, damit ``base`` ohne httpx-Adapter nutzbar bleibt.
    from .anthropic import AnthropicAdapter
    from .google import GoogleAdapter
    from .openai_compat import OpenAICompatAdapter

    return {
        "openai_compat": OpenAICompatAdapter,
        "anthropic": AnthropicAdapter,
        "google": GoogleAdapter,
    }


def get_adapter(provider: Provider) -> ProviderAdapter:
    """Adapter für ``provider``; unbekannte oder noch nicht umgesetzte Art -> ProviderError."""
    cls = _adapter_classes().get(provider.kind)
    if cls is None:
        raise ProviderError(f"Anbieterart „{provider.kind}“ wird nicht unterstützt.")
    return cls(provider)


# Bekannte OpenAI-kompatible Endpunkte als Vorschläge im Admin (nur Bequemlichkeit,
# jede andere URL bleibt möglich). LM Studio: Rechner im Heimnetz, Port 1234.
OPENAI_COMPAT_PRESETS = [
    ("OpenAI", "https://api.openai.com/v1"),
    ("OpenRouter", "https://openrouter.ai/api/v1"),
    ("Mistral", "https://api.mistral.ai/v1"),
    ("Groq", "https://api.groq.com/openai/v1"),
    ("LM Studio (Beispiel)", "http://192.168.0.10:1234/v1"),
]


def default_base_urls() -> dict[str, str]:
    """Standard-Basis-URL je ``Provider.kind`` (aus den Adaptern)."""
    return {kind: cls.default_base_url() for kind, cls in _adapter_classes().items()}
