"""EUR-Abrechnungskonten auf USD umstellen (A10), siehe billing.usd_switch.

Ohne ``--apply`` nur Vorschau; mit ``--apply`` werden die neuen Preisversionen
„gültig ab jetzt“ angelegt und die Kontowährung umgestellt.
"""

from django.core.management.base import BaseCommand, CommandError

from multigpt.billing import usd_switch
from multigpt.billing.models import BillingAccount


class Command(BaseCommand):
    help = (
        "Stellt EUR-Abrechnungskonten auf USD um: neue Preisversion ab jetzt, umgerechnet "
        "mit dem neuesten Wechselkurs. Ohne --apply nur Vorschau."
    )

    def add_arguments(self, parser):
        parser.add_argument("accounts", nargs="*", help="Name oder Nummer der Konten.")
        parser.add_argument(
            "--all-eur", action="store_true", help="Alle monetären Konten in EUR umstellen."
        )
        parser.add_argument("--apply", action="store_true", help="Umstellung ausführen.")

    def handle(self, *args, accounts=(), all_eur=False, apply=False, **options):
        if all_eur:
            selected = list(BillingAccount.objects.filter(kind="monetary", currency="EUR"))
        elif accounts:
            selected = [self._account(ref) for ref in accounts]
        else:
            raise CommandError("Bitte Konten nennen oder --all-eur angeben.")
        if not selected:
            self.stdout.write("Keine Konten in EUR.")
            return
        failed = False
        for account in selected:
            try:
                planned = usd_switch.plan(account)
            except usd_switch.SwitchError as exc:
                self.stderr.write(str(exc))
                failed = True
                continue
            self._show(planned)
            if apply:
                count = usd_switch.apply(planned)
                self.stdout.write(f"  umgestellt, {count} neue Preisversion(en).")
        if not apply:
            self.stdout.write("Vorschau – zum Ausführen mit --apply aufrufen.")
        if failed:
            raise CommandError("Nicht alle Konten ließen sich umstellen.")

    def _account(self, ref: str) -> BillingAccount:
        qs = BillingAccount.objects.filter(pk=int(ref)) if ref.isdigit() else None
        account = (qs.first() if qs is not None else None) or BillingAccount.objects.filter(
            name=ref
        ).first()
        if account is None:
            raise CommandError(f"Konto „{ref}“ nicht gefunden.")
        return account

    def _show(self, planned: usd_switch.Plan) -> None:
        rate = planned.rate
        self.stdout.write(
            f"{planned.account.name}: Kurs 1 USD = {rate.usd_eur} EUR vom {rate.date:%d.%m.%Y}"
            + (" (älter als eine Woche!)" if planned.rate_stale else "")
        )
        for change in planned.changes:
            prices = ", ".join(f"{label} {eur} -> {usd}" for label, eur, usd in change.pairs())
            if change.old.unit_prices:
                prices = ", ".join(filter(None, [prices, change.units_text]))
            self.stdout.write(f"  {change.ai_model.display_name}: {prices}")
        for model in planned.without_price:
            self.stdout.write(f"  {model.display_name}: ohne Preis")
