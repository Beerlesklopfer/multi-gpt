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

import hashlib

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.contrib.admin.utils import unquote
from django.core.exceptions import PermissionDenied
from django.db import models, transaction
from django.http import Http404, HttpResponseNotAllowed, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from django.utils.text import Truncator

from multigpt.billing.admin import ModelPriceInline
from multigpt.core.fields import mask_secret

from . import capabilities, detect, sandbox, status, websearch
from . import mcp as mcp_client
from .management.commands.sync_models import guess_capability
from .mcp import importer as mcp_importer
from .mcp import status as mcp_status
from .mcp.config import parse_credentials, split_command
from .models import (
    AIModel,
    Attachment,
    ChatSettings,
    Conversation,
    McpServer,
    Message,
    Preset,
    Project,
    Provider,
    SearchSettings,
    Share,
    SourceRef,
    ToolCall,
)
from .providers.base import short_error

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
        fields = [
            "name",
            "kind",
            "base_url",
            "api_key",
            "active",
            "is_local",
            "check_status",
            "billing_account",
        ]


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
        self._apply_ratings(cleaned)
        return cleaned

    # --- Einstufung je gemeldetem Werkzeug (Tabelle im Abschnitt „Werkzeuge“) ---

    RATING_CHOICES = [
        ("", "nicht eingestuft"),
        ("auto", "ohne Rückfrage"),
        ("confirm", "mit Rückfrage"),
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Für die Tabelle in McpServerAdmin.tool_overview (liest form.instance).
        self.instance._admin_form = self
        self.rating_fields: dict[str, str] = {}  # Werkzeugname -> Feldname
        self.ratings_changed = False
        if not self.instance.pk:
            return
        for tool in self.instance.reported_tools or []:
            name = tool.get("name") if isinstance(tool, dict) else None
            if not isinstance(name, str) or not name or name in self.rating_fields:
                continue
            key = "tool_rating_" + hashlib.sha1(name.encode()).hexdigest()[:12]
            current = mcp_status.rating(self.instance, name)
            # Unbewertet: Vorschlag aus den annotations des Servers vorbelegen;
            # wirksam wird er erst, wenn der Verwalter speichert.
            initial = current or mcp_status.suggestion(tool)
            self.fields[key] = forms.ChoiceField(
                label=name, choices=self.RATING_CHOICES, required=False, initial=initial
            )
            self.rating_fields[name] = key

    def _apply_ratings(self, cleaned):
        """Auswahl der Tabelle in die Listen übernehmen.

        Für gemeldete Werkzeuge gilt die Tabelle (nur, wenn ihre Felder mitgeschickt
        wurden); Einträge für andere Werkzeuge bleiben aus den JSON-Feldern erhalten.
        """
        if self.errors.get("known_tools") or self.errors.get("tools_requiring_confirmation"):
            return
        posted = {
            name: cleaned.get(key) or ""
            for name, key in self.rating_fields.items()
            if self.add_prefix(key) in self.data
        }
        known = list(cleaned.get("known_tools") or [])
        confirm = list(cleaned.get("tools_requiring_confirmation") or [])
        if posted:
            known = [n for n in known if n not in posted]
            confirm = [n for n in confirm if n not in posted]
            for name, value in posted.items():
                if value in ("auto", "confirm"):
                    known.append(name)
                if value == "confirm":
                    confirm.append(name)
            cleaned["known_tools"] = known
            cleaned["tools_requiring_confirmation"] = confirm
        before = (
            set(self.instance.known_tools or []),
            set(self.instance.tools_requiring_confirmation or []),
        )
        self.ratings_changed = bool(self.instance.pk) and before != (set(known), set(confirm))


class McpImportForm(forms.Form):
    config = forms.CharField(
        label="Konfiguration (JSON)",
        widget=forms.Textarea(
            attrs={
                "rows": 14,
                "cols": 80,
                "spellcheck": "false",
                "autocomplete": "off",
                "class": "vLargeTextField mcp-import-config",
                "placeholder": '{"mcpServers": {"name": {"type": "http", "url": "https://…", '
                '"headers": {"Authorization": "Bearer …"}}}}',
            }
        ),
        help_text="Format von Claude Desktop, Claude Code, Cursor, n8n usw. "
        "(„mcpServers“). Tokens werden verschlüsselt gespeichert; Platzhalter wie "
        "<YOUR_ACCESS_TOKEN_HERE> nicht – solche Server werden deaktiviert angelegt.",
    )
    update_existing = forms.BooleanField(
        label="Gleichnamige Server aktualisieren",
        required=False,
        help_text="Sonst werden vorhandene Server übersprungen. Einstufungen bleiben erhalten.",
    )

    def clean_config(self):
        try:
            entries = mcp_importer.parse_config(self.cleaned_data["config"])
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from None
        self.cleaned_data["entries"] = entries
        return ""  # nicht weiterreichen (Tokens)

    def clean(self):
        cleaned = super().clean()
        if "entries" in self.cleaned_data:
            cleaned["entries"] = self.cleaned_data["entries"]
        return cleaned


# --- Anbieter, Modelle, MCP --------------------------------------------------

URL_OVERRIDES = {models.URLField: {"assume_scheme": "https"}}


class AIModelInline(admin.TabularInline):
    """Feinarbeit an einzelnen Modellen; übernommen wird über „Modelle auswählen“."""

    model = AIModel
    extra = 0
    # Fähigkeiten-Matrix: Hauptart plus Häkchen; ausgewählte MCP-Server im
    # Detailformular des Modells (Link „Ändern“).
    fields = [
        "model_id",
        "display_name",
        "capability",
        "supports_tools",
        "supports_vision",
        "can_edit_images",
        "mcp_access",
        "active",
        "sort_order",
    ]
    show_change_link = True

    def get_formset(self, request, obj=None, **kwargs):
        # Neue Zeilen: MCP-Freigabe wie bei automatisch angelegten Modellen.
        formset = super().get_formset(request, obj, **kwargs)
        if obj is not None:
            formset.form.base_fields["mcp_access"].initial = detect.default_mcp_access(obj)
        return formset


# Zeitlimits der Prüfungen im Admin (Sekunden). Beim Speichern kurz, damit die
# Seite nicht hängt; auf Knopfdruck und für die Modellauswahl großzügiger
# (OpenRouter liefert Hunderte Modelle).
SAVE_CHECK_TIMEOUT = 3.0
MANUAL_CHECK_TIMEOUT = 10.0
SELECT_MODELS_TIMEOUT = 20.0


def _models(count: int) -> str:
    return f"{count} Modell" if count == 1 else f"{count} Modelle"


def _choice(model_id: str) -> dict:
    """Vorschlag für die Combobox „Modell-ID“ (Heuristik, ohne Netzabruf)."""
    guessed = capabilities.guess(model_id)
    return {
        "id": model_id,
        "capability": guessed.capability,
        "tools": guessed.tools,
        "vision": guessed.vision,
    }


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
        "billing_account",
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
        ("Abrechnung", {"fields": ["billing_account"]}),
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
        extra_context = extra_context or {}
        provider = self.get_object(request, object_id)
        if provider is not None:
            existing = set(provider.ai_models.values_list("model_id", flat=True))
            extra_context["reported_model_choices"] = [
                {**_choice(model_id), "exists": model_id in existing}
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
        taken = list(dict.fromkeys(request.POST.getlist("take")))
        # Werkzeuge/Bilder neuer Modelle: Meldung von LM Studio bzw. Heuristik.
        found = detect.detect(provider, [m for m in taken if m in allowed and m not in existing])
        for model_id in taken:
            if model_id not in allowed or not 0 < len(model_id) <= MODEL_ID_MAX_LENGTH:
                continue
            name = (request.POST.get(f"name:{model_id}") or "").strip()[:MODEL_ID_MAX_LENGTH]
            capability = request.POST.get(f"cap:{model_id}") or ""
            if capability not in valid_caps:
                capability = guess_capability(model_id)
            active = model_id in active_ids
            model = existing.get(model_id)
            if model is None:
                detected = found.get(model_id) or capabilities.guess(model_id)
                new_models.append(
                    detect.new_model(
                        provider,
                        model_id,
                        detected,
                        display_name=name or model_id,
                        capability=capability,
                        supports_tools=detected.tools and capability == AIModel.Capability.CHAT,
                        supports_vision=detected.vision and capability == AIModel.Capability.CHAT,
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
    # Fähigkeiten-Matrix direkt in der Liste pflegbar (list_editable).
    list_display = [
        "display_name",
        "model_id",
        "provider",
        "capability",
        "supports_tools",
        "supports_vision",
        "can_edit_images",
        "mcp_access",
        "active",
        "sort_order",
    ]
    list_editable = [
        "capability",
        "supports_tools",
        "supports_vision",
        "can_edit_images",
        "mcp_access",
        "active",
        "sort_order",
    ]
    list_filter = [
        "provider",
        "capability",
        "active",
        "supports_tools",
        "supports_vision",
        "mcp_access",
    ]
    search_fields = ["display_name", "model_id"]
    list_select_related = ["provider"]
    filter_horizontal = ["mcp_servers"]
    inlines = [ModelPriceInline]  # Preise mit Historie (multigpt/billing)
    actions = ["detect_capabilities_action"]

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        """Gemeldete Modelle je Anbieter für die Combobox am Feld „Modell-ID“."""
        by_provider = {}
        for provider in Provider.objects.prefetch_related("ai_models"):
            existing = {model.model_id for model in provider.ai_models.all()}
            by_provider[str(provider.pk)] = [
                {**_choice(mid), "exists": mid in existing}
                for mid in sorted(provider.reported_models or [])
            ]
        extra_context = {
            **(extra_context or {}),
            "reported_model_choices": {"by_provider": by_provider},
        }
        return super().changeform_view(request, object_id, form_url, extra_context)

    def save_model(self, request, obj, form, change):
        # Neues Modell ohne bewusste MCP-Wahl: Vorgabe nach Anbieter (detect).
        if not change and "mcp_access" not in form.changed_data:
            obj.mcp_access = detect.default_mcp_access(obj.provider)
        super().save_model(request, obj, form, change)

    @admin.action(description="Fähigkeiten automatisch erkennen (Werkzeuge, Bilder)")
    def detect_capabilities_action(self, request, queryset):
        """Vorschau der Abweichungen; gespeichert wird erst nach Bestätigung.

        Quelle: Meldung von LM Studio (lokale Anbieter), sonst Heuristik.
        """
        changes = detect.diff(queryset.select_related("provider"))
        if request.POST.get("apply") == "1":
            count = detect.apply(changes)
            for change in changes:
                self.message_user(
                    request, f"„{change.model.display_name}“: {change.describe()}", messages.INFO
                )
            self.message_user(
                request,
                f"{_models(count)} geändert, {queryset.count() - count} unverändert.",
                messages.SUCCESS,
            )
            return None
        if not changes:
            self.message_user(
                request,
                "Keine Änderungen: Alle gewählten Modelle passen zur Erkennung.",
                messages.INFO,
            )
            return None
        context = {
            **self.admin_site.each_context(request),
            "opts": self.opts,
            "title": "Fähigkeiten automatisch erkennen",
            "subtitle": None,
            "changes": changes,
            "selected": request.POST.getlist(ACTION_CHECKBOX_NAME),
            "action": "detect_capabilities_action",
            "unchanged": queryset.count() - len(changes),
        }
        return TemplateResponse(request, "admin/chat/aimodel/detect_capabilities.html", context)


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
        "state_column",
        "last_checked",
        "tool_count",
        "unrated_column",
    ]
    list_filter = ["transport", "active", "online"]
    search_fields = ["name"]
    readonly_fields = [
        "credentials_hint",
        "status_overview",
        "last_checked",
        "last_online",
        "tools_checked",
        "tool_overview",
        "model_overview",
    ]
    actions = ["check_connection_action"]
    change_list_template = "admin/chat/mcpserver/change_list.html"
    change_form_template = "admin/chat/mcpserver/change_form.html"
    fieldsets = [
        (None, {"fields": ["name", "transport", "command", "url", "timeout_seconds", "active"]}),
        ("Zugangsdaten", {"fields": ["credentials_hint", "credentials", "clear_credentials"]}),
        (
            "Verbindung",
            {
                "fields": ["status_overview", "last_checked", "last_online"],
                "description": "Ergebnis der letzten Prüfung (verbinden, Werkzeugliste abrufen). "
                "Prüfen über „Jetzt prüfen“ oben rechts; beim Speichern wird automatisch "
                "geprüft, im Betrieb alle 5 Minuten (offline: jede Minute) durch den Worker.",
            },
        ),
        (
            "Werkzeuge",
            {
                "fields": ["tool_overview", "tools_checked"],
                "description": "Zuletzt vom Server gemeldete Werkzeuge. Einstufung je Werkzeug "
                "wählen und speichern. Nicht eingestufte Werkzeuge laufen nur mit Rückfrage.",
            },
        ),
        (
            "Erweitert: Einstufung als JSON",
            {
                "classes": ["collapse"],
                "fields": [
                    "tools_requiring_confirmation",
                    "known_tools",
                    "adopt_listed_tools",
                ],
                "description": "Rückfall, z. B. solange der Server offline ist. Für gemeldete "
                "Werkzeuge gilt die Auswahl in der Tabelle oben.",
            },
        ),
        (
            "Modelle",
            {
                "fields": ["model_overview"],
                "description": "Welche KI-Modelle diesen Server nutzen dürfen (Spalte „MCP“ "
                "bei den KI-Modellen). Zusätzlich gelten die Rechte der Rolle.",
            },
        ),
    ]

    @admin.display(description="erlaubte Modelle")
    def model_overview(self, obj):
        if obj is None or not obj.pk:
            return "Nach dem Speichern sichtbar."
        allowed = (
            AIModel.objects.filter(
                models.Q(mcp_access=AIModel.McpAccess.ALL)
                | models.Q(mcp_access=AIModel.McpAccess.SELECTED, mcp_servers=obj)
            )
            .select_related("provider")
            .distinct()
        )
        if not allowed:
            return "Kein Modell."
        return format_html(
            "<ul>{}</ul>",
            format_html_join(
                "",
                '<li><a href="{}">{}</a> ({}, {}){}</li>',
                (
                    (
                        reverse("admin:chat_aimodel_change", args=[m.pk]),
                        m.display_name,
                        m.provider.name,
                        m.get_mcp_access_display(),
                        "" if m.active else " – inaktiv",
                    )
                    for m in allowed
                ),
            ),
        )

    @admin.display(description="gespeicherte Zugangsdaten")
    def credentials_hint(self, obj):
        return mask_secret(obj.credentials) or "–"

    # --- Status (chat/mcp/status.py) -------------------------------------------

    @admin.display(description="Status", ordering="online")
    def state_column(self, obj):
        label = mcp_status.state_label(obj)
        if label == "offline":
            return format_html(
                '<span class="mcp-state mcp-offline" title="{}">offline – {}</span>',
                obj.last_error,
                short_error(obj.last_error),
            )
        css = {"online": "online", "ungeprüft": "unchecked"}.get(label, "inactive")
        return format_html('<span class="mcp-state mcp-{}">{}</span>', css, label)

    @admin.display(description="Werkzeuge")
    def tool_count(self, obj):
        return len(obj.reported_tools or []) if obj.last_online else "–"

    @admin.display(description="davon nicht eingestuft")
    def unrated_column(self, obj):
        if not obj.last_online:
            return "–"
        count = mcp_status.unrated_count(obj)
        if not count:
            return 0
        return format_html('<strong class="mcp-unrated">{}</strong>', count)

    @admin.display(description="Status")
    def status_overview(self, obj):
        if obj is None or not obj.pk:
            return "Wird nach dem Speichern geprüft."
        label = mcp_status.state_label(obj)
        if label == "offline":
            return format_html(
                '<span class="mcp-state mcp-offline">offline</span> – {}', obj.last_error
            )
        if label == "online":
            count = len(obj.reported_tools or [])
            return format_html(
                '<span class="mcp-state mcp-online">online</span> – {} Werkzeuge gemeldet', count
            )
        if label == "deaktiviert":
            return "deaktiviert – wird nicht geprüft"
        return "noch nicht geprüft"

    @admin.display(description="Werkzeugliste")
    def tool_overview(self, obj):
        """Tabelle der zuletzt gemeldeten Werkzeuge mit Auswahl der Einstufung.

        Name und Beschreibung kommen vom Server: nur escaped (format_html) und
        gekürzt anzeigen. Kein Netzaufruf beim Seitenaufruf.
        """
        if obj is None or not obj.pk:
            return "Nach dem Speichern sichtbar."
        tools = [t for t in obj.reported_tools or [] if isinstance(t, dict) and t.get("name")]
        new, gone = mcp_status.tool_changes(obj)
        form = getattr(obj, "_admin_form", None)
        fields = getattr(form, "rating_fields", {})
        labels = dict(McpServerForm.RATING_CHOICES)
        rows = []
        for tool in tools:
            name = tool["name"]
            key = fields.get(name)
            choice = form[key] if form is not None and key else labels[mcp_status.rating(obj, name)]
            hints = tool.get("annotations") or {}
            hint_text = ", ".join(
                text
                for flag, text in (
                    ("readOnlyHint", "nur lesend"),
                    ("destructiveHint", "verändernd"),
                    ("idempotentHint", "wiederholbar"),
                    ("openWorldHint", "nach außen"),
                )
                if hints.get(flag) is True
            )
            proposal = mcp_status.suggestion(tool)
            note = ""
            if name in new:
                note = "neu"
                if proposal:
                    note += f" – Vorschlag des Servers: {labels[proposal]} (bitte prüfen)"
            params = [
                f"{p}*" if p in (tool.get("required") or []) else p
                for p in tool.get("params") or []
            ]
            rows.append(
                (
                    "mcp-tool-new" if name in new else "",
                    Truncator(name).chars(80),
                    note,
                    Truncator(tool.get("description") or "").chars(mcp_status.DESCRIPTION_MAX),
                    ", ".join(params) or "–",
                    hint_text or "–",
                    choice,
                )
            )
        if obj.last_online is None:
            head = format_html(
                "<p>{}</p>", "Noch keine Werkzeugliste – „Jetzt prüfen“ oben rechts ruft sie ab."
            )
        elif not tools:
            head = format_html("<p>{}</p>", "Der Server bietet keine Werkzeuge an.")
        else:
            head = format_html("<p>Stand: {}</p>", _local_time(obj.last_online))
        body = ""
        if rows:
            body = format_html(
                '<table class="mcp-tools"><thead><tr><th>Werkzeug</th><th>Beschreibung</th>'
                "<th>Parameter</th><th>Hinweise des Servers</th><th>Einstufung</th></tr></thead>"
                "<tbody>{}</tbody></table>",
                format_html_join(
                    "",
                    '<tr class="{}"><td><code>{}</code><div class="mcp-tool-note">{}</div></td>'
                    "<td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                    rows,
                ),
            )
        foot = ""
        if gone:
            foot = format_html(
                '<p class="mcp-tools-gone">Nicht mehr gemeldet, aber eingestuft: {}</p>',
                ", ".join(Truncator(n).chars(80) for n in gone),
            )
        return format_html("{}{}{}", head, body, foot)

    def get_urls(self):
        view = self.admin_site.admin_view
        return [
            path("import/", view(self.import_view), name="chat_mcpserver_import"),
            path("<path:object_id>/check/", view(self.check_view), name="chat_mcpserver_check"),
            *super().get_urls(),
        ]

    def import_view(self, request):
        """MCP-Server aus einer JSON-Konfiguration („mcpServers“) übernehmen."""
        if not self.has_add_permission(request):
            raise PermissionDenied
        form = McpImportForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            results = mcp_importer.apply_import(
                form.cleaned_data["entries"],
                update_existing=form.cleaned_data["update_existing"],
            )
            levels = {"error": messages.ERROR, "skipped": messages.WARNING}
            for result in results:
                level = levels.get(result.action, messages.SUCCESS)
                if result.action == "created" and "Platzhalter" in result.message:
                    level = messages.WARNING
                self.message_user(request, f"„{result.name}“: {result.message}", level=level)
            if any(r.action == "created" for r in results):
                self.message_user(
                    request,
                    "Neue Server haben noch keine eingestuften Werkzeuge – alle laufen bis "
                    "zur Einstufung nur mit Rückfrage.",
                    level=messages.INFO,
                )
            names = {r.name: r.action for r in results if r.action in ("created", "updated")}
            touched = list(McpServer.objects.filter(name__in=names))
            for server in touched:
                if names[server.name] == "updated":
                    mcp_status.invalidate(server.pk)  # Worker prüft beim nächsten Durchlauf
            created = [s for s in touched if names[s.name] == "created" and s.active]
            if created:
                # Neue, aktive Server gleich prüfen (parallel, je höchstens 10 s). Die
                # Anlage ist schon festgeschrieben (eigene Transaktion je Eintrag).
                self._check_and_report(request, created)
            return HttpResponseRedirect(reverse("admin:chat_mcpserver_changelist"))
        if request.method == "POST":
            # Eingabe enthält ggf. Tokens: nicht ins Formular zurückschreiben.
            form.data = form.data.copy()
            form.data["config"] = ""
        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "MCP-Server aus JSON importieren",
            "form": form,
        }
        return TemplateResponse(request, "admin/chat/mcpserver/import.html", context)

    def _check_and_report(self, request, servers):
        outcomes = mcp_status.force_check_many(servers)
        for server, outcome in zip(servers, outcomes, strict=True):
            self.message_user(request, *_mcp_check_message(server, outcome))

    def check_view(self, request, object_id):
        """„Jetzt prüfen“: verbindet live, aktualisiert Status und Werkzeugliste."""
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        server = self.get_object(request, unquote(object_id))
        if server is None:
            raise Http404("MCP-Server nicht gefunden.")
        if not self.has_change_permission(request, server):
            raise PermissionDenied
        self._check_and_report(request, [server])
        return HttpResponseRedirect(reverse("admin:chat_mcpserver_change", args=[server.pk]))

    @admin.action(description="Ausgewählte jetzt prüfen")
    def check_connection_action(self, request, queryset):
        self._check_and_report(request, list(queryset))

    def save_model(self, request, obj, form, change):
        if getattr(form, "ratings_changed", False):
            obj.tools_checked = timezone.now()
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
        obj.tools_checked = timezone.now()
        obj.save(update_fields=["known_tools", "tools_checked"])
        self.message_user(
            request, f"{len(added)} Werkzeuge als eingestuft übernommen.", level=messages.SUCCESS
        )

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        server = form.instance
        if not server.active:
            return
        # Erst nach dem Commit prüfen (wie bei den Anbietern): Die Prüfung kann bis
        # 10 s dauern und startet bei stdio den Prozess.
        transaction.on_commit(lambda: self._check_and_report(request, [server]))


ADMIN_TIMEOUT = 15


def _local_time(value) -> str:
    return timezone.localtime(value).strftime("%d.%m.%Y %H:%M")


def _mcp_check_message(server, outcome) -> tuple[str, int]:
    """Admin-Meldung (Text, Stufe) zu einer MCP-Prüfung."""
    if outcome.skipped:
        return f"„{server.name}“: {outcome.skipped} Nicht geprüft.", messages.INFO
    if not outcome.online:
        return f"„{server.name}“: Offline: {outcome.error}", messages.ERROR
    new, _gone = mcp_status.tool_changes(server)
    text = f"„{server.name}“: Verbindung in Ordnung, {len(outcome.tools)} Werkzeuge."
    if new:
        shown = ", ".join(Truncator(n).chars(60) for n in new[:20])
        more = f" und {len(new) - 20} weitere" if len(new) > 20 else ""
        return (
            f"{text} Nicht eingestuft (laufen nur mit Rückfrage): {shown}{more}",
            messages.WARNING,
        )
    return text, messages.SUCCESS


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
        (
            "Seiten abrufen und Websites durchsuchen",
            {
                "fields": [
                    "fetch_url_enabled",
                    "crawl_enabled",
                    "crawl_max_pages",
                    "crawl_time_seconds",
                    "blocked_domains",
                ],
                "description": "Eingebaute Werkzeuge fetch_url und crawl_site (nur Modelle mit "
                "Werkzeugen, Recht „Websuche“). Gilt nur bei eingeschalteter Websuche; "
                "eine SearXNG-URL ist dafür nicht nötig. „Abruf testen“ (oben rechts) prüft "
                "eine Adresse mit diesen Einstellungen.",
            },
        ),
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
            path(
                "<path:object_id>/fetch-check/",
                self.admin_site.admin_view(self.fetch_check_view),
                name="chat_searchsettings_fetch_check",
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

    def fetch_check_view(self, request, object_id):
        """„Abruf testen“: eine Adresse wie fetch_url abrufen (SSRF-Schutz,
        gesperrte Domains), Titel und Textlänge melden."""
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        if not self.has_change_permission(request):
            raise PermissionDenied
        cfg = SearchSettings.load()
        level, text = websearch.pages.check(request.POST.get("url", ""), cfg)
        self.message_user(request, text, SEARCH_CHECK_LEVELS[level])
        return HttpResponseRedirect(reverse("admin:chat_searchsettings_change", args=[cfg.pk]))


@admin.register(ChatSettings)
class ChatSettingsAdmin(admin.ModelAdmin):
    """Genau ein Datensatz wie bei den Sucheinstellungen: Die Liste führt direkt
    zum Formular, Löschen und zweites Anlegen gibt es nicht."""

    fieldsets = [
        (None, {"fields": ["base_instructions"]}),
        # Bilderzeugung (M9-01, chat/images.py)
        ("Bilder", {"fields": ["default_image_model", "image_tool_confirm"]}),
        # Berechnungen (M4a-10, chat/tools_python.py, chat/sandbox.py)
        (
            "Berechnungen (run_python)",
            {
                "fields": [
                    "python_sandbox_status",
                    "python_enabled",
                    "python_confirm",
                    "python_cpu_seconds",
                    "python_wall_seconds",
                    "python_memory_mb",
                    "python_processes",
                    "python_file_mb",
                    "python_output_kb",
                ],
                "description": "Python mit numpy, sympy, mpmath und matplotlib in einer "
                "Sandbox (bubblewrap: kein Netz, keine Server-Dateien). Nur für Modelle mit "
                "Werkzeugen und Rollen mit „Berechnungen ausführen“. „Sandbox testen“ (oben "
                "rechts) rechnet print(1+1) und versucht Netz- und Dateizugriffe.",
            },
        ),
    ]
    readonly_fields = ["python_sandbox_status"]
    formfield_overrides = {models.TextField: {"widget": forms.Textarea(attrs={"rows": 6})}}

    @admin.display(description="Sandbox verfügbar")
    def python_sandbox_status(self, obj):
        state = sandbox.status()
        if state.available:
            extra = "mit seccomp" if state.seccomp else "ohne seccomp (Architektur)"
            return format_html("<strong>ja</strong> ({})", extra)
        return format_html(
            "<strong>nein</strong> – {}<br>Ohne Sandbox wird das Werkzeug nicht angeboten.",
            state.reason,
        )

    def get_urls(self):
        return [
            path(
                "<path:object_id>/sandbox-test/",
                self.admin_site.admin_view(self.sandbox_test_view),
                name="chat_chatsettings_sandbox_test",
            ),
            *super().get_urls(),
        ]

    def sandbox_test_view(self, request, object_id):
        """„Sandbox testen“: print(1+1), Netz-, Datei- und Umgebungszugriff (sandbox.selftest)."""
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        if not self.has_change_permission(request):
            raise PermissionDenied
        for ok, text in sandbox.selftest():
            self.message_user(request, text, messages.SUCCESS if ok else messages.ERROR)
        cfg = ChatSettings.load()
        return HttpResponseRedirect(reverse("admin:chat_chatsettings_change", args=[cfg.pk]))

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        if not self.has_view_or_change_permission(request):
            raise PermissionDenied
        obj = ChatSettings.load()
        return HttpResponseRedirect(reverse("admin:chat_chatsettings_change", args=[obj.pk]))

    def get_object(self, request, object_id, from_field=None):
        if str(object_id) == str(ChatSettings.SINGLETON_PK):
            ChatSettings.load()
        return super().get_object(request, object_id, from_field)


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
        "author",
        "created",
    ]
    list_filter = ["role", "status", "model"]
    fields = list_display
    list_select_related = ["model", "author"]


@admin.register(Attachment)
class AttachmentAdmin(MetadataOnlyAdmin):
    # Ohne Dateiname und Inhalt (privat), nur Metadaten.
    list_display = [
        "id",
        message_ref,
        "owner",
        "kind",
        "mime_type",
        "size",
        "generated_by_model",
        source_image_ref,
        "cost",
    ]
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
    # Freigaben legen die Besitzer in der Oberfläche an. Nur Metadaten
    # (Besitzer, Empfänger, Rechte), nie Titel oder Inhalte – auch Verwalter
    # bekommen über Freigaben keinen Einblick in fremde Chats.
    list_display = [
        "id",
        conversation_ref,
        collection_ref,
        "share_owner",
        "group",
        "user",
        "rights",
        "created",
    ]
    list_filter = ["can_write", "can_update", "can_delete", "group"]
    fields = list_display
    list_select_related = ["group", "user", "conversation__user", "collection__owner"]

    @admin.display(description="Besitzer")
    def share_owner(self, obj):
        if obj.conversation_id:
            return obj.conversation.user
        return obj.collection.owner if obj.collection_id else None

    @admin.display(description="Rechte")
    def rights(self, obj):
        return obj.rights_label


@admin.register(Project)
class ProjectAdmin(MetadataOnlyAdmin):
    # Projekte (chat/projects.py) sind privat: nur Besitzer, Name und Anzahl
    # der Chats, nie Beschreibung, Anweisungen oder Titel der Chats.
    list_display = ["id", "owner", "name", "chat_count", "archived", "created", "updated"]
    list_filter = ["archived"]
    fields = list_display
    list_select_related = ["owner"]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(chats=models.Count("conversations"))

    @admin.display(description="Chats", ordering="chats")
    def chat_count(self, obj):
        return obj.chats
