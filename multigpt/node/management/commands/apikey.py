"""``mgpt-ctl apikey …`` – API-Keys für den MCP-Server verwalten (M15).

    apikey create <konto> --name … --scopes chat.ask,docs.read [--expires 90d|JJJJ-MM-TT|never]
                          [--collection ID …]
    apikey list [<konto>]
    apikey revoke <id oder präfix>

Der Key wird bei ``create`` genau einmal ausgegeben (auf stdout, damit er sich
in eine Datei umleiten lässt) und nur als Hash gespeichert. Es gelten dieselben
Regeln wie auf der Seite „API-Keys“: Rechte höchstens im Rahmen der Rolle.
"""

from __future__ import annotations

import datetime as dt
import re

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from multigpt.chat.api_collections import readable_collections

from ... import keys
from ... import scopes as api_scopes
from ...models import KEY_PREFIX, ApiKey

DEFAULT_EXPIRY_DAYS = 90


def parse_expiry(text: str | None):
    """``90d`` bzw. ``90`` (Tage), ``JJJJ-MM-TT`` (Ende des Tages) oder ``never``."""
    value = (text or f"{DEFAULT_EXPIRY_DAYS}d").strip().lower()
    if value in ("never", "nie", "0"):
        return None
    match = re.fullmatch(r"(\d{1,4})d?", value)
    if match:
        return timezone.now() + dt.timedelta(days=int(match.group(1)))
    try:
        day = dt.date.fromisoformat(value)
    except ValueError:
        raise CommandError("--expires: z. B. 90d, 2027-01-31 oder never.") from None
    end = dt.datetime.combine(day, dt.time(23, 59, 59))
    return timezone.make_aware(end)


class Command(BaseCommand):
    help = "API-Keys für den MCP-Server anlegen, auflisten und widerrufen."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", metavar="UNTERBEFEHL")
        sub.required = True
        create = sub.add_parser("create", help="Key anlegen (wird einmal angezeigt).")
        create.add_argument("account", help="Anmeldename des Kontos.")
        create.add_argument("--name", required=True, help="Wofür der Key ist.")
        create.add_argument(
            "--scopes",
            required=True,
            help=f"Kommagetrennt aus {', '.join(api_scopes.ALL_SCOPES)} oder „all“.",
        )
        create.add_argument("--expires", help="90d (Standard), JJJJ-MM-TT oder never.")
        create.add_argument(
            "--collection", type=int, action="append", help="Nur diese Sammlung (mehrfach)."
        )
        listing = sub.add_parser("list", help="Keys auflisten (ohne Geheimnis).")
        listing.add_argument("account", nargs="?", help="Nur dieses Konto.")
        revoke = sub.add_parser("revoke", help="Key widerrufen (sofort wirksam).")
        revoke.add_argument("key", help="ID oder Präfix (mgpt_…).")

    def handle(self, *args, **options):
        return getattr(self, f"_{options['action']}")(options)

    def _user(self, username):
        user = get_user_model().objects.filter(username=username).first()
        if user is None:
            raise CommandError(f"Konto „{username}“ nicht gefunden.")
        return user

    def _create(self, options):
        user = self._user(options["account"])
        try:
            wanted = api_scopes.parse(options["scopes"])
        except ValueError as exc:
            raise CommandError(str(exc)) from None
        collections = []
        if options["collection"]:
            collections = list(readable_collections(user).filter(pk__in=options["collection"]))
            if len(collections) != len(set(options["collection"])):
                raise CommandError("Mindestens eine Sammlung ist für dieses Konto nicht lesbar.")
        try:
            created = keys.create_key(
                user,
                options["name"],
                wanted,
                expires_at=parse_expiry(options["expires"]),
                collections=collections,
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from None
        key = created.key
        expiry = (
            timezone.localtime(key.expires_at).strftime("%d.%m.%Y %H:%M")
            if key.expires_at
            else "kein Ablauf"
        )
        self.stderr.write(
            f"Key #{key.pk} „{key.name}“ für {user.get_username()} angelegt "
            f"(Rechte: {', '.join(key.scopes)}; gültig bis: {expiry}).\n"
            "Der Key wird nur dieses eine Mal angezeigt:"
        )
        self.stdout.write(created.secret)

    def _list(self, options):
        qs = ApiKey.objects.select_related("owner").order_by("owner__username", "-created")
        if options["account"]:
            qs = qs.filter(owner=self._user(options["account"]))
        if not qs:
            self.stdout.write("Keine API-Keys.")
        for key in qs:
            used = (
                timezone.localtime(key.last_used_at).strftime("%d.%m.%Y %H:%M")
                if key.last_used_at
                else "nie"
            )
            expiry = (
                timezone.localtime(key.expires_at).strftime("%d.%m.%Y") if key.expires_at else "–"
            )
            self.stdout.write(
                f"#{key.pk:<4} {key.display_prefix:<18} {key.owner.get_username():<12} "
                f"{key.state_label:<11} bis {expiry:<10} zuletzt {used:<16} "
                f"„{key.name}“ [{', '.join(key.scopes)}]"
            )

    def _revoke(self, options):
        ref = options["key"].strip()
        if ref.isdigit():
            key = ApiKey.objects.filter(pk=int(ref)).first()
        else:
            prefix = ref.removeprefix(KEY_PREFIX).rstrip("…").split("_", 1)[0]
            key = ApiKey.objects.filter(prefix=prefix).first() if prefix else None
        if key is None:
            raise CommandError("Key nicht gefunden.")
        if keys.revoke(key):
            self.stdout.write(self.style.SUCCESS(f"Key #{key.pk} „{key.name}“ widerrufen."))
        else:
            self.stdout.write(f"Key #{key.pk} war schon widerrufen.")
