"""Modus „Bild“ (M9-01): Nachricht direkt an das Bildmodell, ohne Chatmodell.

Aufruf aus ``api._stream``, wenn der Request ``"image": {...}`` enthält –
nach den Prüfungen W (geteilte Chats) und CHAT. Ablauf wie eine Antwort:
Nutzernachricht (Beschreibung) und Antwort des Bildmodells (``Message.model``
= Bildmodell, ``tool_state["image"]`` = gewählte Optionen), das Bild hängt als
erzeugter Anhang an der Antwort. SSE-Events wie beim Chat: start, status,
[error], usage, done; die Oberfläche lädt danach den Verlauf neu und zeigt das
Bild mit Vorschau. Das Standardmodell des Chats bleibt unverändert.

Noch nicht im Modus „Bild“: Bearbeiten, Neu erzeugen und Anhänge (Bild
bearbeiten folgt mit M9-02).
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from . import images, services, sharing
from . import status as provider_status
from .models import Conversation, Message

logger = logging.getLogger(__name__)

MSG_ONLY_NEW = "Im Modus „Bild“ lassen sich nur neue Nachrichten senden."
MSG_NO_ATTACHMENTS = (
    "Im Modus „Bild“ sind noch keine Anhänge möglich. Bilder bearbeiten folgt später."
)
MSG_INVALID = "Ungültige Angaben für den Modus „Bild“."


def answer_text(ai_model, fmt: str, quality: str, revised_prompt: str = "") -> str:
    """Text der Antwort (geht wie jede Antwort später auch an Chatmodelle)."""
    text = (
        f"Bild erzeugt mit {ai_model.display_name} "
        f"({images.FORMAT_LABELS[fmt]}, Qualität {images.QUALITIES[quality]})."
    )
    if revised_prompt.strip():
        text += f"\n\nVom Anbieter umformulierte Beschreibung: {revised_prompt.strip()}"
    return text


def stream(request, conversation: Conversation, data: dict, *, error, event_stream, parse_pk):
    """Prüfen, Nachrichten anlegen, SSE-Antwort. ``error``/``event_stream``/
    ``parse_pk`` kommen aus ``api`` (gleiche Fehlerform, kein Kreisimport)."""
    user = request.user
    options = data.get("image")
    if not isinstance(options, dict):
        return error(MSG_INVALID, 400)
    if data.get("regenerate") is True or data.get("edit_of") is not None:
        return error(MSG_ONLY_NEW, 400)
    if data.get("attachments"):
        return error(MSG_NO_ATTACHMENTS, 400)
    content = data.get("content")
    if not isinstance(content, str) or not content.strip():
        return error(images.MSG_EMPTY_PROMPT, 400)
    content = content.strip()
    if len(content) > images.MAX_PROMPT:
        return error(images.MSG_PROMPT_TOO_LONG, 400)
    try:
        fmt, quality, transparent = images.clean_options(
            options.get("format"), options.get("quality"), options.get("transparent")
        )
    except images.ImageError as exc:
        return error(exc.message, exc.status)
    expected_leaf, valid = parse_pk(data, "leaf")
    if not valid:
        return error("Ungültige Anfrage.", 400)
    ai_model, reason = images.pick_image_model(user)
    if ai_model is not None and ai_model.provider.check_status:
        # Lokaler bzw. geprüfter Anbieter: Status bei Bedarf auffrischen.
        provider_status.refresh(ai_model.provider)
        if images.blocked_reason(user, ai_model):
            ai_model, reason = images.pick_image_model(user)
    if ai_model is None:
        return error(reason, images.reason_status(reason))
    try:
        turn = prepare(
            user, conversation, ai_model, content, expected_leaf, fmt, quality, transparent
        )
    except services.TurnError as exc:
        return error(exc.message, exc.status)
    return event_stream(run(turn, fmt, quality, transparent))


def prepare(user, conversation, ai_model, content, expected_leaf, fmt, quality, transparent):
    """Nutzernachricht und (leere) Antwort des Bildmodells anlegen."""
    with transaction.atomic():
        Conversation.objects.select_for_update().filter(pk=conversation.pk).first()
        tree = services.Tree(conversation)
        parent_id = services._current_leaf_id(conversation, tree)
        if expected_leaf is not None and expected_leaf != parent_id:
            raise services.TurnError(sharing.STALE_MESSAGE, 409)
        services.close_pending(conversation)
        user_message = services.append_message(
            conversation, parent=parent_id, role=Message.Role.USER, content=content, author=user
        )
        assistant = services.append_message(
            conversation,
            parent=user_message.pk,
            role=Message.Role.ASSISTANT,
            model=ai_model,
            author=user,
            status=Message.Status.ABORTED,  # Platzhalter wie in services
            tool_state={"image": {"format": fmt, "quality": quality, "transparent": transparent}},
        )
        fields = {"updated": timezone.now()}
        if not conversation.title:
            fields["title"] = services._title_from(content)
        Conversation.objects.filter(pk=conversation.pk).update(**fields)
        for key, value in fields.items():
            setattr(conversation, key, value)
    return services.Turn(user, conversation, ai_model, user_message, assistant, query=content)


def run(turn, fmt: str, quality: str, transparent: bool):
    """SSE-Events (Name, Daten) der Bild-Antwort."""
    msg = turn.assistant_message
    status, error, text, cost = Message.Status.COMPLETE, "", "", None
    yield (
        "start",
        {
            "user_message_id": turn.user_message.pk,
            "assistant_message_id": msg.pk,
            "parent_id": turn.user_message.parent_id,
        },
    )
    yield "status", {"text": f"Bild wird mit {turn.ai_model.display_name} erzeugt …"}
    try:
        sharing.check_turn(turn)  # Freigabe entzogen? (geteilte Chats)
        attachment, revised = images.generate(
            turn.user,
            msg,
            turn.query,
            ai_model=turn.ai_model,
            fmt=fmt,
            quality=quality,
            transparent=transparent,
        )
        text = answer_text(turn.ai_model, fmt, quality, revised)
        cost = attachment.cost
    except sharing.AccessRevoked:
        status, error = Message.Status.ABORTED, sharing.MSG_REVOKED
    except images.ImageError as exc:
        status, error = Message.Status.ERROR, exc.message
    except Exception as exc:
        logger.error("Bild-Antwort %s fehlgeschlagen: %s", msg.pk, type(exc).__name__)
        status, error = Message.Status.ERROR, images.MSG_FAILED
    Message.objects.filter(pk=msg.pk).update(content=text, status=status, error=error)
    Conversation.objects.filter(pk=turn.conversation.pk).update(updated=timezone.now())
    if error:
        yield "error", {"message": error}
    yield "usage", {"tokens_in": 0, "tokens_out": 0, "cost": None if cost is None else str(cost)}
    yield "done", {"status": status}
