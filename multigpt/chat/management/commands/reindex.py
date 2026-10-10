"""``manage.py reindex`` (``make reindex``): alle Dokumente neu indexieren.

Nötig nach einem Wechsel des Embedding-Modells oder der Zerteilung (Plan 8b).
Legt je Dokument einen Indexierungsjob an (``rag.jobs.enqueue_index``, ein
schon wartender Job wird wiederverwendet); die Arbeit macht der Worker
(``make worker``). Bis ein Dokument neu indexiert ist, bleiben seine alten
Abschnitte durchsuchbar. Seit M15 hängen die Aufträge an einem Lauf, den
``mgpt-ctl index status --follow`` beobachtet (gleich: ``mgpt-ctl index reindex``).
"""

from django.core.management.base import BaseCommand

from multigpt.node.runs import reindex_selection


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
        count, run = reindex_selection(
            collections=options["collection"],
            documents=options["document"],
            errors_only=options["errors_only"],
        )
        if count:
            self.stdout.write(
                self.style.SUCCESS(
                    f"{count} Dokument(e) zur Indexierung eingereiht (Lauf #{run.pk}). "
                    "Der Worker (make worker) arbeitet sie ab; Fortschritt: "
                    f"mgpt-ctl index status --follow --run {run.pk}"
                )
            )
        else:
            self.stdout.write("Keine Dokumente gefunden.")
