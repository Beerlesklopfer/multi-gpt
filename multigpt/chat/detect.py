"""Fähigkeiten von Modellen erkennen und an ``AIModel`` übertragen.

Quelle je Modell: Was ein lokaler Anbieter selbst meldet (LM Studio,
``/api/v0/models``: ``type`` und ``capabilities``), sonst die Heuristik aus
``capabilities`` (Modell-ID). Die Meldung von LM Studio wird nur bei Bedarf
abgerufen: beim Anlegen neuer Modelle eines lokalen Anbieters, bei der
Admin-Aktion und bei ``manage.py guess_capabilities`` – nicht bei jeder
Statusprüfung.

Neue Modelle bekommen die erkannten Werte (``new_model``). Bestehende werden
nur auf ausdrücklichen Wunsch geändert (``diff``/``apply``), weil der
Verwalter sie angepasst haben kann.

MCP-Freigabe neuer Modelle (``default_mcp_access``): „alle“ nur bei bekannten
Cloud-Anbietern (OpenAI, Anthropic, Google), sonst „kein“. Unbekannte lokale
Modelle oder Modelle über Sammelanbieter (OpenRouter u. a.) sollen keine
Werkzeuge erreichen, die etwas verändern, bevor ein Verwalter das freigibt
(Prompt-Injection über Webseiten und Dokumente).
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from . import capabilities
from .capabilities import Detected
from .models import AIModel, Provider
from .providers import registry

DETECT_TIMEOUT = 2.0

# Hosts der OpenAI-API (OpenAI-kompatible Anbieter mit diesem Host gelten als OpenAI).
_OPENAI_HOSTS = {"api.openai.com"}

# Felder, die die Erkennung setzt, mit Admin-Bezeichnung.
FIELDS = {
    "capability": "Fähigkeit",
    "supports_tools": "Werkzeuge",
    "supports_vision": "Bilder verstehen",
}


def is_known_cloud(provider: Provider) -> bool:
    """OpenAI, Anthropic oder Google (nicht lokal)."""
    if provider.is_local:
        return False
    if provider.kind in (Provider.Kind.ANTHROPIC, Provider.Kind.GOOGLE):
        return True
    if provider.kind == Provider.Kind.OPENAI_COMPAT:
        url = provider.base_url or registry.get_adapter(provider).default_base_url()
        return (urlsplit(url).hostname or "").lower() in _OPENAI_HOSTS
    return False


def default_mcp_access(provider: Provider) -> str:
    """MCP-Freigabe für neu angelegte Modelle (siehe Moduldoku)."""
    if is_known_cloud(provider):
        return AIModel.McpAccess.ALL
    return AIModel.McpAccess.NONE


def reported(provider: Provider, timeout: float = DETECT_TIMEOUT) -> dict[str, Detected]:
    """Vom Anbieter gemeldete Fähigkeiten; nur lokale Anbieter, sonst ``{}``."""
    if not provider.is_local:
        return {}
    try:
        return registry.get_adapter(provider).model_capabilities(timeout=timeout)
    except Exception:
        return {}


def detect(provider: Provider, model_ids, timeout: float = DETECT_TIMEOUT) -> dict[str, Detected]:
    """Fähigkeiten je Modell-ID: Meldung des Anbieters, sonst Heuristik."""
    model_ids = list(model_ids)
    known = reported(provider, timeout) if model_ids else {}
    return {mid: known.get(mid) or capabilities.guess(mid) for mid in model_ids}


def new_model(provider: Provider, model_id: str, detected: Detected, **fields) -> AIModel:
    """Ungespeichertes ``AIModel`` mit erkannten Fähigkeiten; ``fields`` überschreiben."""
    values = {
        "provider": provider,
        "model_id": model_id,
        "display_name": model_id,
        "capability": detected.capability,
        "supports_tools": detected.tools,
        "supports_vision": detected.vision,
        "mcp_access": default_mcp_access(provider),
        **fields,
    }
    return AIModel(**values)


@dataclass(frozen=True)
class Change:
    model: AIModel
    detected: Detected
    changes: dict  # Feld -> (alt, neu)

    def describe(self) -> str:
        parts = []
        for name, (old, new) in self.changes.items():
            if name == "capability":
                old, new = AIModel.Capability(old).label, AIModel.Capability(new).label
            else:
                old, new = ("ja" if old else "nein"), ("ja" if new else "nein")
            parts.append(f"{FIELDS[name]}: {old} → {new}")
        return "; ".join(parts)


def diff(models) -> list[Change]:
    """Abweichungen bestehender Modelle von der Erkennung (nichts wird gespeichert).

    Je Anbieter höchstens ein Abruf der Meldung (nur lokale Anbieter).
    """
    by_provider: dict[int, list[AIModel]] = {}
    for model in models:
        by_provider.setdefault(model.provider_id, []).append(model)
    result = []
    for group in by_provider.values():
        found = detect(group[0].provider, [m.model_id for m in group])
        for model in group:
            detected = found[model.model_id]
            target = {
                "capability": detected.capability,
                "supports_tools": detected.tools,
                "supports_vision": detected.vision,
            }
            changes = {
                name: (getattr(model, name), value)
                for name, value in target.items()
                if getattr(model, name) != value
            }
            if changes:
                result.append(Change(model, detected, changes))
    return result


def apply(changes: list[Change]) -> int:
    """Änderungen aus ``diff`` speichern; Anzahl geänderter Modelle."""
    for change in changes:
        for name, (_, new) in change.changes.items():
            setattr(change.model, name, new)
        change.model.save(update_fields=list(change.changes))
    return len(changes)
