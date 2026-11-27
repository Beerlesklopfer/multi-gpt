"""Konten auflisten (``mgpt-ctl listusers``), Gegenstück zu ``create_account`` und
``changepassword``.

Zeigt je Konto Anmeldename, Anzeigename, Rolle, Status und letzte Anmeldung. Keine
Passwörter, Hashes, E-Mail-Adressen oder API-Keys. Filter: ``--role``, ``--active`` bzw.
``--inactive``; ``--format csv`` für Skripte.
"""

import csv

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from multigpt.accounts.models import Role

HEADERS = ("Anmeldename", "Anzeigename", "Rolle", "Status", "Letzte Anmeldung")


class Command(BaseCommand):
    help = "Listet die Konten mit Rolle, Status und letzter Anmeldung."

    def add_arguments(self, parser):
        parser.add_argument("--role", help="Nur diese Rolle (Schlüssel, z. B. adult, teen)")
        state = parser.add_mutually_exclusive_group()
        state.add_argument("--active", action="store_true", help="Nur aktive Konten")
        state.add_argument("--inactive", action="store_true", help="Nur gesperrte Konten")
        parser.add_argument(
            "--format", choices=("table", "csv"), default="table", help="Ausgabe (Standard table)"
        )

    def handle(self, *args, **options):
        users = get_user_model().objects.select_related("role").order_by("username")
        if options["role"]:
            if not Role.objects.filter(key=options["role"]).exists():
                keys = ", ".join(Role.objects.order_by("key").values_list("key", flat=True))
                raise CommandError(f"Unbekannte Rolle „{options['role']}“. Möglich: {keys}.")
            users = users.filter(role__key=options["role"])
        if options["active"]:
            users = users.filter(is_active=True)
        elif options["inactive"]:
            users = users.filter(is_active=False)
        rows = [self._row(user) for user in users]
        if options["format"] == "csv":
            writer = csv.writer(self.stdout, lineterminator="\n")
            writer.writerow(HEADERS)
            writer.writerows(rows)
            return
        if not rows:
            self.stdout.write("Keine Konten gefunden.")
            return
        widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(HEADERS)]

        def line(cells) -> str:
            return "  ".join(
                f"{cell:<{width}}" for cell, width in zip(cells, widths, strict=True)
            ).rstrip()

        self.stdout.write(line(HEADERS))
        self.stdout.write(line("-" * w for w in widths))
        for row in rows:
            self.stdout.write(line(row))
        self.stdout.write(f"{len(rows)} Konto/Konten.")

    @staticmethod
    def _row(user) -> tuple[str, ...]:
        if not user.is_active:
            status = "gesperrt"
        elif user.is_superuser:
            status = "aktiv, Superuser"
        elif user.is_staff:
            status = "aktiv, Admin"
        else:
            status = "aktiv"
        last = (
            timezone.localtime(user.last_login).strftime("%d.%m.%Y %H:%M")
            if user.last_login
            else "nie"
        )
        role = user.role.name if user.role_id else "–"
        return (user.username, user.display_name or "", role, status, last)
