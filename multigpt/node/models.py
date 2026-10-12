"""API-Keys je Konto und Audit-Log der Aufrufe (M15, Knoten).

**Speicherung des Keys (Entscheidung):** Der Key besteht aus einem öffentlichen
Präfix (Suche, Anzeige) und 256 Bit Zufall (``secrets.token_urlsafe(32)``).
Gespeichert wird nur SHA-256 über den ganzen Key; verglichen wird in konstanter
Zeit (``hmac.compare_digest``). Langsame Passwort-Hasher (Argon2, PBKDF2)
schützen Geheimnisse mit wenig Entropie gegen Durchprobieren nach einem
Datenbankdiebstahl – bei 256 Bit Zufall ist das ohnehin aussichtslos, ein
langsamer Hasher kostete aber bei *jedem* Aufruf ~100 ms CPU. Wie die
Kopplungs-Tokens des Runners (M14-02). Der Klartext wird nur einmal beim
Anlegen angezeigt und nie gespeichert oder geloggt.

Datenschutz: ``ApiCall`` hält nur Metadaten (Key, Methode, Werkzeug, Status,
Dauer, Größen, IP), nie Argumente oder Ergebnisse.

``SseSession``/``SseMessage`` (Legacy-Transport HTTP+SSE, ``sse.py``): Antworten
liegen dort nur bis zur Zustellung an den Stream (Sekunden) und werden beim
Senden gelöscht, spätestens mit der Sitzung.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone

KEY_PREFIX = "mgpt_"


class ApiKey(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="api_keys",
        verbose_name="Konto",
    )
    name = models.CharField("Name", max_length=100)
    # Öffentlicher Teil des Keys (mgpt_<prefix>_<geheim>), eindeutig, zur Suche.
    prefix = models.CharField("Präfix", max_length=16, unique=True, editable=False)
    key_hash = models.CharField("Hash", max_length=64, editable=False)
    scopes = models.JSONField("Rechte", default=list, blank=True)
    # Optional eingeschränkt; leer = alle, die das Konto lesen bzw. schreiben darf.
    collections = models.ManyToManyField(
        "chat.Collection",
        blank=True,
        related_name="+",
        verbose_name="erlaubte Sammlungen",
    )
    sources = models.ManyToManyField(
        "rag.DirectorySource",
        blank=True,
        related_name="+",
        verbose_name="erlaubte Verzeichnisquellen",
    )
    # Chat, in dem direkte Werkzeugaufrufe (tools.run) sichtbar werden.
    tool_conversation = models.ForeignKey(
        "chat.Conversation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        editable=False,
        verbose_name="Chat für Werkzeugaufrufe",
    )
    # Neue Chats von ``ask`` im Projekt „API“ des Kontos sammeln (A4, node/chats.py).
    collect_in_project = models.BooleanField("Chats im Projekt „API“ sammeln", default=True)
    expires_at = models.DateTimeField("läuft ab", null=True, blank=True)
    active = models.BooleanField("aktiv", default=True)
    revoked_at = models.DateTimeField("widerrufen", null=True, blank=True)
    last_used_at = models.DateTimeField("zuletzt benutzt", null=True, blank=True)
    last_used_ip = models.GenericIPAddressField("zuletzt von", null=True, blank=True)
    created = models.DateTimeField("erstellt", auto_now_add=True)

    class Meta:
        ordering = ["-created", "-pk"]
        verbose_name = "API-Key"
        verbose_name_plural = "API-Keys"
        indexes = [models.Index(fields=["owner", "active"], name="node_apikey_owner_active")]

    def __str__(self):
        # Name ist vom Mitglied gewählt, aber kein Inhalt; das Präfix verrät nichts.
        return f"API-Key {self.display_prefix}"

    @property
    def display_prefix(self) -> str:
        return f"{KEY_PREFIX}{self.prefix}…"

    def is_expired(self, now=None) -> bool:
        return self.expires_at is not None and self.expires_at <= (now or timezone.now())

    def is_usable(self, now=None) -> bool:
        return self.active and not self.is_expired(now)

    @property
    def state_label(self) -> str:
        if not self.active:
            return "widerrufen"
        if self.is_expired():
            return "abgelaufen"
        return "aktiv"


class ApiCall(models.Model):
    """Ein Aufruf des MCP-Endpunkts – nur Metadaten, nie Inhalte."""

    class Status(models.TextChoices):
        OK = "ok", "erfolgreich"
        TOOL_ERROR = "tool_error", "Werkzeug meldet Fehler"
        DENIED = "denied", "nicht erlaubt"
        INVALID = "invalid", "ungültige Anfrage"
        UNAUTHORIZED = "unauthorized", "Key ungültig"
        RATE_LIMITED = "rate_limited", "gedrosselt"
        ERROR = "error", "interner Fehler"

    key = models.ForeignKey(
        ApiKey,
        # Gelöschte Keys: Aufrufe bleiben im Audit-Log („gelöschter Key“).
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="calls",
        verbose_name="API-Key",
    )
    method = models.CharField("Methode", max_length=50, blank=True)
    tool = models.CharField("Werkzeug", max_length=100, blank=True)
    status = models.CharField("Status", max_length=20, choices=Status.choices)
    http_status = models.PositiveSmallIntegerField("HTTP-Status", default=200)
    duration_ms = models.PositiveIntegerField("Dauer (ms)", default=0)
    request_bytes = models.PositiveBigIntegerField("Anfrage (Bytes)", default=0)
    response_bytes = models.PositiveBigIntegerField("Antwort (Bytes)", default=0)
    ip = models.GenericIPAddressField("IP-Adresse", null=True, blank=True)
    created = models.DateTimeField("Zeit", default=timezone.now)

    class Meta:
        ordering = ["-created", "-pk"]
        verbose_name = "API-Aufruf"
        verbose_name_plural = "API-Aufrufe"
        indexes = [
            models.Index(fields=["key", "created"], name="node_apicall_key_created"),
            models.Index(fields=["ip", "status", "created"], name="node_apicall_ip_status"),
            models.Index(fields=["created"], name="node_apicall_created"),
        ]

    def __str__(self):
        return f"API-Aufruf {self.pk} ({self.get_status_display()})"


class SseSession(models.Model):
    """Offener Stream des Legacy-Transports HTTP+SSE (``GET /mcp/sse``).

    Die ID (128 Bit Zufall, hex) steht in der POST-URL; gültig ist sie nur
    zusammen mit dem Key, der den Stream geöffnet hat. Der Stream setzt
    ``last_seen`` bei jedem Keepalive; der Worker löscht Sitzungen, deren
    Stream nicht mehr lebt (Prozess beendet o. Ä.).
    """

    id = models.CharField("Sitzung", max_length=32, primary_key=True, editable=False)
    key = models.ForeignKey(
        ApiKey,
        on_delete=models.CASCADE,
        related_name="sse_sessions",
        verbose_name="API-Key",
    )
    created = models.DateTimeField("geöffnet", default=timezone.now)
    last_seen = models.DateTimeField("zuletzt aktiv", default=timezone.now)

    class Meta:
        verbose_name = "SSE-Sitzung"
        verbose_name_plural = "SSE-Sitzungen"
        indexes = [models.Index(fields=["key", "last_seen"], name="node_ssesession_key_seen")]

    def __str__(self):
        return f"SSE-Sitzung {self.id[:8]}…"


class SseMessage(models.Model):
    """Ausgehende Nachricht an einen SSE-Stream (Warteschlange zwischen Prozessen).

    Der POST-Handler legt die JSON-RPC-Antwort hier ab und meldet die ID per
    ``NOTIFY``; der Stream liest, sendet und löscht. Lebt nur Sekunden.
    """

    session = models.ForeignKey(
        SseSession, on_delete=models.CASCADE, related_name="messages", verbose_name="Sitzung"
    )
    payload = models.TextField("Nachricht (JSON)")
    created = models.DateTimeField("erstellt", default=timezone.now)

    class Meta:
        verbose_name = "SSE-Nachricht"
        verbose_name_plural = "SSE-Nachrichten"

    def __str__(self):
        return f"SSE-Nachricht {self.pk}"


class IntegrationOverview(ApiKey):
    """Admin-Seite „Integrationen“ (n8n, API-Keys); keine eigenen Daten."""

    class Meta:
        proxy = True
        verbose_name = "Integrationen"
        verbose_name_plural = "Integrationen"
