from django.contrib import admin
from django.urls import include, path

from multigpt.accounts import views_avatar, views_settings, views_usage
from multigpt.node import sse as node_sse
from multigpt.node import views as node_views
from multigpt.node import views_keys

urlpatterns = [
    # Profilbild (persönliches Menü): ausliefern bzw. ändern (views_avatar).
    path("konto/profilbild/<int:pk>/", views_avatar.avatar_image, name="avatar_image"),
    path("einstellungen/profilbild/", views_avatar.avatar_update, name="avatar_update"),
    path("konto/", include("django.contrib.auth.urls")),
    path("admin/", admin.site.urls),
    path("familie/", include("multigpt.accounts.urls_family")),
    # Eigener Verbrauch (M6, budget); vor dem chat-include, der api/ belegt.
    path("verbrauch/", views_usage.usage_page, name="usage"),
    path("api/usage/", views_usage.usage_api, name="api_usage"),
    # Persönliche Einstellungen (Zitierstil u. a.).
    path("einstellungen/", views_settings.settings_page, name="settings"),
    # Knoten (M15): API-Keys je Konto und MCP-Server (Streamable HTTP, Bearer-Key).
    path("einstellungen/api-keys/", views_keys.api_keys_page, name="api_keys"),
    path("einstellungen/api-keys/erzeugen/", views_keys.api_key_quick, name="api_key_quick"),
    path(
        "einstellungen/api-keys/<int:pk>/projekt/",
        views_keys.api_key_project,
        name="api_key_project",
    ),
    path(
        "einstellungen/api-keys/<int:pk>/widerrufen/",
        views_keys.api_key_revoke,
        name="api_key_revoke",
    ),
    path(
        "einstellungen/api-keys/<int:pk>/loeschen/",
        views_keys.api_key_delete,
        name="api_key_delete",
    ),
    # Runner (M14): Kopplung und Arbeitsstände; /runner/ws bedient der Runner-Dienst.
    path("runner/", include("multigpt.runner.urls")),
    # Mit und ohne Schrägstrich: Clients posten an die eingetragene Adresse, eine
    # Weiterleitung (APPEND_SLASH) würde den POST verlieren.
    path("mcp/", node_views.mcp_endpoint, name="mcp"),
    path("mcp", node_views.mcp_endpoint),
    # Legacy-Transport HTTP+SSE (MCP 2024-11-05) für ältere Clients (node/sse.py).
    path("mcp/sse", node_sse.sse_endpoint, name="mcp_sse"),
    path("mcp/sse/", node_sse.sse_endpoint),
    path("mcp/messages", node_sse.messages_endpoint, name="mcp_messages"),
    path("mcp/messages/", node_sse.messages_endpoint),
    path("", include("multigpt.chat.urls")),
]
