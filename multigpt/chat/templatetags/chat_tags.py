"""Template-Tags der Oberfläche: Seitenleiste mit Chatliste (M5-02, auf allen
Seiten mit App-Layout) und Statusanzeige der Anbieter (M4-04).

Als Tags statt Context-Processor, damit die Abfragen nur laufen, wenn eine
Seite die Seitenleiste wirklich zeigt (nicht bei Login, JSON, Admin).
"""

from django import template

from ..models import Conversation, Provider

register = template.Library()

# Wie viele Chats die Seitenleiste zeigt. Ältere findet die Suche.
SIDEBAR_LIMIT = 100
SEARCH_MAX_LENGTH = 100


def sidebar_conversations(user, query: str = "", archived: bool = False):
    """Eigene Chats (aktiv oder Archiv), optional Titelsuche, neueste zuerst."""
    qs = Conversation.objects.filter(user=user, archived=archived)
    if query:
        qs = qs.filter(title__icontains=query)
    return qs.order_by("-updated", "-pk")[:SIDEBAR_LIMIT]


@register.inclusion_tag("chat/_sidebar.html", takes_context=True)
def chat_sidebar(context):
    request = context["request"]
    query = request.GET.get("q", "").strip()[:SEARCH_MAX_LENGTH]
    show_archived = request.GET.get("archived") == "1"
    return {
        "request": request,
        "q": query,
        "show_archived": show_archived,
        "sidebar_conversations": sidebar_conversations(request.user, query, show_archived),
        "active_conversation": context.get("active_conversation"),
    }


@register.inclusion_tag("chat/_provider_status.html", takes_context=True)
def provider_status_indicator(context):
    """Platz für die Statuspunkte; nur, wenn es Anbieter mit Statusprüfung gibt."""
    providers = list(
        Provider.objects.filter(active=True, check_status=True)
        .order_by("name")
        .values("id", "name", "online", "last_online")
    )
    return {"status_providers": providers}
