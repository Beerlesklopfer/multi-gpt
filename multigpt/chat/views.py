"""Seiten der Chatoberfläche (M3-05). JSON/SSE-Endpunkte liegen in api.py."""

from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, render

from multigpt.accounts.permissions import Action, can

from . import services
from .models import Conversation

# Wie viele Chats die Seitenleiste zeigt. Suche und Archiv folgen in M5.
SIDEBAR_LIMIT = 100


def _sidebar_conversations(user):
    """Eigene, nicht archivierte Chats, neueste zuerst."""
    return Conversation.objects.filter(user=user, archived=False).order_by("-updated", "-pk")[
        :SIDEBAR_LIMIT
    ]


def _has_chat_model(user) -> bool:
    """Darf der Nutzer chatten und mindestens ein Chat-Modell verwenden?"""
    return can(user, Action.CHAT) and bool(services.available_chat_models(user))


def _page_context(request, conversation=None):
    return {
        "sidebar_conversations": _sidebar_conversations(request.user),
        "active_conversation": conversation,
        "has_chat_model": _has_chat_model(request.user),
    }


@login_required
def index(request):
    """Startseite: leerer Chat. Der Chat wird erst beim ersten Senden angelegt."""
    return render(request, "chat/index.html", _page_context(request))


@login_required
def conversation(request, pk):
    """Chatansicht mit serverseitig gerendertem Verlauf. Ohne READ-Recht 404."""
    conv = get_object_or_404(Conversation.objects.select_related("default_model"), pk=pk)
    if not can(request.user, Action.READ, conv):
        raise Http404
    chat_messages = services.visible_messages(conv)
    context = _page_context(request, conv)
    context.update(
        {
            "chat_messages": chat_messages,
            "can_write": can(request.user, Action.WRITE, conv),
        }
    )
    return render(request, "chat/conversation.html", context)
