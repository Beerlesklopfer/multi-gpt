"""Kreativität (Temperatur) der Antworten.

Wirksamer Wert für einen Chat (``effective``): Wahl im Chat
(``Conversation.temperature``) -> Vorgabe des Projekts (``Project.temperature``,
nur Projekte des Chat-Besitzers) -> ``ChatSettings.default_temperature``
(Verwalter, Standard 0,3). Leer überall = Standard des Anbieters.

An das Modell geht der Wert als ``params["temperature"]`` (``params_for``) –
nur, wenn das Modell sie annimmt (``capabilities.accepts_temperature``) und der
Anbieter sie in diesem Prozess nicht schon einmal abgelehnt hat
(``remember_rejected``, gesetzt vom Wiederholversuch in services.py). Alle
Spalten eines Vergleichs nutzen denselben Wert des Chats. Ob ein Modell sie
annimmt, hängt auch von der Denktiefe ab (reasoning.py).
"""

import logging
from decimal import Decimal, InvalidOperation

from . import capabilities
from .models import AIModel, ChatSettings, Conversation

logger = logging.getLogger(__name__)

MIN, MAX = Decimal("0"), Decimal("2")

# Auswahl im Chat und im Projekt; leer = Standard (nächste Ebene).
PRESETS = (
    (Decimal("0.2"), "Präzise (0,2)"),
    (Decimal("0.7"), "Ausgewogen (0,7)"),
    (Decimal("1.0"), "Kreativ (1,0)"),
)

MSG_INVALID = "Ungültige Kreativität (erlaubt: 0 bis 2 oder leer)."

# (Anbieter-ID, Modell-ID), deren Anbieter die Temperatur abgelehnt hat. Nur für
# diesen Prozess; nach einem Neustart wird einmal neu probiert.
_rejected: set[tuple[int, str]] = set()


def fmt(value: Decimal | None) -> str:
    """Deutsche Schreibweise ohne überflüssige Nullen: 0,2 / 1,0 / 0,35."""
    if value is None:
        return ""
    text = f"{value.normalize():f}"
    if "." not in text:
        text += ".0"
    return text.replace(".", ",")


def label(value: Decimal | None) -> str:
    for preset, text in PRESETS:
        if value == preset:
            return text
    return f"Eigener Wert ({fmt(value)})"


def clean(raw) -> tuple[Decimal | None, str | None]:
    """JSON-Wert prüfen: ``null``/``""`` = Standard, sonst Zahl 0–2 (zwei Stellen)."""
    if raw is None or raw == "":
        return None, None
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        return None, MSG_INVALID
    try:
        value = Decimal(str(raw).replace(",", "."))
    except InvalidOperation:
        return None, MSG_INVALID
    if not value.is_finite() or not MIN <= value <= MAX:
        return None, MSG_INVALID
    return value.quantize(Decimal("0.01")), None


def owner_project(conversation: Conversation | None):
    """Projekt des Chats, sofern es dem Chat-Besitzer gehört (auch reasoning.py)."""
    if conversation is None or not conversation.project_id:
        return None
    project = conversation.project
    if project is not None and project.owner_id != conversation.user_id:
        return None
    return project


def inherited(conversation: Conversation | None, project=None) -> tuple[Decimal | None, str]:
    """Wert ohne Wahl im Chat und seine Herkunft (``project`` bzw. ``settings``)."""
    if project is None:
        project = owner_project(conversation)
    if project is not None and project.temperature is not None:
        return project.temperature, "project"
    return default_temperature(), "settings"


def default_temperature() -> Decimal | None:
    """Standard des Verwalters; ohne Datensatz der Feldstandard (ohne ihn anzulegen)."""
    row = ChatSettings.objects.filter(pk=ChatSettings.SINGLETON_PK).values("default_temperature")
    found = row.first()
    if found is None:
        return ChatSettings._meta.get_field("default_temperature").default
    return found["default_temperature"]


def effective(conversation: Conversation) -> Decimal | None:
    """Wirksame Temperatur des Chats (siehe Moduldoku); ``None`` = Anbieterstandard."""
    if conversation.temperature is not None:
        return conversation.temperature
    return inherited(conversation)[0]


def remember_rejected(ai_model: AIModel) -> None:
    _rejected.add((ai_model.provider_id, ai_model.model_id))
    logger.info("Modell %s: Temperatur abgelehnt, künftig ohne", ai_model.pk)


def forget_rejected() -> None:
    """Für Tests."""
    _rejected.clear()


def params_for(
    ai_model: AIModel,
    value: Decimal | None,
    *,
    thinking: bool = False,
    reasoning: str | None = None,
) -> dict:
    """``{"temperature": float}`` oder ``{}`` (kein Wert, Modell nimmt keine an).
    ``reasoning``: Denktiefe, die mitgeht (reasoning.level_for); mit Thinking
    (Claude) keine Temperatur, mit „Aus“ (gpt-5.1+: ``none``) wieder erlaubt."""
    if value is None:
        return {}
    if (ai_model.provider_id, ai_model.model_id) in _rejected:
        return {}
    kind = ai_model.provider.kind if ai_model.provider_id else ""
    if not capabilities.accepts_temperature(
        ai_model.model_id, kind, thinking=thinking, reasoning=reasoning
    ):
        return {}
    return {"temperature": float(value)}
