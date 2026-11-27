"""KI-Modelle (``mgpt-ctl models``).

mgpt-ctl models list [--provider NAME]
mgpt-ctl models set MODELL_ID [--provider NAME] [--load-priority 0-100]
                              [--pinned an|aus] [--active an|aus]
"""

from django.core.management.base import BaseCommand, CommandError

from multigpt.chat.models import AIModel
from multigpt.core.cli import switch, table, yesno


class Command(BaseCommand):
    help = "KI-Modelle anzeigen und Priorität, Anheften und Aktivierung setzen."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)
        listing = sub.add_parser("list", help="Modelle")
        listing.add_argument("--provider", help="nur dieser Anbieter")
        change = sub.add_parser("set", help="Einstellungen eines Modells ändern")
        change.add_argument("model_id")
        change.add_argument("--provider", help="Anbieter, falls die Modell-ID mehrfach vorkommt")
        change.add_argument("--load-priority", type=int, help="0–100")
        change.add_argument("--pinned", help="an|aus: nie automatisch entladen")
        change.add_argument("--active", help="an|aus")

    def _queryset(self, provider):
        qs = AIModel.objects.select_related("provider")
        return qs.filter(provider__name__iexact=provider) if provider else qs

    def handle(self, *args, **options):
        if options["action"] == "list":
            rows = [
                (
                    m.provider.name,
                    m.model_id,
                    m.get_capability_display(),
                    yesno(m.active),
                    m.load_priority,
                    yesno(m.pinned),
                )
                for m in self._queryset(options["provider"]).order_by("provider__name", "model_id")
            ]
            headers = ("Anbieter", "Modell", "Art", "aktiv", "Priorität", "angeheftet")
            table(self.stdout, headers, rows, "Keine Modelle.")
            return
        matches = list(self._queryset(options["provider"]).filter(model_id=options["model_id"]))
        if not matches:
            raise CommandError(f"Modell „{options['model_id']}“ nicht gefunden.")
        if len(matches) > 1:
            names = ", ".join(m.provider.name for m in matches)
            raise CommandError(
                f"Modell gibt es bei mehreren Anbietern ({names}): --provider angeben."
            )
        model = matches[0]
        changes = {}
        if options["load_priority"] is not None:
            if not 0 <= options["load_priority"] <= 100:
                raise CommandError("--load-priority: 0 bis 100.")
            changes["load_priority"] = options["load_priority"]
        if options["pinned"] is not None:
            changes["pinned"] = switch(options["pinned"], "--pinned")
        if options["active"] is not None:
            changes["active"] = switch(options["active"], "--active")
        if not changes:
            raise CommandError("Nichts zu ändern (Optionen siehe --help).")
        for field, value in changes.items():
            self.stdout.write(f"{model.model_id}: {field} {getattr(model, field)} -> {value}")
            setattr(model, field, value)
        model.save(update_fields=list(changes))
        self.stdout.write(self.style.SUCCESS("Gespeichert."))
