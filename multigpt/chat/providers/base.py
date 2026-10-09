"""Gemeinsame Schnittstelle aller Anbieter-Adapter (Plan Abschnitt 7, M3-01).

Ein Adapter liefert beim Streamen eine Folge von Events. Garantie für alle
Adapter: Das letzte Event ist genau ein ``Done`` oder ein ``Error``; Ausnahmen
werden intern zu ``Error`` und gelangen nie roh zum Aufrufer.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from multigpt.chat.models import Provider


# --- Events ------------------------------------------------------------------


@dataclass(frozen=True)
class Delta:
    """Ein Stück Antworttext."""

    text: str


@dataclass(frozen=True)
class ToolCallEvent:
    """Das Modell möchte ein Werkzeug aufrufen (ab M4a)."""

    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class Usage:
    """Tokenverbrauch der Anfrage."""

    tokens_in: int
    tokens_out: int


@dataclass(frozen=True)
class Error:
    """Fehler; ``message`` ist ein deutscher Text für Nutzer, ohne Keys und Interna."""

    message: str
    retryable: bool = False


@dataclass(frozen=True)
class Done:
    """Regulärer Abschluss des Streams."""

    finish_reason: str | None = None


Event = Delta | ToolCallEvent | Usage | Error | Done


# --- Nachrichten -------------------------------------------------------------

Role = Literal["user", "assistant", "system", "tool"]


@dataclass
class ChatMessage:
    role: Role
    content: str


# --- Fehler und Basisklasse --------------------------------------------------


class ProviderError(Exception):
    """Fehler bei Konfiguration oder Aufruf eines Anbieters (Text ohne Keys)."""


class ProviderAdapter:
    """Basisklasse. Nicht unterstützte Fähigkeiten werfen ``NotImplementedError``."""

    def __init__(self, provider: Provider):
        self.provider = provider

    def stream(
        self,
        model_id: str,
        messages: list[ChatMessage],
        system: str | None = None,
        tools: list[dict] | None = None,
        **params,
    ) -> Iterator[Event]:
        raise NotImplementedError

    def list_models(self) -> list[str]:
        raise NotImplementedError

    def is_online(self, timeout: float = 2) -> bool:
        """Kurzer Abruf der Modellliste; True/False, nie eine Ausnahme."""
        try:
            self.list_models()
        except Exception:
            return False
        return True

    def embed(self, model_id: str, texts: list[str]):
        raise NotImplementedError

    def transcribe(self, model_id: str, audio):
        raise NotImplementedError

    def speak(self, model_id: str, text: str, voice: str | None = None):
        raise NotImplementedError

    def generate_image(self, model_id: str, prompt: str, **params):
        raise NotImplementedError

    def edit_image(self, model_id: str, image, prompt: str, **params):
        raise NotImplementedError

    def __repr__(self):
        # Bewusst ohne Key oder URL-Interna.
        return f"<{type(self).__name__} provider={getattr(self.provider, 'pk', None)}>"
