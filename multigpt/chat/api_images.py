"""Modus „Bild“ (M9-01): Nachricht direkt an das Bildmodell, ohne Chatmodell.

Aufruf aus ``api._stream``, wenn der Request ``"image": {...}`` enthält –
nach den Prüfungen W (geteilte Chats) und CHAT. Ablauf wie eine Antwort:
Nutzernachricht (Beschreibung) und Antwort des Bildmodells (``Message.model``
= Bildmodell, ``tool_state["image"]`` = gewählte Optionen), das Bild hängt als
erzeugter Anhang an der Antwort. SSE-Events wie beim Chat: start, status,
[error], usage, done; die Oberfläche lädt danach den Verlauf neu und zeigt das
Bild mit Vorschau. Das Standardmodell des Chats bleibt unverändert.

Bilder bearbeiten (M9-02): Hängen Bilder an der Nachricht (``"attachments"``), geht die
Nachricht mit den Bildern als Vorlage an ein Bildmodell mit „Bilder bearbeiten“
(``images.edit``); das Ergebnis verweist über ``source_image`` auf die Vorlage.

Noch nicht im Modus „Bild“: Neu erzeugen und Bearbeiten einer gesendeten Nachricht.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from . import images, services, sharing
from . import status as provider_status
from .models import Attachment, Conversation, Message

logger = logging.getLogger(__name__)

MSG_ONLY_NEW = "Im Modus „Bild“ lassen sich nur neue Nachrichten senden."
MSG_ONLY_IMAGES = "Im Modus „Bild“ lassen sich nur Bilder anhängen."
MSG_INVALID = "Ungültige Angaben für den Modus „Bild“."


def answer_text(
    ai_model,
    fmt: str,
    quality: str,
    revised_prompt: str = "",
    edited: bool = False,
    chained: bool = False,
) -> str:
    """Text der Antwort (geht wie jede Antwort später auch an Chatmodelle)."""
    text = (
        f"Bild {'bearbeitet' if edited else 'erzeugt'} mit {ai_model.display_name} "
        f"({images.FORMAT_LABELS[fmt]}, Qualität {images.QUALITIES[quality]})."
    )
    if chained:
        text += "\n\nWeitergearbeitet an der Beschreibung des vorigen Bildes."
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
    sources = data.get("attachments")
    if sources is not None and not (
        isinstance(sources, list)
        and len(sources) <= images.MAX_SOURCES
        and all(isinstance(i, int) and not isinstance(i, bool) for i in sources)
    ):
        return error(MSG_INVALID, 400)
    sources = list(dict.fromkeys(sources or []))
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
        continue_last = options.get("continue", False)
        if not isinstance(continue_last, bool):
            return error(MSG_INVALID, 400)
    except images.ImageError as exc:
        return error(exc.message, exc.status)
    expected_leaf, valid = parse_pk(data, "leaf")
    if not valid:
        return error("Ungültige Anfrage.", 400)
    preferred = options.get("model")
    if preferred is not None and (isinstance(preferred, bool) or not isinstance(preferred, int)):
        return error(MSG_INVALID, 400)
    editing = bool(sources)
    ai_model, reason = images.pick_image_model(user, preferred, edit=editing)
    if ai_model is not None and ai_model.provider.check_status:
        # Lokaler bzw. geprüfter Anbieter: Status bei Bedarf auffrischen.
        provider_status.refresh(ai_model.provider)
        if images.blocked_reason(user, ai_model):
            ai_model, reason = images.pick_image_model(user, preferred, edit=editing)
    if ai_model is None:
        return error(reason, images.reason_status(reason))
    try:
        turn = prepare(
            user,
            conversation,
            ai_model,
            content,
            expected_leaf,
            fmt,
            quality,
            transparent,
            sources,
            continue_last=continue_last and not sources,
        )
    except services.TurnError as exc:
        return error(exc.message, exc.status)
    return event_stream(run(turn, fmt, quality, transparent))


def prepare(
    user,
    conversation,
    ai_model,
    content,
    expected_leaf,
    fmt,
    quality,
    transparent,
    sources=(),
    continue_last=False,
):
    """Nutzernachricht und (leere) Antwort des Bildmodells anlegen."""
    with transaction.atomic():
        Conversation.objects.select_for_update().filter(pk=conversation.pk).first()
        tree = services.Tree(conversation)
        parent_id = services._current_leaf_id(conversation, tree)
        if expected_leaf is not None and expected_leaf != parent_id:
            raise services.TurnError(sharing.STALE_MESSAGE, 409)
        services.close_pending(conversation)
        drafts = []
        if sources:
            drafts, _ = services._resolve_attachments(user, list(sources), None)
            if not all(a.kind == Attachment.Kind.IMAGE for a in drafts):
                raise services.TurnError(MSG_ONLY_IMAGES)
        user_message = services.append_message(
            conversation, parent=parent_id, role=Message.Role.USER, content=content, author=user
        )
        if drafts:
            Attachment.objects.filter(pk__in=[a.pk for a in drafts]).update(
                message=user_message, conversation=conversation
            )
        image_state = {"format": fmt, "quality": quality, "transparent": transparent}
        prompt = content  # was das Bildmodell bekommt
        reuse = []
        if drafts:
            image_state["edit"] = True
        elif continue_last:
            # Am letzten Bild des Zweigs weiterarbeiten: bearbeiten, wenn das Modell es kann,
            # sonst die vorige Beschreibung mit der Änderung verketten.
            previous = images.previous_image(parent_id)
            if previous is not None:
                last_image, last_prompt = previous
                if ai_model.can_edit_images:
                    reuse = [last_image.pk]
                    image_state["edit"] = True
                else:
                    prompt = images.chain_prompt(last_prompt, content)
                image_state["continued"] = True
        image_state["prompt"] = prompt[: images.MAX_PROMPT]
        assistant = services.append_message(
            conversation,
            parent=user_message.pk,
            role=Message.Role.ASSISTANT,
            model=ai_model,
            author=user,
            status=Message.Status.ABORTED,  # Platzhalter wie in services
            tool_state={"image": image_state},
        )
        fields = {"updated": timezone.now()}
        if not conversation.title:
            fields["title"] = services._title_from(content)
        Conversation.objects.filter(pk=conversation.pk).update(**fields)
        for key, value in fields.items():
            setattr(conversation, key, value)
    source_ids = [a.pk for a in drafts] + reuse
    options = {"image_sources": source_ids} if source_ids else {}
    return services.Turn(
        user, conversation, ai_model, user_message, assistant, options=options, query=prompt
    )


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
    verb = "bearbeitet" if turn.options.get("image_sources") else "erzeugt"
    yield "status", {"text": f"Bild wird mit {turn.ai_model.display_name} {verb} …"}
    try:
        sharing.check_turn(turn)  # Freigabe entzogen? (geteilte Chats)
        source_ids = turn.options.get("image_sources") or []
        if source_ids:
            sources = list(Attachment.objects.filter(pk__in=source_ids).order_by("created", "id"))
            attachment, revised = images.edit(
                turn.user,
                msg,
                turn.query,
                sources,
                ai_model=turn.ai_model,
                fmt=fmt,
                quality=quality,
                transparent=transparent,
            )
        else:
            attachment, revised = images.generate(
                turn.user,
                msg,
                turn.query,
                ai_model=turn.ai_model,
                fmt=fmt,
                quality=quality,
                transparent=transparent,
            )
        state = (msg.tool_state or {}).get("image") or {}
        text = answer_text(
            turn.ai_model,
            fmt,
            quality,
            revised,
            edited=bool(source_ids),
            chained=bool(state.get("continued")) and not source_ids,
        )
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
