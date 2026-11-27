"""Monatsbudgets (``mgpt-ctl budgets``).

    mgpt-ctl budgets list
    mgpt-ctl budgets set (--role KEY | --user NAME) BETRAG|aus

Gesamt-Monatsbudget in EUR über alle monetären Konten: je Rolle
(``Role.monthly_budget``) bzw. je Konto (``User.monthly_budget_override``, geht vor).
``aus`` entfernt das Budget (unbegrenzt bzw. wieder das der Rolle).
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from multigpt.accounts.models import Role
from multigpt.core.cli import OFF, decimal, table


def _eur(value) -> str:
    return "unbegrenzt" if value is None else f"{value:.2f} €".replace(".", ",")


class Command(BaseCommand):
    help = "Monatsbudgets je Rolle bzw. Konto anzeigen und setzen."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)
        sub.add_parser("list", help="Budgets der Rollen und abweichende Konten")
        change = sub.add_parser("set", help="Budget setzen")
        who = change.add_mutually_exclusive_group(required=True)
        who.add_argument("--role", help="Schlüssel der Rolle (z. B. adult)")
        who.add_argument("--user", help="Anmeldename")
        change.add_argument("amount", help="Betrag in EUR oder „aus“")

    def handle(self, *args, **options):
        User = get_user_model()
        if options["action"] == "list":
            rows = [
                ("Rolle", r.key, r.name, _eur(r.monthly_budget))
                for r in Role.objects.order_by("key")
            ]
            rows += [
                ("Konto", u.username, u.display_name, _eur(u.monthly_budget_override))
                for u in User.objects.filter(monthly_budget_override__isnull=False)
                .only("username", "display_name", "monthly_budget_override")
                .order_by("username")
            ]
            table(self.stdout, ("Art", "Schlüssel", "Name", "Monatsbudget"), rows)
            return
        raw = options["amount"]
        value = None if str(raw).strip().lower() in OFF else decimal(raw, "Betrag")
        if options["role"]:
            target = Role.objects.filter(key=options["role"]).first()
            if target is None:
                raise CommandError(f"Unbekannte Rolle „{options['role']}“.")
            old, target.monthly_budget = target.monthly_budget, value
            target.save(update_fields=["monthly_budget"])
            label = f"Rolle {target.name}"
        else:
            target = User.objects.filter(username__iexact=options["user"]).first()
            if target is None:
                raise CommandError(f"Unbekanntes Konto „{options['user']}“.")
            old, target.monthly_budget_override = target.monthly_budget_override, value
            target.save(update_fields=["monthly_budget_override"])
            label = f"Konto {target.username}"
        self.stdout.write(self.style.SUCCESS(f"{label}: {_eur(old)} -> {_eur(value)}"))
