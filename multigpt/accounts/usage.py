"""Verbrauch und Monatsbudgets (Plan 8, 8a, 8f; M6-02, M6-03, M6-10).

Grundlage sind die Buchungen (``billing.UsageEntry``, Kontenrahmen siehe
multigpt/billing): eine je Antwort, Anhang bzw. Gebühr, mit Tokens, Betrag in
Kontowährung und in EUR. Gebucht wird auf das Konto, das die Antwort ausgelöst
hat (``Message.author``; in geteilten Chats also der Absender), bei Altdaten
ohne Verfasser auf den Besitzer des Chats – auch ältere Versionen (Bearbeiten,
Neu erzeugen), abgebrochene Antworten, archivierte und gelöschte Chats.
Token- und Pauschalkonten (z. B. lokal) kosten nichts, Modelle ohne Preis
zählen 0. Beträge, deren Kurs fehlte, zählen geschätzt (nie 0).

Dieses Modul bleibt die Fassade für Oberfläche und Rechte; Budgets je Konto
und die Sperre stehen in ``billing.budgets``.

Monate sind Kalendermonate in Europe/Berlin: Eine Antwort am 1. um 00:30
Ortszeit gehört schon zum neuen Monat, auch wenn sie in UTC noch im alten liegt.

Alle Summen laufen als Aggregation in der Datenbank (eine Abfrage je
Tabelle), nie über Python-Schleifen über Nachrichten.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from django.db.models import Count, DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce, TruncMonth
from django.utils import timezone

from multigpt.billing import budgets

USAGE_TIME_ZONE = ZoneInfo("Europe/Berlin")
WARNING_RATIO = budgets.WARNING_RATIO

LEVEL_OK = budgets.LEVEL_OK
LEVEL_WARNING = budgets.LEVEL_WARNING
LEVEL_EXHAUSTED = budgets.LEVEL_EXHAUSTED

BUDGET_EXHAUSTED_MESSAGE = budgets.TOTAL_EXHAUSTED_MESSAGE

MONTH_NAMES = (
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
)

_ZERO = Decimal(0)
_MONEY = DecimalField(max_digits=14, decimal_places=6)


# --- Monate ---------------------------------------------------------------------


def month_start(year: int, month: int) -> dt.datetime:
    """Beginn des Kalendermonats (00:00 Europe/Berlin) als aware datetime."""
    return dt.datetime(year, month, 1, tzinfo=USAGE_TIME_ZONE)


def _add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def month_bounds(now: dt.datetime | dt.date | None = None) -> tuple[dt.datetime, dt.datetime]:
    """(Beginn, Ende) des Kalendermonats von ``now`` in Europe/Berlin; Ende exklusiv.

    ``now``: aware datetime (beliebige Zeitzone), ein Datum (gilt als
    Berliner Datum) oder None (jetzt).
    """
    if now is None:
        now = timezone.now()
    if isinstance(now, dt.datetime):
        if timezone.is_naive(now):
            now = now.replace(tzinfo=USAGE_TIME_ZONE)
        local = now.astimezone(USAGE_TIME_ZONE)
        year, month = local.year, local.month
    else:
        year, month = now.year, now.month
    next_year, next_month = _add_months(year, month, 1)
    return month_start(year, month), month_start(next_year, next_month)


def month_label(start: dt.datetime | dt.date) -> str:
    """„Oktober 2026“."""
    return f"{MONTH_NAMES[start.month - 1]} {start.year}"


def month_choice(raw: str, months: int = 12, now=None) -> tuple[list[dt.date], dt.date]:
    """Auswahl „JJJJ-MM“ aus den letzten ``months`` Monaten: (Optionen, gewählt).

    Unbekannte Werte ergeben den laufenden Monat (kein Fehler, kein Zugriff
    auf beliebige Zeiträume).
    """
    current, _ = month_bounds(now)
    options = [dt.date(*_add_months(current.year, current.month, -i), 1) for i in range(months)]
    selected = next((o for o in options if raw == f"{o:%Y-%m}"), options[0])
    return options, selected


# --- Summen ----------------------------------------------------------------------


def _user_filter(user) -> Q:
    """Filter auf das zahlende Konto der Buchung: ein Konto, mehrere (ids/QuerySet) oder alle."""
    if user is None:
        return Q()
    if hasattr(user, "pk") and not hasattr(user, "model"):
        return Q(user_id=user.pk)
    return Q(user__in=user)


def _range(field: str, start, end) -> Q:
    q = Q()
    if start is not None:
        q &= Q(**{f"{field}__gte": start})
    if end is not None:
        q &= Q(**{f"{field}__lt": end})
    return q


def _entries(user, start, end):
    from multigpt.billing.models import UsageEntry

    return UsageEntry.objects.filter(_user_filter(user) & _range("created", start, end))


def _money_sums() -> dict:
    """EUR-Summe (bekannt) und Summe der Beträge ohne EUR (Kurs fehlte)."""
    return {
        "eur": Coalesce(Sum("amount_eur"), Value(_ZERO), output_field=_MONEY),
        "unknown": Coalesce(
            Sum("amount", filter=Q(amount_eur__isnull=True)), Value(_ZERO), output_field=_MONEY
        ),
    }


class _Rate:
    """Ersatzkurs für Beträge ohne EUR, höchstens eine Abfrage (budgets.fallback_rate)."""

    def __init__(self):
        self.value = None

    def eur(self, row) -> Decimal:
        total = Decimal(row["eur"])
        if row["unknown"]:
            if self.value is None:
                self.value = budgets.fallback_rate()
            total += Decimal(row["unknown"]) * self.value
        return total


def spent(user, month: dt.datetime | dt.date | None = None) -> Decimal:
    """Kosten (EUR) von ``user`` im Kalendermonat von ``month`` über alle Konten.

    Summe der Buchungen (Antworten aller Versionen, Anhänge, Gebühren). Beträge
    ohne Kurs zählen geschätzt (``budgets.fallback_rate``), nie als 0.
    """
    start, end = month_bounds(month)
    row = _entries(user, start, end).aggregate(**_money_sums())
    return _Rate().eur(row)


def spent_by_user(users, start: dt.datetime, end: dt.datetime) -> dict[int, Decimal]:
    """{user_id: Kosten (EUR)} für mehrere Konten im Zeitraum (eine Abfrage).

    ``users``: Konten, ids oder QuerySet; None = alle. Konten ohne Verbrauch fehlen.
    """
    rate = _Rate()
    rows = _entries(users, start, end).values("user_id").annotate(**_money_sums())
    return {row["user_id"]: rate.eur(row) for row in rows if row["user_id"] is not None}


def usage_by_model(
    user, start: dt.datetime | None, end: dt.datetime | None, *, by_user: bool = False
) -> list[dict]:
    """Verbrauch je Modell (optional je Nutzer und Modell) im Zeitraum.

    ``user``: ein Konto, mehrere (ids/QuerySet) oder None (alle; nur für
    Verwalter mit VIEW_USAGE_ALL). Zeilen: ``model_id`` (None = gelöschtes
    Modell), ``model``, ``provider``, ``is_local``, ``account``, ``answers``
    (Antworten, alle Versionen), ``tokens_in``, ``tokens_out``, ``files``
    (erzeugte Dateien, z. B. Bilder), ``cost`` (EUR) – teuerste zuerst. Mit
    ``by_user`` zusätzlich ``user_id``.
    """
    from multigpt.billing.models import UsageEntry

    rate = _Rate()
    user_field = ["user_id"] if by_user else []
    rows = (
        _entries(user, start, end)
        .values(
            *user_field,
            "ai_model",
            "model_name",
            "ai_model__display_name",
            "ai_model__provider__name",
            "ai_model__provider__is_local",
            "account__name",
        )
        .annotate(
            answers=Count("pk", filter=Q(kind=UsageEntry.Kind.ANSWER)),
            files=Count("pk", filter=Q(kind=UsageEntry.Kind.ATTACHMENT)),
            tokens_in=Coalesce(Sum("tokens_in"), 0),
            tokens_out=Coalesce(Sum("tokens_out"), 0),
            **_money_sums(),
        )
    )
    result: dict[tuple, dict] = {}
    for r in rows:
        key = (r.get("user_id"), r["ai_model"], r["account__name"])
        row = result.setdefault(
            key,
            {
                "model_id": r["ai_model"],
                "model": r["ai_model__display_name"] or r["model_name"] or "Gelöschtes Modell",
                "provider": r["ai_model__provider__name"] or "",
                "is_local": bool(r["ai_model__provider__is_local"]),
                "account": r["account__name"],
                "answers": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "files": 0,
                "cost": _ZERO,
            },
        )
        if by_user:
            row["user_id"] = r["user_id"]
        for name in ("answers", "files", "tokens_in", "tokens_out"):
            row[name] += r[name]
        row["cost"] += rate.eur(r)
    return sorted(result.values(), key=lambda r: (-r["cost"], -r["tokens_out"], r["model"]))


def usage_by_account(user, start, end, *, by_user: bool = False) -> list[dict]:
    """Verbrauch je Abrechnungskonto (optional je Nutzer) im Zeitraum, eine Abfrage.

    Zeilen: ``account_id``, ``account``, ``kind`` (monetary | tokens | flat),
    ``currency``, ``answers``, ``files``, ``requests``, ``tokens_in``,
    ``tokens_out``, ``cached_read``, ``cache_write`` (5 Min. + 1 Std.),
    ``reasoning``, ``amount`` (Kontowährung, nur bepreiste Buchungen), ``eur``
    (bekannt), ``eur_missing`` (Buchungen ohne Kurs), ``unpriced`` (monetär
    ohne Preis). Mit ``by_user`` zusätzlich ``user_id``.
    """
    from multigpt.billing.models import UsageEntry

    user_field = ["user_id"] if by_user else []
    rows = (
        _entries(user, start, end)
        .values(*user_field, "account_id", "account__name", "account__kind", "account__currency")
        .annotate(
            answers=Count("pk", filter=Q(kind=UsageEntry.Kind.ANSWER)),
            files=Count("pk", filter=Q(kind=UsageEntry.Kind.ATTACHMENT)),
            requests=Coalesce(Sum("requests"), 0),
            tokens_in=Coalesce(Sum("tokens_in"), 0),
            tokens_out=Coalesce(Sum("tokens_out"), 0),
            cached_read=Coalesce(Sum("cached_read"), 0),
            cache_write=Coalesce(Sum(F("cache_write") + F("cache_write_1h")), 0),
            reasoning=Coalesce(Sum("reasoning"), 0),
            amount_sum=Sum("amount"),
            eur_sum=Sum("amount_eur"),
            eur_missing=Count("pk", filter=Q(amount__isnull=False, amount_eur__isnull=True)),
            unpriced=Count(
                "pk", filter=Q(account__kind="monetary", amount__isnull=True, legacy=False)
            ),
        )
        .order_by("account__name")
    )
    result = []
    for r in rows:
        result.append(
            {
                **({"user_id": r["user_id"]} if by_user else {}),
                "account_id": r["account_id"],
                "account": r["account__name"],
                "kind": r["account__kind"],
                "currency": r["account__currency"],
                **{
                    k: r[k]
                    for k in (
                        "answers",
                        "files",
                        "requests",
                        "tokens_in",
                        "tokens_out",
                        "cached_read",
                        "cache_write",
                        "reasoning",
                        "eur_missing",
                        "unpriced",
                    )
                },
                "amount": r["amount_sum"],
                "eur": r["eur_sum"],
            }
        )
    return result


def monthly_totals(user, months: int = 12, now: dt.datetime | None = None) -> list[dict]:
    """Kosten der letzten ``months`` Kalendermonate (inkl. laufendem), neueste zuerst.

    Zeilen: ``start`` (aware, Europe/Berlin), ``year``, ``month``, ``label``,
    ``cost`` (EUR), ``tokens_in``, ``tokens_out``, ``answers``. Monate ohne
    Verbrauch sind mit 0 enthalten. Gruppiert wird in der Datenbank
    (TruncMonth in Europe/Berlin).
    """
    from multigpt.billing.models import UsageEntry

    current, end = month_bounds(now)
    first = month_start(*_add_months(current.year, current.month, -(months - 1)))
    result: dict[tuple[int, int], dict] = {}
    for i in range(months):
        year, month = _add_months(current.year, current.month, -i)
        result[(year, month)] = {
            "start": month_start(year, month),
            "year": year,
            "month": month,
            "label": month_label(month_start(year, month)),
            "cost": _ZERO,
            "tokens_in": 0,
            "tokens_out": 0,
            "answers": 0,
        }

    def local_key(value) -> tuple[int, int]:
        if isinstance(value, dt.datetime):
            value = value.astimezone(USAGE_TIME_ZONE) if timezone.is_aware(value) else value
        return value.year, value.month

    rate = _Rate()
    rows = (
        _entries(user, first, end)
        .annotate(period=TruncMonth("created", tzinfo=USAGE_TIME_ZONE))
        .values("period")
        .annotate(
            tokens_in=Coalesce(Sum("tokens_in"), 0),
            tokens_out=Coalesce(Sum("tokens_out"), 0),
            answers=Count("pk", filter=Q(kind=UsageEntry.Kind.ANSWER)),
            **_money_sums(),
        )
    )
    for m in rows:
        row = result.get(local_key(m["period"]))
        if row is not None:
            row["cost"] += rate.eur(m)
            row["tokens_in"] += m["tokens_in"]
            row["tokens_out"] += m["tokens_out"]
            row["answers"] += m["answers"]
    return list(result.values())


# --- Budget -----------------------------------------------------------------------


def budget_for(user) -> Decimal | None:
    """Wirksames Gesamt-Monatsbudget (EUR): Konto-Override vor Rolle; None = unbegrenzt."""
    return user.monthly_budget


@dataclass(frozen=True)
class BudgetState:
    spent: Decimal
    budget: Decimal | None  # None = unbegrenzt
    ratio: Decimal | None  # spent / budget; None ohne Budget
    level: str  # ok | warning (>= 80 %) | exhausted (>= 100 %)
    remaining: Decimal | None

    @property
    def percent(self) -> int | None:
        """Verbrauch in ganzen Prozent (abgerundet), None ohne Budget."""
        if self.ratio is None:
            return None
        return int(self.ratio * 100)

    @property
    def exhausted(self) -> bool:
        return self.level == LEVEL_EXHAUSTED

    def as_dict(self) -> dict:
        return asdict(self)


def level_for(spent_amount: Decimal, budget: Decimal | None) -> str:
    return budgets.level_for(spent_amount, budget)


def budget_state(user, now: dt.datetime | None = None) -> BudgetState:
    """Gesamtbudget im laufenden Monat: Verbrauch (alle Konten), Budget, Anteil, Stufe."""
    budget = budget_for(user)
    amount = spent(user, now)
    if budget is None:
        ratio = remaining = None
    else:
        ratio = (amount / budget) if budget > 0 else Decimal(1)
        remaining = max(budget - amount, _ZERO)
    return BudgetState(amount, budget, ratio, level_for(amount, budget), remaining)


def account_states(user, now: dt.datetime | None = None) -> list:
    """Budgets je Abrechnungskonto (ohne Gesamt), siehe ``billing.budgets.AccountState``."""
    return list(budgets.snapshot(user, now).accounts.values())


def model_is_free(ai_model) -> bool:
    """Verursacht das Modell keine Kosten? Token-/Pauschalkonto (z. B. lokal) oder
    ohne gültigen Preis.

    Begründung: Kosten entstehen nur über die hinterlegten Preise
    (``billing.ModelPrice``). Ein Modell ohne Preis erhöht den Verbrauch nie –
    es zu sperren, schützte das Budget nicht. Damit Cloud-Modelle nicht
    versehentlich frei bleiben, müssen Verwalter Preise eintragen (Admin).
    """
    return budgets.is_free(ai_model)


def blocked_reason(user, ai_model) -> str:
    """Warum das Budget das Modell gerade sperrt (Konto bzw. gesamt); leer = frei."""
    return budgets.blocked_reason(user, ai_model)


def blocked_reasons(user, ai_models) -> dict[int, str]:
    return budgets.blocked_reasons(user, ai_models)


def warnings_for(user, ai_model) -> list[str]:
    """Hinweise ab 80 % für das Konto des Modells und das Gesamtbudget."""
    return budgets.warnings_for(user, ai_model)


def billing_title(ai_model) -> str:
    """Preisinfo und Kontoname für den title der Modellauswahl."""
    from multigpt.billing.booking import account_of
    from multigpt.billing.pricing import describe, price_at

    return describe(price_at(ai_model), account_of(ai_model.provider))


def warning_text(state: BudgetState) -> str:
    """Hinweis zum Gesamtbudget für Oberfläche und SSE-Status; leer bei Stufe ok."""
    if state.level == LEVEL_EXHAUSTED:
        return BUDGET_EXHAUSTED_MESSAGE
    if state.level == LEVEL_WARNING:
        return (
            f"{state.percent} % deines Monatsbudgets sind verbraucht "
            f"({format_eur(state.spent)} von {format_eur(state.budget)})."
        )
    return ""


# --- Anzeige ----------------------------------------------------------------------


def format_eur(value) -> str:
    """Euro deutsch: 2 Nachkommastellen, bei Beträgen unter 0,01 € 4 Stellen."""
    if value is None:
        return "–"
    value = Decimal(value)
    places = Decimal("0.0001") if 0 < abs(value) < Decimal("0.01") else Decimal("0.01")
    text = f"{value.quantize(places, rounding=ROUND_HALF_UP):,}"
    text = text.replace(",", " ").replace(".", ",")
    return f"{text} €"


def format_tokens(value) -> str:
    """Ganzzahl mit Tausenderpunkten: 12345 -> „12.345 Tokens“."""
    try:
        return f"{int(value):,} Tokens".replace(",", ".")
    except (TypeError, ValueError):
        return "–"


def format_amount(value, currency: str) -> str:
    """Betrag in Kontowährung: EUR wie ``format_eur``, USD „1,23 $“."""
    if currency != "USD":
        return format_eur(value)
    return format_eur(value).replace(" €", " $")
