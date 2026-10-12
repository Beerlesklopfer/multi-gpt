"""MCP-Werkzeuge zum Lesen von Chats (Recht ``chat.read``).

``list_chats`` listet eigene und mit dem Konto geteilte Chats, ``read_chat`` liefert
den angezeigten Verlauf eines Chats als Markdown – mit Werkzeugaufrufen (Argumente
und Ergebnis gekürzt), Quellen, Status und Fehlern, damit sich ein Problem aus der Ferne
nachvollziehen lässt. Lange Verläufe kommen seitenweise.

Rechte: nur Chats, die das Konto selbst besitzt oder die mit ihm geteilt sind
(``sharing.access_for``). Die Einsicht von Verwaltern (Familie) gilt hier bewusst
nicht – über einen Key werden nie fremde Inhalte gelesen. Jeder Aufruf steht wie
alle Werkzeugaufrufe im Audit-Log (ohne Inhalte).
"""

from __future__ import annotations

import json

from django.utils import timezone

from multigpt.chat import services, sharing
from multigpt.chat.models import Conversation, Message, SourceRef
from multigpt.chat.templatetags.source_tags import source_items

from . import scopes as S
from .tools import (
    NodeTool,
    ToolFailure,
    ToolOutput,
    arg_bool,
    arg_int,
    arg_text,
    json_output,
    register,
    schema,
)

PAGE_CHARS = 30_000  # Zeichen je Seite von read_chat
ARGS_CHARS = 600  # Werkzeugargumente je Aufruf
RESULT_CHARS = 1_500  # Werkzeugergebnis je Aufruf
MSG_NOT_FOUND = "Chat nicht gefunden (oder nicht für dieses Konto lesbar)."


def _readable(user, pk: int) -> Conversation:
    conversation = Conversation.objects.filter(pk=pk).select_related("user", "project").first()
    if conversation is None:
        raise ToolFailure(MSG_NOT_FOUND)
    access = sharing.access_for(user, conversation)
    if not (access.owner or (access.read and access.shared)):
        raise ToolFailure(MSG_NOT_FOUND)  # Existenz fremder Chats bleibt verborgen
    return sharing.bind_viewer(conversation, user)


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split()) if limit < 1000 else str(text)
    return text if len(text) <= limit else text[:limit] + f" … (+{len(text) - limit} Zeichen)"


def _summary(conversation: Conversation, user) -> dict:
    data = {
        "chat_id": conversation.pk,
        "title": conversation.title or "Neuer Chat",
        "updated": timezone.localtime(conversation.updated).isoformat(timespec="minutes"),
        "archived": conversation.archived,
    }
    if conversation.user_id == user.pk:
        if conversation.project_id:
            data["project"] = conversation.project.name
    else:
        data["shared_by"] = sharing.display_name(conversation.user)
    return data


def list_chats(ctx, args):
    query = arg_text(args, "query", max_len=100).strip()
    archived = arg_bool(args, "archived")
    include_shared = arg_bool(args, "include_shared", default=True)
    page = arg_int(args, "page") or 1
    size = arg_int(args, "page_size", maximum=50) or 20
    own = Conversation.objects.filter(user=ctx.user, archived=archived).select_related("project")
    if query:
        own = own.filter(title__icontains=query)
    items = list(own.order_by("-updated", "-pk")[: page * size + 1])
    if include_shared:
        items += list(sharing.shared_with(ctx.user, query, archived, limit=page * size + 1))
        items.sort(key=lambda c: (c.updated, c.pk), reverse=True)
    start = (page - 1) * size
    chunk = items[start : start + size]
    more = len(items) > start + size
    data = {
        "page": page,
        "more": more,
        "chats": [_summary(c, ctx.user) for c in chunk],
    }
    note = f" Weitere mit page={page + 1}." if more else ""
    return json_output(data, f"{len(chunk)} Chat(s), Seite {page}.{note}")


def _tool_lines(message: Message) -> list[str]:
    lines = []
    for call in message.tool_calls.all():
        server = call.server.name if call.server_id and call.server else "MultiGPT"
        args = json.dumps(call.arguments or {}, ensure_ascii=False)
        line = f"- Werkzeug `{server} / {call.tool}` – {call.get_status_display()}"
        if call.duration is not None:
            line += f", {call.duration.total_seconds():.1f} s"
        lines.append(line)
        lines.append(f"  - Argumente: {_clip(args, ARGS_CHARS)}")
        result = (call.result or {}) if isinstance(call.result, dict) else {}
        text = result.get("text") or ""
        if text:
            label = "Fehler" if result.get("is_error") else "Ergebnis"
            lines.append(f"  - {label}: {_clip(text, RESULT_CHARS)}")
        names = [a.display_name for a in call.attachments.all()]
        if names:
            lines.append(f"  - Anhänge: {', '.join(names)}")
    return lines


def _source_lines(msg, user) -> list[str]:
    """Quellen [n] einer Antwort wie unter der Antwort in der Oberfläche: Webseiten
    mit URL, Dokumente mit Zitierangabe und Fundstelle (ohne internen Link)."""
    refs = list(msg.sources.all())
    if not refs:
        return []
    lines = ["**Quellen:**"]
    for ref, item in zip(refs, source_items(refs, user), strict=True):
        line = f"- [{item['n']}] {item['label']}"
        if ref.kind == SourceRef.Kind.WEB and ref.url and not item.get("restricted"):
            line += f" – {ref.url}"
        if item.get("location"):
            line += f" ({item['location']})"
        if item.get("removed"):
            line += " (entfernt)"
        lines.append(line)
    return lines + [""]


def render(conversation: Conversation, user) -> str:
    """Angezeigter Verlauf als Markdown, mit Werkzeugaufrufen und Status."""
    show_authors = sharing.is_shared(conversation) or sharing.has_other_authors(conversation)
    owner_name = sharing.display_name(conversation.user) if show_authors else ""
    title = " ".join((conversation.title or "Neuer Chat").split())
    lines = [f"# Chat {conversation.pk}: {title}", ""]
    if conversation.system_prompt.strip():
        lines += ["## System-Prompt des Chats", "", conversation.system_prompt.strip(), ""]
    for msg in services.visible_messages(conversation):
        if msg.role == Message.Role.USER:
            author = sharing.author_label(msg, user, conversation.user_id, owner_name, show_authors)
        else:
            author = msg.model.display_name if msg.model else "Assistent"
        stamp = timezone.localtime(msg.created)
        head = f"## [{msg.pk}] {author} – {stamp:%d.%m.%Y %H:%M}"
        if msg.status != Message.Status.COMPLETE:
            head += f" ({msg.get_status_display()})"
        lines += ["---", head, ""]
        if msg.role == Message.Role.ASSISTANT:
            tools = _tool_lines(msg)
            if tools:
                lines += tools + [""]
        if msg.content.strip():
            lines += [msg.content.rstrip(), ""]
        if msg.role == Message.Role.ASSISTANT:
            lines += _source_lines(msg, user)
        names = [a.display_name for a in msg.attachments.all() if a.tool_call_id is None]
        if names:
            lines += [f"*Anhänge: {', '.join(names)}*", ""]
        if msg.error:
            lines += [f"**Fehler:** {msg.error}", ""]
    return "\n".join(lines).rstrip() + "\n"


def read_chat(ctx, args):
    conversation = _readable(ctx.user, arg_int(args, "chat_id", required=True))
    page = arg_int(args, "page") or 1
    text = render(conversation, ctx.user)
    pages = max(1, -(-len(text) // PAGE_CHARS))
    if page > pages:
        raise ToolFailure(f"Den Chat gibt es nur in {pages} Seite(n).")
    chunk = text[(page - 1) * PAGE_CHARS : page * PAGE_CHARS]
    footer = f"\n\n(Seite {page} von {pages}" + (
        f"; weiter mit page={page + 1})" if page < pages else ")"
    )
    data = {"chat_id": conversation.pk, "page": page, "pages": pages}
    return ToolOutput(chunk + footer, data)


register(
    NodeTool(
        name="list_chats",
        title="Chats auflisten",
        description="Listet eigene und mit dem Konto geteilte Chats (neueste zuerst) mit ID, "
        "Titel, Projekt bzw. Besitzer und letzter Änderung. Für read_chat.",
        scopes=(S.CHAT_READ,),
        input_schema=schema(
            {
                "query": {"type": "string", "description": "Teil des Titels."},
                "archived": {"type": "boolean", "description": "Archivierte statt aktive Chats."},
                "include_shared": {
                    "type": "boolean",
                    "description": "Mit dem Konto geteilte Chats einbeziehen (Standard ja).",
                },
                "page": {"type": "integer", "minimum": 1},
                "page_size": {"type": "integer", "minimum": 1, "maximum": 50},
            }
        ),
        handler=list_chats,
        read_only=True,
    )
)
register(
    NodeTool(
        name="read_chat",
        title="Chat lesen",
        description="Liefert den angezeigten Verlauf eines Chats als Markdown: Nachrichten mit "
        "ID, Verfasser bzw. Modell, Zeit und Status, Werkzeugaufrufe mit Argumenten und "
        "Ergebnis (gekürzt), Anhänge und Fehler. Lange Chats seitenweise (page).",
        scopes=(S.CHAT_READ,),
        input_schema=schema(
            {
                "chat_id": {"type": "integer", "minimum": 1},
                "page": {"type": "integer", "minimum": 1},
            },
            required=["chat_id"],
        ),
        handler=read_chat,
        read_only=True,
    )
)
