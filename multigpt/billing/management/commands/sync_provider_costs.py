"""Guthaben und Istkosten der Anbieter sofort abrufen.

Den täglichen Abruf übernimmt der Worker (``provider_costs.auto_sync``); der Befehl ist für
den sofortigen Abruf von Hand.
"""

from django.core.management.base import BaseCommand

from multigpt.billing import provider_costs
from multigpt.billing.models import BillingAccount


class Command(BaseCommand):
    help = "Ruft Guthaben und Istkosten der Anbieter ab (nur monetäre, aktive Konten)."

    def handle(self, *args, **options):
        for account in BillingAccount.objects.filter(active=True, kind="monetary"):
            errors = provider_costs.sync_account(account)
            for error in errors:
                self.stderr.write(f"{account.name}: {error}")
            account.refresh_from_db()
            state = f"Guthaben {account.balance} {account.balance_currency}"
            self.stdout.write(f"{account.name}: {state if account.balance is not None else 'ok'}")
