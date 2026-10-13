"""``mgpt-ctl runner token …`` – Kopplungs-Tokens des Runners verwalten (M14-02).

    runner token create <konto> [--name …] [--expires 90d|JJJJ-MM-TT|never]
    runner token list [<konto>]
    runner token revoke <id oder präfix>
    runner keygen [--force]      Installationsschlüssel erzeugen (postinst; idempotent)
    runner pubkey                öffentlichen Installationsschlüssel ausgeben

Der Token wird bei ``create`` genau einmal ausgegeben (auf stdout, damit er sich
direkt in ``podman secret create mgpt_runner_token -`` leiten lässt) und nur als
Hash gespeichert. Es gelten dieselben Regeln wie auf der Seite „Runner“.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from multigpt.node.management.commands.apikey import parse_expiry

from ... import audit, tokens, trust
from ...mcp_server import ensure_mcp_server
from ...models import RunnerAudit, RunnerToken


class Command(BaseCommand):
    help = "Runner: Kopplungs-Tokens anlegen, auflisten und widerrufen."

    def add_arguments(self, parser):
        groups = parser.add_subparsers(dest="group", metavar="BEREICH")
        groups.required = True
        token = groups.add_parser("token", help="Kopplungs-Tokens.")
        sub = token.add_subparsers(dest="action", metavar="UNTERBEFEHL")
        sub.required = True
        create = sub.add_parser("create", help="Token anlegen (wird einmal angezeigt).")
        create.add_argument("account", help="Anmeldename des Kontos.")
        create.add_argument("--name", default="", help="Wofür der Token ist (z. B. Laptop).")
        create.add_argument("--expires", help="90d (Standard), JJJJ-MM-TT oder never.")
        listing = sub.add_parser("list", help="Tokens auflisten (ohne Geheimnis).")
        listing.add_argument("account", nargs="?", help="Nur dieses Konto.")
        revoke = sub.add_parser("revoke", help="Token widerrufen (trennt sofort).")
        revoke.add_argument("token", help="ID oder Präfix (mgrt_…).")
        keygen = groups.add_parser(
            "keygen", help="Installationsschlüssel (Ed25519) erzeugen, falls er fehlt."
        )
        keygen.add_argument(
            "--force",
            action="store_true",
            help="Vorhandenen Schlüssel ersetzen – alle Runner müssen neu gekoppelt werden.",
        )
        groups.add_parser("pubkey", help="Öffentlichen Installationsschlüssel ausgeben.")

    def handle(self, *args, **options):
        if options["group"] in ("keygen", "pubkey"):
            return getattr(self, f"_{options['group']}")(options)
        return getattr(self, f"_{options['group']}_{options['action']}")(options)

    def _keygen(self, options):
        path = trust.server_key_path()
        try:
            created, key = trust.generate_server_key(path, force=options["force"])
        except (OSError, trust.TrustError) as exc:
            raise CommandError(f"Installationsschlüssel {path}: {exc}") from None
        if created:
            self.stdout.write(f"Installationsschlüssel erzeugt: {path} (Kennung {key.key_id})")
            if options["force"]:
                self.stdout.write(
                    "Alle Runner müssen neu gekoppelt werden; Runner-Dienst neu starten."
                )
        else:
            self.stdout.write(f"Installationsschlüssel vorhanden: {path} (Kennung {key.key_id})")

    def _pubkey(self, options):
        try:
            key = trust.load_server_key()
        except trust.TrustError as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(key.public_b64)

    def _user(self, username):
        user = get_user_model().objects.filter(username=username).first()
        if user is None:
            raise CommandError(f"Konto „{username}“ nicht gefunden.")
        return user

    def _token_create(self, options):
        user = self._user(options["account"])
        try:
            created = tokens.create_token(
                user, options["name"], expires_at=parse_expiry(options["expires"])
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from None
        ensure_mcp_server(user)
        token = created.token
        audit.record(
            RunnerAudit.Kind.TOKEN,
            owner=user,
            message=f"Token {token.display_prefix} angelegt (mgpt-ctl)",
        )
        expiry = (
            timezone.localtime(token.expires_at).strftime("%d.%m.%Y %H:%M")
            if token.expires_at
            else "kein Ablauf"
        )
        self.stderr.write(
            f"Runner-Token #{token.pk} „{token.name}“ für {user.get_username()} angelegt "
            f"(gültig bis: {expiry}).\nDer Token wird nur dieses eine Mal angezeigt:"
        )
        self.stdout.write(created.secret)

    def _token_list(self, options):
        qs = RunnerToken.objects.select_related("owner", "runner").order_by(
            "owner__username", "-created"
        )
        if options["account"]:
            qs = qs.filter(owner=self._user(options["account"]))
        if not qs:
            self.stdout.write("Keine Runner-Tokens.")
        for token in qs:
            used = (
                timezone.localtime(token.last_used_at).strftime("%d.%m.%Y %H:%M")
                if token.last_used_at
                else "nie"
            )
            expiry = (
                timezone.localtime(token.expires_at).strftime("%d.%m.%Y")
                if token.expires_at
                else "–"
            )
            runner = getattr(token, "runner", None)
            state = f"{runner.get_status_display()} ({runner.name or '–'})" if runner else "–"
            self.stdout.write(
                f"#{token.pk:<4} {token.display_prefix:<18} {token.owner.get_username():<12} "
                f"{token.state_label:<11} bis {expiry:<10} zuletzt {used:<16} "
                f"Runner {state} „{token.name}“"
            )

    def _token_revoke(self, options):
        token = tokens.find(options["token"])
        if token is None:
            raise CommandError("Token nicht gefunden.")
        if tokens.revoke(token):
            audit.record(
                RunnerAudit.Kind.TOKEN,
                owner=token.owner,
                message=f"Token {token.display_prefix} widerrufen (mgpt-ctl)",
            )
            self.stdout.write(self.style.SUCCESS(f"Token #{token.pk} „{token.name}“ widerrufen."))
        else:
            self.stdout.write(f"Token #{token.pk} war schon widerrufen.")
