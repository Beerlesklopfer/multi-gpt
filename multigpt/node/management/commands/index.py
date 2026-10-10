"""``mgpt-ctl index …`` – Indexierung auf der Konsole überwachen und steuern (M15).

Unterbefehle::

    index status [--follow] [--run ID] [--interval S]   Läufe, Warteschlange, Worker
    index runs [--open] [--limit N]                     letzte Läufe als Tabelle
    index cancel <lauf>                                 Lauf abbrechen
    index scan <quelle>                                 Verzeichnisquelle jetzt einlesen
    index upload <sammlung> <dateien…> --as <konto>     Dateien hochladen (Rechte des Kontos)
    index reindex [--collection ID] [--document ID] [--errors-only]
    index sources                                       Verzeichnisquellen

``--follow`` aktualisiert die Anzeige wie ``top`` (Strg+C beendet). Der Betrieb
(root bzw. Dienstnutzer) steuert wie ein Verwalter; nur ``upload`` handelt im
Namen eines Kontos und prüft dessen Rechte wie die Oberfläche. Ausgaben ohne
Dateiinhalte und ohne Serverpfade der Dokumente.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.files import File
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from multigpt.chat.models import IndexRun
from multigpt.chat.rag import jobs, upload
from multigpt.rag import crawl
from multigpt.rag import paths as source_paths
from multigpt.rag.models import DirectorySource

from ... import runs

CLEAR = "\033[H\033[2J"


def _age(when, now) -> str:
    if when is None:
        return "–"
    seconds = int((now - when).total_seconds())
    if seconds < 120:
        return f"vor {seconds} s"
    if seconds < 7200:
        return f"vor {seconds // 60} min"
    return timezone.localtime(when).strftime("%d.%m. %H:%M")


def _error_text(response) -> str:
    try:
        return json.loads(response.content)["error"]
    except (ValueError, KeyError, TypeError):
        return "Nicht möglich."


def _run_line(run: IndexRun) -> str:
    target = ""
    if run.collection_id:
        target = f"Sammlung #{run.collection_id} „{run.collection.name}“"
    if run.source_id:
        target += f" (Quelle #{run.source_id})"
    by = f", von {run.started_by.get_username()}" if run.started_by_id else ""
    return (
        f"  #{run.pk:<5} {run.get_kind_display():<26} {run.get_status_display():<17} "
        f"{target}{by}\n         {run.progress_text().split(': ', 1)[-1]}"
        + (f"\n         Fehler: {run.error_text}" if run.error_text else "")
    )


class Command(BaseCommand):
    help = "Indexierung überwachen und steuern (Läufe, Warteschlange, Verzeichnisquellen)."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", metavar="UNTERBEFEHL")
        sub.required = True

        status = sub.add_parser("status", help="Läufe, Warteschlange und Worker anzeigen.")
        status.add_argument("--follow", "-f", action="store_true", help="Laufend aktualisieren.")
        status.add_argument("--run", type=int, help="Nur diesen Lauf (ausführlich).")
        status.add_argument(
            "--interval", type=float, default=2.0, help="Sekunden zwischen Aktualisierungen."
        )
        # Für Tests: nach N Anzeigen beenden (0 = endlos).
        status.add_argument("--ticks", type=int, default=0, help="Nach N Anzeigen beenden.")

        runs_parser = sub.add_parser("runs", help="Letzte Läufe auflisten.")
        runs_parser.add_argument("--open", action="store_true", help="Nur offene Läufe.")
        runs_parser.add_argument("--limit", type=int, default=20)

        cancel = sub.add_parser("cancel", help="Lauf abbrechen.")
        cancel.add_argument("run", type=int, help="ID des Laufs.")

        scan = sub.add_parser("scan", help="Verzeichnisquelle jetzt einlesen.")
        scan.add_argument("source", type=int, help="ID der Verzeichnisquelle (index sources).")

        sub.add_parser("sources", help="Verzeichnisquellen auflisten.")

        up = sub.add_parser("upload", help="Dateien in eine Sammlung hochladen.")
        up.add_argument("collection", help="ID oder Name der Sammlung (aus Sicht des Kontos).")
        up.add_argument("files", nargs="+", help="Dateien.")
        up.add_argument("--as", dest="account", required=True, help="Anmeldename des Kontos.")

        re = sub.add_parser("reindex", help="Dokumente neu indexieren (als Lauf).")
        re.add_argument("--collection", type=int, action="append")
        re.add_argument("--document", type=int, action="append")
        re.add_argument("--errors-only", action="store_true")

    def handle(self, *args, **options):
        action = options["action"]
        return getattr(self, f"_{action}")(options)

    # --- Anzeige -------------------------------------------------------------------

    def _render(self, run_id=None) -> str:
        now = timezone.now()
        lines = [f"MultiGPT – Indexierung   {timezone.localtime(now):%d.%m.%Y %H:%M:%S}", ""]
        if run_id is not None:
            run = IndexRun.objects.select_related("collection", "started_by").filter(pk=run_id)
            run = run.first()
            if run is None:
                raise CommandError(f"Lauf #{run_id} nicht gefunden.")
            data = runs.serialize_run(run)
            lines.append(_run_line(run))
            lines.append("")
            for key in (
                "files_found",
                "files_checked",
                "files_new",
                "files_changed",
                "files_deleted",
                "files_skipped",
                "docs_queued",
                "docs_done",
                "docs_failed",
                "docs_cancelled",
            ):
                lines.append(f"  {IndexRun._meta.get_field(key).verbose_name:<24} {data[key]}")
            lines.append(f"  {'Dauer':<24} {run.duration_text()}")
            return "\n".join(lines)
        queue = runs.queue_state(now)
        lines.append(
            f"Worker: letztes Lebenszeichen {_age(queue.last_heartbeat, now)} | Aufträge: "
            f"{queue.running} laufen, {queue.due} fällig, {queue.waiting} warten, "
            f"{queue.failed} fehlgeschlagen"
            + (f", {queue.cancelling} werden abgebrochen" if queue.cancelling else "")
        )
        if queue.worker_warning:
            lines.append(
                "WARNUNG: Worker läuft nicht? (systemctl status multi-gpt-worker)"
                + (f" – {queue.stale} Auftrag/Aufträge ohne Lebenszeichen" if queue.stale else "")
            )
        lines.append("")
        open_runs = list(
            IndexRun.objects.filter(status__in=IndexRun.OPEN)
            .select_related("collection", "started_by")
            .order_by("-pk")
        )
        lines.append(f"Offene Läufe: {len(open_runs)}")
        lines += [_run_line(r) for r in open_runs] or ["  –"]
        lines.append("")
        done = (
            IndexRun.objects.exclude(status__in=IndexRun.OPEN)
            .select_related("collection", "started_by")
            .order_by("-pk")[:5]
        )
        lines.append("Zuletzt beendet:")
        lines += [_run_line(r) for r in done] or ["  –"]
        return "\n".join(lines)

    def _status(self, options):
        follow, ticks = options["follow"], max(0, options["ticks"])
        interval = max(0.0, options["interval"])
        tty = hasattr(self.stdout, "isatty") and self.stdout.isatty()
        shown = 0
        try:
            while True:
                text = self._render(options["run"])
                if follow and tty:
                    self.stdout.write(CLEAR + text)
                else:
                    if shown:
                        self.stdout.write("-" * 60)
                    self.stdout.write(text)
                shown += 1
                if not follow or (ticks and shown >= ticks):
                    break
                if follow and options["run"] is not None:
                    run = IndexRun.objects.filter(pk=options["run"]).first()
                    if run is not None and not run.is_open and not ticks:
                        self.stdout.write("Lauf beendet.")
                        break
                time.sleep(interval)
        except KeyboardInterrupt:
            self.stdout.write("\nBeendet.")

    def _runs(self, options):
        qs = IndexRun.objects.select_related("collection", "started_by").order_by("-pk")
        if options["open"]:
            qs = qs.filter(status__in=IndexRun.OPEN)
        items = list(qs[: max(1, options["limit"])])
        if not items:
            self.stdout.write("Keine Läufe.")
        for run in items:
            self.stdout.write(_run_line(run))

    def _sources(self, options):
        if not source_paths.enabled():
            self.stdout.write("Verzeichnisquellen sind aus (RAG_SOURCE_ROOTS leer).")
        items = DirectorySource.objects.select_related("collection").order_by("pk")
        if not items:
            self.stdout.write("Keine Verzeichnisquellen.")
        for source in items:
            run = crawl.open_run(source)
            state = f"Lauf #{run.pk} offen" if run else "kein offener Lauf"
            self.stdout.write(
                f"  #{source.pk:<4} Sammlung #{source.collection_id} „{source.collection.name}“, "
                f"{'aktiv' if source.active else 'pausiert'}, alle {source.interval_minutes} min, "
                f"zuletzt {_age(source.last_scan_started, timezone.now())}, {state}"
            )

    # --- Steuern -------------------------------------------------------------------

    def _cancel(self, options):
        run = IndexRun.objects.filter(pk=options["run"]).first()
        if run is None:
            raise CommandError(f"Lauf #{options['run']} nicht gefunden.")
        outcome = jobs.cancel_run(run.pk)
        if outcome is None:
            raise CommandError(f"Lauf #{run.pk} ist schon beendet.")
        removed, marked = outcome
        self.stdout.write(
            self.style.SUCCESS(f"Lauf #{run.pk}: {removed} Aufträge entfernt, {marked} markiert.")
        )
        if marked:
            self.stdout.write("Laufende Aufträge enden nach dem aktuellen Schritt.")

    def _scan(self, options):
        if not source_paths.enabled():
            raise CommandError("Verzeichnisquellen sind aus (RAG_SOURCE_ROOTS leer).")
        source = DirectorySource.objects.filter(pk=options["source"]).first()
        if source is None:
            raise CommandError(f"Verzeichnisquelle #{options['source']} nicht gefunden.")
        job = crawl.enqueue_scan(source)
        if job is None:
            run = crawl.open_run(source)
            self.stdout.write(
                f"Für Quelle #{source.pk} läuft schon Lauf #{run.pk if run else '?'}."
            )
            return
        self.stdout.write(
            self.style.SUCCESS(
                f"Lauf #{job.run_id} gestartet. Verfolgen: mgpt-ctl index status --follow "
                f"--run {job.run_id}"
            )
        )

    def _reindex(self, options):
        count, run = runs.reindex_selection(
            collections=options["collection"],
            documents=options["document"],
            errors_only=options["errors_only"],
        )
        self.stdout.write(
            self.style.SUCCESS(f"{count} Dokument(e) eingereiht (Lauf #{run.pk}).")
            if count
            else "Keine Dokumente gefunden."
        )

    def _upload(self, options):
        user = get_user_model().objects.filter(username=options["account"]).first()
        if user is None or not user.is_active:
            raise CommandError(f"Konto „{options['account']}“ nicht gefunden oder gesperrt.")
        try:
            collection = runs.find_collection(user, options["collection"])
        except runs.RunError as exc:
            raise CommandError(str(exc)) from None
        denied = upload.check_upload_permission(user, collection)
        if denied is not None:
            raise CommandError(_error_text(denied))
        limit = upload.max_upload_bytes()
        failures = 0
        for name in options["files"]:
            path = Path(name)
            if not path.is_file():
                self.stderr.write(f"{name}: keine Datei.")
                failures += 1
                continue
            size = path.stat().st_size
            if size > limit or size == 0:
                self.stderr.write(f"{name}: {'leer' if not size else 'zu groß'}.")
                failures += 1
                continue
            with path.open("rb") as fh:
                document, error = upload.store_upload(user, collection, File(fh, name=path.name))
            if error is not None:
                self.stderr.write(f"{name}: {_error_text(error)}")
                failures += 1
                continue
            self.stdout.write(f"{name}: Dokument #{document.pk}, Lauf #{document.index_run.pk}")
        if failures:
            raise CommandError(f"{failures} Datei(en) nicht hochgeladen.")
