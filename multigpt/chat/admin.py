"""Admin-Masken der App chat.

Geheimnisse (API-Keys, MCP-Zugangsdaten) erscheinen nie im Klartext: Das
Eingabefeld ist ein PasswordInput ohne Vorbelegung, angezeigt werden nur die
letzten 4 Zeichen. Ein leeres Feld beim Bearbeiten lässt den Wert unverändert.

Privatsphäre (Plan 8f): Auch Verwalter sehen fremde Chats nicht. Chats,
Nachrichten, Anhänge, Werkzeugaufrufe, Quellen, Vorlagen und Freigaben sind
deshalb nur als Metadaten sichtbar (ohne Inhalte, Chattitel und Dateien) und
im Admin weder änderbar noch löschbar. Namen von Vorlagen erscheinen als
Objektbezeichnung (``__str__``).

Sammlungen, Dokumente, Indexierungsaufträge und RAG-Einstellungen liegen im
eigenen Abschnitt „Dokumente (RAG)“ (``multigpt/rag/admin.py``).
"""

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.utils import unquote
from django.core.exceptions import PermissionDenied
from django.db import models, transaction
from django.http import Http404, HttpResponseNotAllowed, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe
from django.utils.text import Truncator

from multigpt.core.fields import mask_secret

from . import mcp as mcp_client
from . import status, websearch
from .management.commands.sync_models import guess_capability
from .mcp.config import parse_credentials, split_command
from .models import (
    AIModel,
    Attachment,
    Conversation,
    McpServer,
    Message,
    Preset,
    Provider,
    SearchSettings,
    Share,
    SourceRef,
    ToolCall,
)

# --- Geheimnisse -------------------------------------------------------------


class SecretFieldFormMixin:
    """ModelForm-Mixin für ein verschlüsseltes Feld (Name in ``secret_field``).

    Erwartet zusätzlich ein Feld ``clear_<secret_field>`` (Checkbox zum Löschen).
    """

    secret_field: str

    def clean(self):
        cleaned = super().clean()
        name = self.secret_field
        new_value = cleaned.get(name) or ""
        if cleaned.get(f"clear_{name}"):
            cleaned[name] = ""
        elif not new_value and self.instance.pk:
            # Leer gelassen: gespeicherten Wert behalten.
            cleaned[name] = getattr(self.instance, name)
        return cleaned


def _formfield(db_field, **kwargs):
    # URLs ohne Schema als https deuten (Djangos Vorgabe ab 6.0, vermeidet die Warnung).
    if isinstance(db_field, models.URLField):
        kwargs.setdefault("assume_scheme", "https")
    return db_field.formfield(**kwargs)


def _secret_widget():
    return forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"})


class ProviderForm(SecretFieldFormMixin, forms.ModelForm):
    secret_field = "api_key"
    api_key = forms.CharField(
        label="API-Key",
        required=False,
        strip=True,
        widget=_secret_widget(),
        help_text="Wird verschlüsselt gespeichert. Beim Bearbeiten leer lassen, "
        "um den gespeicherten Key zu behalten.",
    )
    clear_api_key = forms.BooleanField(label="API-Key entfernen", required=False)

    def clean_base_url(self):
        """Leer = Standard-URL des Anbieters, sichtbar gespeichert."""
        from .providers.registry import default_base_urls

        value = (self.cleaned_data.get("base_url") or "").strip()
        if not value:
            value = default_base_urls().get(self.data.get("kind", ""), "")
        return value

    class Meta:
        model = Provider
        formfield_callback = _formfield
        fields = ["name", "kind", "base_url", "api_key", "active", "is_local", "check_status"]


class McpServerForm(SecretFieldFormMixin, forms.ModelForm):
    secret_field = "credentials"
    credentials = forms.CharField(
        label="Zugangsdaten",
        required=False,
        strip=True,
        widget=_secret_widget(),
        help_text="Wird verschlüsselt gespeichert. Beim Bearbeiten leer lassen, "
        "um die gespeicherten Zugangsdaten zu behalten. Format: JSON-Objekt, "
        'bei stdio {"env": {"NAME": "Wert"}}, bei HTTP {"bearer_token": "…"} '
        'oder {"headers": {"Name": "Wert"}}; bei HTTP genügt auch das Token allein.',
    )
    clear_credentials = forms.BooleanField(label="Zugangsdaten entfernen", required=False)
    adopt_listed_tools = forms.BooleanField(
        label="Angebotene Werkzeuge als eingestuft übernehmen",
        required=False,
        help_text="Beim Speichern die aktuelle Werkzeugliste abrufen und alle Werkzeuge "
        "in „eingestufte Werkzeuge“ aufnehmen. Vorher die Werkzeuge mit Rückfrage eintragen.",
    )

    class Meta:
        model = McpServer
        formfield_callback = _formfield
        fields = [
            "name",
            "transport",
            "command",
            "url",
            "credentials",
            "active",
            "timeout_seconds",
            "tools_requiring_confirmation",
            "known_tools",
        ]

    def clean(self):
        cleaned = super().clean()
        transport = cleaned.get("transport")
        for key in ("tools_requiring_confirmation", "known_tools"):
            value = cleaned.get(key)
            if value is not None and (
                not isinstance(value, list) or not all(isinstance(v, str) for v in value)
            ):
                self.add_error(key, 'Bitte eine JSON-Liste von Werkzeugnamen, z. B. ["suche"].')
        if transport == McpServer.Transport.STDIO and cleaned.get("command"):
            try:
                split_command(cleaned["command"])
            except ValueError as exc:
                self.add_error("command", str(exc))
        if transport:
            try:
                parse_credentials(cleaned.get("credentials") or "", transport)
            except ValueError as exc:
                self.add_error("credentials", str(exc))
        return cleaned


# --- Anbieter, Modelle, MCP --------------------------------------------------

URL_OVERRIDES = {models.URLField: {"assume_scheme": "https"}}


class AIModelInline(admin.TabularInline):
    """Feinarbeit an einzelnen Modellen; übernommen wird über „Modelle auswählen“."""

    model = AIModel
    extra = 0
    fields = ["model_id", "display_name", "capability", "active", "sort_order"]
    show_change_link = True


# Zeitlimits der Prüfungen im Admin (Sekunden). Beim Speichern kurz, damit die
# Seite nicht hängt; auf Knopfdruck und für die Modellauswahl großzügiger
# (OpenRouter liefert Hunderte Modelle).
SAVE_CHECK_TIMEOUT = 3.0
MANUAL_CHECK_TIMEOUT = 10.0
SELECT_MODELS_TIMEOUT = 20.0


def _models(count: int) -> str:
    return f"{count} Modell" if count == 1 else f"{count} Modelle"


def check_message(provider: Provider, result) -> tuple[str, int]:
    """Admin-Meldung (Text, Stufe) zu einem ``CheckResult``."""
    if result.online:
        return (
            f"„{provider.name}“: Online – {_models(len(result.models))} gemeldet.",
            messages.SUCCESS,
        )
    return f"„{provider.name}“: Offline: {result.error}", messages.ERROR


@admin.register(Provider)
class ProviderAdmin(admin.ModelAdmin):
    form = ProviderForm
    formfield_overrides = URL_OVERRIDES
    list_display = [
        "name",
        "kind",
        "base_url",
        "api_key_hint",
        "active",
        "is_local",
        "online_state",
        "last_checked",
        "error_short",
        "select_models_link",
    ]
    list_filter = ["kind", "active", "is_local"]
    search_fields = ["name", "base_url"]
    readonly_fields = ["api_key_hint", "last_online", "online", "last_checked", "last_error"]
    actions = ["check_selected_action"]
    fieldsets = [
        (None, {"fields": ["name", "kind", "base_url", "active"]}),
        ("API-Key", {"fields": ["api_key_hint", "api_key", "clear_api_key"]}),
        ("Lokaler Anbieter", {"fields": ["is_local", "check_status"]}),
        (
            "Verbindung",
            {
                "fields": ["online", "last_checked", "last_online", "last_error"],
                "description": "Ergebnis der letzten Prüfung. Prüfen über „Verbindung jetzt "
                "prüfen“ oben rechts; beim Speichern wird automatisch kurz geprüft.",
            },
        ),
    ]
    inlines = [AIModelInline]

    @admin.display(description="gespeicherter Key")
    def api_key_hint(self, obj):
        return mask_secret(obj.api_key) or "–"

    @admin.display(description="Online", boolean=True, ordering="online")
    def online_state(self, obj):
        # Ungeprüft: unbekannt (Fragezeichen) statt „offline“.
        return obj.online if obj.last_checked else None

    @admin.display(description="Ursache")
    def error_short(self, obj):
        if obj.online or not obj.last_error:
            return "–"
        return format_html(
            '<span title="{}">{}</span>', obj.last_error, Truncator(obj.last_error).chars(60)
        )

    @admin.display(description="Modelle")
    def select_models_link(self, obj):
        url = reverse("admin:chat_provider_select_models", args=[obj.pk])
        return format_html('<a href="{}">Modelle auswählen</a>', url)

    # --- Prüfen ----------------------------------------------------------------

    @admin.action(description="Ausgewählte prüfen")
    def check_selected_action(self, request, queryset):
        for provider in queryset:
            result = status.force_check(provider, timeout=MANUAL_CHECK_TIMEOUT)
            self.message_user(request, *check_message(provider, result))

    def save_related(self, request, form, formsets, change):
        # Nach den Inlines prüfen: Die Prüfung legt bei lokalen Anbietern ggf.
        # Modelle an und darf neuen Inline-Zeilen nicht zuvorkommen.
        super().save_related(request, form, formsets, change)
        provider = form.instance
        if not provider.active:
            self.message_user(
                request, f"„{provider.name}“ ist deaktiviert – nicht geprüft.", messages.INFO
            )
            return

        # Erst nach dem Commit prüfen: Die Prüfung kann Sekunden dauern (DNS,
        # Timeout). Liefe sie in der Admin-Transaktion, bliebe der neue Anbieter so
        # lange unsichtbar, und ein zweites Absenden scheiterte mit IntegrityError
        # statt mit der Formularmeldung „existiert bereits“.
        def check_after_commit():
            result = status.force_check(provider, timeout=SAVE_CHECK_TIMEOUT)
            self.message_user(request, *check_message(provider, result))

        transaction.on_commit(check_after_commit)

    def render_change_form(self, request, context, *args, **kwargs):
        """Standard-URLs je Art und Vorschläge für das Feld „Basis-URL“ (JS)."""
        from .providers.registry import OPENAI_COMPAT_PRESETS, default_base_urls

        context["provider_url_defaults"] = {
            "defaults": default_base_urls(),
            "presets": [{"label": label, "url": url} for label, url in OPENAI_COMPAT_PRESETS],
        }
        return super().render_change_form(request, context, *args, **kwargs)

    def change_view(self, request, object_id, form_url="", extra_context=None):
        """Gemeldete Modelle für die Combobox am Feld „Modell-ID“ der Inline."""
        from .management.commands.sync_models import guess_capability

        extra_context = extra_context or {}
        provider = self.get_object(request, object_id)
        if provider is not None:
            existing = set(provider.ai_models.values_list("model_id", flat=True))
            extra_context["reported_model_choices"] = [
                {
                    "id": model_id,
                    "capability": guess_capability(model_id),
                    "exists": model_id in existing,
                }
                for model_id in sorted(provider.reported_models or [])
            ]
        return super().change_view(request, object_id, form_url, extra_context)

    def get_urls(self):
        view = self.admin_site.admin_view
        return [
            path(
                "<path:object_id>/check/",
                view(self.check_view),
                name="chat_provider_check",
            ),
            path(
                "<path:object_id>/models/",
                view(self.select_models_view),
                name="chat_provider_select_models",
            ),
            *super().get_urls(),
        ]

    def _provider_or_deny(self, request, object_id) -> Provider:
        provider = self.get_object(request, unquote(object_id))
        if provider is None:
            raise Http404("Anbieter nicht gefunden.")
        if not self.has_change_permission(request, provider):
            raise PermissionDenied
        return provider

    def _change_url(self, provider):
        return reverse("admin:chat_provider_change", args=[provider.pk])

    def check_view(self, request, object_id):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        provider = self._provider_or_deny(request, object_id)
        result = status.force_check(provider, timeout=MANUAL_CHECK_TIMEOUT)
        self.message_user(request, *check_message(provider, result))
        return HttpResponseRedirect(self._change_url(provider))

    # --- Modelle auswählen -----------------------------------------------------

    def select_models_view(self, request, object_id):
        provider = self._provider_or_deny(request, object_id)
        if request.method == "POST":
            return self._adopt_models(request, provider)
        result = status.force_check(provider, timeout=SELECT_MODELS_TIMEOUT)
        existing = {m.model_id: m for m in provider.ai_models.all()}
        query = (request.GET.get("q") or "").strip()
        rows = []
        if result.online:
            for model_id in sorted(result.models, key=str.lower):
                if query and query.lower() not in model_id.lower():
                    continue
                model = existing.get(model_id)
                rows.append(
                    {
                        "model_id": model_id,
                        "existing": model is not None,
                        "display_name": model.display_name if model else model_id,
                        "capability": model.capability if model else guess_capability(model_id),
                        "active": model.active if model else True,
                        "too_long": len(model_id) > MODEL_ID_MAX_LENGTH,
                    }
                )
        missing = sorted(set(existing) - set(result.models)) if result.online else []
        context = {
            **self.admin_site.each_context(request),
            "opts": self.opts,
            "title": f"Modelle auswählen: {provider.name}",
            "subtitle": None,
            "provider": provider,
            "result": result,
            "rows": rows,
            "query": query,
            "missing": missing,
            "capabilities": AIModel.Capability.choices,
            "change_url": self._change_url(provider),
            "has_view_permission": True,
            "original": provider,
        }
        return TemplateResponse(request, "admin/chat/provider/select_models.html", context)

    def _adopt_models(self, request, provider):
        # Nur Modelle, die der Anbieter bei der Prüfung (GET der Seite) gemeldet
        # hat oder die es schon gibt – keine beliebigen IDs aus dem Formular.
        provider.refresh_from_db(fields=["reported_models"])
        allowed = set(provider.reported_models or [])
        existing = {m.model_id: m for m in provider.ai_models.all()}
        allowed |= set(existing)
        valid_caps = set(AIModel.Capability.values)
        active_ids = set(request.POST.getlist("active"))
        created = updated = 0
        new_models = []
        for model_id in dict.fromkeys(request.POST.getlist("take")):
            if model_id not in allowed or not 0 < len(model_id) <= MODEL_ID_MAX_LENGTH:
                continue
            name = (request.POST.get(f"name:{model_id}") or "").strip()[:MODEL_ID_MAX_LENGTH]
            capability = request.POST.get(f"cap:{model_id}") or ""
            if capability not in valid_caps:
                capability = guess_capability(model_id)
            active = model_id in active_ids
            model = existing.get(model_id)
            if model is None:
                new_models.append(
                    AIModel(
                        provider=provider,
                        model_id=model_id,
                        display_name=name or model_id,
                        capability=capability,
                        active=active,
                    )
                )
                continue
            changes = {
                "display_name": name or model.display_name,
                "capability": capability,
                "active": active,
            }
            changed = [k for k, v in changes.items() if getattr(model, k) != v]
            if changed:
                for key in changed:
                    setattr(model, key, changes[key])
                model.save(update_fields=changed)
                updated += 1
        if new_models:
            # ignore_conflicts: Ein paralleler Abgleich (Statusprüfung) darf nicht stören.
            AIModel.objects.bulk_create(new_models, ignore_conflicts=True)
            created = len(new_models)
        self.message_user(
            request,
            f"{_models(created)} übernommen, {updated} aktualisiert.",
            messages.SUCCESS,
        )
        return HttpResponseRedirect(self._change_url(provider))


MODEL_ID_MAX_LENGTH = AIModel._meta.get_field("model_id").max_length


@admin.register(AIModel)
class AIModelAdmin(admin.ModelAdmin):
    list_display = [
        "display_name",
        "model_id",
        "provider",
        "capability",
        "supports_tools",
        "can_edit_images",
        "active",
        "sort_order",
    ]
    list_editable = ["active", "sort_order"]
    list_filter = ["provider", "capability", "active", "supports_tools"]
    search_fields = ["display_name", "model_id"]
    list_select_related = ["provider"]

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        """Gemeldete Modelle je Anbieter für die Combobox am Feld „Modell-ID“."""
        from .management.commands.sync_models import guess_capability

        by_provider = {}
        for provider in Provider.objects.prefetch_related("ai_models"):
            existing = {model.model_id for model in provider.ai_models.all()}
            by_provider[str(provider.pk)] = [
                {"id": mid, "capability": guess_capability(mid), "exists": mid in existing}
                for mid in sorted(provider.reported_models or [])
            ]
        extra_context = {
            **(extra_context or {}),
            "reported_model_choices": {"by_provider": by_provider},
        }
        return super().changeform_view(request, object_id, form_url, extra_context)


@admin.register(McpServer)
class McpServerAdmin(admin.ModelAdmin):
    form = McpServerForm
    formfield_overrides = URL_OVERRIDES
    list_display = [
        "name",
        "transport",
        "command",
        "url",
        "credentials_hint",
        "timeout_seconds",
        "active",
    ]
    list_filter = ["transport", "active"]
    search_fields = ["name"]
    readonly_fields = ["credentials_hint", "tool_overview"]
    actions = ["check_connection_action"]
    fieldsets = [
        (None, {"fields": ["name", "transport", "command", "url", "timeout_seconds", "active"]}),
        ("Zugangsdaten", {"fields": ["credentials_hint", "credentials", "clear_credentials"]}),
        (
            "Werkzeuge",
            {
                "fields": [
                    "tool_overview",
                    "tools_requiring_confirmation",
                    "known_tools",
                    "adopt_listed_tools",
                ]
            },
        ),
    ]

    @admin.display(description="gespeicherte Zugangsdaten")
    def credentials_hint(self, obj):
        return mask_secret(obj.credentials) or "–"

    @admin.display(description="Werkzeugliste")
    def tool_overview(self, obj):
        if obj is None or not obj.pk:
            return "Nach dem Speichern sichtbar."
        if not obj.active:
            return "Server ist deaktiviert."
        if not getattr(obj, "_show_tools", False):
            # Nicht bei jedem Aufruf der Seite verbinden, nur auf Wunsch.
            return mark_safe('<a href="?tools=1">Werkzeugliste abrufen</a>')
        try:
            tools = mcp_client.list_tools(obj, timeout=min(obj.timeout_seconds, ADMIN_TIMEOUT))
        except mcp_client.McpError as exc:
            return format_html('<span class="errornote">{}</span>', str(exc))
        if not tools:
            return "Der Server bietet keine Werkzeuge an."
        rows = format_html_join(
            "",
            "<tr><td><code>{}</code></td><td>{}</td><td>{}</td></tr>",
            ((t.name, t.description[:300], _tool_rating(obj, t.name)) for t in tools),
        )
        return format_html(
            "<table><thead><tr><th>Werkzeug</th><th>Beschreibung</th><th>Einstufung</th>"
            "</tr></thead><tbody>{}</tbody></table>",
            rows,
        )

    def get_object(self, request, object_id, from_field=None):
        obj = super().get_object(request, object_id, from_field)
        if obj is not None and request.GET.get("tools"):
            obj._show_tools = True
        return obj

    @admin.action(description="Verbindung testen")
    def check_connection_action(self, request, queryset):
        for server in queryset:
            try:
                tools = mcp_client.check_connection(
                    server, timeout=min(server.timeout_seconds, ADMIN_TIMEOUT)
                )
            except mcp_client.McpError as exc:
                self.message_user(request, str(exc), level=messages.ERROR)
                continue
            known = set(server.known_tools or []) | set(server.tools_requiring_confirmation or [])
            new = [t.name for t in tools if t.name not in known]
            text = f"„{server.name}“: Verbindung in Ordnung, {len(tools)} Werkzeuge."
            if new:
                text += " Nicht eingestuft (laufen nur mit Rückfrage): " + ", ".join(new)
                self.message_user(request, text, level=messages.WARNING)
            else:
                self.message_user(request, text, level=messages.SUCCESS)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if not form.cleaned_data.get("adopt_listed_tools"):
            return
        try:
            tools = mcp_client.check_connection(
                obj, timeout=min(obj.timeout_seconds, ADMIN_TIMEOUT)
            )
        except mcp_client.McpError as exc:
            self.message_user(request, f"Werkzeuge nicht übernommen: {exc}", level=messages.ERROR)
            return
        known = list(obj.known_tools or [])
        added = [t.name for t in tools if t.name not in known]
        obj.known_tools = known + added
        obj.save(update_fields=["known_tools"])
        self.message_user(
            request, f"{len(added)} Werkzeuge als eingestuft übernommen.", level=messages.SUCCESS
        )


ADMIN_TIMEOUT = 15


def _tool_rating(server, name: str) -> str:
    if name in (server.tools_requiring_confirmation or []):
        return "mit Rückfrage"
    if name in (server.known_tools or []):
        return "ohne Rückfrage"
    return "nicht eingestuft (Rückfrage)"


# --- Websuche (M8) ----------------------------------------------------------

SEARCH_CHECK_LEVELS = {
    websearch.CHECK_OK: messages.SUCCESS,
    websearch.CHECK_WARNING: messages.WARNING,
    websearch.CHECK_ERROR: messages.ERROR,
}


@admin.register(SearchSettings)
class SearchSettingsAdmin(admin.ModelAdmin):
    """Genau ein Datensatz: Die Liste führt direkt zum Formular, Löschen und
    zweites Anlegen gibt es nicht. „SearXNG testen“ prüft die *gespeicherten*
    Einstellungen (Suche nach „test“) und zeigt Trefferzahl oder Ursache."""

    fieldsets = [
        (None, {"fields": ["enabled"]}),
        (
            "Such-Backend",
            {
                "fields": ["backend", "searxng_url", "language", "safesearch"],
                "description": "SearXNG im Intranet. Nach dem Speichern über "
                "„SearXNG testen“ (oben rechts) prüfen.",
            },
        ),
        ("Umfang", {"fields": ["max_results", "fetch_pages", "timeout_seconds"]}),
    ]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        if not self.has_view_or_change_permission(request):
            raise PermissionDenied
        obj = SearchSettings.load()
        return HttpResponseRedirect(reverse("admin:chat_searchsettings_change", args=[obj.pk]))

    def get_object(self, request, object_id, from_field=None):
        # Fehlt der Datensatz noch (frische Installation), wird er angelegt.
        if str(object_id) == str(SearchSettings.SINGLETON_PK):
            SearchSettings.load()
        return super().get_object(request, object_id, from_field)

    def get_urls(self):
        return [
            path(
                "<path:object_id>/check/",
                self.admin_site.admin_view(self.check_view),
                name="chat_searchsettings_check",
            ),
            *super().get_urls(),
        ]

    def check_view(self, request, object_id):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        if not self.has_change_permission(request):
            raise PermissionDenied
        cfg = SearchSettings.load()
        level, text = websearch.check(cfg)
        self.message_user(request, text, SEARCH_CHECK_LEVELS[level])
        return HttpResponseRedirect(reverse("admin:chat_searchsettings_change", args=[cfg.pk]))


# --- Dokumentsuche (M7) -----------------------------------------------------
# RAG-Einstellungen, Sammlungen, Dokumente und Hintergrundjobs verwaltet der
# eigene Admin-Abschnitt „Dokumente (RAG)“ (multigpt/rag/admin.py).


# --- Nur Metadaten (Privatsphäre) -------------------------------------------


def _ref(attname: str, label: str):
    """Spalte mit der reinen ID eines Fremdschlüssels.

    Nicht den Feldnamen selbst verwenden: Der Admin zeigt sonst ``__str__`` des
    Ziels an, bei Chats also den Titel.
    """

    @admin.display(description=label)
    def show(obj):
        return getattr(obj, attname)

    show.__name__ = f"{attname}_ref"
    return show


conversation_ref = _ref("conversation_id", "Chat-Nr.")
message_ref = _ref("message_id", "Nachricht-Nr.")
collection_ref = _ref("collection_id", "Sammlung-Nr.")
source_image_ref = _ref("source_image_id", "Ausgangsbild-Nr.")


class MetadataOnlyAdmin(admin.ModelAdmin):
    """Nur ansehen, keine Inhalte; ``fields`` listet ausschließlich Metadaten."""

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return self.fields


@admin.register(Conversation)
class ConversationAdmin(MetadataOnlyAdmin):
    list_display = ["id", "user", "default_model", "created", "updated", "archived"]
    list_filter = ["archived"]
    fields = list_display
    list_select_related = ["user", "default_model"]


@admin.register(Message)
class MessageAdmin(MetadataOnlyAdmin):
    list_display = [
        "id",
        conversation_ref,
        "role",
        "model",
        "tokens_in",
        "tokens_out",
        "cost",
        "status",
        "created",
    ]
    list_filter = ["role", "status", "model"]
    fields = list_display
    list_select_related = ["model"]


@admin.register(Attachment)
class AttachmentAdmin(MetadataOnlyAdmin):
    list_display = ["id", message_ref, "kind", "generated_by_model", source_image_ref, "cost"]
    list_filter = ["kind"]
    fields = [*list_display, "created"]


@admin.register(ToolCall)
class ToolCallAdmin(MetadataOnlyAdmin):
    list_display = ["id", message_ref, "server", "tool", "status", "duration", "created"]
    list_filter = ["status", "server"]
    fields = list_display


@admin.register(SourceRef)
class SourceRefAdmin(MetadataOnlyAdmin):
    list_display = ["id", message_ref, "kind"]
    list_filter = ["kind"]
    fields = list_display


@admin.register(Preset)
class PresetAdmin(MetadataOnlyAdmin):
    list_display = ["id", "user"]
    fields = list_display


@admin.register(Share)
class ShareAdmin(MetadataOnlyAdmin):
    # Freigaben legen die Besitzer in der Oberfläche an.
    list_display = ["id", conversation_ref, collection_ref, "group", "can_write", "created"]
    list_filter = ["can_write", "group"]
    fields = list_display
