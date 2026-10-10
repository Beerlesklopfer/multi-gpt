"""Budgets je Konto und Gesamtbudget (M6-10), Grundlage für Sperre und Hinweise.

- Monetäre Konten: Monatsbudget in EUR je Rolle bzw. Nutzer (``AccountBudget``,
  Nutzer vor Rolle). Dazu das bisherige Budget an Rolle/Konto als
  **Gesamtbudget (EUR)** über alle monetären Konten (``User.monthly_budget``).
- Token-Konten: Monatskontingent in Tokens (Eingabe + Ausgabe), leer = unbegrenzt.
- Pauschalkonten: kein Budget, nur gezählt.
- Sperre: Ist ein Konto ausgeschöpft, sind nur *dessen* Modelle gesperrt;
  ist das Gesamtbudget ausgeschöpft, alle kostenpflichtigen Modelle monetärer
  Konten. Modelle ohne Preis bleiben frei (sie erhöhen den Verbrauch nie).
  Ein inaktives Konto sperrt seine Modelle immer.
- EUR-Beträge, deren Kurs fehlte, zählen mit dem neuesten gepflegten Kurs,
  ohne jeden Kurs 1:1 (vorsichtig: 1 USD ist meist weniger als 1 EUR) – nie 0.

Rennen: Geprüft wird vor jeder Antwort mit dem Stand der abgeschlossenen
Buchungen (wie bisher). Parallel laufende Antworten desselben Kontos können
das Budget deshalb um ihre eigenen Kosten überschreiten; danach greift die
Sperre. Eine Reservierung je Anfrage wäre nötig, um das auszuschließen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import F, Q, Sum

from .models import AccountBudget, BillingAccount, ExchangeRate, UsageEntry

WARNING_RATIO = Decimal("0.8")
LEVEL_OK = "ok"
LEVEL_WARNING = "warning"
LEVEL_EXHAUSTED = "exhausted"
ZERO = Decimal(0)

TOTAL_EXHAUSTED_MESSAGE = (
    "Monatsbudget ausgeschöpft – bis zum Monatsende sind nur lokale Modelle nutzbar."
)


def level_for(spent, limit) -> str:
    if limit is None:
        return LEVEL_OK
    if spent >= limit:  # Budget 0: sofort gesperrt
        return LEVEL_EXHAUSTED
    if spent >= limit * WARNING_RATIO:
        return LEVEL_WARNING
    return LEVEL_OK


@dataclass(frozen=True)
class AccountState:
    """Stand eines Budgets im Monat. ``account`` None = Gesamtbudget (EUR)."""

    account: BillingAccount | None
    unit: str  # "eur" | "tokens"
    spent: Decimal | int
    limit: Decimal | int | None
    level: str

    @property
    def name(self) -> str:
        return self.account.name if self.account else "Gesamt"

    @property
    def ratio(self):
        if self.limit is None:
            return None
        return Decimal(self.spent) / Decimal(self.limit) if self.limit > 0 else Decimal(1)

    @property
    def percent(self) -> int | None:
        return None if self.ratio is None else int(self.ratio * 100)

    @property
    def remaining(self):
        return None if self.limit is None else max(self.limit - self.spent, 0)

    @property
    def exhausted(self) -> bool:
        return self.level == LEVEL_EXHAUSTED

    def blocked_text(self) -> str:
        if self.account is None:
            return TOTAL_EXHAUSTED_MESSAGE
        if self.unit == "tokens":
            return (
                f"Token-Kontingent für „{self.name}“ ausgeschöpft – bis zum Monatsende "
                "sind dessen Modelle gesperrt."
            )
        return (
            f"Monatsbudget für „{self.name}“ ausgeschöpft – bis zum Monatsende "
            "sind dessen Modelle gesperrt."
        )

    def warning_text(self) -> str:
        """Hinweis ab 80 %; leer bei Stufe ok."""
        if self.level == LEVEL_EXHAUSTED:
            return self.blocked_text()
        if self.level != LEVEL_WARNING:
            return ""
        from multigpt.accounts.usage import format_eur, format_tokens

        fmt = format_tokens if self.unit == "tokens" else format_eur
        what = "deines Monatsbudgets" if self.account is None else f"des Budgets für „{self.name}“"
        if self.unit == "tokens":
            what = f"deines Token-Kontingents für „{self.name}“"
        return f"{self.percent} % {what} sind verbraucht ({fmt(self.spent)} von {fmt(self.limit)})."


# --- Abfragen ----------------------------------------------------------------------


def limits_for(user) -> dict[int, AccountBudget]:
    """{account_id: wirksamer Eintrag} – Nutzer vor Rolle; eine Abfrage."""
    if user is None or user.pk is None:
        return {}
    q = Q(user_id=user.pk)
    if user.role_id:
        q |= Q(role_id=user.role_id)
    result: dict[int, AccountBudget] = {}
    for row in AccountBudget.objects.filter(q).select_related("account"):
        if row.account_id not in result or row.user_id is not None:
            result[row.account_id] = row
    return result


def fallback_rate() -> Decimal:
    """Kurs für Beträge ohne EUR: neuester gepflegter Kurs, sonst 1."""
    rate = ExchangeRate.objects.order_by("-date").values_list("usd_eur", flat=True).first()
    return rate if rate is not None else Decimal(1)


def spent_by_account(users, start, end, *, by_user: bool = False) -> dict:
    """{account_id: {"eur", "tokens", ...}} bzw. mit ``by_user`` {(user_id, account_id): …}.

    ``users``: Konto, ids/QuerySet oder None (alle). Eine Abfrage plus
    höchstens eine für den Ersatzkurs.
    """
    qs = UsageEntry.objects.filter(created__gte=start, created__lt=end)
    if users is not None:
        if hasattr(users, "pk") and not hasattr(users, "model"):
            qs = qs.filter(user_id=users.pk)
        else:
            qs = qs.filter(user__in=users)
    keys = ["user_id", "account_id"] if by_user else ["account_id"]
    rows = qs.values(*keys).annotate(
        eur=Sum("amount_eur"),
        unknown=Sum("amount", filter=Q(amount_eur__isnull=True)),
        tokens=Sum(F("tokens_in") + F("tokens_out")),
    )
    result = {}
    rate = None
    for row in rows:
        eur = row["eur"] or ZERO
        if row["unknown"]:
            rate = rate if rate is not None else fallback_rate()
            eur += row["unknown"] * rate
        key = (row["user_id"], row["account_id"]) if by_user else row["account_id"]
        result[key] = {
            "eur": eur,
            "tokens": int(row["tokens"] or 0),
            "estimated": bool(row["unknown"]),
        }
    return result


@dataclass
class Snapshot:
    """Budgetstand eines Kontos im Monat; einmal je Anfrage berechnet."""

    total: AccountState | None
    accounts: dict[int, AccountState] = field(default_factory=dict)

    def states(self) -> list[AccountState]:
        return ([self.total] if self.total else []) + sorted(
            self.accounts.values(), key=lambda s: s.name.lower()
        )

    def worst(self) -> AccountState | None:
        order = {LEVEL_EXHAUSTED: 2, LEVEL_WARNING: 1, LEVEL_OK: 0}
        states = self.states()
        return max(states, key=lambda s: order[s.level]) if states else None


def snapshot(user, now=None) -> Snapshot:
    """Gesamtbudget und Budgets je Konto für den laufenden Monat.

    Ohne jedes Budget keine Verbrauchsabfrage.
    """
    from multigpt.accounts.usage import month_bounds

    total_limit = getattr(user, "monthly_budget", None)
    limits = {
        aid: row
        for aid, row in limits_for(user).items()
        if row.account.kind != BillingAccount.Kind.FLAT
        and (row.monthly_budget is not None or row.monthly_tokens is not None)
    }
    if total_limit is None and not limits:
        return Snapshot(None)
    start, end = month_bounds(now)
    spent = spent_by_account(user, start, end)
    total = None
    if total_limit is not None:
        amount = sum((v["eur"] for v in spent.values()), ZERO)
        total = AccountState(None, "eur", amount, total_limit, level_for(amount, total_limit))
    states = {}
    for aid, row in limits.items():
        account = row.account
        used = spent.get(aid, {"eur": ZERO, "tokens": 0})
        if account.kind == BillingAccount.Kind.TOKENS:
            limit, value, unit = row.monthly_tokens, used["tokens"], "tokens"
        else:
            limit, value, unit = row.monthly_budget, used["eur"], "eur"
        if limit is None:
            continue
        states[aid] = AccountState(account, unit, value, limit, level_for(value, limit))
    return Snapshot(total, states)


# --- Sperre ------------------------------------------------------------------------


def is_free(ai_model, account: BillingAccount | None = None, now=None) -> bool:
    """Verursacht das Modell keine Kosten (EUR)? Token-/Pauschalkonto oder ohne Preis."""
    from .booking import account_of
    from .pricing import price_at

    account = account or account_of(ai_model.provider)
    if account.kind != BillingAccount.Kind.MONETARY:
        return True
    price = price_at(ai_model, now)
    return price is None or price.costs_nothing


def blocked_reason(user, ai_model, snap: Snapshot | None = None, now=None) -> str:
    """Grund, warum ``user`` das Modell gerade nicht nutzen darf; leer = frei."""
    from .booking import account_of

    if ai_model is None:
        return ""
    account = account_of(ai_model.provider)
    if not account.active:
        return f"Das Abrechnungskonto „{account.name}“ ist deaktiviert."
    if account.kind == BillingAccount.Kind.FLAT:
        return ""
    if account.kind == BillingAccount.Kind.MONETARY and is_free(ai_model, account, now):
        return ""
    snap = snap if snap is not None else snapshot(user, now)
    state = snap.accounts.get(account.pk)
    if state is not None and state.exhausted:
        return state.blocked_text()
    if account.kind == BillingAccount.Kind.MONETARY and snap.total and snap.total.exhausted:
        return snap.total.blocked_text()
    return ""


def blocked_reasons(user, ai_models) -> dict[int, str]:
    """{model_pk: Grund} für mehrere Modelle mit einem Budgetstand."""
    snap = None
    result = {}
    for m in ai_models:
        if snap is None:
            snap = snapshot(user)
        result[m.pk] = blocked_reason(user, m, snap)
    return result


def warnings_for(user, ai_model, snap: Snapshot | None = None) -> list[str]:
    """Hinweise (ab 80 %) zum Konto des Modells und zum Gesamtbudget."""
    from .booking import account_of

    snap = snap if snap is not None else snapshot(user)
    texts = []
    account = account_of(ai_model.provider) if ai_model is not None else None
    if account is not None and account.pk in snap.accounts:
        texts.append(snap.accounts[account.pk].warning_text())
    if snap.total and (account is None or account.is_monetary):
        texts.append(snap.total.warning_text())
    return [t for t in texts if t]
