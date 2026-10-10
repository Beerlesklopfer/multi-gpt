"""Datenmodell der App chat (Plan Abschnitt 6).

Bezeichner sind englisch, verbose_name und Choice-Labels deutsch (Plan 2).
"""

import json
import secrets
from datetime import timedelta
from decimal import Decimal
from pathlib import PurePath
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector, SearchVectorField
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone
from pgvector.django import HnswIndex, VectorField

from multigpt.core.fields import EncryptedTextField, UnicodeJSONEncoder

from . import citations

# Geldbeträge (Preise und Kosten-Momentaufnahmen) mit Bruchteilen von Cent.
MONEY = {"max_digits": 12, "decimal_places": 6}

# Feste Dimension der Abschnittsvektoren (RAG, M7), siehe settings.
EMBEDDING_DIMENSIONS = settings.RAG_EMBEDDING_DIMENSIONS
# Textsuchkonfiguration für die Volltextsuche (Dokumente überwiegend deutsch).
SEARCH_CONFIG = "german"


def _random_name(filename: str) -> str:
    """Zufälliger Dateiname; vom Original bleibt nur eine harmlose Endung.

    Plan 9: Dateinamen von Uploads werden nicht übernommen.
    """
    suffix = PurePath(filename or "").suffix.lower()
    if not (2 <= len(suffix) <= 10 and suffix[1:].isascii() and suffix[1:].isalnum()):
        suffix = ""
    return secrets.token_hex(16) + suffix


def _attachment_user_id(instance) -> int | None:
    """Besitzer eines Anhangs: ``owner`` (Uploads), sonst Besitzer des Chats."""
    if instance.owner_id is not None:
        return instance.owner_id
    return instance.message.conversation.user_id


def attachment_upload_to(instance, filename: str) -> str:
    """attachments/<Nutzer-ID>/<Zufall>.<endung>"""
    return f"attachments/{_attachment_user_id(instance)}/{_random_name(filename)}"


def attachment_thumbnail_upload_to(instance, filename: str) -> str:
    """attachments/<Nutzer-ID>/thumbs/<Zufall>.<endung>"""
    return f"attachments/{_attachment_user_id(instance)}/thumbs/{_random_name(filename)}"


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
    # Kontenrahmen (multigpt/billing): Jeder Anbieter gehört zu genau einem
    # Abrechnungskonto. Leer gespeichert -> Vorbelegung (billing.signals).
    billing_account = models.ForeignKey(
        "billing.BillingAccount",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="providers",
        verbose_name="Abrechnungskonto",
        help_text="Leer = automatisch: lokal „Lokale Modelle“ (Tokens), sonst ein Konto "
        "mit dem Namen des Anbieters (USD). Preise: je Modell unter „Modellpreise“.",
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
        IMAGE = "image", "Bilderzeugung"  # M9-01
        EMBEDDING = "embedding", "Embedding"
        STT = "stt", "Spracherkennung"  # M10-01
        TTS = "tts", "Sprachausgabe"  # M10-02
        MUSIC = "music", "Musik"  # M11
        # Reine Texterkennung (olmOCR): nur für die OCR der Dokumentsuche, nie im Chat.
        OCR = "ocr", "Texterkennung (OCR)"

    class McpAccess(models.TextChoices):
        # Welche MCP-Server das Modell nutzen darf, zusätzlich zur Rolle des
        # Nutzers (Schnittmenge). Eingebaute Werkzeuge (Websuche, Dokumente)
        # hängen nicht daran; die steuert ``supports_tools``.
        NONE = "none", "kein"
        ALL = "all", "alle"
        SELECTED = "selected", "ausgewählte"

    provider = models.ForeignKey(
        Provider, on_delete=models.CASCADE, related_name="ai_models", verbose_name="Anbieter"
    )
    model_id = models.CharField(
        "Modell-ID", max_length=200, help_text="Bezeichnung beim Anbieter, z. B. gpt-4o"
    )
    display_name = models.CharField("Anzeigename", max_length=200)
    capability = models.CharField(
        "Fähigkeit",
        max_length=20,
        choices=Capability.choices,
        default=Capability.CHAT,
        help_text="Hauptart. Nur Chat-Modelle erscheinen in der Chat-Auswahl.",
    )
    supports_tools = models.BooleanField(
        "Werkzeuge",
        default=False,
        help_text="Kann Werkzeuge aufrufen (Function Calling): Websuche als Werkzeug, "
        "Dokumentsuche, MCP-Server.",
    )
    supports_vision = models.BooleanField(
        "Bilder verstehen",
        default=False,
        help_text="Bilder im Chat dürfen an dieses Modell gehen (Bild-Eingabe).",
    )
    can_edit_images = models.BooleanField(
        "Bilder bearbeiten",
        default=False,
        help_text="Kann vorhandene Bilder bearbeiten bzw. Varianten erzeugen (M9-02).",
    )
    mcp_access = models.CharField(
        "MCP",
        max_length=10,
        choices=McpAccess.choices,
        default=McpAccess.NONE,
        help_text="Welche MCP-Server das Modell nutzen darf (zusätzlich zu den Rechten "
        "der Rolle). „kein“ trennt nicht vertrauenswürdige Modelle von Werkzeugen, "
        "die etwas verändern können.",
    )
    mcp_servers = models.ManyToManyField(
        "McpServer",
        blank=True,
        related_name="allowed_models",
        verbose_name="erlaubte MCP-Server",
        help_text="Nur bei MCP „ausgewählte“ wirksam.",
    )
    active = models.BooleanField("aktiv", default=True)
    sort_order = models.PositiveIntegerField("Reihenfolge", default=0)
    # Preise: billing.ModelPrice (Preisversionen je Modell, Währung des Kontos).

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

    def allowed_mcp_ids(self) -> set[int] | None:
        """Vom Verwalter erlaubte MCP-Server: ``None`` = alle (Rolle entscheidet),
        sonst die Menge der IDs (leer bei „kein“). Liest jedes Mal neu aus der DB:
        Ein Entzug wirkt auch auf laufende Antworten und spätere Bestätigungen."""
        access = (
            AIModel.objects.filter(pk=self.pk).values_list("mcp_access", flat=True).first()
            if self.pk
            else self.mcp_access
        )
        if access == self.McpAccess.ALL:
            return None
        if access == self.McpAccess.SELECTED:
            return set(
                AIModel.mcp_servers.through.objects.filter(aimodel_id=self.pk).values_list(
                    "mcpserver_id", flat=True
                )
            )
        return set()


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
    # Ergebnis der letzten Prüfung (verbinden, initialize, tools/list), siehe
    # chat/mcp/status.py. In der DB, damit es für alle gunicorn-Worker gilt.
    online = models.BooleanField("online", default=False, editable=False)
    last_checked = models.DateTimeField("zuletzt geprüft", null=True, blank=True, editable=False)
    last_online = models.DateTimeField("zuletzt online", null=True, blank=True, editable=False)
    last_error = models.TextField(
        "Fehlerursache",
        blank=True,
        default="",
        editable=False,
        help_text="Ursache, falls die letzte Prüfung fehlschlug (ohne Zugangsdaten); sonst leer.",
    )
    reported_tools = models.JSONField(
        "gemeldete Werkzeuge",
        default=list,
        blank=True,
        editable=False,
        help_text="Werkzeuge der letzten erfolgreichen Prüfung: Name, gekürzte Beschreibung, "
        "Parameternamen und Hinweise (annotations) des Servers.",
    )
    tools_checked = models.DateTimeField(
        "zuletzt eingestuft",
        null=True,
        blank=True,
        editable=False,
        help_text="Zeitpunkt, zu dem ein Verwalter die Einstufung der Werkzeuge zuletzt "
        "geändert hat.",
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


# --- Chat-Einstellungen --------------------------------------------------------

# Standard der Grundregeln. Neue Installationen und Updates bekommen ihn per
# Datenmigration (chat 0024, dort als Kopie des Textes); danach gilt nur noch
# der Wert in der DB, den Verwalter ändern oder leeren können.
DEFAULT_BASE_INSTRUCTIONS = (
    "Erfinde keine URLs, Zahlen, Namen, Ereignisse oder Zitate. Nenne Links nur, wenn sie "
    "aus Quellmaterial oder Werkzeugergebnissen stammen. Wenn du etwas nicht überprüfen "
    "kannst, sag das ausdrücklich und biete an, mit der Websuche nachzusehen."
)


class ReasoningEffort(models.TextChoices):
    """Denktiefe (chat/reasoning.py, Abbildung je Modell in capabilities.py)."""

    OFF = "off", "Aus/minimal"
    LOW = "low", "Niedrig"
    MEDIUM = "medium", "Mittel"
    HIGH = "high", "Hoch"
    XHIGH = "xhigh", "Sehr hoch"
    MAX = "max", "Maximal"


class ChatSettings(models.Model):
    """Allgemeine Chat-Einstellungen – genau ein Datensatz (pk=1), siehe ``load()``."""

    SINGLETON_PK = 1

    base_instructions = models.TextField(
        "Grundregeln für alle Modelle",
        blank=True,
        default=DEFAULT_BASE_INSTRUCTIONS,
        help_text="Gehen bei jedem Chat an jedes Modell, als erster Teil des System-Prompts – "
        "vor den Hinweisen von MultiGPT, den Projekt-Anweisungen und dem System-Prompt des "
        "Chats. Nutzer sehen sie im Chat unter „System-Prompt“ (nur lesend). Leer = keine "
        "Grundregeln. Bei zu ausschweifenden Antworten hilft z. B. der Zusatz „Antworte "
        "sachlich und knapp. Wenn du etwas vermutest, kennzeichne es als Vermutung.“",
    )
    # Kreativität (Temperatur, chat/creativity.py): Standard für Chats ohne
    # eigene Wahl bzw. Projektvorgabe. Leer = Standard des Anbieters.
    default_temperature = models.DecimalField(
        "Standard-Kreativität (Temperatur)",
        max_digits=3,
        decimal_places=2,
        null=True,
        blank=True,
        default=Decimal("0.3"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("2"))],
        help_text="0 bis 2; niedrig = sachlich und wiederholbar, hoch = abwechslungsreicher, "
        "aber eher erfunden. 0,3 ist sachlich. Gilt für Chats ohne eigene Wahl unter "
        "„Kreativität“ und ohne Projektvorgabe. Leer = Standard des Anbieters (oft 0,7–1,0). "
        "Modelle, die keine Temperatur annehmen (z. B. OpenAI o-Serie und GPT-5, neuere "
        "Claude-Modelle), bekommen keine.",
    )
    # Denktiefe (chat/reasoning.py): Standard für Chats ohne eigene Wahl bzw.
    # Projektvorgabe. Leer = Standard des Anbieters.
    default_reasoning_effort = models.CharField(
        "Standard-Denktiefe",
        max_length=10,
        blank=True,
        default="",
        choices=ReasoningEffort.choices,
        help_text="Wie gründlich Modelle mit Reasoning vor der Antwort nachdenken. Höher = "
        "gründlicher, aber langsamer und teurer (Denk-Tokens werden als Ausgabe berechnet). "
        "Gilt für Chats ohne eigene Wahl unter „Denktiefe“ und ohne Projektvorgabe. Leer = "
        "Standard des Anbieters. Modelle ohne Reasoning bekommen nichts; nicht unterstützte "
        "Stufen werden auf die nächste passende abgebildet.",
    )
    # Bilderzeugung (M9-01, chat/images.py).
    default_image_model = models.ForeignKey(
        AIModel,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        limit_choices_to={"capability": "image"},
        verbose_name="Standard-Bildmodell",
        help_text="Für das Werkzeug „Bild erzeugen“ und den Modus „Bild“. Leer bzw. nicht "
        "nutzbar (Rolle, Budget, offline) = das erste freigegebene Bildmodell.",
    )
    image_tool_confirm = models.BooleanField(
        "Rückfrage vor Bilderzeugung",
        default=False,
        help_text="Chatmodelle fragen vor jedem Bild über das Werkzeug nach (Bilder kosten "
        "Geld). Aus: ohne Rückfrage, das Budget wird trotzdem vor jedem Bild geprüft.",
    )
    # Berechnungen (M4a-10, chat/tools_python.py, chat/sandbox.py). Grenzen wie
    # sandbox.LIMIT_RANGES; die Sandbox begrenzt zusätzlich selbst.
    python_enabled = models.BooleanField(
        "Berechnungen aktiv",
        default=True,
        help_text="Werkzeug run_python für Modelle mit Werkzeugen (Recht „Berechnungen "
        "ausführen“). Wird nur angeboten, wenn die Sandbox (bubblewrap) funktioniert.",
    )
    python_confirm = models.BooleanField(
        "Rückfrage vor Berechnungen",
        default=False,
        help_text="Chatmodelle fragen vor jeder Ausführung nach. Aus: ohne Rückfrage – der "
        "Code läuft ohnehin abgeschottet (kein Netz, keine Server-Dateien).",
    )
    python_cpu_seconds = models.PositiveSmallIntegerField(
        "CPU-Zeit (s)",
        default=10,
        validators=[MinValueValidator(1), MaxValueValidator(60)],
    )
    python_wall_seconds = models.PositiveSmallIntegerField(
        "Laufzeit gesamt (s)",
        default=20,
        validators=[MinValueValidator(2), MaxValueValidator(120)],
        help_text="Danach wird der Lauf abgebrochen (Wanduhr, auch beim Warten).",
    )
    python_memory_mb = models.PositiveIntegerField(
        "Speicher je Prozess (MB)",
        default=512,
        validators=[MinValueValidator(128), MaxValueValidator(4096)],
    )
    python_processes = models.PositiveSmallIntegerField(
        "Prozesse und Threads",
        default=4,
        validators=[MinValueValidator(2), MaxValueValidator(64)],
        help_text="Einschließlich Python selbst und dem Start-Prozess der Sandbox.",
    )
    python_file_mb = models.PositiveSmallIntegerField(
        "Dateigröße (MB)",
        default=10,
        validators=[MinValueValidator(1), MaxValueValidator(100)],
        help_text="Höchstgröße je Datei im Arbeitsordner.",
    )
    python_output_kb = models.PositiveSmallIntegerField(
        "Ausgabe (KB)",
        default=64,
        validators=[MinValueValidator(4), MaxValueValidator(1024)],
        help_text="So viel Text (stdout/stderr) geht an das Modell; der Rest wird gekürzt.",
    )

    class Meta:
        verbose_name = "Chat-Einstellungen"
        verbose_name_plural = "Chat-Einstellungen"

    def __str__(self):
        return "Chat-Einstellungen"

    def save(self, *args, **kwargs):
        self.pk = self.SINGLETON_PK
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> "ChatSettings":
        obj, _ = cls.objects.get_or_create(pk=cls.SINGLETON_PK)
        return obj

    @classmethod
    def base_text(cls) -> str:
        """Grundregeln für den System-Prompt; ohne Datensatz der Standard."""
        obj = cls.objects.filter(pk=cls.SINGLETON_PK).only("base_instructions").first()
        text = obj.base_instructions if obj is not None else DEFAULT_BASE_INSTRUCTIONS
        return (text or "").strip()


# --- Websuche (Plan 8d, M8) --------------------------------------------------


def validate_service_url(value: str) -> None:
    """http(s)-URL mit Rechnernamen; einteilige Intranet-Namen sind erlaubt
    (``URLField`` verlangt eine Domainendung)."""
    try:
        parts = urlsplit(value)
        host = parts.hostname
        parts.port  # noqa: B018 - löst bei ungültigem Port ValueError aus
    except ValueError:
        host = None
        parts = None
    if parts is None or parts.scheme not in ("http", "https") or not host:
        raise ValidationError(
            "Bitte eine http- oder https-Adresse angeben, z. B. http://searx:8888"
        )
    if parts.username or parts.password:
        raise ValidationError("Bitte keine Zugangsdaten in die Adresse schreiben.")
    if parts.query or parts.fragment:
        raise ValidationError("Bitte nur die Basisadresse ohne ?… oder #… angeben.")


class SearchSettings(models.Model):
    """Einstellungen der Websuche – genau ein Datensatz (pk=1), siehe ``load()``.

    Die SearXNG-URL kommt nur aus dieser Verwalter-Konfiguration und ist vom
    SSRF-Schutz des Seitenabrufs ausgenommen (sie liegt im Intranet).
    """

    class Backend(models.TextChoices):
        SEARXNG = "searxng", "SearXNG"

    class SafeSearch(models.IntegerChoices):
        OFF = 0, "aus"
        MODERATE = 1, "mittel"
        STRICT = 2, "streng"

    SINGLETON_PK = 1

    enabled = models.BooleanField(
        "Websuche aktiv",
        default=False,
        help_text="Erst einschalten, wenn „SearXNG testen“ erfolgreich war.",
    )
    backend = models.CharField(
        "Such-Backend", max_length=20, choices=Backend.choices, default=Backend.SEARXNG
    )
    searxng_url = models.CharField(
        "SearXNG-URL",
        max_length=500,
        blank=True,
        validators=[validate_service_url],
        help_text="Basisadresse der SearXNG-Instanz im Intranet, z. B. http://searx.intern:8888. "
        "SearXNG muss das JSON-Format erlauben (settings.yml: search.formats: [html, json]).",
    )
    max_results = models.PositiveSmallIntegerField(
        "Trefferzahl",
        default=5,
        validators=[MinValueValidator(1), MaxValueValidator(20)],
        help_text="So viele Treffer gehen als nummerierte Quellen an das Modell.",
    )
    fetch_pages = models.PositiveSmallIntegerField(
        "Seiten abrufen",
        default=3,
        validators=[MinValueValidator(0), MaxValueValidator(10)],
        help_text="Von so vielen der besten Treffer wird der Seitentext abgerufen; "
        "von den übrigen nur der Kurztext der Suche. 0 = nur Kurztexte.",
    )
    timeout_seconds = models.PositiveSmallIntegerField(
        "Zeitlimit (s)",
        default=10,
        validators=[MinValueValidator(1), MaxValueValidator(60)],
        help_text="Höchstdauer für die Suche und je abgerufener Seite.",
    )
    language = models.CharField(
        "Sprache",
        max_length=20,
        default="de",
        help_text="Sprachcode für SearXNG, z. B. de, en, de-DE oder all.",
    )
    safesearch = models.PositiveSmallIntegerField(
        "Jugendschutzfilter",
        choices=SafeSearch.choices,
        default=SafeSearch.MODERATE,
        help_text="SafeSearch der Suchmaschinen (soweit sie es unterstützen).",
    )
    # Seiten abrufen und Websites durchsuchen (eingebaute Werkzeuge, websearch.pages).
    fetch_url_enabled = models.BooleanField(
        "Seiten abrufen (fetch_url)",
        default=True,
        help_text="Modelle mit Werkzeugen dürfen einzelne Webseiten lesen; im festen Ablauf "
        "werden URLs aus der Frage mit abgerufen.",
    )
    crawl_enabled = models.BooleanField(
        "Websites durchsuchen (crawl_site)",
        default=True,
        help_text="Modelle mit Werkzeugen dürfen Links einer Website folgen (robots.txt wird "
        "beachtet).",
    )
    crawl_max_pages = models.PositiveSmallIntegerField(
        "Seiten je Durchsuchung",
        default=10,
        validators=[MinValueValidator(1), MaxValueValidator(20)],
        help_text="Obergrenze für crawl_site (höchstens 20, Linktiefe höchstens 2).",
    )
    crawl_time_seconds = models.PositiveSmallIntegerField(
        "Zeitlimit Durchsuchung (s)",
        default=30,
        validators=[MinValueValidator(5), MaxValueValidator(120)],
        help_text="Gesamtdauer einer Durchsuchung; danach zählen die bis dahin gelesenen Seiten.",
    )
    blocked_domains = models.TextField(
        "Gesperrte Domains",
        blank=True,
        help_text="Eine Domain je Zeile, Subdomains eingeschlossen (example.com sperrt auch "
        "www.example.com). Gilt für fetch_url, crawl_site und den Abruf von Suchtreffern.",
    )

    class Meta:
        verbose_name = "Sucheinstellungen"
        verbose_name_plural = "Sucheinstellungen"

    def __str__(self):
        return "Sucheinstellungen"

    def save(self, *args, **kwargs):
        self.pk = self.SINGLETON_PK
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> "SearchSettings":
        obj, _ = cls.objects.get_or_create(pk=cls.SINGLETON_PK)
        return obj

    @property
    def is_ready(self) -> bool:
        """Eingeschaltet und eingerichtet."""
        return self.enabled and bool(self.searxng_url.strip())


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
    # Kreativität (chat/creativity.py); leer = Projektvorgabe bzw. Chat-Einstellungen.
    temperature = models.DecimalField(
        "Kreativität (Temperatur)",
        max_digits=3,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("2"))],
    )
    # Denktiefe (chat/reasoning.py); leer = Projektvorgabe bzw. Chat-Einstellungen.
    reasoning_effort = models.CharField(
        "Denktiefe", max_length=10, blank=True, default="", choices=ReasoningEffort.choices
    )
    # Projekt des Besitzers (chat/projects.py); gelöschtes Projekt -> Chat ohne Projekt.
    project = models.ForeignKey(
        "Project",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="conversations",
        verbose_name="Projekt",
    )
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
    # Geteilte Chats: wer die Nachricht geschrieben bzw. die Antwort ausgelöst
    # hat. Bestimmt Anzeige, Rückfragen und das Konto, auf das die Kosten
    # gehen (usage.py). Leer bei Altdaten = Besitzer des Chats.
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        editable=False,
        verbose_name="Verfasser",
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
            # Budget je Absender in geteilten Chats.
            models.Index(fields=["author", "created"], name="chat_msg_author_created"),
        ]

    def __str__(self):
        return f"{self.get_role_display()} #{self.pk}"

    @property
    def notices(self) -> dict:
        """Hinweise der festen Abläufe an den Nutzer, z. B. {"web_search": "…"}."""
        state = self.tool_state if isinstance(self.tool_state, dict) else {}
        notices = state.get("notices")
        return {str(k): str(v) for k, v in notices.items()} if isinstance(notices, dict) else {}

    @property
    def web_search_notice(self) -> str:
        """Hinweis zur Websuche dieser Antwort (z. B. Suche fehlgeschlagen), sonst leer."""
        return self.notices.get("web_search", "")


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

    # Leer bei Entwürfen: Hochgeladen, aber noch nicht mit einer Nachricht gesendet.
    message = models.ForeignKey(
        Message,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="attachments",
        verbose_name="Nachricht",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="attachments",
        verbose_name="Besitzer",
        help_text="Wer die Datei hochgeladen hat (bei erzeugten Dateien leer).",
    )
    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Chat (beim Hochladen)",
    )
    kind = models.CharField("Art", max_length=10, choices=Kind.choices)
    file = models.FileField("Datei", upload_to=attachment_upload_to, max_length=255)
    thumbnail = models.FileField(
        "Vorschaubild", upload_to=attachment_thumbnail_upload_to, max_length=255, blank=True
    )
    original_name = models.CharField(
        "Dateiname", max_length=255, blank=True, help_text="Nur zur Anzeige, bereinigt."
    )
    mime_type = models.CharField("Inhaltstyp", max_length=100, blank=True)
    size = models.PositiveBigIntegerField("Größe (Bytes)", default=0)
    width = models.PositiveIntegerField("Breite", null=True, blank=True)
    height = models.PositiveIntegerField("Höhe", null=True, blank=True)
    extracted_text = models.TextField(
        "Extrahierter Text", blank=True, help_text="Text von Dokumenten (gekürzt)."
    )
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
        indexes = [
            # Aufräumen alter Entwürfe (ohne Nachricht).
            models.Index(
                fields=["created"],
                condition=Q(message__isnull=True),
                name="chat_attachment_draft_created",
            ),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} #{self.pk}"

    @property
    def is_image(self) -> bool:
        return self.kind == self.Kind.IMAGE

    @property
    def is_draft(self) -> bool:
        return self.message_id is None

    @property
    def display_name(self) -> str:
        """Anzeigename: bereinigter Originalname, sonst Art und Nummer."""
        return self.original_name or f"{self.get_kind_display()} {self.pk}"

    @property
    def url(self) -> str:
        from django.urls import reverse

        return reverse("chat:attachment", args=[self.pk])

    @property
    def thumbnail_url(self) -> str:
        from django.urls import reverse

        if not self.thumbnail:
            return ""
        return reverse("chat:attachment_thumb", args=[self.pk])

    @property
    def size_label(self) -> str:
        """Größe für Menschen, z. B. „1,2 MB“."""
        size = int(self.size or 0)
        if size < 1024:
            return f"{size} B"
        for unit in ("KB", "MB", "GB"):
            size_f = size / 1024
            if size_f < 1024 or unit == "GB":
                return f"{size_f:.1f} {unit}".replace(".", ",")
            size = size_f
        return ""


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
    """Freigabe eines Chats oder einer Sammlung an eine Gruppe oder ein Konto.

    Ziel als zwei nullable Fremdschlüssel; ein CheckConstraint erzwingt, dass
    genau eines gesetzt ist (referenzielle Integrität statt GenericForeignKey).
    Empfänger genauso: genau eines von ``group`` und ``user``. Sammlungen
    werden nur an Gruppen geteilt (Constraint), Chats an Gruppen oder Konten.

    Rechte (RWUD, Lesen ist immer enthalten): ``can_write`` (W), bei Chats
    zusätzlich ``can_update`` (U: Nachrichten bearbeiten, umbenennen,
    System-Prompt) und ``can_delete`` (D: archivieren, löschen – für alle).
    Bei Sammlungen gelten U und D nicht (nur der Besitzer). ``left_by``: Konten,
    die einen an ihre Gruppe geteilten Chat „aus ihrer Liste entfernt“ haben;
    für sie gilt diese Freigabe nicht mehr (siehe chat/sharing.py).
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
        null=True,
        blank=True,
        related_name="shares",
        verbose_name="Gruppe",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="received_shares",
        verbose_name="Konto",
    )
    can_write = models.BooleanField("Schreibrecht", default=False)
    can_update = models.BooleanField("Bearbeiten", default=False)
    can_delete = models.BooleanField("Löschen", default=False)
    left_by = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="+",
        verbose_name="Aus der Liste entfernt von",
    )
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
            models.CheckConstraint(
                condition=(Q(group__isnull=False) & Q(user__isnull=True))
                | (Q(group__isnull=True) & Q(user__isnull=False)),
                name="chat_share_exactly_one_recipient",
                violation_error_message="Freigabe gilt genau einer Gruppe oder einem Konto.",
            ),
            models.CheckConstraint(
                condition=Q(collection__isnull=True) | Q(group__isnull=False),
                name="chat_share_collection_group_only",
                violation_error_message="Sammlungen werden nur an Gruppen geteilt.",
            ),
            models.UniqueConstraint(
                fields=["conversation", "group"],
                condition=Q(conversation__isnull=False),
                name="chat_share_unique_conversation_group",
            ),
            models.UniqueConstraint(
                fields=["conversation", "user"],
                condition=Q(conversation__isnull=False, user__isnull=False),
                name="chat_share_unique_conversation_user",
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
        return f"{target} → {self.recipient}"

    @property
    def target(self):
        return self.conversation if self.conversation_id else self.collection

    @property
    def recipient(self):
        """Gruppe oder Konto, an das freigegeben ist."""
        return self.group if self.group_id else self.user

    @property
    def rights_label(self) -> str:
        """Kurzform der Rechte, z. B. „R“, „RW“, „RWUD“."""
        flags = (("W", self.can_write), ("U", self.can_update), ("D", self.can_delete))
        return "R" + "".join(letter for letter, on in flags if on)


class ConversationView(models.Model):
    """Angezeigter Zweig eines geteilten Chats je Empfänger (current_leaf je Konto).

    Der Besitzer sieht weiter ``Conversation.current_leaf`` (Hauptpfad). Ein
    Empfänger ohne Eintrag folgt dem Hauptpfad; wer zu einer anderen Version
    umschaltet oder auf einem Nebenzweig schreibt, bekommt einen Eintrag.
    Umschalten ändert so nie die Ansicht der anderen (auch nur lesend möglich).
    """

    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="views", verbose_name="Chat"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="+",
        verbose_name="Konto",
    )
    current_leaf = models.ForeignKey(
        Message,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="angezeigte Version",
    )

    class Meta:
        verbose_name = "Ansicht eines geteilten Chats"
        verbose_name_plural = "Ansichten geteilter Chats"
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "user"], name="chat_conversationview_unique"
            ),
        ]

    def __str__(self):
        return f"Chat {self.conversation_id} / Konto {self.user_id}"


class Document(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "wartet"
        INDEXED = "indexed", "indexiert"
        ERROR = "error", "Fehler"

    collection = models.ForeignKey(
        Collection, on_delete=models.CASCADE, related_name="documents", verbose_name="Sammlung"
    )
    file = models.FileField("Datei", upload_to=document_upload_to, max_length=255, blank=True)
    title = models.CharField("Titel", max_length=300)
    status = models.CharField(
        "Status", max_length=20, choices=Status.choices, default=Status.PENDING
    )
    error_text = models.TextField("Fehlertext", blank=True)
    created = models.DateTimeField("hochgeladen", auto_now_add=True)
    # Verzeichnisquelle (Agent crawler): Die Datei bleibt auf dem Server/NAS,
    # ``file`` ist dann leer; gelesen wird über source.path + source_path.
    source = models.ForeignKey(
        "rag.DirectorySource",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="documents",
        verbose_name="Verzeichnisquelle",
    )
    source_path = models.CharField("Pfad in der Quelle", max_length=1000, blank=True)
    source_mtime = models.DateTimeField("geändert (Quelle)", null=True, blank=True)
    source_size = models.BigIntegerField("Größe (Quelle)", null=True, blank=True)
    source_sha256 = models.CharField("SHA-256 (Quelle)", max_length=64, blank=True)
    # Literaturangaben fürs Zitieren (multigpt.chat.citations). Beim Indexieren
    # aus PDF-/DOCX-Metadaten vorbelegt, solange ``bib_edited`` nicht gesetzt ist.
    bib_type = models.CharField(
        "Art", max_length=10, choices=citations.TYPE_CHOICES, default=citations.TYPE_OTHER
    )
    bib_authors = models.TextField(
        "Autor(en)",
        blank=True,
        help_text="Eine Person je Zeile als „Nachname, Vorname“; Körperschaften ohne Komma.",
    )
    bib_title = models.CharField(
        "Titel (Zitat)", max_length=500, blank=True, help_text="Leer = Titel des Dokuments."
    )
    bib_date = models.CharField(
        "Jahr bzw. Datum",
        max_length=10,
        blank=True,
        validators=[citations.validate_bib_date],
        help_text="JJJJ, JJJJ-MM oder JJJJ-MM-TT.",
    )
    bib_publisher = models.CharField("Verlag/Herausgeber", max_length=300, blank=True)
    bib_place = models.CharField("Ort", max_length=200, blank=True)
    bib_edition = models.CharField("Auflage", max_length=50, blank=True)
    bib_url = models.URLField("URL", max_length=2000, blank=True)
    bib_isbn = models.CharField(
        "ISBN", max_length=20, blank=True, validators=[citations.validate_isbn]
    )
    bib_doi = models.CharField(
        "DOI", max_length=200, blank=True, validators=[citations.validate_doi]
    )
    bib_journal = models.CharField("Zeitschrift", max_length=300, blank=True)
    bib_volume = models.CharField("Band", max_length=30, blank=True)
    bib_issue = models.CharField("Heft", max_length=30, blank=True)
    bib_pages = models.CharField(
        "Seitenbereich",
        max_length=30,
        blank=True,
        validators=[citations.validate_pages],
        help_text="Artikel bzw. Kapitel im Sammelwerk, z. B. 45–67.",
    )
    bib_editors = models.TextField(
        "Herausgeber (Hrsg.)",
        blank=True,
        help_text="Eine Person je Zeile als „Nachname, Vorname“ (Sammelband, Kapitel).",
    )
    bib_container = models.CharField(
        "Sammelwerk/Tagungsband",
        max_length=500,
        blank=True,
        help_text="Buchtitel beim Kapitel bzw. Titel der Proceedings.",
    )
    bib_series = models.CharField("Reihe", max_length=300, blank=True)
    bib_series_number = models.CharField("Band der Reihe", max_length=30, blank=True)
    bib_isbn_e = models.CharField(
        "ISBN (eBook)", max_length=20, blank=True, validators=[citations.validate_isbn]
    )
    bib_number = models.CharField(
        "Normnummer", max_length=100, blank=True, help_text="z. B. DIN EN ISO 9001"
    )
    bib_institution = models.CharField(
        "Herausgebende Stelle",
        max_length=300,
        blank=True,
        help_text="Norm: DIN, ISO, IEC …; Bericht: Institution.",
    )
    bib_status = models.CharField(
        "Status (Norm)", max_length=10, choices=citations.STATUS_CHOICES, blank=True
    )
    bib_replaces = models.CharField("Ersatz für", max_length=300, blank=True)
    bib_edited = models.BooleanField(
        "Angaben von Hand gepflegt",
        default=False,
        help_text="Dann überschreibt die Indexierung die Angaben nicht mehr.",
    )
    # Bei der letzten Indexierung beschriebene Abbildungen (``rag.figures``).
    figures_described = models.PositiveIntegerField("Abbildungen beschrieben", default=0)

    class Meta:
        ordering = ["title"]
        verbose_name = "Dokument"
        verbose_name_plural = "Dokumente"
        constraints = [
            models.UniqueConstraint(
                fields=["source", "source_path"],
                condition=Q(source__isnull=False),
                name="chat_document_unique_source_path",
            ),
        ]

    def __str__(self):
        return self.title

    @property
    def from_source(self) -> bool:
        return self.source_id is not None


class Chunk(models.Model):
    """Textabschnitt eines Dokuments mit Vektor (Plan 8b, M7-04).

    ``heading`` ist der Kontextkopf des Abschnitts („Dokument: …“, bei
    Verzeichnisquellen „Pfad: …“, „Seite: …“, siehe ``rag.chunking.heading``).

    Fundstelle zum Zitieren: ``page``/``page_end`` (erste/letzte Seite) und
    ``paragraph``/``paragraph_end`` (Absatz des ersten/letzten Wortes, je Seite
    ab 1; ohne Seiten durchgehend). Ältere Abschnitte haben keine Absätze
    (NULL), bis das Dokument neu indexiert wird.
    Er wird beim Einbetten vor den Text gestellt; ``text`` bleibt unverändert.

    ``search_vector`` ist eine von PostgreSQL berechnete Spalte
    (``setweight(to_tsvector('german', coalesce(heading, '')), 'A') ||
    setweight(to_tsvector('german', coalesce(text, '')), 'B')``, GENERATED …
    STORED): Sie stimmt immer mit Kopf und Text überein, auch bei
    ``bulk_create``, ohne Trigger. ``ts_rank_cd`` gewichtet Treffer im Kopf
    (Titel, Pfad) höher als im Text.
    """

    document = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name="chunks", verbose_name="Dokument"
    )
    position = models.PositiveIntegerField("Position", help_text="Reihenfolge im Dokument, ab 0.")
    text = models.TextField("Text")
    heading = models.TextField(
        "Kontextkopf",
        blank=True,
        default="",
        help_text="Dokumentname, Pfad und Seite; wird mit eingebettet und durchsucht.",
    )
    page = models.PositiveIntegerField(
        "Seite",
        null=True,
        blank=True,
        help_text="Erste Seite des Abschnitts ab 1; leer bei Formaten ohne Seiten.",
    )
    page_end = models.PositiveIntegerField(
        "bis Seite", null=True, blank=True, help_text="Letzte Seite des Abschnitts."
    )
    paragraph = models.PositiveIntegerField(
        "Absatz",
        null=True,
        blank=True,
        help_text="Absatz des Abschnittsbeginns ab 1 (je Seite neu gezählt); leer bei "
        "Abschnitten aus der Zeit vor der Absatzzählung.",
    )
    paragraph_end = models.PositiveIntegerField("bis Absatz", null=True, blank=True)
    section = models.CharField(
        "Gliederungsabschnitt",
        max_length=30,
        blank=True,
        help_text="Nummer der am Abschnittsbeginn geltenden Überschrift, z. B. 7.5.3.",
    )
    section_title = models.CharField("Abschnittstitel", max_length=200, blank=True)
    section_end = models.CharField(
        "bis Gliederungsabschnitt",
        max_length=30,
        blank=True,
        help_text="Nur gesetzt, wenn der Abschnitt über eine Gliederungsgrenze reicht.",
    )
    embedding = VectorField("Vektor", dimensions=EMBEDDING_DIMENSIONS)
    search_vector = models.GeneratedField(
        expression=SearchVector("heading", config=SEARCH_CONFIG, weight="A")
        + SearchVector("text", config=SEARCH_CONFIG, weight="B"),
        output_field=SearchVectorField(),
        db_persist=True,
        verbose_name="Suchvektor",
    )

    class Meta:
        ordering = ["document", "position"]
        verbose_name = "Abschnitt"
        verbose_name_plural = "Abschnitte"
        constraints = [
            models.UniqueConstraint(
                fields=["document", "position"], name="chat_chunk_unique_document_position"
            ),
        ]
        indexes = [
            HnswIndex(
                name="chat_chunk_embedding_hnsw",
                fields=["embedding"],
                m=16,
                ef_construction=64,
                opclasses=["vector_cosine_ops"],
            ),
            GinIndex(fields=["search_vector"], name="chat_chunk_search_gin"),
        ]

    def __str__(self):
        # Ohne Text: privater Inhalt (Plan 6, Admin nur Metadaten).
        return f"Abschnitt {self.position} von Dokument {self.document_id}"


class RagSettings(models.Model):
    """Einstellungen für Sammlungen und Dokumentsuche – genau ein Datensatz (pk=1)."""

    SINGLETON_PK = 1

    class OcrBackend(models.TextChoices):
        OLMOCR = "olmocr", "olmOCR (Vision-Modell, z. B. über LM Studio)"
        TESSERACT = "tesseract", "Tesseract (auf dem Server)"

    embedding_model = models.ForeignKey(
        AIModel,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        limit_choices_to={"capability": AIModel.Capability.EMBEDDING},
        verbose_name="Embedding-Modell",
        help_text="Genau ein Modell, z. B. text-embedding-nomic-embed-text-v1.5 über "
        "LM Studio (768 Dimensionen). Nach einem Wechsel „Alles neu indexieren“.",
    )
    document_prefix = models.CharField(
        "Präfix für Abschnitte",
        max_length=100,
        blank=True,
        default="",
        help_text="Wird jedem Abschnitt beim Einbetten vorangestellt, bei nomic-embed "
        "„search_document: “ (laut Modellkarte). Wird bei Auswahl eines nomic-Modells "
        "vorbelegt.",
    )
    query_prefix = models.CharField(
        "Präfix für Suchanfragen",
        max_length=100,
        blank=True,
        default="",
        help_text="Wird jeder Suchanfrage beim Einbetten vorangestellt, bei nomic-embed "
        "„search_query: “.",
    )
    ocr_backend = models.CharField(
        "OCR-Verfahren",
        max_length=20,
        choices=OcrBackend.choices,
        default=OcrBackend.TESSERACT,
        help_text="Texterkennung für gescannte PDF-Seiten ohne Textebene.",
    )
    ocr_model = models.ForeignKey(
        AIModel,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="OCR-Modell",
        help_text="Vision-Modell für olmOCR, z. B. allenai/olmocr-2-7b in LM Studio. "
        "Wird auch genutzt, wenn es für den Chat deaktiviert ist.",
    )
    ocr_fallback_tesseract = models.BooleanField(
        "Tesseract als Ersatz",
        default=True,
        help_text="Ist das OCR-Modell nicht erreichbar, liest Tesseract die Seite. "
        "Sonst wartet die Indexierung, bis der Anbieter wieder erreichbar ist.",
    )
    # Abbildungen beschreiben (``rag.figures``): Bilder in PDF/DOCX und
    # Bilddateien gehen einzeln an ein Vision-Modell.
    describe_figures = models.BooleanField(
        "Abbildungen beschreiben",
        default=False,
        help_text="Bilder und Diagramme in PDF- und Word-Dokumenten sowie Bilddateien "
        "beschreibt ein Vision-Modell; die Beschreibung wird mit durchsucht. Kostet einen "
        "Modellaufruf je Abbildung (Dauer bzw. Gebühren). Lokales Modell (LM Studio): Die "
        "Bilder bleiben im Haus. Cloud-Modell: Die Bilder gehen an den Anbieter. Wirkt für "
        "vorhandene Dokumente erst nach „Alles neu indexieren“.",
    )
    figure_model = models.ForeignKey(
        AIModel,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Modell für Abbildungen",
        help_text="Allgemeines Vision-Modell, z. B. qwen/qwen3-vl-8b in LM Studio "
        "(olmOCR liest nur Text). Wird auch genutzt, wenn es für den Chat deaktiviert ist.",
    )
    figure_max_per_document = models.PositiveSmallIntegerField(
        "Abbildungen je Dokument (höchstens)",
        default=50,
        validators=[MinValueValidator(1), MaxValueValidator(1000)],
    )
    figure_max_per_page = models.PositiveSmallIntegerField(
        "Abbildungen je Seite (höchstens)",
        default=10,
        validators=[MinValueValidator(1), MaxValueValidator(100)],
    )
    figure_min_edge = models.PositiveSmallIntegerField(
        "Mindestgröße (Pixel)",
        default=150,
        validators=[MinValueValidator(16), MaxValueValidator(2000)],
        help_text="Kürzere Kante einer Abbildung; kleinere Bilder (Symbole, Logos) "
        "werden übersprungen, ebenso sehr schmale Linien und Wiederholungen.",
    )
    figure_max_edge = models.PositiveSmallIntegerField(
        "Bildgröße für das Modell (Pixel)",
        default=1024,
        validators=[MinValueValidator(256), MaxValueValidator(4096)],
        help_text="Längste Kante; größere Bilder werden vor dem Senden verkleinert.",
    )
    # Literaturangaben per DOI bei Crossref nachschlagen (``rag.crossref``).
    crossref_enabled = models.BooleanField(
        "Literaturangaben bei Crossref nachschlagen",
        default=False,
        help_text="Erkennt die Indexierung eine DOI, werden fehlende Angaben (Autoren, "
        "Sammelwerk, Verlag …) bei api.crossref.org abgefragt. Achtung: Crossref erfährt "
        "dabei, welche Dokumente hier liegen. Von Hand gepflegte Angaben bleiben unverändert.",
    )
    crossref_mailto = models.EmailField(
        "Kontakt-Adresse für Crossref",
        blank=True,
        help_text="Optional; Crossref bittet um eine Adresse im User-Agent („polite pool“).",
    )
    chunk_tokens = models.PositiveIntegerField(
        "Abschnittsgröße (Tokens)",
        default=800,
        validators=[MinValueValidator(100), MaxValueValidator(4000)],
    )
    overlap_tokens = models.PositiveIntegerField(
        "Überlappung (Tokens)",
        default=100,
        validators=[MinValueValidator(0), MaxValueValidator(1000)],
    )
    top_k = models.PositiveSmallIntegerField(
        "Treffer je Frage",
        default=6,
        validators=[MinValueValidator(1), MaxValueValidator(20)],
        help_text="So viele Abschnitte gehen als Quellen an das Modell.",
    )
    hybrid = models.BooleanField(
        "Volltextsuche dazunehmen",
        default=True,
        help_text="Vektorsuche und Volltextsuche (deutsch) zusammenführen "
        "(Reciprocal Rank Fusion).",
    )

    class Meta:
        verbose_name = "RAG-Einstellungen"
        verbose_name_plural = "RAG-Einstellungen"

    def __str__(self):
        return "RAG-Einstellungen"

    def save(self, *args, **kwargs):
        self.pk = self.SINGLETON_PK
        super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        model = self.embedding_model
        if model is not None:
            if model.capability != AIModel.Capability.EMBEDDING:
                raise ValidationError({"embedding_model": "Bitte ein Embedding-Modell wählen."})
        # Lokale Anbieter (LM Studio) sind ausdrücklich erlaubt (M7-09): Ist der
        # Anbieter offline, wartet die Indexierung, der Chat antwortet ohne Dokumente.
        if self.ocr_backend == self.OcrBackend.OLMOCR and self.ocr_model is None:
            raise ValidationError({"ocr_model": "Für olmOCR bitte ein Vision-Modell wählen."})
        if self.describe_figures:
            figure_model = self.figure_model
            if figure_model is None:
                message = "Zum Beschreiben der Abbildungen bitte ein Vision-Modell wählen."
                raise ValidationError({"figure_model": message})
            if figure_model.provider.kind != Provider.Kind.OPENAI_COMPAT:
                message = "Für Abbildungen wird ein OpenAI-kompatibler Anbieter benötigt."
                raise ValidationError({"figure_model": message})
        if self.overlap_tokens >= self.chunk_tokens:
            raise ValidationError(
                {"overlap_tokens": "Die Überlappung muss kleiner als die Abschnittsgröße sein."}
            )

    @classmethod
    def load(cls) -> "RagSettings":
        obj, _ = cls.objects.get_or_create(pk=cls.SINGLETON_PK)
        return obj


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
    """Quellenangabe einer Antwort; Nummer [n] = Reihenfolge (id) je Nachricht.

    Bei Dokumentquellen verweist ``chunk`` auf den Textabschnitt; Fundstelle
    (``page``/``page_end``, ``paragraph``/``paragraph_end``) und Literaturangaben
    (``biblio``) werden zusätzlich festgehalten, damit die Angabe nach Löschen
    oder Ändern des Dokuments bleibt.
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
    chunk = models.ForeignKey(
        Chunk,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Abschnitt",
    )
    page = models.PositiveIntegerField("Seite", null=True, blank=True)
    page_end = models.PositiveIntegerField("bis Seite", null=True, blank=True)
    paragraph = models.PositiveIntegerField("Absatz", null=True, blank=True)
    paragraph_end = models.PositiveIntegerField("bis Absatz", null=True, blank=True)
    section = models.CharField("Gliederungsabschnitt", max_length=30, blank=True)
    section_end = models.CharField("bis Gliederungsabschnitt", max_length=30, blank=True)
    biblio = models.JSONField(
        "Literaturangaben",
        default=dict,
        blank=True,
        encoder=UnicodeJSONEncoder,
        help_text="Stand der Dokumentangaben zur Zeit der Antwort (citations.Reference).",
    )

    class Meta:
        ordering = ["id"]
        verbose_name = "Quelle"
        verbose_name_plural = "Quellen"

    def __str__(self):
        return self.title or self.url or f"Quelle {self.pk}"


class IndexRun(models.Model):
    """Ein Lauf (Einlesen eines Verzeichnisses, Neuindexierung, Upload).

    Der Scan-Auftrag und alle Indexierungsaufträge, die er bzw. die Aktion
    erzeugt, hängen über ``Job.run`` am Lauf. Der Lauf endet („beendet“), wenn
    keiner seiner Aufträge mehr offen ist (``rag.jobs.check_run``). Abbrechen
    setzt „wird abgebrochen“: wartende Aufträge werden entfernt, laufende
    markiert; danach „abgebrochen“, die Zähler bleiben stehen.
    """

    class Kind(models.TextChoices):
        DIRECTORY_SCAN = "directory_scan", "Verzeichnis einlesen"
        REINDEX_ALL = "reindex_all", "Alles neu indexieren"
        REINDEX_COLLECTION = "reindex_collection", "Sammlung neu indexieren"
        UPLOAD = "upload", "Hochladen"

    class Status(models.TextChoices):
        RUNNING = "running", "läuft"
        CANCELLING = "cancelling", "wird abgebrochen"
        CANCELLED = "cancelled", "abgebrochen"
        FINISHED = "finished", "beendet"
        FAILED = "failed", "fehlgeschlagen"

    OPEN = (Status.RUNNING, Status.CANCELLING)

    kind = models.CharField("Art", max_length=30, choices=Kind.choices)
    status = models.CharField(
        "Status", max_length=20, choices=Status.choices, default=Status.RUNNING
    )
    collection = models.ForeignKey(
        Collection,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="index_runs",
        verbose_name="Sammlung",
    )
    source = models.ForeignKey(
        "rag.DirectorySource",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="runs",
        verbose_name="Verzeichnisquelle",
    )
    # Leer bei automatischen Läufen (Periodik des Workers).
    started_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="gestartet von",
    )
    files_found = models.PositiveIntegerField("Dateien gefunden", default=0)
    files_checked = models.PositiveIntegerField("Dateien geprüft", default=0)
    files_new = models.PositiveIntegerField("Dateien neu", default=0)
    files_changed = models.PositiveIntegerField("Dateien geändert", default=0)
    files_deleted = models.PositiveIntegerField("Dateien entfernt", default=0)
    files_skipped = models.PositiveIntegerField("Dateien übersprungen", default=0)
    docs_queued = models.PositiveIntegerField("Dokumente eingereiht", default=0)
    docs_done = models.PositiveIntegerField("Dokumente fertig", default=0)
    docs_failed = models.PositiveIntegerField("Dokumente mit Fehler", default=0)
    docs_cancelled = models.PositiveIntegerField("Dokumente abgebrochen", default=0)
    error_text = models.TextField("Fehler", blank=True)
    started = models.DateTimeField("gestartet", default=timezone.now)
    finished = models.DateTimeField("beendet", null=True, blank=True)

    class Meta:
        ordering = ["-started", "-id"]
        verbose_name = "Lauf"
        verbose_name_plural = "Läufe"
        indexes = [models.Index(fields=["status"], name="chat_indexrun_status")]

    def __str__(self):
        return f"Lauf #{self.pk} ({self.get_kind_display()}, {self.get_status_display()})"

    @property
    def is_open(self) -> bool:
        return self.status in self.OPEN

    @property
    def duration(self):
        end = self.finished or timezone.now()
        return max(end - self.started, timedelta(0))

    def duration_text(self) -> str:
        seconds = int(self.duration.total_seconds())
        hours, rest = divmod(seconds, 3600)
        minutes, seconds = divmod(rest, 60)
        if hours:
            return f"{hours} h {minutes} min"
        if minutes:
            return f"{minutes} min {seconds} s"
        return f"{seconds} s"

    def progress_text(self) -> str:
        """z. B. „Lauf #12: 340 von 1000 Dokumenten indexiert, 3 Fehler (5 min 2 s)“."""
        parts = []
        if self.kind == self.Kind.DIRECTORY_SCAN and self.status == self.Status.RUNNING:
            if self.files_checked < self.files_found:
                parts.append(f"{self.files_checked} von {self.files_found} Dateien geprüft")
        parts.append(f"{self.docs_done} von {self.docs_queued} Dokumenten indexiert")
        if self.docs_failed:
            parts.append(f"{self.docs_failed} Fehler")
        if self.docs_cancelled:
            parts.append(f"{self.docs_cancelled} abgebrochen")
        return f"Lauf #{self.pk}: {', '.join(parts)} ({self.duration_text()})"


class Job(models.Model):
    class Kind(models.TextChoices):
        INDEX_DOCUMENT = "index_document", "Dokument indexieren"
        SCAN_DIRECTORY = "scan_directory", "Verzeichnis einlesen"

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
    # Wiederholung mit Backoff (M7-02): frühestens dann wieder abholen.
    run_after = models.DateTimeField("frühestens ab", default=timezone.now)
    # Abholzeit bzw. letztes Lebenszeichen des Workers; veraltet -> Job neu einreihen.
    locked_at = models.DateTimeField("in Arbeit seit", null=True, blank=True)
    last_error = models.TextField("letzter Fehler", blank=True)
    created = models.DateTimeField("erstellt", auto_now_add=True)
    # Abbruch eines laufenden Auftrags: Der Worker prüft die Markierung an der
    # nächsten Prüfstelle (Lebenszeichen, zwischen PDF-Seiten bzw. Embedding-
    # Paketen, je Datei beim Einlesen) und entfernt den Auftrag dann.
    cancel_requested = models.BooleanField("Abbruch angefordert", default=False)
    run = models.ForeignKey(
        IndexRun,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="jobs",
        verbose_name="Lauf",
    )

    class Meta:
        ordering = ["created", "id"]
        verbose_name = "Hintergrundjob"
        verbose_name_plural = "Hintergrundjobs"
        indexes = [
            models.Index(fields=["status", "created"], name="chat_job_status_created"),
            models.Index(fields=["status", "run_after"], name="chat_job_status_run_after"),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} #{self.pk} ({self.get_status_display()})"


# --- Projekte ----------------------------------------------------------------


class Project(models.Model):
    """Projekt: Chats eines Kontos gruppieren, mit gemeinsamen Vorgaben.

    Privat für den Besitzer. ``instructions`` gehen als zusätzlicher,
    gekennzeichneter Teil in den System-Prompt aller Chats des Projekts (vor dem
    System-Prompt des Chats, siehe ``services.build_system_prompt``).
    ``default_model`` und ``collections`` sind nur Vorauswahl im Eingabefeld;
    Modellfreigabe und Leserechte der Sammlungen gelten weiter. Ein Chat gehört
    höchstens einem Projekt (``Conversation.project``), immer einem des
    Chat-Besitzers. Teilen eines Projekts ist vorbereitet, aber noch nicht
    umgesetzt (dann über ``Share`` mit Ziel Projekt, siehe chat/projects.py).
    """

    class Color(models.TextChoices):
        NONE = "", "ohne"
        BLUE = "blue", "Blau"
        GREEN = "green", "Grün"
        YELLOW = "yellow", "Gelb"
        ORANGE = "orange", "Orange"
        RED = "red", "Rot"
        PURPLE = "purple", "Lila"
        GRAY = "gray", "Grau"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="projects",
        verbose_name="Besitzer",
    )
    name = models.CharField("Name", max_length=200)
    description = models.TextField("Beschreibung", blank=True)
    instructions = models.TextField("Anweisungen", blank=True)
    # Vorgabe für Chats ohne eigene Wahl (chat/creativity.py); leer = Chat-Einstellungen.
    temperature = models.DecimalField(
        "Kreativität",
        max_digits=3,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("2"))],
    )
    # Vorgabe für Chats ohne eigene Wahl (chat/reasoning.py); leer = Chat-Einstellungen.
    reasoning_effort = models.CharField(
        "Denktiefe", max_length=10, blank=True, default="", choices=ReasoningEffort.choices
    )
    default_model = models.ForeignKey(
        AIModel,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Standardmodell",
    )
    collections = models.ManyToManyField(
        Collection,
        blank=True,
        related_name="+",
        verbose_name="Sammlungen",
    )
    color = models.CharField("Farbe", max_length=10, choices=Color.choices, blank=True)
    pinned = models.BooleanField("angeheftet", default=False)
    archived = models.BooleanField("archiviert", default=False)
    created = models.DateTimeField("erstellt", auto_now_add=True)
    updated = models.DateTimeField("geändert", auto_now=True)

    class Meta:
        ordering = ["-pinned", "name", "pk"]
        verbose_name = "Projekt"
        verbose_name_plural = "Projekte"
        indexes = [
            models.Index(fields=["owner", "archived"], name="chat_project_owner_archived"),
        ]

    def __str__(self):
        # Wie bei Chats ohne Namen: __str__ erscheint in Logs und Auswahllisten.
        return f"Projekt {self.pk}"
