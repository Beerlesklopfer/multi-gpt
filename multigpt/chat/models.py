"""Datenmodell der App chat (Plan Abschnitt 6, ohne Chunk – der folgt in M7).

Bezeichner sind englisch, verbose_name und Choice-Labels deutsch (Plan 2).
"""

import json
import secrets
from pathlib import PurePath

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q

from multigpt.core.fields import EncryptedTextField, UnicodeJSONEncoder

# Geldbeträge (Preise und Kosten-Momentaufnahmen) mit Bruchteilen von Cent.
MONEY = {"max_digits": 12, "decimal_places": 6}


def _random_name(filename: str) -> str:
    """Zufälliger Dateiname; vom Original bleibt nur eine harmlose Endung.

    Plan 9: Dateinamen von Uploads werden nicht übernommen.
    """
    suffix = PurePath(filename or "").suffix.lower()
    if not (2 <= len(suffix) <= 10 and suffix[1:].isascii() and suffix[1:].isalnum()):
        suffix = ""
    return secrets.token_hex(16) + suffix


def attachment_upload_to(instance, filename: str) -> str:
    """attachments/<Nutzer-ID>/<Zufall>.<endung>"""
    user_id = instance.message.conversation.user_id
    return f"attachments/{user_id}/{_random_name(filename)}"


def document_upload_to(instance, filename: str) -> str:
    """documents/<Besitzer-ID>/<Zufall>.<endung>"""
    owner_id = instance.collection.owner_id
    return f"documents/{owner_id}/{_random_name(filename)}"


# --- Anbieter und Modelle ----------------------------------------------------


class Provider(models.Model):
    class Kind(models.TextChoices):
        OPENAI_COMPAT = "openai_compat", "OpenAI-kompatibel"
        ANTHROPIC = "anthropic", "Anthropic"
        GOOGLE = "google", "Google"

    name = models.CharField("Name", max_length=100, unique=True)
    kind = models.CharField("Art", max_length=20, choices=Kind.choices)
    base_url = models.URLField(
        "Basis-URL",
        max_length=500,
        blank=True,
        help_text="Leer lassen für die Standard-URL des Anbieters. "
        "LM Studio z. B. http://pc.local:1234/v1",
    )
    api_key = EncryptedTextField(
        "API-Key",
        blank=True,
        default="",
        help_text="Wird verschlüsselt gespeichert. Für LM Studio nicht nötig.",
    )
    active = models.BooleanField("aktiv", default=True)
    is_local = models.BooleanField(
        "lokal", default=False, help_text="Läuft im Intranet (z. B. LM Studio), kostet nichts."
    )
    check_status = models.BooleanField(
        "Online-Status prüfen",
        default=False,
        help_text="Für Anbieter, die nicht immer erreichbar sind (Plan 8a).",
    )
    last_online = models.DateTimeField("zuletzt online", null=True, blank=True)
    # Ergebnis der letzten Statusprüfung (Plan 8a, M4-03). In der DB statt im
    # Prozess-Cache, damit es für alle gunicorn-Worker gilt.
    online = models.BooleanField("online", default=False, editable=False)
    last_checked = models.DateTimeField("zuletzt geprüft", null=True, blank=True, editable=False)
    reported_models = models.JSONField(
        "gemeldete Modelle",
        default=list,
        blank=True,
        editable=False,
        help_text="Modell-IDs, die der Anbieter bei der letzten Prüfung gemeldet hat.",
    )
    last_error = models.TextField(
        "Fehlerursache",
        blank=True,
        default="",
        editable=False,
        help_text="Ursache, falls die letzte Prüfung fehlschlug (ohne Key); sonst leer.",
    )

    class Meta:
        ordering = ["name"]
        verbose_name = "Anbieter"
        verbose_name_plural = "Anbieter"

    def __str__(self):
        return self.name


class AIModel(models.Model):
    class Capability(models.TextChoices):
        CHAT = "chat", "Chat"
        IMAGE = "image", "Bild"
        EMBEDDING = "embedding", "Embedding"
        STT = "stt", "Spracherkennung"
        TTS = "tts", "Sprachausgabe"

    provider = models.ForeignKey(
        Provider, on_delete=models.CASCADE, related_name="ai_models", verbose_name="Anbieter"
    )
    model_id = models.CharField(
        "Modell-ID", max_length=200, help_text="Bezeichnung beim Anbieter, z. B. gpt-4o"
    )
    display_name = models.CharField("Anzeigename", max_length=200)
    capability = models.CharField(
        "Fähigkeit", max_length=20, choices=Capability.choices, default=Capability.CHAT
    )
    supports_tools = models.BooleanField("unterstützt Werkzeuge", default=False)
    can_edit_images = models.BooleanField("kann Bilder bearbeiten", default=False)
    active = models.BooleanField("aktiv", default=True)
    sort_order = models.PositiveIntegerField("Reihenfolge", default=0)
    price_in = models.DecimalField(
        "Preis Eingabe",
        **MONEY,
        null=True,
        blank=True,
        help_text="Euro je 1 Mio. Eingabe-Tokens (Bildmodelle: je Bild).",
    )
    price_out = models.DecimalField(
        "Preis Ausgabe", **MONEY, null=True, blank=True, help_text="Euro je 1 Mio. Ausgabe-Tokens."
    )

    class Meta:
        ordering = ["sort_order", "display_name"]
        verbose_name = "KI-Modell"
        verbose_name_plural = "KI-Modelle"
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "model_id"], name="chat_aimodel_unique_provider_model_id"
            ),
        ]

    def __str__(self):
        return self.display_name


# --- MCP ---------------------------------------------------------------------


class McpServer(models.Model):
    class Transport(models.TextChoices):
        STDIO = "stdio", "stdio (lokaler Prozess)"
        HTTP = "http", "HTTP (Streamable HTTP)"

    name = models.CharField("Name", max_length=100, unique=True)
    transport = models.CharField("Transport", max_length=10, choices=Transport.choices)
    command = models.CharField(
        "Befehl", max_length=500, blank=True, help_text="Nur bei stdio: Programm mit Argumenten."
    )
    url = models.URLField("URL", max_length=500, blank=True, help_text="Nur bei HTTP.")
    credentials = EncryptedTextField(
        "Zugangsdaten",
        blank=True,
        default="",
        help_text="Wird verschlüsselt gespeichert, z. B. Token oder JSON mit Umgebungsvariablen.",
    )
    active = models.BooleanField("aktiv", default=True)
    tools_requiring_confirmation = models.JSONField(
        "Werkzeuge mit Rückfrage",
        default=list,
        blank=True,
        help_text="Liste der Werkzeugnamen, die erst nach Bestätigung ausgeführt werden.",
    )
    known_tools = models.JSONField(
        "eingestufte Werkzeuge",
        default=list,
        blank=True,
        help_text="Vom Verwalter eingestufte Werkzeugnamen. Werkzeuge, die hier fehlen, "
        "gelten als neu und laufen nur nach Bestätigung.",
    )
    timeout_seconds = models.PositiveSmallIntegerField(
        "Zeitlimit (s)",
        default=30,
        validators=[MinValueValidator(1), MaxValueValidator(600)],
        help_text="Höchstdauer je Werkzeugaufruf in Sekunden.",
    )

    class Meta:
        ordering = ["name"]
        verbose_name = "MCP-Server"
        verbose_name_plural = "MCP-Server"
        constraints = [
            models.CheckConstraint(
                condition=(Q(transport="stdio") & ~Q(command=""))
                | (Q(transport="http") & ~Q(url="")),
                name="chat_mcpserver_command_or_url",
                violation_error_message="stdio braucht einen Befehl, HTTP eine URL.",
            ),
        ]

    def __str__(self):
        return self.name


# --- Chats -------------------------------------------------------------------


class Conversation(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="conversations",
        verbose_name="Konto",
    )
    title = models.CharField("Titel", max_length=200, blank=True)
    default_model = models.ForeignKey(
        AIModel,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Standardmodell",
    )
    system_prompt = models.TextField("System-Prompt", blank=True)
    created = models.DateTimeField("erstellt", auto_now_add=True)
    updated = models.DateTimeField("geändert", auto_now=True)
    archived = models.BooleanField("archiviert", default=False)
    # Ende des angezeigten Zweigs (Nachrichten bearbeiten, Versionen): Der
    # Verlauf ist der Pfad von der Wurzel bis hierher über ``Message.parent``.
    current_leaf = models.ForeignKey(
        "Message",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        editable=False,
        verbose_name="angezeigte Version",
    )

    class Meta:
        ordering = ["-updated"]
        verbose_name = "Chat"
        verbose_name_plural = "Chats"
        indexes = [
            models.Index(fields=["user", "archived", "-updated"], name="chat_conv_user_updated"),
        ]

    def __str__(self):
        # Bewusst ohne Titel: __str__ erscheint im Admin und in Logs, der Titel
        # ist privater Inhalt (Plan 8f). Die Oberfläche nutzt ``title`` direkt.
        return f"Chat {self.pk}"


class Message(models.Model):
    class Role(models.TextChoices):
        USER = "user", "Nutzer"
        ASSISTANT = "assistant", "Assistent"
        SYSTEM = "system", "System"
        TOOL = "tool", "Werkzeug"

    class Status(models.TextChoices):
        COMPLETE = "complete", "vollständig"
        ABORTED = "aborted", "abgebrochen"
        ERROR = "error", "Fehler"
        # Nur noch Altdaten: Früher markierte "Neu erzeugen" die alte Antwort
        # so. Seit den Versionen (Message.parent) wird der Wert nicht mehr
        # vergeben; Migration 0010 hat bestehende Werte umgewandelt.
        SUPERSEDED = "superseded", "ersetzt"
        # Werkzeugschleife pausiert, bis der Nutzer die Aufrufe bestätigt (M4a-05).
        AWAITING_CONFIRMATION = "awaiting_confirmation", "wartet auf Bestätigung"

    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="messages", verbose_name="Chat"
    )
    # Vorgänger im Gesprächsbaum (null für die erste Nachricht). Nachrichten mit
    # gleichem parent sind Versionen voneinander (Bearbeiten, Neu erzeugen).
    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="children",
        editable=False,
        verbose_name="Vorgänger",
    )
    role = models.CharField("Rolle", max_length=20, choices=Role.choices)
    content = models.TextField("Inhalt", blank=True)
    model = models.ForeignKey(
        AIModel,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="messages",
        verbose_name="Modell",
    )
    tokens_in = models.PositiveIntegerField("Tokens Eingabe", default=0)
    tokens_out = models.PositiveIntegerField("Tokens Ausgabe", default=0)
    cost = models.DecimalField(
        "Kosten",
        **MONEY,
        null=True,
        blank=True,
        help_text="Euro, festgehalten zum Zeitpunkt der Antwort.",
    )
    status = models.CharField(
        "Status", max_length=30, choices=Status.choices, default=Status.COMPLETE
    )
    error = models.TextField("Fehlertext", blank=True)
    # Zustand der Werkzeugschleife (M4a-04, siehe services/tool_loop.py): Runden
    # mit Werkzeugaufrufen, Ergebnissen und provider_state, damit der Verlauf
    # wörtlich an das Modell zurückgeht – auch über eine Rückfrage hinweg.
    tool_state = models.JSONField(
        "Werkzeugzustand", default=dict, blank=True, editable=False, encoder=UnicodeJSONEncoder
    )
    created = models.DateTimeField("erstellt", auto_now_add=True)

    class Meta:
        ordering = ["created", "id"]
        verbose_name = "Nachricht"
        verbose_name_plural = "Nachrichten"
        indexes = [
            models.Index(fields=["conversation", "created"], name="chat_msg_conv_created"),
            # Verbrauchsübersicht je Modell und Monat.
            models.Index(fields=["model", "created"], name="chat_msg_model_created"),
        ]

    def __str__(self):
        return f"{self.get_role_display()} #{self.pk}"


class Preset(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="presets",
        verbose_name="Konto",
    )
    name = models.CharField("Name", max_length=100)
    system_prompt = models.TextField("System-Prompt")

    class Meta:
        ordering = ["name"]
        verbose_name = "Vorlage"
        verbose_name_plural = "Vorlagen"
        constraints = [
            models.UniqueConstraint(fields=["user", "name"], name="chat_preset_unique_user_name"),
        ]

    def __str__(self):
        return self.name


class Attachment(models.Model):
    class Kind(models.TextChoices):
        IMAGE = "image", "Bild"
        AUDIO = "audio", "Audio"
        FILE = "file", "Datei"

    message = models.ForeignKey(
        Message, on_delete=models.CASCADE, related_name="attachments", verbose_name="Nachricht"
    )
    kind = models.CharField("Art", max_length=10, choices=Kind.choices)
    file = models.FileField("Datei", upload_to=attachment_upload_to, max_length=255)
    generated_by_model = models.ForeignKey(
        AIModel,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="erzeugt von",
    )
    source_image = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="derived",
        verbose_name="Ausgangsbild",
    )
    tool_call = models.ForeignKey(
        "ToolCall",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="attachments",
        verbose_name="Werkzeugaufruf",
        help_text="Werkzeugaufruf, der diese Datei geliefert hat (Plan 8g).",
    )
    cost = models.DecimalField(
        "Kosten",
        **MONEY,
        null=True,
        blank=True,
        help_text="Euro, festgehalten zum Zeitpunkt der Erzeugung.",
    )
    created = models.DateTimeField("erstellt", auto_now_add=True)

    class Meta:
        ordering = ["created", "id"]
        verbose_name = "Anhang"
        verbose_name_plural = "Anhänge"

    def __str__(self):
        return f"{self.get_kind_display()} #{self.pk}"


# --- Sammlungen (RAG) und Freigaben ------------------------------------------


class Collection(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="collections",
        verbose_name="Besitzer",
    )
    name = models.CharField("Name", max_length=200)
    created = models.DateTimeField("erstellt", auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Sammlung"
        verbose_name_plural = "Sammlungen"
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "name"], name="chat_collection_unique_owner_name"
            ),
        ]

    def __str__(self):
        return self.name


class Share(models.Model):
    """Freigabe eines Chats oder einer Sammlung an eine Gruppe.

    Ziel als zwei nullable Fremdschlüssel; ein CheckConstraint erzwingt, dass
    genau eines gesetzt ist (referenzielle Integrität statt GenericForeignKey).
    """

    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="shares",
        verbose_name="Chat",
    )
    collection = models.ForeignKey(
        Collection,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="shares",
        verbose_name="Sammlung",
    )
    group = models.ForeignKey(
        "accounts.UserGroup",
        on_delete=models.CASCADE,
        related_name="shares",
        verbose_name="Gruppe",
    )
    can_write = models.BooleanField("Schreibrecht", default=False)
    created = models.DateTimeField("erstellt", auto_now_add=True)

    class Meta:
        ordering = ["-created"]
        verbose_name = "Freigabe"
        verbose_name_plural = "Freigaben"
        constraints = [
            models.CheckConstraint(
                condition=(Q(conversation__isnull=False) & Q(collection__isnull=True))
                | (Q(conversation__isnull=True) & Q(collection__isnull=False)),
                name="chat_share_exactly_one_target",
                violation_error_message="Freigabe gilt genau einem Chat oder einer Sammlung.",
            ),
            models.UniqueConstraint(
                fields=["conversation", "group"],
                condition=Q(conversation__isnull=False),
                name="chat_share_unique_conversation_group",
            ),
            models.UniqueConstraint(
                fields=["collection", "group"],
                condition=Q(collection__isnull=False),
                name="chat_share_unique_collection_group",
            ),
        ]

    def __str__(self):
        # Ohne Titel des Ziels: Der Admin zeigt Freigaben nur als Metadaten.
        target = (
            f"Chat {self.conversation_id}"
            if self.conversation_id
            else f"Sammlung {self.collection_id}"
        )
        return f"{target} → {self.group}"

    @property
    def target(self):
        return self.conversation if self.conversation_id else self.collection


class Document(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "wartet"
        INDEXED = "indexed", "indexiert"
        ERROR = "error", "Fehler"

    collection = models.ForeignKey(
        Collection, on_delete=models.CASCADE, related_name="documents", verbose_name="Sammlung"
    )
    file = models.FileField("Datei", upload_to=document_upload_to, max_length=255)
    title = models.CharField("Titel", max_length=300)
    status = models.CharField(
        "Status", max_length=20, choices=Status.choices, default=Status.PENDING
    )
    error_text = models.TextField("Fehlertext", blank=True)
    created = models.DateTimeField("hochgeladen", auto_now_add=True)

    class Meta:
        ordering = ["title"]
        verbose_name = "Dokument"
        verbose_name_plural = "Dokumente"

    def __str__(self):
        return self.title


# --- Werkzeugaufrufe, Quellen, Hintergrundjobs -------------------------------


class ToolCall(models.Model):
    class Status(models.TextChoices):
        AWAITING_CONFIRMATION = "awaiting_confirmation", "wartet auf Bestätigung"
        REJECTED = "rejected", "abgelehnt"
        RUNNING = "running", "läuft"
        OK = "ok", "erfolgreich"
        ERROR = "error", "Fehler"
        TIMEOUT = "timeout", "Zeitüberschreitung"

    message = models.ForeignKey(
        Message, on_delete=models.CASCADE, related_name="tool_calls", verbose_name="Nachricht"
    )
    # SET_NULL: Das Protokoll bleibt erhalten, auch wenn der Server entfernt wird.
    server = models.ForeignKey(
        McpServer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tool_calls",
        verbose_name="MCP-Server",
    )
    tool = models.CharField("Werkzeug", max_length=200)
    provider_call_id = models.CharField(
        "Aufruf-ID des Anbieters",
        max_length=200,
        blank=True,
        help_text="Kennung des Werkzeugaufrufs beim KI-Anbieter (ordnet das Ergebnis zu).",
    )
    arguments = models.JSONField("Argumente", default=dict, blank=True, encoder=UnicodeJSONEncoder)
    result = models.JSONField("Ergebnis", null=True, blank=True, encoder=UnicodeJSONEncoder)
    status = models.CharField(
        "Status", max_length=30, choices=Status.choices, default=Status.RUNNING
    )
    duration = models.DurationField("Dauer", null=True, blank=True)
    created = models.DateTimeField("erstellt", auto_now_add=True)

    class Meta:
        ordering = ["created", "id"]
        verbose_name = "Werkzeugaufruf"
        verbose_name_plural = "Werkzeugaufrufe"

    def __str__(self):
        return f"{self.tool} ({self.get_status_display()})"

    # Hilfen für die Anzeige (Templates, API).

    @property
    def result_text(self) -> str:
        """Ergebnistext (``result = {"text": str, "is_error": bool}``), sonst leer."""
        if isinstance(self.result, dict):
            return str(self.result.get("text") or "")
        return ""

    @property
    def result_is_error(self) -> bool:
        return isinstance(self.result, dict) and bool(self.result.get("is_error"))

    @property
    def duration_ms(self) -> int | None:
        if self.duration is None:
            return None
        return int(self.duration.total_seconds() * 1000)

    @property
    def arguments_json(self) -> str:
        """Argumente als eingerücktes JSON (Escapen übernimmt das Template)."""
        return json.dumps(self.arguments, ensure_ascii=False, indent=2, sort_keys=True)


class SourceRef(models.Model):
    """Quellenangabe einer Antwort.

    Das Feld ``chunk`` (Verweis auf den Textabschnitt bei Dokumentquellen) kommt
    mit dem Modell Chunk in M7 als echter Fremdschlüssel dazu.
    """

    class Kind(models.TextChoices):
        WEB = "web", "Webseite"
        DOCUMENT = "document", "Dokument"

    message = models.ForeignKey(
        Message, on_delete=models.CASCADE, related_name="sources", verbose_name="Nachricht"
    )
    kind = models.CharField("Art", max_length=10, choices=Kind.choices)
    title = models.CharField("Titel", max_length=500, blank=True)
    url = models.URLField("URL", max_length=2000, blank=True)

    class Meta:
        ordering = ["id"]
        verbose_name = "Quelle"
        verbose_name_plural = "Quellen"

    def __str__(self):
        return self.title or self.url or f"Quelle {self.pk}"


class Job(models.Model):
    class Kind(models.TextChoices):
        INDEX_DOCUMENT = "index_document", "Dokument indexieren"

    class Status(models.TextChoices):
        PENDING = "pending", "wartet"
        RUNNING = "running", "läuft"
        DONE = "done", "erledigt"
        FAILED = "failed", "fehlgeschlagen"

    kind = models.CharField("Art", max_length=50, choices=Kind.choices)
    payload = models.JSONField("Daten", default=dict, blank=True)
    status = models.CharField(
        "Status", max_length=20, choices=Status.choices, default=Status.PENDING
    )
    attempts = models.PositiveSmallIntegerField("Versuche", default=0)
    created = models.DateTimeField("erstellt", auto_now_add=True)

    class Meta:
        ordering = ["created", "id"]
        verbose_name = "Hintergrundjob"
        verbose_name_plural = "Hintergrundjobs"
        indexes = [
            models.Index(fields=["status", "created"], name="chat_job_status_created"),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} #{self.pk} ({self.get_status_display()})"
