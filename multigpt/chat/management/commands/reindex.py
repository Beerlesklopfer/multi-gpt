"""``manage.py reindex`` (``make reindex``): alle Dokumente neu indexieren.

Nötig nach einem Wechsel des Embedding-Modells oder der Zerteilung (Plan 8b).
Legt je Dokument einen Indexierungsjob an (``rag.jobs.enqueue_index``, ein
schon wartender Job wird wiederverwendet); die Arbeit macht der Worker
(``make worker``). Bis ein Dokument neu indexiert ist, bleiben seine alten
Abschnitte durchsuchbar.
"""

from django.core.management.base import BaseCommand

from multigpt.chat.models import Document
from multigpt.chat.rag.jobs import enqueue_index


class Command(BaseCommand):
    help = "Legt für alle (bzw. die gewählten) Dokumente Indexierungsjobs an."

    def add_arguments(self, parser):
        parser.add_argument(
            "--collection", type=int, action="append", help="Nur diese Sammlung (ID, mehrfach)."
        )
        parser.add_argument(
            "--document", type=int, action="append", help="Nur dieses Dokument (ID, mehrfach)."
        )
        parser.add_argument(
            "--errors-only", action="store_true", help="Nur Dokumente mit Status „Fehler“."
        )

    def handle(self, *args, **options):
        documents = Document.objects.order_by("pk")
        if options["collection"]:
            documents = documents.filter(collection_id__in=options["collection"])
        if options["document"]:
            documents = documents.filter(pk__in=options["document"])
        if options["errors_only"]:
            documents = documents.filter(status=Document.Status.ERROR)
        count = 0
        for document in documents.iterator():
            enqueue_index(document)
            count += 1
        if count:
            self.stdout.write(
                self.style.SUCCESS(
                    f"{count} Dokument(e) zur Indexierung eingereiht. "
                    "Der Worker (make worker) arbeitet sie ab."
                )
            )
        else:
            self.stdout.write("Keine Dokumente gefunden.")
