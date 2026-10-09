"""Verbrauch und Monatsbudgets (Plan 8, 8a, 8f; M6-02, M6-03).

Grundlage sind die Momentaufnahmen ``Message.cost`` und ``Attachment.cost``
(Euro, zum Zeitpunkt der Antwort bzw. Erzeugung festgehalten). Gezählt wird
alles, was in den Chats eines Kontos liegt – auch ältere Versionen
(Bearbeiten, Neu erzeugen), abgebrochene Antworten und archivierte Chats.
Lokale Modelle haben Kosten 0, Modelle ohne Preise ``NULL`` (zählt als 0).

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

from django.db.models import Count, DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce, TruncMonth
from django.utils import timezone

USAGE_TIME_ZONE = ZoneInfo("Europe/Berlin")
WARNING_RATIO = Decimal("0.8")

LEVEL_OK = "ok"
LEVEL_WARNING = "warning"
LEVEL_EXHAUSTED = "exhausted"

BUDGET_EXHAUSTED_MESSAGE = (
    "Monatsbudget ausgeschöpft – bis zum Monatsende sind nur lokale Modelle nutzbar."
)

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


# --- Summen ----------------------------------------------------------------------


def _user_filter(prefix: str, user) -> Q:
    """Filter auf den Besitzer der Chats: ein Konto, mehrere (ids/QuerySet) oder alle."""
    field = f"{prefix}conversation__user"
    if user is None:
        return Q()
    if hasattr(user, "pk") and not hasattr(user, "model"):
        return Q(**{field: user.pk})
    return Q(**{f"{field}__in": user})


def _range(field: str, start, end) -> Q:
    q = Q()
    if start is not None:
        q &= Q(**{f"{field}__gte": start})
    if end is not None:
        q &= Q(**{f"{field}__lt": end})
    return q


def _sum(field: str):
    return Coalesce(Sum(field), Value(_ZERO), output_field=_MONEY)


def spent(user, month: dt.datetime | dt.date | None = None) -> Decimal:
    """Kosten (Euro) aller Chats von ``user`` im Kalendermonat von ``month``.

    Summe aus ``Message.cost`` und ``Attachment.cost`` inkl. aller Versionen.
    """
    from multigpt.chat.models import Attachment, Message

    start, end = month_bounds(month)
    messages = Message.objects.filter(
        _user_filter("", user) & _range("created", start, end)
    ).aggregate(total=_sum("cost"))["total"]
    attachments = Attachment.objects.filter(
        _user_filter("message__", user) & _range("created", start, end)
    ).aggregate(total=_sum("cost"))["total"]
    return Decimal(messages) + Decimal(attachments)


def spent_by_user(users, start: dt.datetime, end: dt.datetime) -> dict[int, Decimal]:
    """{user_id: Kosten} für mehrere Konten im Zeitraum (je Tabelle eine Abfrage).

    ``users``: Konten, ids oder QuerySet; None = alle. Konten ohne Verbrauch fehlen.
    """
    from multigpt.chat.models import Attachment, Message

    totals: dict[int, Decimal] = {}
    rows = (
        Message.objects.filter(_user_filter("", users) & _range("created", start, end))
        .values("conversation__user")
        .annotate(total=_sum("cost"))
    )
    for row in rows:
        uid = row["conversation__user"]
        totals[uid] = totals.get(uid, _ZERO) + Decimal(row["total"])
    rows = (
        Attachment.objects.filter(_user_filter("message__", users) & _range("created", start, end))
        .values("message__conversation__user")
        .annotate(total=_sum("cost"))
    )
    for row in rows:
        uid = row["message__conversation__user"]
        totals[uid] = totals.get(uid, _ZERO) + Decimal(row["total"])
    return totals


def usage_by_model(
    user, start: dt.datetime | None, end: dt.datetime | None, *, by_user: bool = False
) -> list[dict]:
    """Verbrauch je Modell (optional je Konto und Modell) im Zeitraum.

    ``user``: ein Konto, mehrere (ids/QuerySet) oder None (alle; nur für
    Verwalter mit VIEW_USAGE_ALL). Zeilen: ``model_id`` (None = gelöschtes
    Modell), ``model``, ``provider``, ``is_local``, ``answers`` (Antworten,
    alle Versionen), ``tokens_in``, ``tokens_out``, ``files`` (erzeugte
    Dateien, z. B. Bilder), ``cost`` – teuerste zuerst. Mit ``by_user`` zusätzlich
    ``user_id``.
    """
    from multigpt.chat.models import Attachment, Message

    rows: dict[tuple, dict] = {}

    def row_for(key: tuple, model_id, name, provider, is_local, user_id):
        if key not in rows:
            rows[key] = {
                "model_id": model_id,
                "model": name or "Gelöschtes Modell",
                "provider": provider or "",
                "is_local": bool(is_local),
                "answers": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "files": 0,
                "cost": _ZERO,
            }
            if by_user:
                rows[key]["user_id"] = user_id
        return rows[key]

    user_field = ["conversation__user"] if by_user else []
    messages = (
        Message.objects.filter(
            _user_filter("", user) & _range("created", start, end) & Q(role=Message.Role.ASSISTANT)
        )
        .values(
            *user_field,
            "model",
            "model__display_name",
            "model__provider__name",
            "model__provider__is_local",
        )
        .annotate(
            answers=Count("pk"),
            tokens_in=Coalesce(Sum("tokens_in"), 0),
            tokens_out=Coalesce(Sum("tokens_out"), 0),
            cost=_sum("cost"),
        )
    )
    for m in messages:
        uid = m.get("conversation__user")
        r = row_for(
            (uid, m["model"]),
            m["model"],
            m["model__display_name"],
            m["model__provider__name"],
            m["model__provider__is_local"],
            uid,
        )
        r["answers"] += m["answers"]
        r["tokens_in"] += m["tokens_in"]
        r["tokens_out"] += m["tokens_out"]
        r["cost"] += Decimal(m["cost"])

    user_field = ["message__conversation__user"] if by_user else []
    attachments = (
        Attachment.objects.filter(
            _user_filter("message__", user)
            & _range("created", start, end)
            & (Q(cost__isnull=False) | Q(generated_by_model__isnull=False))
        )
        .values(
            *user_field,
            "generated_by_model",
            "generated_by_model__display_name",
            "generated_by_model__provider__name",
            "generated_by_model__provider__is_local",
        )
        .annotate(files=Count("pk"), cost=_sum("cost"))
    )
    for a in attachments:
        uid = a.get("message__conversation__user")
        r = row_for(
            (uid, a["generated_by_model"]),
            a["generated_by_model"],
            a["generated_by_model__display_name"],
            a["generated_by_model__provider__name"],
            a["generated_by_model__provider__is_local"],
            uid,
        )
        r["files"] += a["files"]
        r["cost"] += Decimal(a["cost"])

    return sorted(rows.values(), key=lambda r: (-r["cost"], -r["tokens_out"], r["model"]))


def monthly_totals(user, months: int = 12, now: dt.datetime | None = None) -> list[dict]:
    """Kosten der letzten ``months`` Kalendermonate (inkl. laufendem), neueste zuerst.

    Zeilen: ``start`` (aware, Europe/Berlin), ``year``, ``month``, ``label``,
    ``cost``, ``tokens_in``, ``tokens_out``, ``answers``. Monate ohne Verbrauch
    sind mit 0 enthalten. Gruppiert wird in der Datenbank (TruncMonth in
    Europe/Berlin).
    """
    from multigpt.chat.models import Attachment, Message

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

    messages = (
        Message.objects.filter(_user_filter("", user) & _range("created", first, end))
        .annotate(period=TruncMonth("created", tzinfo=USAGE_TIME_ZONE))
        .values("period")
        .annotate(
            cost=_sum("cost"),
            tokens_in=Coalesce(Sum("tokens_in"), 0),
            tokens_out=Coalesce(Sum("tokens_out"), 0),
            answers=Count("pk", filter=Q(role=Message.Role.ASSISTANT)),
        )
    )
    for m in messages:
        row = result.get(local_key(m["period"]))
        if row is not None:
            row["cost"] += Decimal(m["cost"])
            row["tokens_in"] += m["tokens_in"]
            row["tokens_out"] += m["tokens_out"]
            row["answers"] += m["answers"]
    attachments = (
        Attachment.objects.filter(_user_filter("message__", user) & _range("created", first, end))
        .annotate(period=TruncMonth("created", tzinfo=USAGE_TIME_ZONE))
        .values("period")
        .annotate(cost=_sum("cost"))
    )
    for a in attachments:
        row = result.get(local_key(a["period"]))
        if row is not None:
            row["cost"] += Decimal(a["cost"])
    return list(result.values())


# --- Budget -----------------------------------------------------------------------


def budget_for(user) -> Decimal | None:
    """Wirksames Monatsbudget (Euro): Konto-Override vor Rolle; None = unbegrenzt."""
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
    if budget is None:
        return LEVEL_OK
    if spent_amount >= budget:  # Budget 0: sofort nur noch kostenfreie Modelle
        return LEVEL_EXHAUSTED
    if spent_amount >= budget * WARNING_RATIO:
        return LEVEL_WARNING
    return LEVEL_OK


def budget_state(user, now: dt.datetime | None = None) -> BudgetState:
    """Stand des laufenden Monats: Verbrauch, Budget, Anteil und Stufe."""
    budget = budget_for(user)
    amount = spent(user, now)
    if budget is None:
        ratio = remaining = None
    else:
        ratio = (amount / budget) if budget > 0 else Decimal(1)
        remaining = max(budget - amount, _ZERO)
    return BudgetState(amount, budget, ratio, level_for(amount, budget), remaining)


def model_is_free(ai_model) -> bool:
    """Verursacht das Modell keine Kosten? Lokaler Anbieter oder keine Preise (bzw. 0).

    Begründung: Kosten entstehen im System nur über die hinterlegten Preise
    (``services.compute_cost``). Ein Modell ohne Preise erhöht den Verbrauch nie –
    es zu sperren, schützte das Budget nicht. Damit Cloud-Modelle nicht
    versehentlich frei bleiben, müssen Verwalter Preise eintragen (Admin).
    """
    if ai_model.provider.is_local:
        return True
    return not (ai_model.price_in or ai_model.price_out)


def warning_text(state: BudgetState) -> str:
    """Hinweis für Oberfläche und SSE-Status; leer bei Stufe ok."""
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
