"""Admin-Masken der App chat.

Geheimnisse (API-Keys, MCP-Zugangsdaten) erscheinen nie im Klartext: Das
Eingabefeld ist ein PasswordInput ohne Vorbelegung, angezeigt werden nur die
letzten 4 Zeichen. Ein leeres Feld beim Bearbeiten lässt den Wert unverändert.

Privatsphäre (Plan 8f): Auch Verwalter sehen fremde Chats nicht. Chats,
Nachrichten, Anhänge, Werkzeugaufrufe, Quellen, Vorlagen, Sammlungen und
Dokumente sind deshalb nur als Metadaten sichtbar (ohne Inhalte, Chattitel
und Dateien) und im Admin weder änderbar noch löschbar. Namen von Sammlungen,
Dokumenten und Vorlagen erscheinen als Objektbezeichnung (``__str__``).
"""

from django import forms
from django.contrib import admin
from django.db import models

from multigpt.core.fields import mask_secret

from .models import (
    AIModel,
    Attachment,
    Collection,
    Conversation,
    Document,
    Job,
    McpServer,
    Message,
    Preset,
    Provider,
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
        "um die gespeicherten Zugangsdaten zu behalten.",
    )
    clear_credentials = forms.BooleanField(label="Zugangsdaten entfernen", required=False)

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
            "tools_requiring_confirmation",
        ]


# --- Anbieter, Modelle, MCP --------------------------------------------------

URL_OVERRIDES = {models.URLField: {"assume_scheme": "https"}}


class AIModelInline(admin.TabularInline):
    model = AIModel
    extra = 0
    fields = ["model_id", "display_name", "capability", "active", "sort_order"]
    show_change_link = True


@admin.register(Provider)
class ProviderAdmin(admin.ModelAdmin):
    form = ProviderForm
    formfield_overrides = URL_OVERRIDES
    list_display = ["name", "kind", "base_url", "api_key_hint", "active", "is_local", "last_online"]
    list_filter = ["kind", "active", "is_local"]
    search_fields = ["name", "base_url"]
    readonly_fields = ["api_key_hint", "last_online"]
    fieldsets = [
        (None, {"fields": ["name", "kind", "base_url", "active"]}),
        ("API-Key", {"fields": ["api_key_hint", "api_key", "clear_api_key"]}),
        ("Lokaler Anbieter", {"fields": ["is_local", "check_status", "last_online"]}),
    ]
    inlines = [AIModelInline]

    @admin.display(description="gespeicherter Key")
    def api_key_hint(self, obj):
        return mask_secret(obj.api_key) or "–"


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


@admin.register(McpServer)
class McpServerAdmin(admin.ModelAdmin):
    form = McpServerForm
    formfield_overrides = URL_OVERRIDES
    list_display = ["name", "transport", "command", "url", "credentials_hint", "active"]
    list_filter = ["transport", "active"]
    search_fields = ["name"]
    readonly_fields = ["credentials_hint"]
    fieldsets = [
        (None, {"fields": ["name", "transport", "command", "url", "active"]}),
        ("Zugangsdaten", {"fields": ["credentials_hint", "credentials", "clear_credentials"]}),
        ("Werkzeuge", {"fields": ["tools_requiring_confirmation"]}),
    ]

    @admin.display(description="gespeicherte Zugangsdaten")
    def credentials_hint(self, obj):
        return mask_secret(obj.credentials) or "–"


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


@admin.register(Collection)
class CollectionAdmin(MetadataOnlyAdmin):
    list_display = ["id", "owner", "created"]
    fields = list_display


@admin.register(Document)
class DocumentAdmin(MetadataOnlyAdmin):
    # Fehlertext bleibt sichtbar: Er hilft bei Indexierungsproblemen und
    # enthält keine Dokumentinhalte.
    list_display = ["id", collection_ref, "status", "created"]
    list_filter = ["status"]
    fields = [*list_display, "error_text"]


@admin.register(Share)
class ShareAdmin(MetadataOnlyAdmin):
    # Freigaben legen die Besitzer in der Oberfläche an.
    list_display = ["id", conversation_ref, collection_ref, "group", "can_write", "created"]
    list_filter = ["can_write", "group"]
    fields = list_display


# --- Hintergrundjobs ---------------------------------------------------------


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ["id", "kind", "status", "attempts", "created"]
    list_filter = ["kind", "status"]
    readonly_fields = ["created"]
