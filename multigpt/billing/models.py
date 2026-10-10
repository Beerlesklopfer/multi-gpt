"""Kontenrahmen, Preise, Kurse und Buchungen (M6-06 bis M6-10).

Eigene App statt weiterer Modelle in ``chat``/``accounts``: Abrechnung ist ein
abgeschlossener Bereich mit eigenen Migrationen, eigenem Admin-Abschnitt und
eigener Logik (pricing, booking, budgets). ``chat`` kennt nur
``Provider.billing_account``; alles andere hängt an dieser App.

- ``BillingAccount``: Abrechnungskonto. Jeder Anbieter gehört zu genau einem
  Konto, ein Konto kann mehrere Anbieter bündeln (z. B. OpenRouter neben dem
  direkten Zugang). Art: ``monetary`` (Preise in EUR oder USD), ``tokens``
  (nur Token-Zählung, z. B. LM Studio), ``flat`` (Pauschale/Abo, je Anfrage
  0, nur gezählt).
- ``ModelPrice``: Preisversion eines Modells ab ``valid_from``, in der Währung
  des Kontos, je 1 Mio. Tokens. Alte Buchungen behalten ihren Betrag; die
  verwendete Version steht in der Buchung.
- ``ExchangeRate``: Referenzkurs USD -> EUR je Tag. Verwendet wird der zum
  Buchungszeitpunkt gültige (letzter Kurs am oder vor dem Buchungstag).
- ``UsageEntry``: eine Buchung je Antwort, Anhang bzw. Werkzeuggebühr.
- ``AccountBudget``: Monatsbudget (EUR) bzw. Token-Kontingent je Konto und
  Rolle bzw. Konto und Nutzer (Nutzer vor Rolle). Das bisherige Budget an
  Rolle und Konto gilt weiter als Gesamtbudget über alle monetären Konten.
"""

import re
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

# Preise und Beträge mit Bruchteilen von Cent (wie chat.models.MONEY).
MONEY = {"max_digits": 12, "decimal_places": 6}
PRICE_HELP = "je 1 Mio. Tokens in der Währung des Kontos; leer = wie Eingabe"

# Gebühren je Einheit (``ModelPrice.unit_prices``, ``UsageEntry.units``):
# Schlüssel -> (Bezeichnung, Menge je Preis). Varianten wie
# ``image:high:1024x1024`` erben vom Grundschlüssel ``image`` (siehe
# ``pricing.unit_price``).
UNITS = {
    "web_search": ("Websuche beim Anbieter, je Aufruf", 1),
    "web_fetch": ("Webabruf beim Anbieter, je Aufruf", 1),
    "image": ("Bild, je Stück (Varianten image:<qualität>[:<größe>])", 1),
    "audio_minute": ("Audio, je Minute (Spracherkennung)", 1),
    "tts_characters": ("Sprachausgabe, je 1 Mio. Zeichen", 1_000_000),
    "request": ("Anfrage, je Aufruf", 1),
}
UNIT_KEY = re.compile(r"^(?P<base>[a-z_]+)(?::[a-z0-9x_-]{1,30}){0,2}$")


def validate_unit_key(key: str) -> str:
    """Schlüssel prüfen; liefert den Grundschlüssel. ValueError bei Unbekanntem."""
    match = UNIT_KEY.match(key or "")
    if not match or match["base"] not in UNITS:
        raise ValueError(key)
    return match["base"]


class BillingAccount(models.Model):
    class Kind(models.TextChoices):
        MONETARY = "monetary", "monetär (Preise je Token)"
        TOKENS = "tokens", "nur Tokens (z. B. lokal)"
        FLAT = "flat", "Pauschale/Abo (nur gezählt)"

    class Currency(models.TextChoices):
        EUR = "EUR", "Euro (EUR)"
        USD = "USD", "US-Dollar (USD)"

    name = models.CharField("Name", max_length=100, unique=True)
    kind = models.CharField("Art", max_length=10, choices=Kind.choices, default=Kind.MONETARY)
    currency = models.CharField(
        "Währung",
        max_length=3,
        choices=Currency.choices,
        blank=True,
        help_text="Nur bei monetären Konten; in dieser Währung stehen die Preise. "
        "Lässt sich nicht mehr ändern, sobald Preise oder Buchungen existieren.",
    )
    note = models.TextField("Notiz", blank=True, help_text="z. B. Vertragsnummer, Abo-Preis.")
    active = models.BooleanField(
        "aktiv",
        default=True,
        help_text="Inaktiv: Modelle der zugeordneten Anbieter sind gesperrt (z. B. Abo gekündigt).",
    )
    created = models.DateTimeField("angelegt", auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Abrechnungskonto"
        verbose_name_plural = "Abrechnungskonten"
        constraints = [
            models.CheckConstraint(
                condition=(Q(kind="monetary") & ~Q(currency=""))
                | (~Q(kind="monetary") & Q(currency="")),
                name="billing_account_currency_by_kind",
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def is_monetary(self) -> bool:
        return self.kind == self.Kind.MONETARY

    def clean(self):
        if self.kind == self.Kind.MONETARY and not self.currency:
            raise ValidationError({"currency": "Monetäre Konten brauchen eine Währung."})
        if self.kind != self.Kind.MONETARY:
            self.currency = ""
        if self.pk:
            old = BillingAccount.objects.filter(pk=self.pk).values("kind", "currency").first()
            changed = old and (old["kind"], old["currency"]) != (self.kind, self.currency)
            in_use = (
                ModelPrice.objects.filter(ai_model__provider__billing_account=self).exists()
                or UsageEntry.objects.filter(account=self).exists()
            )
            if changed and in_use:
                raise ValidationError(
                    "Art und Währung lassen sich nicht mehr ändern, weil es schon Preise "
                    "oder Buchungen gibt. Bitte ein neues Konto anlegen und die Anbieter "
                    "umhängen."
                )


class ModelPrice(models.Model):
    """Preisversion eines Modells; gilt ab ``valid_from`` bis zur nächsten Version."""

    ai_model = models.ForeignKey(
        "chat.AIModel", on_delete=models.CASCADE, related_name="prices", verbose_name="Modell"
    )
    valid_from = models.DateTimeField("gültig ab", default=timezone.now)
    input = models.DecimalField(
        "Eingabe", **MONEY, null=True, blank=True, help_text="je 1 Mio. Tokens"
    )
    cached_input = models.DecimalField(
        "Eingabe aus Cache", **MONEY, null=True, blank=True, help_text=PRICE_HELP
    )
    cache_write = models.DecimalField(
        "Cache schreiben (5 Min.)",
        **MONEY,
        null=True,
        blank=True,
        help_text=PRICE_HELP + " (Anthropic: 5-Minuten-Cache)",
    )
    cache_write_1h = models.DecimalField(
        "Cache schreiben (1 Std.)",
        **MONEY,
        null=True,
        blank=True,
        help_text="je 1 Mio. Tokens; leer = wie „Cache schreiben (5 Min.)“",
    )
    output = models.DecimalField(
        "Ausgabe",
        **MONEY,
        null=True,
        blank=True,
        help_text="je 1 Mio. Tokens, einschließlich Reasoning-/Nachdenk-Tokens",
    )
    long_context_threshold = models.PositiveIntegerField(
        "Langkontext ab (Tokens)",
        null=True,
        blank=True,
        help_text="Liegt die Eingabe einer Anfrage darüber, gelten für die ganze Anfrage "
        "die Langkontext-Preise (z. B. 200000). Leer = kein Langkontext-Tarif.",
    )
    long_input = models.DecimalField("Langkontext: Eingabe", **MONEY, null=True, blank=True)
    long_cached_input = models.DecimalField(
        "Langkontext: Eingabe aus Cache", **MONEY, null=True, blank=True
    )
    long_cache_write = models.DecimalField(
        "Langkontext: Cache schreiben (5 Min.)", **MONEY, null=True, blank=True
    )
    long_cache_write_1h = models.DecimalField(
        "Langkontext: Cache schreiben (1 Std.)", **MONEY, null=True, blank=True
    )
    long_output = models.DecimalField("Langkontext: Ausgabe", **MONEY, null=True, blank=True)
    unit_prices = models.JSONField(
        "Gebühren je Einheit",
        default=dict,
        blank=True,
        help_text='JSON, z. B. {"web_search": 0.01, "image:high": 0.17}. Schlüssel: '
        + ", ".join(UNITS),
    )
    note = models.CharField(
        "Quelle/Notiz", max_length=300, blank=True, help_text="z. B. Preisseite und Datum."
    )

    class Meta:
        ordering = ["ai_model", "-valid_from"]
        verbose_name = "Modellpreis"
        verbose_name_plural = "Modellpreise"
        constraints = [
            models.UniqueConstraint(
                fields=["ai_model", "valid_from"], name="billing_price_unique_model_valid_from"
            ),
        ]

    def __str__(self):
        return f"{self.ai_model} ab {timezone.localtime(self.valid_from):%d.%m.%Y}"

    TOKEN_FIELDS = ("input", "cached_input", "cache_write", "cache_write_1h", "output")

    @property
    def is_empty(self) -> bool:
        """Weder Token- noch Einheitenpreise: Das Modell gilt als „ohne Preis“."""
        return all(getattr(self, f) is None for f in self.TOKEN_FIELDS) and not self.unit_prices

    @property
    def costs_nothing(self) -> bool:
        """Kein Preis über 0 (Token- und Einheitenpreise): sperrt nie ein Budget."""
        names = self.TOKEN_FIELDS + tuple(f"long_{f}" for f in self.TOKEN_FIELDS)
        if any(getattr(self, f) for f in names):
            return False
        for value in (self.unit_prices or {}).values():
            try:
                if Decimal(str(value)) > 0:
                    return False
            except ArithmeticError:
                continue
        return True

    def clean(self):
        prices = self.unit_prices if self.unit_prices is not None else {}
        if not isinstance(prices, dict):
            raise ValidationError({"unit_prices": "Bitte ein JSON-Objekt {Einheit: Preis}."})
        errors = []
        for key, value in prices.items():
            try:
                validate_unit_key(key)
            except ValueError:
                errors.append(f"Unbekannte Einheit „{key}“.")
                continue
            try:
                if isinstance(value, bool) or Decimal(str(value)) < 0:
                    raise ValueError
            except (ArithmeticError, ValueError):
                errors.append(f"Preis für „{key}“ ist keine Zahl ≥ 0.")
        if errors:
            raise ValidationError({"unit_prices": errors})
        for name in self.TOKEN_FIELDS + tuple(f"long_{f}" for f in self.TOKEN_FIELDS):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValidationError({name: "Preise dürfen nicht negativ sein."})


class ExchangeRate(models.Model):
    class Source(models.TextChoices):
        MANUAL = "manual", "von Hand"
        ECB = "ecb", "EZB-Referenzkurs"

    date = models.DateField("Datum", unique=True, help_text="Gilt ab diesem Tag (Europe/Berlin).")
    usd_eur = models.DecimalField(
        "1 USD in EUR",
        max_digits=12,
        decimal_places=6,
        validators=[MinValueValidator(Decimal("0.000001"))],
        help_text="z. B. 0,86 (EZB veröffentlicht EUR->USD, z. B. 1,16; Kehrwert eintragen).",
    )
    source = models.CharField("Quelle", max_length=10, choices=Source.choices, default="manual")

    class Meta:
        ordering = ["-date"]
        verbose_name = "Wechselkurs"
        verbose_name_plural = "Wechselkurse"

    def __str__(self):
        return f"{self.date:%d.%m.%Y}: 1 USD = {self.usd_eur} EUR"


class UsageEntry(models.Model):
    """Buchung: Verbrauch einer Antwort, eines Anhangs oder eine Werkzeuggebühr.

    ``amount`` in der Kontowährung (NULL bei Token- und Pauschalkonten und
    ohne Preis), ``amount_eur`` über den Kurs zum Buchungszeitpunkt (NULL =
    unbekannt, z. B. Kurs fehlt; nie stillschweigend 0). Token-Felder: wie
    ``providers.base.Usage`` (Ein-/Ausgabe gesamt, Teilmengen daneben).
    Gebucht wird auf den Verfasser (``Message.author``, sonst Besitzer).
    """

    class Kind(models.TextChoices):
        ANSWER = "answer", "Antwort"
        ATTACHMENT = "attachment", "Datei/Anhang"
        TOOL = "tool", "Werkzeuggebühr"

    kind = models.CharField("Art", max_length=12, choices=Kind.choices, default=Kind.ANSWER)
    account = models.ForeignKey(
        BillingAccount, on_delete=models.PROTECT, related_name="entries", verbose_name="Konto"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Nutzer",
    )
    ai_model = models.ForeignKey(
        "chat.AIModel",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Modell",
    )
    model_name = models.CharField("Modellname", max_length=200, blank=True)
    # Bezug; bleibt bei gelöschten Chats als Buchung erhalten (SET_NULL).
    message = models.ForeignKey(
        "chat.Message",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="usage_entries",
        verbose_name="Nachricht",
    )
    attachment = models.ForeignKey(
        "chat.Attachment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="usage_entries",
        verbose_name="Anhang",
    )
    created = models.DateTimeField("Zeitpunkt", default=timezone.now)
    tokens_in = models.PositiveBigIntegerField("Eingabe", default=0)
    tokens_out = models.PositiveBigIntegerField("Ausgabe", default=0)
    cached_read = models.PositiveBigIntegerField("davon Cache gelesen", default=0)
    cache_write = models.PositiveBigIntegerField("davon Cache geschrieben (5 Min.)", default=0)
    cache_write_1h = models.PositiveBigIntegerField("davon Cache geschrieben (1 Std.)", default=0)
    reasoning = models.PositiveBigIntegerField("davon Reasoning", default=0)
    requests = models.PositiveIntegerField("Anfragen", default=0)
    units = models.JSONField("Einheiten", default=dict, blank=True)
    # Je Anbieteraufruf (Werkzeugrunden): nötig für den Langkontext-Tarif,
    # der je Anfrage gilt. [{"in", "out", "cr", "cw", "cw1h", "re"}]
    rounds = models.JSONField("Anfragen im Einzelnen", default=list, blank=True)
    amount = models.DecimalField("Betrag", **MONEY, null=True, blank=True)
    currency = models.CharField("Währung", max_length=3, blank=True)
    amount_eur = models.DecimalField("Betrag (EUR)", **MONEY, null=True, blank=True)
    rate = models.DecimalField(
        "Kurs USD->EUR", max_digits=12, decimal_places=6, null=True, blank=True
    )
    rate_date = models.DateField("Kurs vom", null=True, blank=True)
    price = models.ForeignKey(
        ModelPrice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Preisversion",
    )
    legacy = models.BooleanField(
        "aus Bestand", default=False, help_text="Aus den alten Kosten übernommen (Migration)."
    )

    class Meta:
        ordering = ["-created", "-id"]
        verbose_name = "Buchung"
        verbose_name_plural = "Buchungen"
        indexes = [
            models.Index(fields=["user", "created"], name="billing_entry_user_created"),
            models.Index(fields=["account", "created"], name="billing_entry_account_created"),
        ]
        constraints = [
            # Eine Buchung je Antwort bzw. Anhang (Fortsetzungen aktualisieren sie).
            models.UniqueConstraint(
                fields=["message"],
                condition=Q(kind="answer", message__isnull=False),
                name="billing_entry_unique_answer",
            ),
            models.UniqueConstraint(
                fields=["attachment"],
                condition=Q(kind="attachment", attachment__isnull=False),
                name="billing_entry_unique_attachment",
            ),
        ]

    def __str__(self):
        return f"Buchung {self.pk}"

    @property
    def eur_missing(self) -> bool:
        """Betrag bekannt, aber kein EUR-Betrag (Kurs fehlt)."""
        return self.amount is not None and self.amount_eur is None


class AccountBudget(models.Model):
    """Monatsbudget bzw. Token-Kontingent je Konto für eine Rolle oder ein Konto.

    Genau eines von ``role``/``user``. Nutzerwerte gehen vor Rollenwerten.
    Leer = unbegrenzt. Pauschalkonten haben kein Budget.
    """

    account = models.ForeignKey(
        BillingAccount, on_delete=models.CASCADE, related_name="budgets", verbose_name="Konto"
    )
    role = models.ForeignKey(
        "accounts.Role",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="account_budgets",
        verbose_name="Rolle",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="account_budgets",
        verbose_name="Nutzer",
    )
    monthly_budget = models.DecimalField(
        "Monatsbudget (EUR)",
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
        help_text="Nur monetäre Konten. Leer = unbegrenzt.",
    )
    monthly_tokens = models.PositiveBigIntegerField(
        "Monatskontingent (Tokens)",
        null=True,
        blank=True,
        help_text="Nur Token-Konten: Eingabe + Ausgabe je Kalendermonat. Leer = unbegrenzt.",
    )

    class Meta:
        verbose_name = "Budget je Konto"
        verbose_name_plural = "Budgets je Konto"
        constraints = [
            models.CheckConstraint(
                condition=Q(role__isnull=False, user__isnull=True)
                | Q(role__isnull=True, user__isnull=False),
                name="billing_budget_role_xor_user",
            ),
            models.UniqueConstraint(
                fields=["account", "role"],
                condition=Q(role__isnull=False),
                name="billing_budget_unique_role",
            ),
            models.UniqueConstraint(
                fields=["account", "user"],
                condition=Q(user__isnull=False),
                name="billing_budget_unique_user",
            ),
        ]

    def __str__(self):
        return f"{self.account}: {self.role or self.user}"

    def clean(self):
        if (self.role_id is None) == (self.user_id is None):
            raise ValidationError("Bitte entweder eine Rolle oder einen Nutzer wählen.")
        try:
            account = self.account  # auch ein noch ungespeichertes Konto (Inline)
        except BillingAccount.DoesNotExist:
            return
        if account.kind == BillingAccount.Kind.FLAT:
            raise ValidationError("Pauschalkonten haben kein Budget.")
        if account.kind == BillingAccount.Kind.MONETARY and self.monthly_tokens is not None:
            raise ValidationError({"monthly_tokens": "Monetäre Konten: Budget in EUR angeben."})
        if account.kind == BillingAccount.Kind.TOKENS and self.monthly_budget is not None:
            raise ValidationError({"monthly_budget": "Token-Konten: Kontingent in Tokens angeben."})
