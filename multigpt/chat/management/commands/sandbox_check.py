"""Rechen-Sandbox prüfen (``mgpt-ctl sandbox_check``), wie „Sandbox testen“ im Admin."""

from django.core.management.base import BaseCommand, CommandError

from multigpt.chat import sandbox


class Command(BaseCommand):
    help = "Prüft, ob die Sandbox für Berechnungen (bubblewrap) nutzbar ist."

    def handle(self, *args, **options):
        status = sandbox.status(refresh=True)
        if not status.available:
            raise CommandError(f"Sandbox nicht nutzbar: {status.reason}")
        extra = f", Pakete: {', '.join(status.packages)}" if status.packages else ""
        self.stdout.write(self.style.SUCCESS(f"Sandbox nutzbar{extra}."))
