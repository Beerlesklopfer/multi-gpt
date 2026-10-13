"""Admin „Runner“ (M14-12, Teil core): nur Metadaten.

Datenschutz: Verwalter sehen keine Hashes oder Tokens, keine Inhalte von
Arbeitsständen (das Bundle ist hier weder sichtbar noch herunterladbar) und
keine Argumente von Werkzeugaufrufen. Tokens lassen sich nur widerrufen;
Tokens, Runner, Arbeitsstände und Sitzungen legt nicht der Admin an.
Hosts für ssh/telnet pflegt nur der Verwalter, der Zugang (Schlüssel bzw.
Passwort) wird verschlüsselt gespeichert und nie wieder angezeigt.
Neue Dienste aus gültig signierten Dienstkatalogen (``RunnerService``) werden
hier eingestuft und freigegeben – vorher bietet der Server sie nicht an.
"""

from __future__ import annotations

from django import forms
from django.contrib import admin, messages
from django.utils import timezone
from django.utils.html import format_html

from multigpt.chat.admin import SecretFieldFormMixin
from multigpt.core.fields import mask_secret

from . import tokens
from .models import (
    Runner,
    RunnerAudit,
    RunnerHost,
    RunnerService,
    RunnerSession,
    RunnerToken,
    RunnerWorkspace,
)


class _ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(RunnerToken)
class RunnerTokenAdmin(_ReadOnlyAdmin):
    list_display = (
        "name",
        "display_prefix",
        "owner",
        "state_label",
        "expires_at",
        "last_used_at",
        "last_used_ip",
        "created",
    )
    list_filter = ("owner",)
    search_fields = ("name", "prefix", "owner__username")
    list_select_related = ("owner",)
    fields = (
        "name",
        "display_prefix",
        "owner",
        "state_label",
        "expires_at",
        "revoked_at",
        "last_used_at",
        "last_used_ip",
        "created",
    )
    readonly_fields = fields
    actions = ["revoke_tokens"]

    @admin.display(description="Token")
    def display_prefix(self, obj):
        return obj.display_prefix

    @admin.display(description="Status")
    def state_label(self, obj):
        return obj.state_label

    def has_delete_permission(self, request, obj=None):
        return False  # erst widerrufen; löschen nur das Mitglied selbst

    @admin.action(description="Ausgewählte Tokens widerrufen")
    def revoke_tokens(self, request, queryset):
        count = sum(tokens.revoke(token) for token in queryset)
        self.message_user(request, f"{count} Token(s) widerrufen.", messages.SUCCESS)


@admin.register(Runner)
class RunnerAdmin(_ReadOnlyAdmin):
    list_display = (
        "name",
        "owner",
        "status",
        "engine",
        "engine_version",
        "rootless",
        "agent_version",
        "catalog_state",
        "last_seen",
    )
    list_filter = ("status", "engine", "rootless", "catalog_state")
    list_select_related = ("token__owner",)
    readonly_fields = (
        "name",
        "owner",
        "token",
        "engine",
        "engine_version",
        "agent_version",
        "protocol_version",
        "rootless",
        "status",
        "connected_at",
        "last_seen",
        "created",
        "catalog_state",
        "catalog_release",
        "services",
    )
    fields = readonly_fields

    @admin.display(description="Konto")
    def owner(self, obj):
        return obj.token.owner


@admin.register(RunnerWorkspace)
class RunnerWorkspaceAdmin(_ReadOnlyAdmin):
    """Nur Name, Besitzer, Größe und Zeiten – kein Inhalt, kein Herunterladen."""

    list_display = ("name", "owner", "size_label", "updated", "locked")
    list_filter = ("owner",)
    search_fields = ("name", "owner__username")
    list_select_related = ("owner",)
    fields = ("name", "owner", "size_label", "created", "updated", "active_session", "locked_at")
    readonly_fields = fields

    @admin.display(description="Größe")
    def size_label(self, obj):
        from django.template.defaultfilters import filesizeformat

        return filesizeformat(obj.size)

    @admin.display(description="in Benutzung", boolean=True)
    def locked(self, obj):
        return obj.locked

    def has_delete_permission(self, request, obj=None):
        return False  # löschen nur der Besitzer (Seite „Arbeitsstände“)


@admin.register(RunnerSession)
class RunnerSessionAdmin(_ReadOnlyAdmin):
    list_display = (
        "short_id",
        "owner",
        "status",
        "runner",
        "network",
        "confirm_mode",
        "started",
        "ended",
        "minutes",
    )
    list_filter = ("status", "network", "confirm_mode")
    list_select_related = ("owner", "runner")
    fields = (
        "uuid",
        "owner",
        "workspace",
        "runner",
        "api_key",
        "status",
        "confirm_mode",
        "network",
        "limits_requested",
        "limits_actual",
        "started",
        "ended",
        "minutes",
        "stop_reason",
    )
    readonly_fields = fields

    @admin.display(description="Sitzung")
    def short_id(self, obj):
        return str(obj.uuid)[:8]

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(RunnerAudit)
class RunnerAuditAdmin(_ReadOnlyAdmin):
    """Nur Metadaten: keine Argumente, bei Werkzeugaufrufen auch keine Meldung (die
    sieht das Konto selbst auf der Seite „Runner“)."""

    list_display = ("created", "kind", "owner", "tool", "exit_code", "duration_ms", "ip")
    list_filter = ("kind",)
    list_select_related = ("owner",)
    date_hierarchy = "created"
    fields = (
        "created",
        "kind",
        "owner",
        "session",
        "runner",
        "tool",
        "exit_code",
        "duration_ms",
        "event_message",
        "ip",
    )
    readonly_fields = fields

    @admin.display(description="Meldung")
    def event_message(self, obj):
        if obj.kind in (RunnerAudit.Kind.TOOL, RunnerAudit.Kind.REMOTE):
            return "(nur für das Konto selbst)"
        return obj.message or "–"

    def has_delete_permission(self, request, obj=None):
        return False  # gelöscht wird nach API_AUDIT_DAYS automatisch


class RunnerHostForm(SecretFieldFormMixin, forms.ModelForm):
    secret_field = "secret"
    secret = forms.CharField(
        label="Schlüssel bzw. Passwort",
        required=False,
        strip=False,
        widget=forms.Textarea(attrs={"rows": 4, "autocomplete": "off", "spellcheck": "false"}),
        help_text="Privater SSH-Schlüssel (PEM/OpenSSH) oder Passwort. Wird verschlüsselt "
        "gespeichert und nie wieder angezeigt; beim Bearbeiten leer lassen, um ihn zu behalten.",
    )
    clear_secret = forms.BooleanField(label="Zugang entfernen", required=False)
    confirm_host_key = forms.BooleanField(
        label="Host-Schlüssel bestätigen",
        required=False,
        help_text="Nur ankreuzen, wenn der Schlüssel oben mit dem des Geräts übereinstimmt "
        "(z. B. ssh-keygen -l auf dem Gerät). Ohne Bestätigung ist ein SSH-Host nicht nutzbar.",
    )

    class Meta:
        model = RunnerHost
        fields = [
            "name",
            "description",
            "protocol",
            "address",
            "port",
            "username",
            "auth_method",
            "secret",
            "host_key",
            "allowed_roles",
            "allowed_users",
            "active",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial["secret"] = ""  # Geheimnis nie ins Formular zurückgeben

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("auth_method") == RunnerHost.AuthMethod.NONE:
            cleaned["secret"] = ""
        elif not cleaned.get("secret"):
            self.add_error(
                "secret", "Für diese Zugangsart wird ein Schlüssel bzw. Passwort benötigt."
            )
        if (
            cleaned.get("protocol") == RunnerHost.Protocol.TELNET
            and cleaned.get("auth_method") == RunnerHost.AuthMethod.KEY
        ):
            self.add_error("auth_method", "Telnet kennt keine SSH-Schlüssel.")
        return cleaned


@admin.register(RunnerHost)
class RunnerHostAdmin(admin.ModelAdmin):
    form = RunnerHostForm
    list_display = ("name", "protocol", "address", "port", "username", "host_key_state", "active")
    list_filter = ("protocol", "active")
    search_fields = ("name", "address", "description")
    filter_horizontal = ("allowed_roles", "allowed_users")
    readonly_fields = ("secret_hint", "host_key_confirmed_at", "created")
    fieldsets = (
        (None, {"fields": ("name", "description", "active")}),
        (
            "Verbindung",
            {
                "fields": ("protocol", "address", "port", "username"),
                "description": "Telnet überträgt alles im Klartext, auch das Passwort – nur für "
                "alte Geräte im Heimnetz.",
            },
        ),
        (
            "Zugang",
            {"fields": ("auth_method", "secret_hint", "secret", "clear_secret")},
        ),
        (
            "SSH-Host-Schlüssel",
            {"fields": ("host_key", "confirm_host_key", "host_key_confirmed_at")},
        ),
        (
            "Freigabe",
            {
                "fields": ("allowed_roles", "allowed_users"),
                "description": "Zusätzlich braucht das Konto die Rollenrechte „Runner nutzen“ "
                "und „Fernzugriff (ssh/telnet)“. Über API-Keys gibt es ssh/telnet nie.",
            },
        ),
    )

    @admin.display(description="gespeicherter Zugang")
    def secret_hint(self, obj):
        return mask_secret(obj.secret) or "–"

    @admin.display(description="Host-Schlüssel")
    def host_key_state(self, obj):
        if obj.protocol == RunnerHost.Protocol.TELNET:
            return "– (Telnet)"
        return "offen" if obj.host_key_pending else "bestätigt"

    def save_model(self, request, obj, form, change):
        if "host_key" in form.changed_data:
            obj.host_key_confirmed_at = None  # geänderter Schlüssel: neu bestätigen
        if form.cleaned_data.get("confirm_host_key") and obj.host_key:
            obj.host_key_confirmed_at = timezone.now()
        super().save_model(request, obj, form, change)


class RunnerServiceForm(forms.ModelForm):
    class Meta:
        model = RunnerService
        fields = ("status", "kind")

    def clean(self):
        data = super().clean()
        if data.get("status") == RunnerService.Status.APPROVED and not data.get("kind"):
            self.add_error("kind", "Zum Freigeben bitte einstufen.")
        return data


@admin.register(RunnerService)
class RunnerServiceAdmin(admin.ModelAdmin):
    """Neue Dienste wie neue Werkzeuge eines importierten MCP-Servers: erst nach
    Einstufung durch einen Verwalter angeboten. Beschreibung und Parameter stammen
    aus dem signierten Katalog und sind hier nicht änderbar."""

    form = RunnerServiceForm
    list_display = ("name", "status", "kind", "proposed_kind", "release", "last_seen")
    list_filter = ("status", "kind")
    search_fields = ("name", "description")
    readonly_fields = (
        "name",
        "proposed_kind",
        "description_block",
        "schema_block",
        "release",
        "first_seen",
        "last_seen",
        "reviewed_by",
        "reviewed_at",
    )
    fields = ("name", "status", "kind", *readonly_fields[1:])
    actions = ["block_services"]

    def has_add_permission(self, request):
        return False  # entstehen nur aus Katalogen der Runner

    @admin.display(description="Beschreibung (aus dem Katalog)")
    def description_block(self, obj):
        return format_html('<pre style="white-space: pre-wrap">{}</pre>', obj.description)

    @admin.display(description="Parameter (aus dem Katalog)")
    def schema_block(self, obj):
        import json

        text = json.dumps(obj.input_schema, ensure_ascii=False, indent=2)
        return format_html('<pre style="white-space: pre-wrap">{}</pre>', text)

    def save_model(self, request, obj, form, change):
        if {"status", "kind"} & set(form.changed_data):
            obj.reviewed_by = request.user
            obj.reviewed_at = timezone.now()
        super().save_model(request, obj, form, change)

    @admin.action(description="Ausgewählte Dienste sperren")
    def block_services(self, request, queryset):
        count = queryset.update(
            status=RunnerService.Status.BLOCKED,
            reviewed_by=request.user,
            reviewed_at=timezone.now(),
        )
        messages.success(request, f"{count} Dienst(e) gesperrt. Gilt ab der nächsten Verbindung.")
