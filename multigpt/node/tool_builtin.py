"""Eingebaute Werkzeuge direkt über die API (M15, ``tools.run``): ``create_pdf``,
``generate_image``, ``run_python``, ``fetch_url``, ``web_search``.

Dieselbe Logik wie im Chat (``tooling.BuiltinTool.run``): Rollenrechte
(``available``), Grenzen, Sandbox und Buchungen (Bilder) gelten unverändert.
Jeder Aufruf wird als Antwort mit Werkzeugaufruf im Chat „API: <Key> –
Werkzeuge“ gespeichert; erzeugte Dateien sind Anhänge dieser Antwort und über
``get_file`` abrufbar.

Rückfrage: Im Chat fragt MultiGPT vor ``generate_image`` bzw. ``run_python``
nach, wenn der Verwalter das eingestellt hat – dort entscheidet ein *Modell*
über den Aufruf. Über die API ruft der Inhaber des Keys bzw. sein Programm das
Werkzeug selbst und ausdrücklich auf; dieser Aufruf ist die Bestätigung.
"""

from __future__ import annotations

import logging
import time

from django.utils import timezone

from multigpt.chat import citations, services, tooling
from multigpt.chat import sources as source_refs
from multigpt.chat.models import Attachment, Conversation, Message, ToolCall

from . import chats
from . import scopes as S
from .tools import NodeTool, ToolFailure, ToolOutput, register

logger = logging.getLogger(__name__)

BUILTIN_NAMES = ("create_pdf", "generate_image", "run_python", "fetch_url", "web_search")


def _available(name: str):
    def check(ctx) -> bool:
        builtin = tooling.get_builtin(name)
        return builtin is not None and bool(builtin.available(ctx.user, None))

    return check


def _handler(name: str):
    def handler(ctx, args) -> ToolOutput:
        builtin = tooling.get_builtin(name)
        if builtin is None or not builtin.available(ctx.user, None):
            raise ToolFailure("Dieses Werkzeug steht dem Konto nicht zur Verfügung.")
        conversation = chats.tool_chat(ctx.key)
        message = services.append_message(
            conversation,
            role=Message.Role.ASSISTANT,
            author=ctx.user,
            status=Message.Status.COMPLETE,
            content=f"Werkzeug „{name}“ über den API-Key „{ctx.key.name}“ aufgerufen.",
        )
        Conversation.objects.filter(pk=conversation.pk).update(updated=timezone.now())
        tool_call = ToolCall.objects.create(
            message=message, tool=name, arguments=args, status=ToolCall.Status.RUNNING
        )
        sources = source_refs.SourceCollector(message, citations.prefs_for(ctx.user))
        started = time.monotonic()
        try:
            result = builtin.run(ctx.user, args, sources)
        except Exception as exc:
            logger.error("Werkzeug %s über API (%s): %s", name, tool_call.pk, type(exc).__name__)
            outcome = tooling.Outcome(ToolCall.Status.ERROR, tooling.MSG_FAILED, True)
        else:
            status = ToolCall.Status.ERROR if result.is_error else ToolCall.Status.OK
            outcome = tooling.Outcome(status, result.text or tooling.MSG_EMPTY, result.is_error)
        tooling.record(tool_call, outcome, started)
        attachments = [
            {
                "attachment_id": a.pk,
                "name": a.display_name,
                "mime_type": a.mime_type,
                "size": a.size,
            }
            for a in Attachment.objects.filter(message=message).order_by("pk")
        ]
        data = {
            "chat_id": conversation.pk,
            "message_id": message.pk,
            "attachments": attachments,
        }
        if sources.refs:
            data["sources"] = sources.items()
        text = outcome.text
        if attachments:
            ids = ", ".join(f"#{a['attachment_id']}" for a in attachments)
            text += f"\n\nAnhänge: {ids} (abholen mit get_file)."
        return ToolOutput(text, data, outcome.is_error)

    return handler


for _name in BUILTIN_NAMES:
    _builtin = tooling.get_builtin(_name)
    if _builtin is None:  # pragma: no cover - alle registriert, sobald services geladen ist
        continue
    register(
        NodeTool(
            name=_name,
            title=_builtin.label,
            description=_builtin.spec.description,
            scopes=(S.TOOLS_RUN,),
            input_schema=_builtin.spec.parameters,
            handler=_handler(_name),
            read_only=_name in ("fetch_url", "web_search"),
            open_world=_name in ("fetch_url", "web_search", "generate_image"),
            available=_available(_name),
        )
    )
