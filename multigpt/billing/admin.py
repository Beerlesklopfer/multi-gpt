"""Admin „Kosten und Abrechnung“ (M6-06 bis M6-10).

- Abrechnungskonten mit Budgets je Rolle bzw. Nutzer (Inline) und der Liste
  der zugeordneten Anbieter. Art und Währung sind fest, sobald es Preise oder
  Buchungen gibt (``BillingAccount.clean``).
- Modellpreise mit Historie (auch als Inline am Modell, siehe chat/admin.py).
  Eine Preisänderung ist eine neue Zeile mit neuem „gültig ab“; alte Buchungen
  behalten Betrag und Preisversion.
- Wechselkurse: von Hand, optional EZB-Abruf (``ecb``, standardmäßig aus).
  Nach dem Speichern werden fehlende EUR-Beträge nachgetragen.
- Buchungen nur lesend, ohne Inhalte; CSV-Export je Monat bzw. Konto.
"""

import csv
import datetime as dt
import json
from decimal import Decimal

from django.contrib import admin, messages
from django.http import HttpResponse, HttpResponseRedirect
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html_join

from . import booking, ecb
from .models import AccountBudget, BillingAccount, ExchangeRate, ModelPrice, UsageEntry

MISSING_RATE_WARNING = (
    "{count} Buchung(en) in USD haben noch keinen EUR-Betrag, weil für ihren Tag kein "
    "Wechselkurs gepflegt ist. Sie zählen für Budgets vorläufig mit dem neuesten Kurs "
    "(ohne Kurs 1:1). Bitte unter „Wechselkurse“ einen Kurs eintragen."
)


def _warn_missing_rates(request):
    count = booking.missing_eur_count()
    if count:
        messages.warning(request, MISSING_RATE_WARNING.format(count=count))


class AccountBudgetInline(admin.TabularInline):
    model = AccountBudget
    extra = 0
    fields = ["role", "user", "monthly_budget", "monthly_tokens"]
    verbose_name = "Budget je Rolle bzw. Nutzer"
    verbose_name_plural = (
        "Budgets je Rolle bzw. Nutzer (Nutzer vor Rolle; monetär in EUR, Token-Konten in "
        "Tokens; Pauschalkonten ohne Budget)"
    )


@admin.register(BillingAccount)
class BillingAccountAdmin(admin.ModelAdmin):
    list_display = ["name", "kind", "currency", "active", "provider_list"]
    list_filter = ["kind", "currency", "active"]
    search_fields = ["name", "note"]
    fields = ["name", "kind", "currency", "active", "note", "provider_list"]
    readonly_fields = ["provider_list"]
    inlines = [AccountBudgetInline]

    @admin.display(description="Anbieter")
    def provider_list(self, obj):
        if obj is None or obj.pk is None:
            return "–"
        providers = list(obj.providers.order_by("name"))
        if not providers:
            return "–"
        return format_html_join(
            ", ",
            '<a href="{}">{}</a>',
            ((reverse("admin:chat_provider_change", args=[p.pk]), p.name) for p in providers),
        )

    def changelist_view(self, request, extra_context=None):
        _warn_missing_rates(request)
        return super().changelist_view(request, extra_context)


PRICE_FIELDSETS = [
    (
        "Preise je 1 Mio. Tokens (Währung des Kontos)",
        {
            "fields": [
                "input",
                "cached_input",
                "cache_write",
                "cache_write_1h",
                "output",
            ],
            "description": "Reasoning-/Nachdenk-Tokens zählen als Ausgabe. Leere Cache-Preise "
            "rechnen wie die Eingabe.",
        },
    ),
    (
        "Langkontext-Tarif (optional)",
        {
            "fields": [
                "long_context_threshold",
                "long_input",
                "long_cached_input",
                "long_cache_write",
                "long_cache_write_1h",
                "long_output",
            ],
            "classes": ["collapse"],
            "description": "Gilt je Anfrage, wenn deren gesamte Eingabe (inkl. Cache) über der "
            "Schwelle liegt – dann für alle Tokens dieser Anfrage. Leere Felder: Normalpreis.",
        },
    ),
    ("Gebühren je Einheit", {"fields": ["unit_prices", "note"]}),
]


class ModelPriceInline(admin.StackedInline):
    """Preisversionen am Modell (chat/admin.py, AIModelAdmin)."""

    model = ModelPrice
    extra = 0
    fieldsets = [(None, {"fields": ["valid_from"]}), *PRICE_FIELDSETS]
    verbose_name = "Preisversion"
    verbose_name_plural = "Preise (neueste zuerst; Änderung = neue Version mit neuem „gültig ab“)"


@admin.register(ModelPrice)
class ModelPriceAdmin(admin.ModelAdmin):
    list_display = [
        "ai_model",
        "account",
        "valid_from",
        "input",
        "cached_input",
        "cache_write",
        "output",
        "long_context_threshold",
        "units_short",
    ]
    list_filter = ["ai_model__provider__billing_account", "ai_model__provider"]
    search_fields = ["ai_model__display_name", "ai_model__model_id", "note"]
    list_select_related = ["ai_model__provider__billing_account"]
    date_hierarchy = "valid_from"
    fieldsets = [(None, {"fields": ["ai_model", "valid_from"]}), *PRICE_FIELDSETS]

    @admin.display(description="Konto", ordering="ai_model__provider__billing_account__name")
    def account(self, obj):
        account = obj.ai_model.provider.billing_account
        if account is None:
            return "–"
        return f"{account.name} ({account.currency or account.get_kind_display()})"

    @admin.display(description="Einheiten")
    def units_short(self, obj):
        return ", ".join(f"{k}={v}" for k, v in (obj.unit_prices or {}).items()) or "–"


@admin.register(ExchangeRate)
class ExchangeRateAdmin(admin.ModelAdmin):
    list_display = ["date", "usd_eur", "source"]
    list_filter = ["source"]
    date_hierarchy = "date"
    change_list_template = "admin/billing/exchangerate/change_list.html"

    def get_urls(self):
        return [
            path(
                "ezb/",
                self.admin_site.admin_view(self.fetch_ecb_view),
                name="billing_exchangerate_fetch_ecb",
            ),
            *super().get_urls(),
        ]

    def changelist_view(self, request, extra_context=None):
        _warn_missing_rates(request)
        extra_context = {**(extra_context or {}), "ecb_enabled": ecb.enabled()}
        return super().changelist_view(request, extra_context)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        filled = booking.fill_missing_eur()
        if filled:
            self.message_user(request, f"{filled} EUR-Betrag/-Beträge nachgetragen.")

    def fetch_ecb_view(self, request):
        if request.method != "POST" or not self.has_add_permission(request):
            return HttpResponseRedirect(reverse("admin:billing_exchangerate_changelist"))
        try:
            rate, created = ecb.fetch()
        except ecb.EcbError as exc:
            self.message_user(request, str(exc), messages.ERROR)
        else:
            self.message_user(
                request, f"EZB-Kurs {'gespeichert' if created else 'vorhanden'}: {rate}"
            )
        return HttpResponseRedirect(reverse("admin:billing_exchangerate_changelist"))


# --- Buchungen ---------------------------------------------------------------------

CSV_COLUMNS = [
    ("Zeitpunkt", lambda e: timezone.localtime(e.created, booking.TIME_ZONE).isoformat()),
    ("Konto", lambda e: e.account.name),
    ("Kontoart", lambda e: e.account.kind),
    ("Buchungsart", lambda e: e.kind),
    ("Nutzer", lambda e: e.user.get_username() if e.user else ""),
    ("Modell", lambda e: e.model_name),
    ("Eingabe", lambda e: e.tokens_in),
    ("Ausgabe", lambda e: e.tokens_out),
    ("Cache gelesen", lambda e: e.cached_read),
    ("Cache geschrieben 5 Min", lambda e: e.cache_write),
    ("Cache geschrieben 1 Std", lambda e: e.cache_write_1h),
    ("Reasoning", lambda e: e.reasoning),
    ("Anfragen", lambda e: e.requests),
    ("Einheiten", lambda e: json.dumps(e.units, sort_keys=True) if e.units else ""),
    ("Betrag", lambda e: e.amount),
    ("Währung", lambda e: e.currency),
    ("Betrag EUR", lambda e: e.amount_eur),
    ("Kurs USD-EUR", lambda e: e.rate),
    ("Kurs vom", lambda e: e.rate_date.isoformat() if e.rate_date else ""),
    ("Preisversion", lambda e: e.price_id or ""),
    ("Altbestand", lambda e: "ja" if e.legacy else ""),
]


def _csv_value(value) -> str:
    """Zahlen mit Dezimalkomma (deutsches Excel); Formel-Injektion entschärfen."""
    if value is None:
        return ""
    if isinstance(value, int | Decimal) and not isinstance(value, bool):
        return str(value).replace(".", ",")
    text = str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        text = "'" + text
    return text


def parse_month(raw: str):
    """„JJJJ-MM“ -> (Beginn, Ende) in Europe/Berlin; None bei leer/ungültig."""
    from multigpt.accounts.usage import month_bounds

    try:
        year, month = (int(x) for x in (raw or "").split("-"))
        return month_bounds(dt.date(year, month, 1))
    except (TypeError, ValueError):
        return None


def entries_csv(queryset) -> HttpResponse:
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response.write("﻿")  # BOM: Excel erkennt UTF-8
    writer = csv.writer(response, delimiter=";")
    writer.writerow([name for name, _ in CSV_COLUMNS])
    for entry in queryset.select_related("account", "user").order_by("created", "pk").iterator():
        writer.writerow([_csv_value(get(entry)) for _, get in CSV_COLUMNS])
    return response


@admin.register(UsageEntry)
class UsageEntryAdmin(admin.ModelAdmin):
    """Nur lesend; ohne Chatinhalte (nur Metadaten und Zahlen)."""

    list_display = [
        "created",
        "user",
        "account",
        "kind",
        "model_name",
        "tokens_in",
        "tokens_out",
        "cached_read",
        "reasoning",
        "amount_text",
        "eur_text",
        "legacy",
    ]
    list_filter = ["account", "kind", "legacy", "currency"]
    search_fields = ["model_name", "user__username", "user__display_name"]
    list_select_related = ["account", "user"]
    date_hierarchy = "created"
    change_list_template = "admin/billing/usageentry/change_list.html"
    exclude = ["message", "attachment"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields if f.name not in self.exclude]

    @admin.display(description="Betrag", ordering="amount")
    def amount_text(self, obj):
        if obj.amount is None:
            if obj.account.kind != BillingAccount.Kind.MONETARY:
                return "–"
            return "ohne Preis"
        return f"{obj.amount} {obj.currency}"

    @admin.display(description="EUR", ordering="amount_eur")
    def eur_text(self, obj):
        if obj.eur_missing:
            return "Kurs fehlt"
        return "–" if obj.amount_eur is None else obj.amount_eur

    def get_urls(self):
        return [
            path(
                "csv/",
                self.admin_site.admin_view(self.csv_view),
                name="billing_usageentry_csv",
            ),
            *super().get_urls(),
        ]

    def changelist_view(self, request, extra_context=None):
        _warn_missing_rates(request)
        from multigpt.accounts.usage import month_choice, month_label

        options, selected = month_choice("")
        extra_context = {
            **(extra_context or {}),
            "csv_months": [(f"{o:%Y-%m}", month_label(o)) for o in options],
            "csv_accounts": BillingAccount.objects.order_by("name"),
        }
        return super().changelist_view(request, extra_context)

    def csv_view(self, request):
        """GET ?monat=JJJJ-MM&konto=<id>: Buchungen als CSV (beides optional)."""
        if not self.has_view_permission(request):
            return HttpResponseRedirect(reverse("admin:index"))
        qs = UsageEntry.objects.all()
        name = ["buchungen"]
        bounds = parse_month(request.GET.get("monat", ""))
        if bounds is not None:
            qs = qs.filter(created__gte=bounds[0], created__lt=bounds[1])
            name.append(f"{bounds[0]:%Y-%m}")
        account_id = request.GET.get("konto", "")
        if account_id.isdigit():
            qs = qs.filter(account_id=int(account_id))
            name.append(f"konto{account_id}")
        response = entries_csv(qs)
        response["Content-Disposition"] = f'attachment; filename="{"-".join(name)}.csv"'
        return response
