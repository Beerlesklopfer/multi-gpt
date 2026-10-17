"""Vorhandene Werkzeuganhänge einmalig in die zentrale Ablage übernehmen."""

from django.core.management.base import BaseCommand
from django.db.models import Q

from multigpt.chat.models import Attachment
from multigpt.scratchpad.services import store_attachment


class Command(BaseCommand):
    help = "Bestehende MCP-Werkzeuganhänge ins Server-Scratchpad übernehmen (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--owner-id", type=int, help="Nur Dateien dieses Kontos.")
        parser.add_argument("--tool", help="Nur Dateien dieses Werkzeugs.")

    def handle(self, *args, **options):
        imported = failed = 0
        files = (
            Attachment.objects.filter(tool_call__isnull=False)
            .exclude(file="")
            .select_related("message__author", "message__conversation__user", "tool_call")
        )
        if options["owner_id"] is not None:
            owner_id = options["owner_id"]
            files = files.filter(
                Q(message__author_id=owner_id)
                | Q(message__author__isnull=True, message__conversation__user_id=owner_id)
            )
        if options["tool"]:
            files = files.filter(tool_call__tool=options["tool"])
        for attachment in files.iterator():
            try:
                store_attachment(attachment)
                imported += 1
            except (OSError, ValueError) as exc:
                failed += 1
                self.stderr.write(f"Anhang #{attachment.pk}: {type(exc).__name__}")
        self.stdout.write(f"{imported} Werkzeugdateien im Scratchpad, {failed} nicht übernommen.")
