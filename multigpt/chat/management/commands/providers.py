"""Anbieter (``mgpt-ctl providers``).

mgpt-ctl providers list
mgpt-ctl providers set NAME [--manage-models an|aus] [--vram-budget GB]
                            [--vram-reserve GB] [--check-status an|aus] [--active an|aus]
"""

from django.core.management.base import BaseCommand, CommandError

from multigpt.chat.models import Provider
from multigpt.core.cli import decimal, switch, table, yesno


class Command(BaseCommand):
    help = "Anbieter anzeigen und Modellverwaltung, Speicherbudget und Online-Status setzen."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)
        sub.add_parser("list", help="Alle Anbieter")
        change = sub.add_parser("set", help="Einstellungen eines Anbieters ändern")
        change.add_argument("name", help="Name des Anbieters (z. B. Eliezer)")
        change.add_argument("--manage-models", help="an|aus: MultiGPT lädt/entlädt Modelle")
        change.add_argument("--vram-budget", help="Speicherbudget für Modelle in GB")
        change.add_argument("--vram-reserve", help="Reserve für Anzeige/System in GB")
        change.add_argument("--check-status", help="an|aus: Online-Status regelmäßig prüfen")
        change.add_argument("--active", help="an|aus")

    def handle(self, *args, **options):
        if options["action"] == "list":
            rows = [
                (
                    p.name,
                    p.get_kind_display(),
                    "lokal" if p.is_local else "Cloud",
                    yesno(p.active),
                    yesno(p.manage_models),
                    p.vram_budget_gb or "",
                    p.vram_reserve_gb,
                    yesno(p.check_status),
                )
                for p in Provider.objects.order_by("name")
            ]
            headers = (
                "Name",
                "Schnittstelle",
                "Ort",
                "aktiv",
                "verwalten",
                "Budget GB",
                "Reserve GB",
                "Online-Prüfung",
            )
            table(self.stdout, headers, rows, "Keine Anbieter.")
            return
        provider = Provider.objects.filter(name__iexact=options["name"]).first()
        if provider is None:
            names = ", ".join(Provider.objects.order_by("name").values_list("name", flat=True))
            raise CommandError(f"Unbekannter Anbieter „{options['name']}“. Vorhanden: {names}.")
        changes = {}
        if options["manage_models"] is not None:
            changes["manage_models"] = switch(options["manage_models"], "--manage-models")
        if options["vram_budget"] is not None:
            changes["vram_budget_gb"] = decimal(options["vram_budget"], "--vram-budget", low=0)
        if options["vram_reserve"] is not None:
            changes["vram_reserve_gb"] = decimal(options["vram_reserve"], "--vram-reserve")
        if options["check_status"] is not None:
            changes["check_status"] = switch(options["check_status"], "--check-status")
        if options["active"] is not None:
            changes["active"] = switch(options["active"], "--active")
        if not changes:
            raise CommandError("Nichts zu ändern (Optionen siehe --help).")
        if changes.get("manage_models") and not (
            changes.get("vram_budget_gb") or provider.vram_budget_gb
        ):
            raise CommandError("„Modelle verwalten“ braucht ein Speicherbudget (--vram-budget).")
        for field, value in changes.items():
            old = getattr(provider, field)
            setattr(provider, field, value)
            self.stdout.write(f"{provider.name}: {field} {old} -> {value}")
        provider.full_clean()
        provider.save(update_fields=list(changes))
        self.stdout.write(self.style.SUCCESS("Gespeichert."))
