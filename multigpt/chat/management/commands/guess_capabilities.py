"""Fähigkeiten bestehender Modelle erkennen (Hauptart, Werkzeuge, Bilder).

Wie die Admin-Aktion „Fähigkeiten automatisch erkennen“: Quelle ist die
Meldung von LM Studio (lokale Anbieter, ``/api/v0/models``), sonst die
Heuristik aus der Modell-ID. Ohne ``--apply`` nur Vorschau; mit ``--apply``
werden die angezeigten Abweichungen gespeichert. MCP-Freigaben bleiben
unverändert.
"""

from django.core.management.base import BaseCommand, CommandError

from multigpt.chat import detect
from multigpt.chat.models import AIModel, Provider


class Command(BaseCommand):
    help = (
        "Erkennt Hauptart, Werkzeuge und Bild-Eingabe der KI-Modelle (LM Studio bzw. "
        "Modell-ID). Ohne --apply nur Vorschau."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Abweichungen speichern.")
        parser.add_argument("--provider", help="Nur Modelle dieses Anbieters (Name oder ID).")

    def handle(self, *args, **options):
        models = AIModel.objects.select_related("provider").order_by("provider__name", "model_id")
        selector = options["provider"]
        if selector:
            lookup = {"pk": int(selector)} if selector.isdigit() else {"name": selector}
            provider = Provider.objects.filter(**lookup).first()
            if provider is None:
                raise CommandError(f"Anbieter „{selector}“ nicht gefunden.")
            models = models.filter(provider=provider)
        models = list(models)
        changes = detect.diff(models)
        for change in changes:
            source = "LM Studio" if change.detected.source == "lmstudio" else "Modell-ID"
            self.stdout.write(
                f"  {change.model.provider.name} / {change.model.model_id} ({source}): "
                f"{change.describe()}"
            )
        if options["apply"]:
            detect.apply(changes)
            verb = "geändert"
        else:
            verb = "würden geändert (Vorschau, speichern mit --apply)"
        self.stdout.write(
            self.style.SUCCESS(
                f"{len(models)} Modelle geprüft, {len(changes)} {verb}, "
                f"{len(models) - len(changes)} passen bereits."
            )
        )
