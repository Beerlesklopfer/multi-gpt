"""Denktiefe (Reasoning) der Antworten.

Wirksame Stufe für einen Chat (``effective``): Wahl im Chat
(``Conversation.reasoning_effort``) -> Vorgabe des Projekts
(``Project.reasoning_effort``, nur Projekte des Chat-Besitzers) ->
``ChatSettings.default_reasoning_effort`` (Verwalter). Leer überall = Standard
des Anbieters (nichts senden). Rangfolge und Rechte wie bei der Kreativität
(creativity.py).

An das Modell geht die Stufe über ``params_for`` in der Form des Anbieters
(``capabilities.reasoning_params``: OpenAI ``reasoning_effort``, Claude
``output_config.effort``/``thinking``, Gemini ``thinkingConfig``) – nur, wenn
das Modell Reasoning kann (``capabilities.reasoning_support``) und der Anbieter
den Parameter in diesem Prozess nicht schon einmal abgelehnt hat
(``remember_rejected``, gesetzt vom Wiederholversuch in services.py). Alle
Spalten eines Vergleichs nutzen dieselbe Stufe des Chats, je Modell auf die
unterstützten Stufen abgebildet (``capabilities.reasoning_level``).
"""

import logging

from . import capabilities
from .creativity import owner_project
from .models import AIModel, ChatSettings, Conversation, ReasoningEffort

logger = logging.getLogger(__name__)

LABELS = dict(ReasoningEffort.choices)

MSG_INVALID = "Ungültige Denktiefe."

# (Anbieter-ID, Modell-ID), deren Anbieter den Parameter abgelehnt hat. Nur für
# diesen Prozess; nach einem Neustart wird einmal neu probiert.
_rejected: set[tuple[int, str]] = set()


def label(value: str) -> str:
    return LABELS.get(value, value)


def clean(raw) -> tuple[str, str | None]:
    """JSON-Wert prüfen: ``null``/``""`` = Standard, sonst eine Stufe."""
    if raw is None or raw == "":
        return "", None
    if not isinstance(raw, str) or raw not in LABELS:
        return "", MSG_INVALID
    return raw, None


def default_effort() -> str:
    """Standard des Verwalters; ohne Datensatz der Feldstandard (ohne ihn anzulegen)."""
    row = ChatSettings.objects.filter(pk=ChatSettings.SINGLETON_PK).values(
        "default_reasoning_effort"
    )
    found = row.first()
    if found is None:
        return ChatSettings._meta.get_field("default_reasoning_effort").default
    return found["default_reasoning_effort"]


def inherited(conversation: Conversation | None, project=None) -> tuple[str, str]:
    """Stufe ohne Wahl im Chat und ihre Herkunft (``project`` bzw. ``settings``)."""
    if project is None:
        project = owner_project(conversation)
    if project is not None and project.reasoning_effort:
        return project.reasoning_effort, "project"
    return default_effort(), "settings"


def effective(conversation: Conversation) -> str:
    """Wirksame Stufe des Chats (siehe Moduldoku); ``""`` = Anbieterstandard."""
    if conversation.reasoning_effort:
        return conversation.reasoning_effort
    return inherited(conversation)[0]


def _kind(ai_model: AIModel) -> str:
    return ai_model.provider.kind if ai_model.provider_id else ""


def levels_for(ai_model: AIModel) -> list[str] | None:
    """Unterstützte Stufen des Modells (für ``/api/models``), ``None`` = keine."""
    levels = capabilities.reasoning_support(ai_model.model_id, _kind(ai_model))
    return list(levels) if levels else None


def level_for(ai_model: AIModel, value: str) -> str | None:
    """Stufe, die für dieses Modell tatsächlich mitgeht, oder ``None``."""
    if not value or (ai_model.provider_id, ai_model.model_id) in _rejected:
        return None
    levels = capabilities.reasoning_support(ai_model.model_id, _kind(ai_model))
    return capabilities.reasoning_level(value, levels)


def params_for(ai_model: AIModel, value: str) -> dict:
    """Parameter des Anbieters für ``adapter.stream`` oder ``{}``."""
    level = level_for(ai_model, value)
    if level is None:
        return {}
    return capabilities.reasoning_params(ai_model.model_id, _kind(ai_model), level)


def remember_rejected(ai_model: AIModel) -> None:
    _rejected.add((ai_model.provider_id, ai_model.model_id))
    logger.info("Modell %s: Denktiefe abgelehnt, künftig ohne", ai_model.pk)


def forget_rejected() -> None:
    """Für Tests."""
    _rejected.clear()
