"""Seiten der Chatoberfläche (M3-05, M5). JSON/SSE-Endpunkte liegen in api.py
und api_manage.py. Die Seitenleiste kommt aus dem Tag ``chat_sidebar``."""

import re
import unicodedata

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_GET

from multigpt.accounts.permissions import Action, can

from . import services
from .models import Conversation, Message


def _has_chat_model(user) -> bool:
    """Darf der Nutzer chatten und mindestens ein Chat-Modell verwenden?"""
    return can(user, Action.CHAT) and bool(services.available_chat_models(user))


def _page_context(request, conversation=None):
    return {
        "active_conversation": conversation,
        "has_chat_model": _has_chat_model(request.user),
    }


def _readable_conversation(request, pk) -> Conversation:
    """Chat laden; ohne READ-Recht 404 (Existenz fremder Chats bleibt verborgen)."""
    conv = get_object_or_404(Conversation.objects.select_related("default_model"), pk=pk)
    if not can(request.user, Action.READ, conv):
        raise Http404
    return conv


@login_required
def index(request):
    """Startseite: leerer Chat. Der Chat wird erst beim ersten Senden angelegt."""
    return render(request, "chat/index.html", _page_context(request))


@login_required
def conversation(request, pk):
    """Chatansicht mit serverseitig gerendertem Verlauf. Ohne READ-Recht 404."""
    conv = _readable_conversation(request, pk)
    chat_messages = services.visible_messages(conv)
    context = _page_context(request, conv)
    context.update(
        {
            "chat_messages": chat_messages,
            "can_write": can(request.user, Action.WRITE, conv),
            "is_owner": conv.user_id == request.user.pk,
        }
    )
    return render(request, "chat/conversation.html", context)


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


def render_export(conversation: Conversation) -> str:
    """Sichtbarer Verlauf als Markdown. Ohne festen Rollen-Prompt (nur der
    eigene System-Prompt des Chats), ohne ersetzte Antworten."""
    title = " ".join((conversation.title or "Neuer Chat").split())
    now = timezone.localtime()
    lines = [f"# {title}", "", f"Exportiert aus MultiGPT am {now:%d.%m.%Y um %H:%M} Uhr.", ""]
    if conversation.system_prompt.strip():
        lines += ["## System-Prompt", ""]
        lines += [f"> {line}".rstrip() for line in conversation.system_prompt.strip().splitlines()]
        lines.append("")
    for msg in services.visible_messages(conversation):
        if msg.role == Message.Role.USER:
            author = "Du"
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
    response = HttpResponse(render_export(conv), content_type="text/markdown; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{export_filename(conv)}"'
    response["Cache-Control"] = "private, no-store"
    return response
