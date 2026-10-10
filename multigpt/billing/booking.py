"""Buchungen (M6-08) und Währung (M6-09).

- ``book_answer``: eine Buchung je Antwort, bei jedem Abschluss (auch nach
  Abbruch, Fehler oder Rückfrage) aktualisiert. Zeitpunkt und Preisversion
  richten sich nach dem Beginn der Antwort (``Message.created``), damit eine
  über eine Rückfrage fortgesetzte Antwort nicht in zwei Preisversionen fällt.
- Gebucht wird auf den Verfasser (``Message.author``; Altdaten: Besitzer).
- ``Message.cost`` bleibt als Momentaufnahme in EUR: monetär ``amount_eur``
  (None ohne Preis oder ohne Kurs), Token- und Pauschalkonten 0.
- Kurse: Verwendet wird der letzte Kurs am oder vor dem Buchungstag
  (Europe/Berlin). Ohne Kurs bleibt ``amount_eur`` leer; ``fill_missing_eur``
  trägt ihn nach, sobald ein passender Kurs gepflegt ist (Admin).
"""

from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from .models import BillingAccount, ExchangeRate, UsageEntry
from .pricing import AMOUNT_STEP, Round, Tally, price_at, quote

logger = logging.getLogger(__name__)

TIME_ZONE = ZoneInfo("Europe/Berlin")
LOCAL_ACCOUNT_NAME = "Lokale Modelle"
DEFAULT_CURRENCY = BillingAccount.Currency.USD


# --- Konten -----------------------------------------------------------------------


def default_account_for(provider) -> BillingAccount:
    """Vorbelegung: lokal -> gemeinsames Token-Konto, sonst ein monetäres Konto
    je Anbieter in USD. Ein vorhandenes Konto gleichen Namens wird verwendet
    (so lassen sich Anbieter über den Namen bündeln)."""
    if provider.is_local:
        account, _ = BillingAccount.objects.get_or_create(
            name=LOCAL_ACCOUNT_NAME,
            defaults={"kind": BillingAccount.Kind.TOKENS, "currency": ""},
        )
        return account
    name = (provider.name or "Anbieter")[:100]
    account, _ = BillingAccount.objects.get_or_create(
        name=name,
        defaults={"kind": BillingAccount.Kind.MONETARY, "currency": DEFAULT_CURRENCY},
    )
    return account


def account_of(provider) -> BillingAccount:
    """Konto des Anbieters; fehlt es (Altbestand, Shell), wird es vorbelegt."""
    if provider.billing_account_id is None:
        provider.billing_account = default_account_for(provider)
        type(provider).objects.filter(pk=provider.pk, billing_account__isnull=True).update(
            billing_account=provider.billing_account
        )
    return provider.billing_account


# --- Kurse -------------------------------------------------------------------------


def booking_day(when):
    """Buchungstag in Europe/Berlin (wie die Monatsgrenzen in accounts.usage)."""
    return timezone.localtime(when, TIME_ZONE).date()


def rate_on(day) -> ExchangeRate | None:
    """Gültiger Kurs am Tag ``day``: letzter Eintrag am oder vor diesem Tag."""
    return ExchangeRate.objects.filter(date__lte=day).order_by("-date").first()


def to_eur(
    amount: Decimal | None, currency: str, when
) -> tuple[Decimal | None, ExchangeRate | None]:
    """(EUR-Betrag, verwendeter Kurs). EUR bleibt EUR; ohne Kurs (None, None)."""
    if amount is None:
        return None, None
    if currency == BillingAccount.Currency.EUR:
        return amount, None
    rate = rate_on(booking_day(when))
    if rate is None:
        return None, None
    return (amount * rate.usd_eur).quantize(AMOUNT_STEP, rounding=ROUND_HALF_UP), rate


def compat_cost(entry: UsageEntry | None) -> Decimal | None:
    """Wert für ``Message.cost``/``Attachment.cost`` (EUR-Momentaufnahme)."""
    if entry is None:
        return None
    if entry.account.kind != BillingAccount.Kind.MONETARY:
        return Decimal(0)
    return entry.amount_eur


# --- Buchen ------------------------------------------------------------------------


def _fill(entry: UsageEntry, account, ai_model, price, tally: Tally, when):
    entry.account = account
    entry.ai_model = ai_model
    entry.model_name = (ai_model.display_name if ai_model else "")[:200]
    entry.created = when
    for name, value in tally.totals().items():
        setattr(entry, name, value)
    entry.requests = len(tally.rounds)
    entry.rounds = [r.to_json() for r in tally.rounds]
    entry.units = dict(tally.units)
    entry.price = price
    entry.currency = account.currency if account.is_monetary else ""
    entry.amount = quote(account, price, tally)
    entry.amount_eur, rate = to_eur(entry.amount, entry.currency, when)
    entry.rate = rate.usd_eur if rate else None
    entry.rate_date = rate.date if rate else None


def payer_id(message) -> int | None:
    return message.author_id or message.conversation.user_id


def book_answer(message, ai_model, tally: Tally) -> UsageEntry:
    """Buchung der Antwort anlegen bzw. aktualisieren (eine je Antwort)."""
    account = account_of(ai_model.provider)
    when = message.created or timezone.now()
    price = price_at(ai_model, when)
    with transaction.atomic():
        entry = (
            UsageEntry.objects.select_for_update()
            .filter(message=message, kind=UsageEntry.Kind.ANSWER)
            .first()
        ) or UsageEntry(message=message, kind=UsageEntry.Kind.ANSWER)
        entry.user_id = payer_id(message)
        _fill(entry, account, ai_model, price, tally, when)
        entry.legacy = False
        entry.save()
    return entry


def book_attachment(attachment, ai_model, units: dict, user=None) -> UsageEntry:
    """Buchung für eine erzeugte Datei (z. B. Bild, M9): Gebühren je Einheit.

    ``units`` z. B. ``{"image:high:1024x1024": 1}``. Setzt ``Attachment.cost``.
    """
    account = account_of(ai_model.provider)
    when = attachment.created or timezone.now()
    tally = Tally()
    tally.units = dict(units or {})
    entry = UsageEntry.objects.filter(
        attachment=attachment, kind=UsageEntry.Kind.ATTACHMENT
    ).first() or UsageEntry(attachment=attachment, kind=UsageEntry.Kind.ATTACHMENT)
    if user is not None:
        entry.user = user
    elif attachment.message_id:
        entry.user_id = payer_id(attachment.message)
    entry.message_id = attachment.message_id
    _fill(entry, account, ai_model, price_at(ai_model, when), tally, when)
    entry.save()
    type(attachment).objects.filter(pk=attachment.pk).update(cost=compat_cost(entry))
    return entry


def tally_of(message) -> Tally:
    """Bisheriger Verbrauch einer Antwort (Fortsetzung nach einer Rückfrage)."""
    entry = UsageEntry.objects.filter(message=message, kind=UsageEntry.Kind.ANSWER).first()
    tally = Tally()
    if entry is not None:
        if entry.rounds:
            tally.rounds = [Round.from_json(r) for r in entry.rounds if isinstance(r, dict)]
        else:
            tally.rounds = [
                Round(
                    entry.tokens_in,
                    entry.tokens_out,
                    entry.cached_read,
                    entry.cache_write,
                    entry.cache_write_1h,
                    entry.reasoning,
                )
            ]
        tally.units = dict(entry.units or {})
    elif message.tokens_in or message.tokens_out:
        tally.rounds = [Round(message.tokens_in, message.tokens_out)]
    return tally


def estimate_eur(ai_model, tally: Tally, when=None) -> Decimal | None:
    """Kosten in EUR ohne Buchung (Vorschau, ``services.compute_cost``)."""
    account = account_of(ai_model.provider)
    if not account.is_monetary:
        return Decimal(0)
    when = when or timezone.now()
    eur, _ = to_eur(quote(account, price_at(ai_model, when), tally), account.currency, when)
    return eur


def fill_missing_eur() -> int:
    """EUR-Beträge nachtragen, wo der Kurs bei der Buchung fehlte. Liefert die Anzahl."""
    from multigpt.chat.models import Attachment, Message

    count = 0
    pending = UsageEntry.objects.filter(amount__isnull=False, amount_eur__isnull=True).exclude(
        currency=BillingAccount.Currency.EUR
    )
    for entry in pending.select_related("account").iterator():
        eur, rate = to_eur(entry.amount, entry.currency, entry.created)
        if eur is None:
            continue
        UsageEntry.objects.filter(pk=entry.pk).update(
            amount_eur=eur, rate=rate.usd_eur, rate_date=rate.date
        )
        if entry.kind == UsageEntry.Kind.ANSWER and entry.message_id:
            Message.objects.filter(pk=entry.message_id).update(cost=eur)
        elif entry.kind == UsageEntry.Kind.ATTACHMENT and entry.attachment_id:
            Attachment.objects.filter(pk=entry.attachment_id).update(cost=eur)
        count += 1
    return count


def missing_eur_count() -> int:
    return (
        UsageEntry.objects.filter(amount__isnull=False, amount_eur__isnull=True)
        .exclude(currency=BillingAccount.Currency.EUR)
        .count()
    )
