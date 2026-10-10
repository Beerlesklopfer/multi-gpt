"""Seiten der Chatoberfläche (M3-05, M5). JSON/SSE-Endpunkte liegen in api.py
und api_manage.py. Die Seitenleiste kommt aus dem Tag ``chat_sidebar``."""

import re
import unicodedata

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_GET

from multigpt.accounts.permissions import Action, can, supervised_conversation_owner

from . import attachments, projects, services, sharing
from .models import Conversation, Message
from .websearch import web_search_available


def _has_chat_model(user) -> bool:
    """Darf der Nutzer chatten und mindestens ein Chat-Modell verwenden?"""
    # Auch vom Budget gesperrte Modelle zählen: Sie erscheinen ausgegraut (M6-03).
    return can(user, Action.CHAT) and bool(services.chat_models_for(user))


def _page_context(request, conversation=None):
    return {
        "active_conversation": conversation,
        "has_chat_model": _has_chat_model(request.user),
        "web_search_available": web_search_available(request.user),
        # Anhänge: Dateiauswahl und Vorprüfung im Browser (maßgeblich prüft der Server).
        "attachment_limits": attachments.limits(),
        # Projekt (M5-07): Kopfzeile, Vorauswahl Modell und Sammlungen, „Neuer Chat im Projekt“.
        **projects.page_context(request, conversation),
    }


def _readable_conversation(request, pk) -> Conversation:
    """Chat laden; ohne READ-Recht 404 (Existenz fremder Chats bleibt verborgen)."""
    conv = get_object_or_404(Conversation.objects.select_related("default_model", "project"), pk=pk)
    if not can(request.user, Action.READ, conv):
        raise Http404
    # Geteilte Chats: angezeigter Zweig je Betrachter (sharing.py).
    return sharing.bind_viewer(conv, request.user)


@login_required
def index(request):
    """Startseite: leerer Chat. Der Chat wird erst beim ersten Senden angelegt."""
    return render(request, "chat/index.html", _page_context(request))


def _path_with_versions(conv: Conversation) -> list[Message]:
    """Angezeigter Pfad; je Nachricht Position (1-basiert) und die Nachbarn
    unter ihren Geschwistern für den Versionsumschalter „‹ i/n ›“."""
    chat_messages = list(services.visible_messages(conv))
    for msg in chat_messages:
        ids = list(getattr(msg, "sibling_ids", None) or [msg.pk])
        index = ids.index(msg.pk) if msg.pk in ids else 0
        msg.sibling_count = len(ids)
        msg.sibling_position = index + 1
        msg.prev_sibling_id = ids[index - 1] if index > 0 else None
        msg.next_sibling_id = ids[index + 1] if index + 1 < len(ids) else None
    return chat_messages


@login_required
def conversation(request, pk):
    """Chatansicht mit serverseitig gerendertem Verlauf. Ohne READ-Recht 404."""
    conv = _readable_conversation(request, pk)
    chat_messages = _path_with_versions(conv)
    context = _page_context(request, conv)
    context.update(
        {
            "chat_messages": chat_messages,
            "can_write": can(request.user, Action.WRITE, conv),
            "is_owner": conv.user_id == request.user.pk,
            # Einsicht (M6-05): Verwalter liest den Chat eines Jugendlichen.
            "supervised_owner": supervised_conversation_owner(request.user, conv),
        }
    )
    # Geteilte Chats (RWUD): Rechte, Hinweis „Geteilt von …“, Verfasser.
    context.update(sharing.page_context(request.user, conv))
    return render(request, "chat/conversation.html", context)


@require_GET
@login_required
def conversation_messages(request, pk):
    """HTML-Fragment des angezeigten Verlaufs (dieselben Templates wie die Seite);
    chat.js ersetzt damit den Verlauf nach Umschalten, Bearbeiten und Streams."""
    conv = _readable_conversation(request, pk)
    context = {
        "chat_messages": _path_with_versions(conv),
        "can_write": can(request.user, Action.WRITE, conv),
    }
    context.update(sharing.page_context(request.user, conv))
    response = render(request, "chat/_messages.html", context)
    response["Cache-Control"] = "private, no-store"
    return response


# --- Export (M5-04) -------------------------------------------------------------


def export_filename(conversation: Conversation) -> str:
    """Sicherer ASCII-Dateiname: ``chat-<id>-<titel>.md``."""
    text = unicodedata.normalize("NFKD", conversation.title or "")
    text = text.replace("ß", "ss").encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()[:50].strip("-")
    return f"chat-{conversation.pk}-{slug}.md" if slug else f"chat-{conversation.pk}.md"


def _status_note(msg: Message) -> str:
    if msg.status == Message.Status.ABORTED:
        return "*Abgebrochen" + (f": {msg.error}" if msg.error else "") + "*"
    if msg.status == Message.Status.ERROR:
        return "*Fehler" + (f": {msg.error}" if msg.error else "") + "*"
    return ""


def render_export(conversation: Conversation, viewer=None) -> str:
    """Sichtbarer Verlauf als Markdown. Ohne festen Rollen-Prompt (nur der
    eigene System-Prompt des Chats), nur der angezeigte Zweig (Versionen).
    ``viewer``: In geteilten Chats stehen Namen statt „Du“ an fremden Nachrichten."""
    show_authors = viewer is not None and (
        sharing.is_shared(conversation) or sharing.has_other_authors(conversation)
    )
    owner_name = sharing.display_name(conversation.user) if show_authors else ""
    title = " ".join((conversation.title or "Neuer Chat").split())
    now = timezone.localtime()
    lines = [f"# {title}", "", f"Exportiert aus MultiGPT am {now:%d.%m.%Y um %H:%M} Uhr.", ""]
    # Projekt nur für den Besitzer (Empfänger sehen fremde Projekte nicht).
    project = conversation.project if conversation.project_id else None
    if project is not None and (viewer is None or viewer.pk == conversation.user_id):
        lines[-1:-1] = [f"Projekt: {' '.join(project.name.split())}", ""]
    if conversation.system_prompt.strip():
        lines += ["## System-Prompt", ""]
        lines += [f"> {line}".rstrip() for line in conversation.system_prompt.strip().splitlines()]
        lines.append("")
    for msg in services.visible_messages(conversation):
        if msg.role == Message.Role.USER:
            author = sharing.author_label(
                msg, viewer, conversation.user_id, owner_name, show_authors
            )
        else:
            author = msg.model.display_name if msg.model else "Assistent"
        stamp = timezone.localtime(msg.created)
        lines += ["---", "", f"## {author}", "", f"*{stamp:%d.%m.%Y %H:%M}*", ""]
        if msg.content.strip():
            lines += [msg.content.rstrip(), ""]
        note = _status_note(msg)
        if note:
            lines += [note, ""]
    return "\n".join(lines).rstrip() + "\n"


@require_GET
@login_required
def export_markdown(request, pk):
    """Chat als Markdown-Download. Lesen genügt (auch geteilte Chats)."""
    conv = _readable_conversation(request, pk)
    response = HttpResponse(
        render_export(conv, request.user), content_type="text/markdown; charset=utf-8"
    )
    response["Content-Disposition"] = f'attachment; filename="{export_filename(conv)}"'
    response["Cache-Control"] = "private, no-store"
    return response
