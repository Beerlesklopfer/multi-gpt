"""EUR-Konto auf USD umstellen (A10), für Admin-Aktion und ``switch_account_to_usd``.

Bestandskonten aus der Zeit vor dem Kontenrahmen stehen in EUR (die alten
Preise ``AIModel.price_in/price_out`` waren Euro), die Anbieter rechnen aber
in USD ab. Umstellen heißt:

- Kontowährung EUR -> USD.
- Vorhandene Preisversionen bleiben als Historie unverändert (sie gelten bis
  jetzt und sind in alten Buchungen referenziert).
- Je Modell des Kontos mit gültigem Preis entsteht eine neue Preisversion
  „gültig ab jetzt“ in USD: EUR-Preis / Kurs (1 USD in EUR), auf 6 Stellen.
  Modelle ohne (oder mit leerem) Preis bleiben ohne Preis.
- Alte Buchungen bleiben unverändert: ``amount``/``currency``/``amount_eur``
  sind Momentaufnahmen (EUR-Buchungen behalten ihren EUR-Betrag).
- Budgets je Konto und das Gesamtbudget stehen immer in EUR und zählen
  ``amount_eur``; sie bleiben unverändert und gelten weiter.

Kurs: der neueste gepflegte Kurs am oder vor heute (in der Regel der
EZB-Tageskurs, siehe ``ecb``); ohne Kurs keine Umstellung.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

from .booking import booking_day, rate_on
from .models import BillingAccount, ExchangeRate, ModelPrice
from .pricing import AMOUNT_STEP, price_at

# Kurs älter als so viele Tage: Hinweis in der Vorschau.
STALE_RATE_DAYS = 7
PRICE_FIELDS = ModelPrice.TOKEN_FIELDS + tuple(f"long_{f}" for f in ModelPrice.TOKEN_FIELDS)


class SwitchError(Exception):
    """Umstellung nicht möglich; Text ist für Verwalter gedacht."""


def to_usd(eur: Decimal | None, rate: ExchangeRate) -> Decimal | None:
    if eur is None:
        return None
    return (Decimal(eur) / rate.usd_eur).quantize(AMOUNT_STEP, rounding=ROUND_HALF_UP)


def _unit_to_usd(value, rate: ExchangeRate):
    """Einheitenpreis (JSON) umrechnen; Unlesbares bleibt, wie es ist."""
    try:
        if isinstance(value, bool):
            raise ValueError
        usd = to_usd(Decimal(str(value)), rate)
    except (ArithmeticError, ValueError):
        return value
    return int(usd) if usd == usd.to_integral_value() else float(usd)


@dataclass
class PriceChange:
    ai_model: object
    old: ModelPrice
    new: ModelPrice  # ungespeichert

    def pairs(self) -> list[tuple[str, Decimal, Decimal]]:
        """(Feldname, EUR, USD) der gesetzten Token-Preise, für die Vorschau."""
        return [
            (ModelPrice._meta.get_field(name).verbose_name, getattr(self.old, name), value)
            for name in PRICE_FIELDS
            if (value := getattr(self.new, name)) is not None
        ]

    @property
    def units_text(self) -> str:
        return ", ".join(
            f"{k}: {v} -> {self.new.unit_prices[k]}" for k, v in self.old.unit_prices.items()
        )


@dataclass
class Plan:
    account: BillingAccount
    rate: ExchangeRate
    when: object
    changes: list[PriceChange] = field(default_factory=list)
    without_price: list = field(default_factory=list)

    @property
    def rate_stale(self) -> bool:
        return (booking_day(self.when) - self.rate.date).days > STALE_RATE_DAYS


def plan(account: BillingAccount, now=None) -> Plan:
    """Vorschau der Umstellung (nichts wird gespeichert). SwitchError, wenn nicht möglich."""
    from multigpt.chat.models import AIModel

    now = now or timezone.now()
    if account.kind != BillingAccount.Kind.MONETARY or account.currency != "EUR":
        raise SwitchError(f"„{account}“ ist kein monetäres Konto in EUR.")
    rate = rate_on(booking_day(now))
    if rate is None:
        raise SwitchError(
            "Es liegt kein Wechselkurs vor. Bitte unter „Wechselkurse“ den EZB-Kurs "
            "abrufen oder einen Kurs eintragen."
        )
    models = AIModel.objects.filter(provider__billing_account=account).select_related("provider")
    future = ModelPrice.objects.filter(ai_model__in=models, valid_from__gt=now)
    if future.exists():
        names = ", ".join(sorted({p.ai_model.display_name for p in future}))
        raise SwitchError(
            f"„{account}“ hat Preisversionen mit „gültig ab“ in der Zukunft ({names}). "
            "Bitte diese erst löschen oder abwarten; sie stünden sonst in EUR im USD-Konto."
        )
    result = Plan(account=account, rate=rate, when=now)
    for model in models.order_by("provider__name", "display_name"):
        old = price_at(model, now)
        if old is None or old.is_empty:
            result.without_price.append(model)
            continue
        new = ModelPrice(
            ai_model=model,
            valid_from=now,
            long_context_threshold=old.long_context_threshold,
            unit_prices={k: _unit_to_usd(v, rate) for k, v in (old.unit_prices or {}).items()},
            note=(
                f"Umgestellt EUR -> USD mit Kurs {rate.usd_eur} vom {rate.date:%d.%m.%Y} "
                f"(vorher {old})"
            )[:300],
        )
        for name in PRICE_FIELDS:
            setattr(new, name, to_usd(getattr(old, name), rate))
        result.changes.append(PriceChange(model, old, new))
    return result


def apply(planned: Plan) -> int:
    """Umstellung ausführen; liefert die Zahl neuer Preisversionen."""
    with transaction.atomic():
        account = BillingAccount.objects.select_for_update().get(pk=planned.account.pk)
        if account.currency != "EUR":
            raise SwitchError(f"„{account}“ steht nicht mehr in EUR.")
        for change in planned.changes:
            change.new.save()
        # Ohne clean(): Die Sperre gegen Währungswechsel gilt für Änderungen von Hand.
        BillingAccount.objects.filter(pk=account.pk).update(currency=BillingAccount.Currency.USD)
    planned.account.currency = BillingAccount.Currency.USD
    return len(planned.changes)
