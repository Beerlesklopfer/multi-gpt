"""Template-Tags der Oberfläche: Seitenleiste mit Chatliste (M5-02, auf allen
Seiten mit App-Layout) und Statusanzeige der Anbieter (M4-04).

Als Tags statt Context-Processor, damit die Abfragen nur laufen, wenn eine
Seite die Seitenleiste wirklich zeigt (nicht bei Login, JSON, Admin).
"""

from django import template

from .. import projects, sharing
from ..models import Conversation, McpServer, Provider
from ..providers.base import short_error

register = template.Library()

# Wie viele Chats die Seitenleiste zeigt. Ältere findet die Suche.
SIDEBAR_LIMIT = 100
SEARCH_MAX_LENGTH = 100


def sidebar_conversations(user, query: str = "", archived: bool = False):
    """Eigene Chats ohne Projekt (aktiv oder Archiv), optional Titelsuche,
    neueste zuerst; Chats in Projekten zeigt der Abschnitt „Projekte“.
    ``is_shared``: Chat ist mit anderen geteilt (Symbol in der Liste)."""
    qs = sharing.with_shared_flag(
        Conversation.objects.filter(user=user, archived=archived, project__isnull=True)
    )
    if query:
        qs = qs.filter(title__icontains=query)
    return qs.order_by("-updated", "-pk")[:SIDEBAR_LIMIT]


def _active_project_id(context) -> int | None:
    """Projekt der angezeigten Seite: Projektseite oder eigener Chat im Projekt."""
    project = context.get("active_project")
    if project is not None:
        return project.pk
    conversation = context.get("active_conversation")
    user = context["request"].user
    if conversation is not None and conversation.user_id == user.pk:
        return conversation.project_id
    return None


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
        # Projekte mit ihren Chats (chat/projects.py), gleiche Suche/Archiv.
        "sidebar_projects": projects.sidebar_projects(
            request.user, query, show_archived, _active_project_id(context)
        ),
        "active_project": context.get("active_project"),
        # „Mit mir geteilt“ (sharing.py): Chats anderer Konten, gleiche Suche/Archiv.
        "shared_conversations": sharing.shared_with(request.user, query, show_archived),
        "active_conversation": context.get("active_conversation"),
    }


@register.inclusion_tag("chat/_provider_status.html", takes_context=True)
def provider_status_indicator(context):
    """Platz für die Statuspunkte; nur, wenn es Anbieter mit Statusprüfung gibt."""
    providers = list(
        Provider.objects.filter(active=True, check_status=True)
        .order_by("name")
        .values("id", "name", "online", "last_online", "last_error")
    )
    for p in providers:
        p["error_short"] = short_error(p["last_error"])
    return {"status_providers": providers, "status_mcp": _offline_mcp(context)}


def _offline_mcp(context) -> list[dict]:
    """Nur für Verwalter: aktive MCP-Server, die zuletzt offline geprüft wurden.

    Gespeicherter Stand (chat/mcp/status.py), kein Netzaufruf; online-Server
    erscheinen nicht, damit die Leiste ruhig bleibt.
    """
    request = context.get("request")
    user = getattr(request, "user", None)
    if not (user and user.is_staff and user.has_perm("chat.view_mcpserver")):
        return []
    servers = McpServer.objects.filter(
        active=True, online=False, last_checked__isnull=False
    ).exclude(last_error="")
    return [
        {
            "id": s.pk,
            "name": s.name,
            "error": s.last_error,
            "error_short": short_error(s.last_error),
        }
        for s in servers.order_by("name")[:5]
    ]
