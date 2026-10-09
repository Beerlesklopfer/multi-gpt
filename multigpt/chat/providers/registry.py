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
