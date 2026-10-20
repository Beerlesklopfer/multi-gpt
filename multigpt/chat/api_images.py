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

Neu erzeugen und Bearbeiten (``rerun``): Betrifft die Anfrage eine Bildantwort
(``tool_state["image"]``), also „Neu erzeugen“ der Antwort bzw. „Bearbeiten“ der Frage davor,
läuft sie wieder über das Bildmodell mit den gespeicherten Optionen statt über ein Chatmodell,
das nur Text liefert.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from multigpt.accounts.permissions import Action, can

from . import attachments as chat_attachments
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
            continue_last=continue_last
            and not sources
            and len(content) <= images.CONTINUE_MAX_CHARS,
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
    if reuse:
        options["image_reuse"] = True
    return services.Turn(
        user, conversation, ai_model, user_message, assistant, options=options, query=prompt
    )


def rerun_target(conversation: Conversation, data: dict):
    """``("regenerate", Antwort)`` bzw. ``("edit", Antwort der Frage)``, wenn die Anfrage eine
    Bildantwort betrifft (Neu erzeugen / Bearbeiten im Chat), sonst ``None``."""
    if data.get("image") is not None:
        return None
    pk = data.get("message_id") if data.get("regenerate") is True else data.get("edit_of")
    if isinstance(pk, bool) or not isinstance(pk, int):
        return None

    def is_image_reply(message) -> bool:
        state = message.tool_state if isinstance(message.tool_state, dict) else {}
        return isinstance(state.get("image"), dict)

    if data.get("regenerate") is True:
        reply = conversation.messages.filter(pk=pk, role=Message.Role.ASSISTANT).first()
        return ("regenerate", reply) if reply is not None and is_image_reply(reply) else None
    original = conversation.messages.filter(pk=pk, role=Message.Role.USER).first()
    if original is None:
        return None
    replies = conversation.messages.filter(parent_id=original.pk, role=Message.Role.ASSISTANT)
    for reply in replies.order_by("-id"):
        if is_image_reply(reply):
            return "edit", reply
    return None


def rerun(request, conversation: Conversation, data: dict, target, *, error, event_stream):
    """Bildantwort neu erzeugen bzw. die Frage dazu bearbeiten und neu senden: dasselbe
    Bildmodell, Format und Qualität der alten Antwort; neue Version im Baum."""
    user = request.user
    kind, old = target
    state = old.tool_state["image"]
    if kind == "edit" and not can(user, Action.UPDATE, conversation):
        return error("Du darfst in diesem Chat keine Nachrichten bearbeiten.", 403)
    try:
        fmt, quality, transparent = images.clean_options(
            state.get("format"), state.get("quality"), state.get("transparent")
        )
    except images.ImageError as exc:
        return error(exc.message, exc.status)
    question = conversation.messages.filter(pk=old.parent_id, role=Message.Role.USER).first()
    if question is None:
        return error(MSG_INVALID, 400)
    content = question.content
    if kind == "edit":
        content = data.get("content")
        if not isinstance(content, str) or not content.strip():
            return error(images.MSG_EMPTY_PROMPT, 400)
        content = content.strip()
        if len(content) > images.MAX_PROMPT:
            return error(images.MSG_PROMPT_TOO_LONG, 400)
    ids = data.get("attachments") if kind == "edit" else None
    if ids is not None and not (
        isinstance(ids, list) and all(isinstance(i, int) and not isinstance(i, bool) for i in ids)
    ):
        return error(MSG_INVALID, 400)
    try:
        turn, ai_model = prepare_rerun(
            user,
            conversation,
            kind,
            old,
            question,
            content,
            state,
            ids,
            (fmt, quality, transparent),
        )
    except services.TurnError as exc:
        return error(exc.message, exc.status)
    return event_stream(run(turn, fmt, quality, transparent))


def prepare_rerun(user, conversation, kind, old, question, content, state, ids, options):
    """Neue Version anlegen (Antwort bzw. Frage mit Antwort) und das Bildmodell festlegen."""
    fmt, quality, transparent = options
    with transaction.atomic():
        Conversation.objects.select_for_update().filter(pk=conversation.pk).first()
        services.close_pending(conversation)
        # Vorlagen: Anhänge der Frage (beim Bearbeiten die gewählten), sonst das Bild davor.
        originals, drafts = [], []
        if kind == "edit" and ids is not None:
            drafts, originals = services._resolve_attachments(user, ids, question.pk)
        else:
            originals = list(
                Attachment.objects.filter(
                    message_id=question.pk, kind=Attachment.Kind.IMAGE, tool_call__isnull=True
                ).order_by("created", "id")
            )
        if not all(a.kind == Attachment.Kind.IMAGE for a in [*drafts, *originals]):
            raise services.TurnError(MSG_ONLY_IMAGES)
        sources = [*drafts, *originals]
        prompt, reuse = content, []
        new_state = {"format": fmt, "quality": quality, "transparent": transparent}
        if sources:
            new_state["edit"] = True
        elif state.get("continued"):
            previous = images.previous_image(question.parent_id)
            if previous is not None:
                last_image, last_prompt = previous
                if old.model is not None and old.model.can_edit_images:
                    reuse = [last_image.pk]
                    new_state["edit"] = True
                else:
                    prompt = images.chain_prompt(last_prompt, content)
                new_state["continued"] = True
        new_state["prompt"] = prompt[: images.MAX_PROMPT]
        ai_model, reason = images.pick_image_model(
            user, old.model_id, edit=bool(new_state.get("edit"))
        )
        if ai_model is None:
            raise services.TurnError(reason, images.reason_status(reason))
        user_message = None
        if kind == "edit":
            user_message = services.append_message(
                conversation,
                parent=question.parent_id,
                role=Message.Role.USER,
                content=content,
                author=user,
            )
            for original in originals:
                chat_attachments.copy_for_message(original, user_message)
            if drafts:
                Attachment.objects.filter(pk__in=[a.pk for a in drafts]).update(
                    message=user_message, conversation=conversation
                )
        parent = user_message.pk if user_message is not None else question.pk
        assistant = services.append_message(
            conversation,
            parent=parent,
            role=Message.Role.ASSISTANT,
            model=ai_model,
            author=user,
            status=Message.Status.ABORTED,
            tool_state={"image": new_state},
        )
        Conversation.objects.filter(pk=conversation.pk).update(updated=timezone.now())
    source_ids = [a.pk for a in sources] + reuse
    options_out = {"image_sources": source_ids} if source_ids else {}
    if reuse:
        options_out["image_reuse"] = True
    turn = services.Turn(
        user, conversation, ai_model, user_message, assistant, options=options_out, query=prompt
    )
    return turn, ai_model


def _question_parent(turn):
    """Elternnachricht der Frage dieser Runde (dort sucht „weiterarbeiten“ das letzte Bild)."""
    if turn.user_message is not None:
        return turn.user_message.parent_id
    return (
        Message.objects.filter(pk=turn.assistant_message.parent_id)
        .values_list("parent_id", flat=True)
        .first()
    )


def run(turn, fmt: str, quality: str, transparent: bool):
    """SSE-Events (Name, Daten) der Bild-Antwort."""
    msg = turn.assistant_message
    status, error, text, cost = Message.Status.COMPLETE, "", "", None
    question = turn.user_message
    yield (
        "start",
        {
            "user_message_id": question.pk if question is not None else None,
            "assistant_message_id": msg.pk,
            "parent_id": question.parent_id if question is not None else msg.parent_id,
        },
    )
    verb = "bearbeitet" if turn.options.get("image_sources") else "erzeugt"
    yield "status", {"text": f"Bild wird mit {turn.ai_model.display_name} {verb} …"}
    try:
        sharing.check_turn(turn)  # Freigabe entzogen? (geteilte Chats)
        source_ids = turn.options.get("image_sources") or []
        state = (msg.tool_state or {}).get("image") or {}
        edited = bool(source_ids)
        chained = bool(state.get("continued")) and not source_ids
        common = {"ai_model": turn.ai_model, "fmt": fmt, "quality": quality}
        common["transparent"] = transparent
        if source_ids:
            sources = list(Attachment.objects.filter(pk__in=source_ids).order_by("created", "id"))
            try:
                attachment, revised = images.edit(turn.user, msg, turn.query, sources, **common)
            except images.EditUnsupported:
                # Beim stillen Weiterarbeiten (nicht bei hochgeladenen Vorlagen) weicht der Server
                # auf die Beschreibung aus, wenn dem Anbieter das Bearbeiten fehlt.
                previous = (
                    images.previous_image(_question_parent(turn))
                    if turn.options.get("image_reuse")
                    else None
                )
                if previous is None:
                    raise
                logger.warning(
                    "Bildmodell %s kann nicht bearbeiten: Weiterarbeiten mit der Beschreibung",
                    turn.ai_model.pk,
                )
                prompt = images.chain_prompt(previous[1], turn.query)
                attachment, revised = images.generate(turn.user, msg, prompt, **common)
                edited, chained = False, True
                state = {**state, "edit": False, "prompt": prompt[: images.MAX_PROMPT]}
                Message.objects.filter(pk=msg.pk).update(
                    tool_state={**(msg.tool_state or {}), "image": state}
                )
        else:
            attachment, revised = images.generate(turn.user, msg, turn.query, **common)
        text = answer_text(turn.ai_model, fmt, quality, revised, edited=edited, chained=chained)
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
