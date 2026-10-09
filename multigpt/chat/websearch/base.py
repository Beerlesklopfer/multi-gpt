"""Gemeinsame Typen der Websuche (Plan 8d): Treffer, Fehler, Backend-Schnittstelle."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

USER_AGENT = "MultiGPT/0.1 (Websuche; selbst gehostet)"


@dataclass(frozen=True)
class SearchHit:
    """Ein Suchtreffer: Titel, URL und Kurztext der Suchmaschine."""

    title: str
    url: str
    snippet: str = ""


class SearchError(Exception):
    """Suche fehlgeschlagen; ``str(exc)`` ist eine deutsche Ursache ohne Interna."""


class SearchBackend(Protocol):
    """Austauschbares Such-Backend: ``search(query) -> [SearchHit]``."""

    def search(self, query: str, *, limit: int) -> list[SearchHit]: ...


class FetchError(Exception):
    """Seitenabruf abgelehnt oder fehlgeschlagen (deutscher Kurztext).

    ``blocked`` ist True, wenn der SSRF-Schutz den Abruf verhindert hat.
    """

    def __init__(self, message: str, *, blocked: bool = False):
        super().__init__(message)
        self.blocked = blocked
