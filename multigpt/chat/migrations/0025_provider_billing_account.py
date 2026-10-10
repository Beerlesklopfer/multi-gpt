"""Kontenrahmen (M6): Anbieter -> Abrechnungskonto, Bestand übernehmen.

Datenmigration (idempotent, rückwärts: Preise zurück an das Modell):

1. Konten: lokale Anbieter -> gemeinsames Token-Konto „Lokale Modelle“, alle
   anderen ein monetäres Konto mit dem Namen des Anbieters – in EUR, wenn
   eines seiner Modelle schon Preise hatte (die alten Preise waren Euro),
   sonst USD.
2. Preise: ``AIModel.price_in/price_out`` -> ``ModelPrice`` (Eingabe/Ausgabe),
   gültig ab der frühesten Nutzung des Modells, ohne Nutzung ab 1.1.2000.
3. Buchungen: je Antwort mit Modell oder Kosten bzw. je Anhang mit Kosten oder
   erzeugendem Modell eine ``UsageEntry`` mit den gespeicherten Tokens und
   Kosten (``legacy``). EUR-Betrag = gespeicherte Kosten. Antworten gelöschter
   Modelle gehen auf das Konto „Altbestand (gelöschte Modelle)“.

``chat 0026_remove_aimodel_prices`` entfernt danach die alten Preisfelder.
"""

import datetime as dt

import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Min, Q

LOCAL_ACCOUNT = "Lokale Modelle"
ORPHAN_ACCOUNT = "Altbestand (gelöschte Modelle)"
FIXED_START = dt.datetime(2000, 1, 1, tzinfo=dt.UTC)
NOTE = "Aus dem bisherigen Preis übernommen (Euro je 1 Mio. Tokens)."
BATCH = 1000


def _account(BillingAccount, name, kind, currency):
    account, _ = BillingAccount.objects.get_or_create(
        name=name[:100], defaults={"kind": kind, "currency": currency}
    )
    return account


def _priced(AIModel):
    return AIModel.objects.filter(Q(price_in__isnull=False) | Q(price_out__isnull=False))


def forward(apps, schema_editor):
    Provider = apps.get_model("chat", "Provider")
    AIModel = apps.get_model("chat", "AIModel")
    Message = apps.get_model("chat", "Message")
    Attachment = apps.get_model("chat", "Attachment")
    BillingAccount = apps.get_model("billing", "BillingAccount")
    ModelPrice = apps.get_model("billing", "ModelPrice")
    UsageEntry = apps.get_model("billing", "UsageEntry")

    # 1. Konten
    priced_providers = set(_priced(AIModel).values_list("provider_id", flat=True))
    for provider in Provider.objects.filter(billing_account__isnull=True):
        if provider.is_local:
            account = _account(BillingAccount, LOCAL_ACCOUNT, "tokens", "")
        else:
            currency = "EUR" if provider.pk in priced_providers else "USD"
            account = _account(BillingAccount, provider.name, "monetary", currency)
        provider.billing_account = account
        provider.save(update_fields=["billing_account"])

    # 2. Preise
    prices = {}
    for model in _priced(AIModel).select_related("provider__billing_account"):
        existing = ModelPrice.objects.filter(ai_model=model).order_by("valid_from").first()
        if existing is not None:
            prices[model.pk] = existing
            continue
        if model.provider.billing_account.kind != "monetary":
            continue  # lokale Preise zählten nie
        first = [
            Message.objects.filter(model=model).aggregate(v=Min("created"))["v"],
            Attachment.objects.filter(generated_by_model=model).aggregate(v=Min("created"))["v"],
        ]
        valid_from = min([v for v in first if v is not None], default=FIXED_START)
        prices[model.pk] = ModelPrice.objects.create(
            ai_model=model,
            valid_from=valid_from,
            input=model.price_in,
            output=model.price_out,
            note=NOTE,
        )

    orphan = None

    def account_for(model):
        nonlocal orphan
        if model is not None:
            return model.provider.billing_account
        if orphan is None:
            orphan = _account(BillingAccount, ORPHAN_ACCOUNT, "monetary", "EUR")
        return orphan

    def amounts(account, cost):
        if account.kind != "monetary" or cost is None:
            return None, "", None
        return (cost if account.currency == "EUR" else None), account.currency, cost

    # 3a. Antworten
    done = set(
        UsageEntry.objects.filter(kind="answer", message__isnull=False).values_list(
            "message_id", flat=True
        )
    )
    entries = []
    messages = (
        Message.objects.filter(role="assistant")
        .filter(Q(model__isnull=False) | Q(cost__isnull=False))
        .select_related("model__provider__billing_account", "conversation")
        .order_by("pk")
    )
    for msg in messages.iterator(chunk_size=BATCH):
        if msg.pk in done:
            continue
        account = account_for(msg.model)
        amount, currency, eur = amounts(account, msg.cost)
        tokens = msg.tokens_in or msg.tokens_out
        entries.append(
            UsageEntry(
                kind="answer",
                account=account,
                user_id=msg.author_id or msg.conversation.user_id,
                ai_model=msg.model,
                model_name=(msg.model.display_name if msg.model else "")[:200],
                message=msg,
                created=msg.created,
                tokens_in=msg.tokens_in,
                tokens_out=msg.tokens_out,
                requests=1 if tokens else 0,
                rounds=[{"in": msg.tokens_in, "out": msg.tokens_out}] if tokens else [],
                amount=amount,
                currency=currency,
                amount_eur=eur,
                price=prices.get(msg.model_id) if msg.cost is not None else None,
                legacy=True,
            )
        )
        if len(entries) >= BATCH:
            UsageEntry.objects.bulk_create(entries)
            entries = []
    UsageEntry.objects.bulk_create(entries)

    # 3b. Anhänge (z. B. erzeugte Bilder)
    done = set(
        UsageEntry.objects.filter(kind="attachment", attachment__isnull=False).values_list(
            "attachment_id", flat=True
        )
    )
    entries = []
    attachments = (
        Attachment.objects.filter(Q(cost__isnull=False) | Q(generated_by_model__isnull=False))
        .select_related("generated_by_model__provider__billing_account", "message__conversation")
        .order_by("pk")
    )
    for att in attachments.iterator(chunk_size=BATCH):
        if att.pk in done:
            continue
        model = att.generated_by_model
        account = account_for(model)
        amount, currency, eur = amounts(account, att.cost)
        msg = att.message
        user_id = (msg.author_id or msg.conversation.user_id) if msg else att.owner_id
        entries.append(
            UsageEntry(
                kind="attachment",
                account=account,
                user_id=user_id,
                ai_model=model,
                model_name=(model.display_name if model else "")[:200],
                message=msg,
                attachment=att,
                created=att.created,
                units={"image": 1} if (model is not None and att.kind == "image") else {},
                amount=amount,
                currency=currency,
                amount_eur=eur,
                price=prices.get(att.generated_by_model_id) if att.cost is not None else None,
                legacy=True,
            )
        )
    UsageEntry.objects.bulk_create(entries)


def backward(apps, schema_editor):
    """Preise zurück an das Modell (neueste Version in EUR); Konten und
    Buchungen bleiben (beim erneuten Vorwärtslauf werden sie wiederverwendet)."""
    ModelPrice = apps.get_model("billing", "ModelPrice")
    AIModel = apps.get_model("chat", "AIModel")
    for model in AIModel.objects.filter(provider__billing_account__currency="EUR"):
        price = ModelPrice.objects.filter(ai_model=model).order_by("-valid_from").first()
        if price is not None:
            AIModel.objects.filter(pk=model.pk).update(price_in=price.input, price_out=price.output)


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0001_initial"),
        ("chat", "0024_web_fetch"),
    ]

    operations = [
        migrations.AddField(
            model_name="provider",
            name="billing_account",
            field=models.ForeignKey(
                blank=True,
                help_text="Leer = automatisch: lokal „Lokale Modelle“ (Tokens), sonst ein Konto "
                "mit dem Namen des Anbieters (USD). Preise: je Modell unter „Modellpreise“.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="providers",
                to="billing.billingaccount",
                verbose_name="Abrechnungskonto",
            ),
        ),
        migrations.RunPython(forward, backward),
    ]
