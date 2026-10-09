"""Anbieter-Adapter (Plan Abschnitt 7).

Einstieg: ``registry.get_adapter(provider)`` liefert den passenden Adapter.
"""

from .base import (
    ChatMessage,
    Delta,
    Done,
    Error,
    Event,
    ProviderAdapter,
    ProviderError,
    ToolCallEvent,
    Usage,
)
from .registry import get_adapter

__all__ = [
    "ChatMessage",
    "Delta",
    "Done",
    "Error",
    "Event",
    "ProviderAdapter",
    "ProviderError",
    "ToolCallEvent",
    "Usage",
    "get_adapter",
]
