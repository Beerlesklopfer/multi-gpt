"""Admin „Knoten“ (M15): API-Keys und API-Aufrufe nur als Metadaten, dazu die
Übersicht „Integrationen“ (n8n als MCP-Server eingerichtet und online? API-Keys
in Benutzung?).

Datenschutz: Verwalter sehen nie Hashes oder Keys, keine Argumente oder
Ergebnisse. Keys lassen sich hier nur widerrufen (Aktion), nicht anlegen oder
ändern – das macht das Mitglied selbst bzw. ``mgpt-ctl apikey``.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.template.response import TemplateResponse
from django.urls import path
from django.utils import timezone

from multigpt.chat.models import McpServer

from . import keys
from .models import ApiCall, ApiKey, IntegrationOverview


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "display_prefix",
        "owner",
        "scope_list",
        "state_label",
        "expires_at",
        "last_used_at",
        "last_used_ip",
        "created",
    )
    list_filter = ("active", "owner")
    search_fields = ("name", "prefix", "owner__username")
    list_select_related = ("owner",)
    fields = (
        "name",
        "display_prefix",
        "owner",
        "scope_list",
        "collections",
        "sources",
        "state_label",
        "expires_at",
        "revoked_at",
        "last_used_at",
        "last_used_ip",
        "created",
    )
    readonly_fields = fields
    actions = ["revoke_keys"]

    @admin.display(description="Key")
    def display_prefix(self, obj):
        return obj.display_prefix

    @admin.display(description="Rechte")
    def scope_list(self, obj):
        return ", ".join(obj.scopes or []) or "–"

    @admin.display(description="Status")
    def state_label(self, obj):
        return obj.state_label

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False  # nur ansehen; widerrufen über die Aktion

    @admin.action(description="Ausgewählte Keys widerrufen")
    def revoke_keys(self, request, queryset):
        count = sum(keys.revoke(key) for key in queryset)
        self.message_user(request, f"{count} Key(s) widerrufen.")


@admin.register(ApiCall)
class ApiCallAdmin(admin.ModelAdmin):
    list_display = (
        "created",
        "key",
        "method",
        "tool",
        "status",
        "http_status",
        "duration_ms",
        "request_bytes",
        "response_bytes",
        "ip",
    )
    list_filter = ("status", "method")
    search_fields = ("tool", "key__name", "key__prefix", "ip")
    list_select_related = ("key",)
    date_hierarchy = "created"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


def n8n_servers():
    """MCP-Server, die nach n8n aussehen (Name oder Adresse)."""
    return McpServer.objects.filter(
        Q(name__icontains="n8n") | Q(url__icontains="n8n") | Q(url__icontains="/mcp-server/")
    ).order_by("name", "pk")


@admin.register(IntegrationOverview)
class IntegrationOverviewAdmin(admin.ModelAdmin):
    """Seite „Integrationen“: n8n in beiden Richtungen auf einen Blick."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_urls(self):
        view = self.admin_site.admin_view
        return [path("", view(self.overview_view), name="node_integrationoverview_changelist")]

    def overview_view(self, request):
        if not self.has_view_permission(request):
            raise PermissionDenied
        now = timezone.now()
        servers = list(n8n_servers())
        active_keys = ApiKey.objects.filter(active=True).filter(
            Q(expires_at__isnull=True) | Q(expires_at__gt=now)
        )
        calls = ApiCall.objects.filter(created__gte=now - timedelta(days=1)).aggregate(
            total=Count("pk"),
            ok=Count("pk", filter=Q(status=ApiCall.Status.OK)),
            unauthorized=Count("pk", filter=Q(status=ApiCall.Status.UNAUTHORIZED)),
            limited=Count("pk", filter=Q(status=ApiCall.Status.RATE_LIMITED)),
        )
        context = {
            **self.admin_site.each_context(request),
            "opts": self.opts,
            "title": "Integrationen",
            "subtitle": None,
            "servers": servers,
            "n8n_online": any(s.active and s.online for s in servers),
            "keys_active": active_keys.count(),
            "keys_used_week": active_keys.filter(last_used_at__gte=now - timedelta(days=7))
            .order_by()
            .count(),
            "key_owners": active_keys.values("owner").distinct().count(),
            "calls": calls,
            "mcp_url": request.build_absolute_uri("/mcp/"),
        }
        return TemplateResponse(request, "admin/node/integrationoverview/overview.html", context)
