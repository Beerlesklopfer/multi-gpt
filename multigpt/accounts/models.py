from decimal import Decimal

from django.contrib.auth.models import AbstractUser, Group
from django.core.validators import MinValueValidator
from django.db import models

from multigpt.chat import citations
from multigpt.node import scopes as api_scopes


class Role(models.Model):
    """Rechtepaket eines Kontos (Plan 8f), im Admin änderbar und erweiterbar.

    Semantik der Freigaben:

    - ``allowed_models``: Liste der erlaubten Modelle. Ist ``all_models`` gesetzt,
      gilt die Liste nicht, dann sind alle aktiven Modelle erlaubt. Eine *leere*
      Liste ohne ``all_models`` heißt bewusst "kein Modell" – eine vergessene
      Auswahl öffnet so nie versehentlich alles (fail closed).
    - ``allowed_mcp_servers`` / ``all_mcp_servers``: genauso für MCP-Server.
    - ``monthly_budget``: Gesamt-Monatsbudget in EUR über alle monetären
      Abrechnungskonten, leer = unbegrenzt. Je Konto über
      ``User.monthly_budget_override`` überschreibbar. Budgets je
      Abrechnungskonto: ``billing.AccountBudget``.
    - ``is_admin``: Verwaltungsrechte (Django-Admin, Seite "Familie", Verbrauch
      aller Mitglieder). Fremde Chats sieht auch ein Verwalter nicht.
    - ``key``: stabiler Schlüssel für den Code (Startrollen: admin, adult, teen,
      guest); ``name`` ist der änderbare Anzeigename.
    """

    ADMIN = "admin"
    ADULT = "adult"
    TEEN = "teen"
    GUEST = "guest"

    name = models.CharField("Name", max_length=100, unique=True)
    key = models.SlugField(
        "Schlüssel",
        max_length=50,
        unique=True,
        help_text="Stabiler technischer Schlüssel, z. B. admin, adult, teen, guest.",
    )
    is_admin = models.BooleanField(
        "Verwaltungsrechte",
        default=False,
        help_text="Darf Anbieter, Modelle, Konten, Rollen, Gruppen und Budgets verwalten "
        "und den Verbrauch aller Mitglieder sehen.",
    )
    all_models = models.BooleanField(
        "Alle aktiven Modelle",
        default=False,
        help_text="Wenn gesetzt, sind alle aktiven Modelle erlaubt und die Liste "
        "„Erlaubte Modelle“ wird ignoriert. Sonst gilt nur die Liste (leer = keines).",
    )
    allowed_models = models.ManyToManyField(
        "chat.AIModel",
        blank=True,
        related_name="roles",
        verbose_name="Erlaubte Modelle",
    )
    can_web_search = models.BooleanField("Websuche", default=False)
    can_images = models.BooleanField("Bilder erzeugen und bearbeiten", default=False)
    can_voice = models.BooleanField("Sprache (Aufnahme und Vorlesen)", default=False)
    can_upload_documents = models.BooleanField("Dokumente hochladen", default=False)
    can_share = models.BooleanField("Mit Gruppen teilen", default=False)
    can_compute = models.BooleanField(
        "Berechnungen ausführen",
        default=False,
        help_text="Modelle dürfen Python-Code für Rechnungen in einer abgeschotteten "
        "Umgebung ausführen (numpy, sympy, Diagramme).",
    )
    can_create_documents = models.BooleanField(
        "Dokumente erzeugen (PDF)",
        default=False,
        help_text="Modelle dürfen Blätter zum Ausdrucken als PDF erzeugen (Arbeitsblätter, "
        "Lineaturen, Briefe, Tabellen).",
    )
    # Knoten (M15): API-Keys und MCP-Zugang von außen (n8n, Claude Desktop, Agenten).
    can_use_api = models.BooleanField(
        "API-Keys/MCP-Zugang",
        default=False,
        help_text="Mitglieder dürfen API-Keys anlegen, mit denen andere Programme MultiGPT "
        "über den MCP-Server steuern. Ein Key kann nie mehr als Rolle und Konto.",
    )
    api_scopes = models.JSONField(
        "Erlaubte API-Rechte",
        default=list,
        blank=True,
        help_text="Höchstmenge der Rechte, die ein API-Key dieser Rolle haben kann. "
        "Wirksam ist die Schnittmenge mit den Rechten des Keys und den übrigen Rollenrechten.",
    )
    all_mcp_servers = models.BooleanField(
        "Alle aktiven MCP-Server",
        default=False,
        help_text="Wenn gesetzt, sind alle aktiven MCP-Server erlaubt. Sonst gilt nur "
        "die Liste (leer = keiner).",
    )
    allowed_mcp_servers = models.ManyToManyField(
        "chat.McpServer",
        blank=True,
        related_name="roles",
        verbose_name="Erlaubte MCP-Server",
    )
    monthly_budget = models.DecimalField(
        "Monatsbudget gesamt (EUR)",
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
        help_text="Über alle monetären Abrechnungskonten. Leer = unbegrenzt. "
        "Budgets je Konto: Kosten und Abrechnung › Abrechnungskonten.",
    )
    fixed_system_prompt = models.TextField(
        "Fester System-Prompt",
        blank=True,
        help_text="Wird jedem Chat vorangestellt; für das Mitglied weder sichtbar "
        "änderbar noch abschaltbar.",
    )

    class Meta:
        ordering = ["name"]
        verbose_name = "Rolle"
        verbose_name_plural = "Rollen"

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        self.api_scopes = api_scopes.clean(self.api_scopes)


class User(AbstractUser):
    """Familienkonto. Erweitert Djangos User; gesperrt wird über ``is_active``.

    Ohne Rolle gespeicherte Konten bekommen eine Vorgabe (siehe signals.py).
    """

    role = models.ForeignKey(
        Role,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="users",
        verbose_name="Rolle",
    )
    display_name = models.CharField("Anzeigename", max_length=150, blank=True)
    monthly_budget_override = models.DecimalField(
        "Eigenes Monatsbudget gesamt (EUR)",
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
        help_text="Überschreibt das Budget der Rolle. Leer = Budget der Rolle.",
    )
    allow_supervision = models.BooleanField(
        "Einsicht in Chats erlaubt",
        default=False,
        help_text="Verwalter dürfen die Chats dieses Kontos einsehen. Das Mitglied "
        "sieht in der Oberfläche, dass die Option aktiv ist.",
    )
    auto_read_aloud = models.BooleanField("Antworten automatisch vorlesen", default=False)
    # Persönliche Einstellungen zum Zitieren (Seite „Einstellungen“).
    citation_style = models.CharField(
        "Zitierstil",
        max_length=10,
        choices=citations.STYLE_CHOICES,
        default=citations.DEFAULT_STYLE,
    )
    citation_short = models.BooleanField(
        "Quellen im Antworttext als Kurzbeleg",
        default=False,
        help_text="Statt nur [n] erscheint im Text der Kurzbeleg, z. B. (Müller 2024, S. 12).",
    )
    citation_locator = models.BooleanField("Seite/Absatz anzeigen", default=True)
    mark_unverified_links = models.BooleanField(
        "Ungeprüfte Links markieren",
        default=True,
        help_text="Links in Antworten, die aus keiner Quelle und keinem Werkzeugergebnis "
        "stammen, bekommen ein Warnsymbol.",
    )

    class Meta(AbstractUser.Meta):
        swappable = "AUTH_USER_MODEL"
        verbose_name = "Konto"
        verbose_name_plural = "Konten"

    def __str__(self):
        return self.display_name or self.get_username()

    @property
    def monthly_budget(self):
        """Wirksames Gesamt-Monatsbudget in EUR; None = unbegrenzt."""
        if self.monthly_budget_override is not None:
            return self.monthly_budget_override
        return self.role.monthly_budget if self.role_id else None


class UserGroup(Group):
    """Erweitert Djangos Group (Tabellenvererbung, 1:1 über group_ptr).

    Mitgliedschaft läuft weiter über User.groups; die Zusatzfelder liegen hier.
    """

    is_default = models.BooleanField(
        "Standardgruppe",
        default=False,
        help_text="Jedes neue Konto wird automatisch Mitglied.",
    )

    class Meta:
        verbose_name = "Gruppe"
        verbose_name_plural = "Gruppen"
