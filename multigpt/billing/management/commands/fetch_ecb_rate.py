"""EZB-Referenzkurs USD abrufen (nur mit BILLING_ECB_FETCH=true), siehe billing.ecb."""

from django.core.management.base import BaseCommand, CommandError

from multigpt.billing import ecb


class Command(BaseCommand):
    help = "Ruft den EZB-Referenzkurs USD ab und trägt fehlende EUR-Beträge nach."

    def handle(self, *args, **options):
        try:
            rate, created = ecb.fetch()
        except ecb.EcbError as exc:
            raise CommandError(str(exc)) from None
        state = "neu" if created else "vorhanden"
        self.stdout.write(f"{rate} ({state})")
