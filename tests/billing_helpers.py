"""Hilfen für Tests mit Kosten (Kontenrahmen, multigpt/billing).

``set_price``: Preisversion für ein Modell anlegen; das Konto des Anbieters wird
dafür auf die gewünschte Währung gestellt (Standard EUR, wie die alten Preise
``AIModel.price_in/price_out``). ``book_stored``: Buchung aus den gespeicherten
Tokens und Kosten einer Nachricht bzw. eines Anhangs – wie die Datenmigration.
"""

import datetime as dt
from decimal import Decimal

from multigpt.billing import booking
from multigpt.billing.models import BillingAccount, ModelPrice, UsageEntry

EARLY = dt.datetime(2000, 1, 1, tzinfo=dt.UTC)


def _dec(value):
    return None if value is None else Decimal(str(value))


def set_price(model, input=None, output=None, *, currency="EUR", valid_from=None, **fields):
    """Preis je 1 Mio. Tokens (Eingabe, Ausgabe; weitere Felder wie ModelPrice)."""
    account = booking.account_of(model.provider)
    if account.is_monetary and account.currency != currency:
        BillingAccount.objects.filter(pk=account.pk).update(currency=currency)
        account.currency = currency
    return ModelPrice.objects.create(
        ai_model=model,
        valid_from=valid_from or EARLY,
        input=_dec(input),
        output=_dec(output),
        **{
            k: (_dec(v) if k != "unit_prices" and k != "long_context_threshold" else v)
            for k, v in fields.items()
        },
    )


def book_stored(message=None, *, attachment=None):
    """Buchung aus gespeicherten Werten (``cost`` in EUR, Tokens) anlegen."""
    target = attachment or message
    model = attachment.generated_by_model if attachment else message.model
    msg = attachment.message if attachment else message
    if model is not None:
        account = booking.account_of(model.provider)
    else:
        account, _ = BillingAccount.objects.get_or_create(
            name="Test ohne Modell", defaults={"kind": "monetary", "currency": "EUR"}
        )
    cost = target.cost
    monetary = account.is_monetary and cost is not None
    return UsageEntry.objects.create(
        kind=UsageEntry.Kind.ATTACHMENT if attachment else UsageEntry.Kind.ANSWER,
        account=account,
        user_id=(msg.author_id or msg.conversation.user_id) if msg else None,
        ai_model=model,
        model_name=model.display_name if model else "",
        message=msg,
        attachment=attachment,
        created=target.created,
        tokens_in=0 if attachment else message.tokens_in,
        tokens_out=0 if attachment else message.tokens_out,
        amount=cost if monetary and account.currency == "EUR" else None,
        currency=account.currency if monetary else "",
        amount_eur=cost if monetary else None,
        legacy=True,
    )
