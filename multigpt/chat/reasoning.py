"""Denktiefe (Reasoning) der Antworten.

Wirksame Stufe für einen Chat (``effective``): Wahl im Chat
(``Conversation.reasoning_effort``) -> Vorgabe des Projekts
(``Project.reasoning_effort``, nur Projekte des Chat-Besitzers) -> Vorgabe des
Modells (``AIModel.default_reasoning_effort``, nur mit ``ai_model``) ->
``ChatSettings.default_reasoning_effort`` (Verwalter).

LM Studio (Schnittstelle „LM Studio“): Welche Stufen ein Modell kann, meldet LM
Studio selbst (``ModelRuntime.reasoning_options`` aus ``GET /api/v1/models``,
``capabilities.lmstudio_levels``); ohne Meldung gelten die Regeln nach Modell-ID.
Leer überall = Standard des Anbieters (nichts senden). Rangfolge und Rechte wie bei der Kreativität
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
from .models import AIModel, ChatSettings, Conversation, Provider, ReasoningEffort
from .models_lmstudio import ModelRuntime

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


def effective(conversation: Conversation, ai_model: AIModel | None = None) -> str:
    """Wirksame Stufe des Chats (siehe Moduldoku); ``""`` = Anbieterstandard."""
    if conversation.reasoning_effort:
        return conversation.reasoning_effort
    value, source = inherited(conversation)
    if source != "project" and ai_model is not None and ai_model.default_reasoning_effort:
        return ai_model.default_reasoning_effort
    return value


def _kind(ai_model: AIModel) -> str:
    return ai_model.provider.kind if ai_model.provider_id else ""


def _lmstudio_levels(ai_model: AIModel) -> tuple[str, ...] | None:
    """Von LM Studio gemeldete Stufen; ``None`` = nicht LM Studio bzw. nichts gemeldet."""
    if _kind(ai_model) != Provider.Kind.LMSTUDIO or not ai_model.pk:
        return None
    options = (
        ModelRuntime.objects.filter(ai_model=ai_model)
        .values_list("reasoning_options", flat=True)
        .first()
    )
    return capabilities.lmstudio_levels(options) if options else None


def _levels(ai_model: AIModel):
    found = _lmstudio_levels(ai_model)
    if found is not None:
        return found
    return capabilities.reasoning_support(ai_model.model_id, _kind(ai_model))


def levels_for(ai_model: AIModel) -> list[str] | None:
    """Unterstützte Stufen des Modells (für ``/api/models``), ``None`` = keine."""
    levels = _levels(ai_model)
    return list(levels) if levels else None


def level_for(ai_model: AIModel, value: str) -> str | None:
    """Stufe, die für dieses Modell tatsächlich mitgeht, oder ``None``."""
    if not value or is_rejected(ai_model):
        return None
    return capabilities.reasoning_level(value, _levels(ai_model))


def params_for(ai_model: AIModel, value: str) -> dict:
    """Parameter des Anbieters für ``adapter.stream`` oder ``{}``."""
    level = level_for(ai_model, value)
    if level is None:
        return {}
    lms = _lmstudio_levels(ai_model)
    if lms is not None:
        return capabilities.lmstudio_params(level, lms)
    return capabilities.reasoning_params(ai_model.model_id, _kind(ai_model), level)


def remember_rejected(ai_model: AIModel) -> None:
    """Denktiefe-Ablehnung merken: im Prozess und am Modell (``rejected_params``), damit sie
    einen Neustart überlebt (LOP C11)."""
    _rejected.add((ai_model.provider_id, ai_model.model_id))
    params = list(ai_model.rejected_params or [])
    if "reasoning" not in params:
        params.append("reasoning")
        ai_model.rejected_params = params
        AIModel.objects.filter(pk=ai_model.pk).update(rejected_params=params)
    logger.info("Modell %s: Denktiefe abgelehnt, künftig ohne", ai_model.pk)


def is_rejected(ai_model: AIModel) -> bool:
    return (ai_model.provider_id, ai_model.model_id) in _rejected or "reasoning" in (
        ai_model.rejected_params or []
    )


def forget_rejected() -> None:
    """Für Tests."""
    _rejected.clear()
