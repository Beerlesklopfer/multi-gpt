"""Vorbelegung des Abrechnungskontos für neue Anbieter (M6-06).

Ein Anbieter ohne Konto bekommt beim Speichern eines: lokale das gemeinsame
Token-Konto „Lokale Modelle“, alle anderen ein monetäres Konto in USD mit dem
Namen des Anbieters (``booking.default_account_for``).
"""

from django.db.models.signals import pre_save
from django.dispatch import receiver

from multigpt.chat.models import Provider

from .booking import default_account_for


@receiver(pre_save, sender=Provider, dispatch_uid="billing_assign_default_account")
def assign_default_account(sender, instance, raw=False, **kwargs):
    if raw or instance.billing_account_id is not None:
        return
    instance.billing_account = default_account_for(instance)
