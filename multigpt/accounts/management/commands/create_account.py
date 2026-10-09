"""Konto mit Rolle anlegen (`make user`, `mgpt-ctl create_account`).

Fehlende Angaben werden abgefragt, die Rolle per Nummer oder Schlüssel. Das
Passwort wird immer verdeckt per getpass gelesen, nie als Argument (es landete
sonst in der Shell-History und in der Prozessliste).
"""

import getpass

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from multigpt.accounts.models import Role

MAX_PASSWORD_TRIES = 3


class Command(BaseCommand):
    help = "Legt ein Familienkonto mit Rolle an. Das Passwort wird verdeckt abgefragt."

    def add_arguments(self, parser):
        parser.add_argument("username", nargs="?", help="Anmeldename")
        parser.add_argument("--role", help="Schlüssel der Rolle, z. B. admin, adult, teen, guest")
        parser.add_argument("--display-name", default="", help="Anzeigename")
        parser.add_argument("--email", default="", help="E-Mail-Adresse")

    def handle(self, *args, **options):
        try:
            self._create(options)
        except (EOFError, KeyboardInterrupt) as exc:
            raise CommandError("Abgebrochen.") from exc

    def _create(self, options):
        User = get_user_model()
        roles = list(Role.objects.order_by("-is_admin", "name"))
        if not roles:
            raise CommandError("Es gibt keine Rollen. Zuerst `make migrate` ausführen.")

        username = (options["username"] or "").strip() or self._ask("Anmeldename: ")
        if User.objects.filter(username=username).exists():
            raise CommandError(f"Das Konto „{username}“ existiert bereits.")

        role = self._resolve_role(options["role"], roles)
        user = User(
            username=username,
            role=role,
            display_name=options["display_name"],
            email=options["email"],
            is_staff=role.is_admin,
        )
        try:
            user.full_clean(exclude=["password"])
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages)) from exc

        user.set_password(self._ask_password(user))
        with transaction.atomic():
            user.save()
        self.stdout.write(
            self.style.SUCCESS(f"Konto „{username}“ mit Rolle „{role.name}“ angelegt.")
        )

    def _ask(self, prompt):
        value = input(prompt).strip()
        if not value:
            raise CommandError("Abgebrochen: keine Eingabe.")
        return value

    def _resolve_role(self, key, roles):
        by_key = {r.key: r for r in roles}
        if key:
            if key not in by_key:
                raise CommandError(
                    f"Unbekannte Rolle „{key}“. Möglich: {', '.join(sorted(by_key))}."
                )
            return by_key[key]
        self.stdout.write("Rollen:")
        for number, role in enumerate(roles, start=1):
            self.stdout.write(f"  {number}) {role.name} ({role.key})")
        answer = self._ask("Rolle (Nummer oder Schlüssel): ")
        if answer in by_key:
            return by_key[answer]
        if answer.isdigit() and 1 <= int(answer) <= len(roles):
            return roles[int(answer) - 1]
        raise CommandError(f"Unbekannte Rolle „{answer}“.")

    def _ask_password(self, user):
        for _ in range(MAX_PASSWORD_TRIES):
            password = getpass.getpass("Passwort: ")
            if password != getpass.getpass("Passwort (Wiederholung): "):
                self.stderr.write("Die Passwörter stimmen nicht überein.")
                continue
            try:
                validate_password(password, user)
            except ValidationError as exc:
                for message in exc.messages:
                    self.stderr.write(message)
                continue
            return password
        raise CommandError("Kein gültiges Passwort eingegeben.")
