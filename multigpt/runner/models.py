"""Datenmodell des Runners (M14-01).

Der Runner ist ein Container auf dem Rechner, an dem der Browser läuft. Er
koppelt sich mit einem ``RunnerToken`` an ein Konto und hält eine ausgehende
WebSocket-Verbindung zum Runner-Dienst (``asgi.py``). Jeder Zugriff läuft über
MultiGPT; Rechte, Kontingent, Rückfragen und Audit-Log prüft immer der Server.

**Token (Entscheidung wie bei ``node.ApiKey``):** ``mgrt_<präfix>_<geheim>``,
256 Bit Zufall, gespeichert nur SHA-256, Vergleich in konstanter Zeit. Der
Klartext wird einmal angezeigt und nie gespeichert oder geloggt.

**Arbeitsstand (Entscheidung 2026-10-13):** ``RunnerWorkspace`` hält den ganzen
Stand als git-Bundle (``git bundle create … --all``) in der Datenbank. Der
Container arbeitet nur im tmpfs; die Datensicherung der DB sichert die
Arbeitsstände mit. Das Bundle ist nicht vertrauenswürdiges Material: Der
Server führt nichts daraus aus und liefert es nur an den Besitzer bzw. dessen
Runner aus. Listen laden es nie mit (``RunnerWorkspace.objects`` schließt das
Feld per ``defer`` aus).

**Datenschutz:** Admin und Verwalter sehen nur Metadaten (Name, Größe,
Zeiten), keine Inhalte der Arbeitsstände und keine Ausgaben fremder Konten.
``RunnerAudit`` hält gekürzte Argumente ohne Geheimnisse, nie Ausgaben.
"""

import secrets
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from multigpt.core.fields import EncryptedTextField, UnicodeJSONEncoder

TOKEN_PREFIX = "mgrt_"


def new_preview_token() -> str:
    """Zufalls-Token im Pfad der Vorschau (8x), je Sitzung, 256 Bit."""
    return secrets.token_urlsafe(32)


class RunnerToken(models.Model):
    """Kopplungs-Token eines Kontos (M14-02); genau ein Runner je Token."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="runner_tokens",
        verbose_name="Konto",
    )
    name = models.CharField("Name", max_length=100)
    # Öffentlicher Teil (mgrt_<prefix>_<geheim>), eindeutig, zur Suche.
    prefix = models.CharField("Präfix", max_length=16, unique=True, editable=False)
    token_hash = models.CharField("Hash", max_length=64, editable=False)
    created = models.DateTimeField("erstellt", auto_now_add=True)
    expires_at = models.DateTimeField("läuft ab", null=True, blank=True)
    revoked_at = models.DateTimeField("widerrufen", null=True, blank=True)
    last_used_at = models.DateTimeField("zuletzt benutzt", null=True, blank=True)
    last_used_ip = models.GenericIPAddressField("zuletzt von", null=True, blank=True)

    class Meta:
        ordering = ["-created", "-pk"]
        verbose_name = "Runner-Token"
        verbose_name_plural = "Runner-Tokens"
        indexes = [models.Index(fields=["owner", "revoked_at"], name="runner_token_owner_rev")]

    def __str__(self):
        return f"Runner-Token {self.display_prefix}"

    @property
    def display_prefix(self) -> str:
        return f"{TOKEN_PREFIX}{self.prefix}…"

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None

    def is_expired(self, now=None) -> bool:
        return self.expires_at is not None and self.expires_at <= (now or timezone.now())

    def is_usable(self, now=None) -> bool:
        return not self.revoked and not self.is_expired(now)

    @property
    def state_label(self) -> str:
        if self.revoked:
            return "widerrufen"
        if self.is_expired():
            return "abgelaufen"
        return "aktiv"


class CatalogState(models.TextChoices):
    """Ergebnis der Prüfung des Dienstkatalogs (wie ``protocol.CATALOG_STATES``)."""

    VALID = "valid", "gültig signiert"
    UNSIGNED = "unsigned", "unsigniert"
    INVALID = "invalid", "Signatur ungültig"
    UNKNOWN_KEY = "unknown_key", "Release-Schlüssel unbekannt (Runner zu alt?)"
    MISSING = "missing", "kein Katalog"
    DEV = "dev", "unsigniert (Entwicklung)"


class Runner(models.Model):
    """Ein gekoppelter Runner-Agent; angelegt bzw. aktualisiert beim ``hello``."""

    class Engine(models.TextChoices):
        PODMAN = "podman", "Podman"
        DOCKER = "docker", "Docker"

    class Status(models.TextChoices):
        ONLINE = "online", "verbunden"
        OFFLINE = "offline", "getrennt"

    token = models.OneToOneField(
        RunnerToken, on_delete=models.CASCADE, related_name="runner", verbose_name="Token"
    )
    # Rechnername aus dem hello (vom Agent gemeldet, nicht vertrauenswürdig).
    name = models.CharField("Rechner", max_length=200, blank=True)
    engine = models.CharField(
        "Engine", max_length=10, choices=Engine.choices, default=Engine.PODMAN
    )
    engine_version = models.CharField("Engine-Version", max_length=50, blank=True)
    agent_version = models.CharField("Agent-Version", max_length=50, blank=True)
    protocol_version = models.PositiveSmallIntegerField("Protokoll", default=1)
    rootless = models.BooleanField("rootless", default=True)
    status = models.CharField(
        "Status", max_length=10, choices=Status.choices, default=Status.OFFLINE
    )
    connected_at = models.DateTimeField("verbunden seit", null=True, blank=True)
    last_seen = models.DateTimeField("zuletzt gesehen", null=True, blank=True)
    created = models.DateTimeField("gekoppelt", auto_now_add=True)
    # Dienstkatalog aus dem letzten hello (trust.py): Ergebnis der Prüfung der
    # Release-Signatur und die daraus freigegebenen Dienste (welcome.services).
    catalog_state = models.CharField(
        "Dienstkatalog", max_length=12, choices=CatalogState.choices, default="missing"
    )
    catalog_release = models.CharField("Katalog-Release", max_length=50, blank=True)
    services = models.JSONField("freigegebene Dienste", default=list, blank=True)

    class Meta:
        ordering = ["-last_seen", "-pk"]
        verbose_name = "Runner"
        verbose_name_plural = "Runner"
        indexes = [models.Index(fields=["status"], name="runner_runner_status")]

    def __str__(self):
        return f"Runner {self.name or self.pk}"

    @property
    def owner(self):
        return self.token.owner

    @property
    def is_online(self) -> bool:
        return self.status == self.Status.ONLINE


class WorkspaceQuerySet(models.QuerySet):
    def with_bundle(self):
        """Bundle mitladen (nur für Herunterladen bzw. Ausliefern an den Runner)."""
        return self.defer(None)


class WorkspaceManager(models.Manager.from_queryset(WorkspaceQuerySet)):
    # Das Bundle (bis 50 MB) gehört nie in Listen: Standard ohne das Feld.
    def get_queryset(self):
        return super().get_queryset().defer("bundle")


class RunnerWorkspace(models.Model):
    """Arbeitsstand eines Kontos als git-Bundle (Entscheidung 2026-10-13, 8q–8s).

    Gehört dem Konto, ist im Chat auswählbar und wird von mehreren Chats
    nacheinander genutzt. Höchstens eine Sitzung zugleich (``active_session``,
    gesetzt über ``workspaces.acquire``). Der letzte gute Stand bleibt, bis ein
    neuer geprüft ist (Annahme: ``tools``).
    """

    class ScratchMode(models.TextChoices):
        # Scratchpad des Runners (Entscheidung 2026-10-13): Ablage außerhalb von git.
        CONTAINER = "container", "im Container (flüchtig, weg mit der Sitzung)"
        LOCAL = "local", "lokal auf dem Rechner (~/MultiGPT/Scratch/<Kennung>/)"

    # Öffentliche Kennung; benennt beim Scratchpad „lokal“ den Ordner auf dem Rechner
    # (der Server nennt dem Agent nur sie, nie einen Pfad).
    uuid = models.UUIDField("Kennung", default=uuid.uuid4, unique=True, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="runner_workspaces",
        verbose_name="Konto",
    )
    name = models.CharField("Name", max_length=100)
    scratch_mode = models.CharField(
        "Scratchpad",
        max_length=10,
        choices=ScratchMode.choices,
        default=ScratchMode.CONTAINER,
    )
    bundle = models.BinaryField("git-Bundle", null=True, blank=True, editable=False)
    size = models.PositiveBigIntegerField("Größe (Bytes)", default=0, editable=False)
    sha256 = models.CharField("Prüfsumme (SHA-256)", max_length=64, blank=True, editable=False)
    head_commit = models.CharField("HEAD-Commit", max_length=64, blank=True, editable=False)
    created = models.DateTimeField("angelegt", auto_now_add=True)
    updated = models.DateTimeField("geändert", default=timezone.now)
    # Sperre: nur eine Sitzung je Arbeitsstand.
    active_session = models.ForeignKey(
        "RunnerSession",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        editable=False,
        verbose_name="aktive Sitzung",
    )
    locked_at = models.DateTimeField("gesperrt seit", null=True, blank=True, editable=False)

    objects = WorkspaceManager()

    class Meta:
        ordering = ["-updated", "-pk"]
        verbose_name = "Arbeitsstand"
        verbose_name_plural = "Arbeitsstände"
        constraints = [
            models.UniqueConstraint(fields=["owner", "name"], name="runner_workspace_owner_name"),
            models.CheckConstraint(
                condition=models.Q(scratch_mode__in=["container", "local"]),
                name="runner_workspace_scratch_mode",
            ),
        ]

    def __str__(self):
        return f"Arbeitsstand {self.name}"

    @property
    def locked(self) -> bool:
        return self.active_session_id is not None


class RunnerSession(models.Model):
    """Eine Sitzung: ein wegwerfbarer Arbeitscontainer mit einem Arbeitsstand."""

    class Status(models.TextChoices):
        STARTING = "starting", "startet"
        RUNNING = "running", "läuft"
        STOPPING = "stopping", "wird beendet"
        STOPPED = "stopped", "beendet"
        UNKNOWN = "unknown", "unbekannt (Runner getrennt)"
        FAILED = "failed", "fehlgeschlagen"

    class ConfirmMode(models.TextChoices):
        # Frage 8t: Modi wie in Claude Code; ssh/telnet fragen immer.
        ASK = "ask", "fragen (jede Änderung bestätigen)"
        EDITS = "edits", "Änderungen automatisch, Befehle fragen"
        AUTO = "auto", "alles automatisch"

    ACTIVE_STATES = (Status.STARTING, Status.RUNNING, Status.STOPPING, Status.UNKNOWN)

    # Öffentliche Kennung (Rahmen ``session_start`` u. a.), nicht die DB-ID.
    uuid = models.UUIDField("Kennung", default=uuid.uuid4, unique=True, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="runner_sessions",
        verbose_name="Konto",
    )
    workspace = models.ForeignKey(
        RunnerWorkspace,
        # Audit und Laufzeit bleiben, auch wenn der Arbeitsstand gelöscht wird.
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sessions",
        verbose_name="Arbeitsstand",
    )
    runner = models.ForeignKey(
        Runner,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sessions",
        verbose_name="Runner",
    )
    conversation = models.ForeignKey(
        "chat.Conversation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="runner_sessions",
        verbose_name="Chat",
    )
    # Sitzung über einen M15-Key (Agententeam, n8n): ohne Netz, ohne ssh/telnet.
    api_key = models.ForeignKey(
        "node.ApiKey",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="runner_sessions",
        verbose_name="API-Key",
    )
    status = models.CharField(
        "Status", max_length=10, choices=Status.choices, default=Status.STARTING
    )
    confirm_mode = models.CharField(
        "Rückfrage", max_length=10, choices=ConfirmMode.choices, default=ConfirmMode.AUTO
    )
    network = models.BooleanField("Netz", default=False)
    # Soll-Grenzen (vom Server aus der Rolle) und Ist-Grenzen (aus ``inspect``).
    limits_requested = models.JSONField(
        "Soll-Grenzen", default=dict, blank=True, encoder=UnicodeJSONEncoder
    )
    limits_actual = models.JSONField(
        "Ist-Grenzen", default=dict, blank=True, encoder=UnicodeJSONEncoder
    )
    started = models.DateTimeField("gestartet", default=timezone.now)
    ended = models.DateTimeField("beendet", null=True, blank=True)
    # Laufzeit (M14-11): bis hierher gezählt; Minuten gesamt.
    counted_until = models.DateTimeField("gezählt bis", null=True, blank=True)
    minutes = models.PositiveIntegerField("Minuten", default=0)
    usage_entry = models.ForeignKey(
        "billing.UsageEntry",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Buchung (laufender Monat)",
    )
    last_tool_at = models.DateTimeField("letzter Werkzeugaufruf", null=True, blank=True)
    # Kontingent ausgeschöpft: Stopp angefordert; ohne Bestätigung trennt der Server nach 60 s.
    quota_stop_sent = models.DateTimeField("Stopp (Kontingent) angefordert", null=True, blank=True)
    # Vorschau (8p/8x): Port am Browser-Rechner (24080–24090) und Zufalls-Token im Pfad.
    preview_port = models.PositiveIntegerField("Vorschau-Port", null=True, blank=True)
    preview_token = models.CharField(
        "Vorschau-Token", max_length=64, default=new_preview_token, editable=False
    )
    stop_reason = models.CharField("Grund für das Ende", max_length=200, blank=True)

    class Meta:
        ordering = ["-started", "-pk"]
        verbose_name = "Runner-Sitzung"
        verbose_name_plural = "Runner-Sitzungen"
        indexes = [
            models.Index(fields=["owner", "status"], name="runner_session_owner_status"),
            models.Index(fields=["runner", "status"], name="runner_session_runner_status"),
        ]

    def __str__(self):
        return f"Runner-Sitzung {str(self.uuid)[:8]}"

    @property
    def is_active(self) -> bool:
        return self.status in self.ACTIVE_STATES

    @property
    def packages(self) -> bool:
        """Netzmodus „Paketquellen“ (Proxy mit Positivliste, apt_install)."""
        return bool((self.limits_requested or {}).get("packages"))


class ChatWorkspace(models.Model):
    """Auswahl im Chat (8j): Arbeitsstand, Rückfrage-Modus, Aufgabenliste, Sitzung.

    Je Chat höchstens ein Eintrag. Der Arbeitsstand muss dem Besitzer des Chats
    gehören (prüft ``runner.chat``); ``mode`` leer = Vorgabe nach Netz (8t).
    """

    conversation = models.OneToOneField(
        "chat.Conversation",
        on_delete=models.CASCADE,
        related_name="runner_workspace",
        verbose_name="Chat",
    )
    workspace = models.ForeignKey(
        RunnerWorkspace,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="chats",
        verbose_name="Arbeitsstand",
    )
    mode = models.CharField(
        "Rückfrage",
        max_length=10,
        choices=RunnerSession.ConfirmMode.choices,
        blank=True,
        help_text="Leer: Vorgabe (ohne Netz alles automatisch, mit Netz fragen Befehle).",
    )
    todo = models.JSONField("Aufgabenliste", default=list, blank=True, encoder=UnicodeJSONEncoder)
    packages = models.BooleanField(
        "Paketquellen",
        default=True,
        help_text="Mit Rollenrecht „Paketquellen“: Sitzung mit Proxy zu den Paketquellen und "
        "apt_install (Vorgabe an). Aus: ohne Paketquellen, dafür setup_host/telnet.",
    )
    session = models.ForeignKey(
        RunnerSession,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Sitzung",
    )
    updated = models.DateTimeField("geändert", auto_now=True)

    class Meta:
        verbose_name = "Arbeitsstand im Chat"
        verbose_name_plural = "Arbeitsstände in Chats"

    def __str__(self):
        return f"Arbeitsstand im Chat {self.conversation_id}"


class RunnerMessage(models.Model):
    """Warteschlange zwischen Web/Worker und Runner-Dienst (LISTEN/NOTIFY, M14-05).

    Der Absender schreibt die Zeile und ruft in derselben Transaktion
    ``pg_notify(kanal, id)`` auf; die Nutzlast liegt wegen der Grenze von knapp
    8000 Byte immer hier. Der Empfänger holt sie mit ``DELETE … RETURNING`` in
    ID-Reihenfolge. Verwaiste Zeilen (älter als 10 min) löscht der Worker.
    """

    class Direction(models.TextChoices):
        TO_RUNNER = "to_runner", "an den Runner"
        TO_SERVER = "to_server", "an den Server"

    runner = models.ForeignKey(
        Runner, on_delete=models.CASCADE, related_name="messages", verbose_name="Runner"
    )
    direction = models.CharField("Richtung", max_length=10, choices=Direction.choices)
    # UUID der MCP-Verbindung (je Sitzung eine), leer für Steuerrahmen.
    pipe = models.UUIDField("Pipe", null=True, blank=True)
    session = models.ForeignKey(
        RunnerSession,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="messages",
        verbose_name="Sitzung",
    )
    # Kanal für die Antwort (mgpt_runner_reply_<prozess>), leer = keine erwartet.
    reply_channel = models.CharField("Antwortkanal", max_length=63, blank=True)
    kind = models.CharField("Art", max_length=30)
    payload = models.JSONField("Nutzlast", default=dict, encoder=UnicodeJSONEncoder)
    created = models.DateTimeField("erstellt", default=timezone.now)

    class Meta:
        ordering = ["pk"]
        verbose_name = "Runner-Nachricht"
        verbose_name_plural = "Runner-Nachrichten"
        indexes = [
            models.Index(fields=["runner", "direction", "id"], name="runner_msg_runner_dir"),
            models.Index(fields=["reply_channel", "id"], name="runner_msg_reply"),
            models.Index(fields=["created"], name="runner_msg_created"),
        ]

    def __str__(self):
        return f"Runner-Nachricht {self.pk} ({self.kind})"


class RunnerAudit(models.Model):
    """Audit-Log: Werkzeugaufrufe und Ereignisse – gekürzte Argumente, nie Ausgaben.

    Einträge legt ``audit.record`` an (kürzt und entfernt Geheimnisse).
    Fehlgeschlagene Anmeldungen (``AUTH_FAILED``) dienen der Drosselung je IP.
    """

    class Kind(models.TextChoices):
        TOOL = "tool", "Werkzeugaufruf"
        REMOTE = "remote", "ssh/telnet"
        SESSION = "session", "Sitzung"
        CONNECT = "connect", "Verbindung"
        AUTH_FAILED = "auth_failed", "Anmeldung abgelehnt"
        TOKEN = "token", "Token"
        WORKSPACE = "workspace", "Arbeitsstand"
        LIMITS = "limits", "Grenzen weichen ab"
        TRUST = "trust", "Dienstkatalog"
        ERROR = "error", "Fehler"

    kind = models.CharField("Art", max_length=12, choices=Kind.choices, default=Kind.TOOL)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Konto",
    )
    session = models.ForeignKey(
        RunnerSession,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit",
        verbose_name="Sitzung",
    )
    runner = models.ForeignKey(
        Runner,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Runner",
    )
    created = models.DateTimeField("Zeit", default=timezone.now)
    tool = models.CharField("Werkzeug", max_length=100, blank=True)
    arguments = models.TextField("Argumente (gekürzt)", blank=True)
    exit_code = models.IntegerField("Exit-Code", null=True, blank=True)
    duration_ms = models.PositiveIntegerField("Dauer (ms)", null=True, blank=True)
    message = models.CharField("Meldung", max_length=500, blank=True)
    ip = models.GenericIPAddressField("IP-Adresse", null=True, blank=True)

    class Meta:
        ordering = ["-created", "-pk"]
        verbose_name = "Runner-Audit"
        verbose_name_plural = "Runner-Audit-Log"
        indexes = [
            models.Index(fields=["session", "created"], name="runner_audit_session"),
            models.Index(fields=["owner", "created"], name="runner_audit_owner"),
            models.Index(fields=["ip", "kind", "created"], name="runner_audit_ip_kind"),
            models.Index(fields=["created"], name="runner_audit_created"),
        ]

    def __str__(self):
        return f"Runner-Audit {self.pk} ({self.get_kind_display()})"


class RunnerHost(models.Model):
    """Freigegebenes Ziel für die Werkzeuge ``ssh``/``telnet`` (nur Verwalter legen an).

    Das Modell wählt nur nach ``name`` und sieht nie Adresse, Benutzer, Schlüssel
    oder Passwort. Den Zugang gibt der Server dem Runner nur für einen
    bestätigten Aufruf mit. Der SSH-Host-Schlüssel wird beim Anlegen einmal
    abgerufen, vom Verwalter bestätigt und festgehalten (8v); weicht er später
    ab, bricht der Agent ab. Freigabe je Rolle bzw. Person; zusätzlich braucht
    das Konto das Rollenrecht „Fernzugriff (ssh/telnet)“.
    """

    class Protocol(models.TextChoices):
        SSH = "ssh", "SSH"
        TELNET = "telnet", "Telnet (unverschlüsselt)"

    class AuthMethod(models.TextChoices):
        KEY = "key", "SSH-Schlüssel"
        PASSWORD = "password", "Passwort"
        NONE = "none", "ohne (z. B. Telnet ohne Anmeldung)"

    name = models.SlugField(
        "Name",
        max_length=50,
        unique=True,
        help_text="Unter diesem Namen wählt das Modell den Host (z. B. nas, router).",
    )
    description = models.CharField("Beschreibung", max_length=200, blank=True)
    address = models.CharField("Adresse", max_length=255, help_text="Hostname oder IP-Adresse.")
    port = models.PositiveIntegerField("Port", default=22)
    protocol = models.CharField(
        "Protokoll", max_length=10, choices=Protocol.choices, default=Protocol.SSH
    )
    username = models.CharField("Benutzer", max_length=100, blank=True)
    auth_method = models.CharField(
        "Zugang", max_length=10, choices=AuthMethod.choices, default=AuthMethod.KEY
    )
    # Privater SSH-Schlüssel bzw. Passwort, verschlüsselt wie die API-Keys der Anbieter.
    secret = EncryptedTextField("Schlüssel bzw. Passwort", blank=True)
    host_key = models.TextField(
        "SSH-Host-Schlüssel",
        blank=True,
        help_text="Festgehaltener öffentlicher Schlüssel des Hosts (known_hosts-Zeile).",
    )
    host_key_confirmed_at = models.DateTimeField("Host-Schlüssel bestätigt", null=True, blank=True)
    allowed_roles = models.ManyToManyField(
        "accounts.Role",
        blank=True,
        related_name="runner_hosts",
        verbose_name="freigegeben für Rollen",
    )
    allowed_users = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="runner_hosts",
        verbose_name="freigegeben für Personen",
    )
    active = models.BooleanField("aktiv", default=True)
    created = models.DateTimeField("angelegt", auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Runner-Host (ssh/telnet)"
        verbose_name_plural = "Runner-Hosts (ssh/telnet)"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(protocol__in=["ssh", "telnet"]),
                name="runner_host_protocol_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(port__gte=1, port__lte=65535),
                name="runner_host_port_valid",
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def host_key_pending(self) -> bool:
        """SSH ohne bestätigten Host-Schlüssel: nicht nutzbar."""
        return self.protocol == self.Protocol.SSH and not (
            self.host_key and self.host_key_confirmed_at
        )


class RunnerService(models.Model):
    """Dienst eines gültig signierten Katalogs, den der Server nicht kennt (trust.py).

    Neue Dienste erscheinen hier wie neue Werkzeuge eines importierten
    MCP-Servers und werden erst angeboten, wenn ein Verwalter sie eingestuft und
    freigegeben hat (Schutz gegen eingeschleuste Werkzeuge oder Beschreibungen).
    Beschreibung und Schema stammen aus dem signierten Katalog; die Einstufung
    (Art, damit die Rückfrage) legt der Verwalter fest.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "nicht eingestuft"
        APPROVED = "approved", "freigegeben"
        BLOCKED = "blocked", "gesperrt"

    class Kind(models.TextChoices):
        READ = "read", "lesen (nie Rückfrage)"
        EDIT = "edit", "Dateien ändern"
        COMMAND = "command", "Befehl ausführen"
        PUBLISH = "publish", "Datei in den Chat"
        REMOTE = "remote", "Fernzugriff (immer Rückfrage)"
        SETUP = "setup", "Einrichtung (immer Rückfrage)"

    name = models.CharField("Name", max_length=64, unique=True)
    status = models.CharField(
        "Status", max_length=10, choices=Status.choices, default=Status.PENDING
    )
    kind = models.CharField(
        "Einstufung",
        max_length=10,
        choices=Kind.choices,
        blank=True,
        help_text="Bestimmt die Rückfrage. Leer: nicht eingestuft (wird nicht angeboten).",
    )
    proposed_kind = models.CharField(
        "Art laut Katalog", max_length=10, choices=Kind.choices, blank=True, editable=False
    )
    description = models.TextField("Beschreibung (aus dem Katalog)", blank=True, editable=False)
    input_schema = models.JSONField(
        "Parameter (aus dem Katalog)", default=dict, blank=True, editable=False
    )
    release = models.CharField("zuletzt in Release", max_length=50, blank=True, editable=False)
    first_seen = models.DateTimeField("zuerst gemeldet", default=timezone.now, editable=False)
    last_seen = models.DateTimeField("zuletzt gemeldet", default=timezone.now, editable=False)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="eingestuft von",
        editable=False,
    )
    reviewed_at = models.DateTimeField("eingestuft am", null=True, blank=True, editable=False)

    class Meta:
        ordering = ["status", "name"]
        verbose_name = "Runner-Dienst (neu im Katalog)"
        verbose_name_plural = "Runner-Dienste (neu im Katalog)"
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(status="approved") | ~models.Q(kind=""),
                name="runner_service_approved_needs_kind",
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def offered(self) -> bool:
        return self.status == self.Status.APPROVED and bool(self.kind)
