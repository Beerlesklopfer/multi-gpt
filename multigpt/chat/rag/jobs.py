"""Job-Warteschlange in PostgreSQL für den Worker (M7-02, Agent ingest).

- Abholen mit ``SELECT … FOR UPDATE SKIP LOCKED``: Mehrere Worker bekommen nie
  denselben Job.
- Wiederholung mit exponentiellem Backoff bis ``settings.JOB_MAX_ATTEMPTS``.
- Lebenszeichen: Während der Arbeit wird ``locked_at`` regelmäßig erneuert.
  Jobs, deren Worker verschwunden ist (Absturz, kill -9), werden nach
  ``STALE_AFTER`` neu eingereiht.
- Sauberes Beenden: ``should_stop`` liefert True -> der laufende Job wird ohne
  Zählung des Versuchs zurückgestellt.
- Abbrechen (``cancel_jobs``, ``cancel_run``): Wartende und hängende Aufträge
  werden sofort entfernt, laufende mit ``cancel_requested`` markiert. Der
  Worker prüft die Markierung an denselben Prüfstellen wie ``should_stop``
  (zwischen PDF-Seiten/OCR, Abbildungen, Embedding-Paketen und Dateien eines
  Scans, beim Lebenszeichen) und entfernt den Auftrag dann; das Dokument zeigt „Fehler:
  Indexierung abgebrochen.“, Abschnitte werden nur in einer Transaktion am Ende
  geschrieben (nie halb). Eine laufende Anfrage an den Anbieter (z. B. OCR einer
  Seite in LM Studio, bis zu 600 s) wird nicht unterbrochen – der Abbruch
  greift danach.
- Läufe (``IndexRun``): Aufträge eines Scans bzw. einer Neuindexierung hängen
  am selben Lauf. Wo ein Auftrag endet (erledigt, endgültig fehlgeschlagen,
  abgebrochen), zählt ``job_ended`` mit und beendet den Lauf, sobald keiner
  seiner Aufträge mehr offen ist. Sperrreihenfolge immer Lauf vor Auftrag.
"""

import logging
import time
from collections.abc import Callable
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from ..models import Document, IndexRun, Job
from . import extract, ingest

logger = logging.getLogger(__name__)

BACKOFF_BASE = timedelta(seconds=30)
BACKOFF_MAX = timedelta(hours=1)
HEARTBEAT_EVERY = 30  # Sekunden
STALE_AFTER = timedelta(minutes=10)
# Anbieter nicht erreichbar (z. B. LM-Studio-PC aus, M7-09): ohne Obergrenze
# in diesem Abstand neu versuchen; der Versuch zählt nicht.
OFFLINE_RETRY = timedelta(minutes=5)
GENERIC_ERROR = "Interner Fehler bei der Verarbeitung."
CANCELLED_DOCUMENT = "Indexierung abgebrochen."
CANCELLED_SCAN = "Einlesen abgebrochen."
# Höchstens so oft (Sekunden) fragt der Worker an den Prüfstellen nach einem Abbruch.
CANCEL_CHECK_EVERY = 5
OPEN = (Job.Status.PENDING, Job.Status.RUNNING)


def max_attempts() -> int:
    return max(1, int(getattr(settings, "JOB_MAX_ATTEMPTS", 5)))


def backoff(attempts: int) -> timedelta:
    """Wartezeit nach dem n-ten fehlgeschlagenen Versuch: 30 s, 2 min, 8 min, … ≤ 1 h."""
    return min(BACKOFF_BASE * (4 ** max(0, attempts - 1)), BACKOFF_MAX)


# --- Einreihen -----------------------------------------------------------------


def enqueue_index(document: Document, run: IndexRun | None = None) -> Job:
    """Indexierungsjob für ``document`` anlegen (oder einen wartenden auffrischen).

    Setzt das Dokument auf „wartet“. Läuft gerade ein Job für das Dokument,
    entsteht ein neuer, damit Änderungen (z. B. neues Embedding-Modell) nicht
    verloren gehen. Mit ``run`` hängt der Job an diesem Lauf (ein wartender
    Job wechselt den Lauf).
    """
    moved_from = None
    with transaction.atomic():
        if run is not None:
            # Sperrreihenfolge Lauf vor Auftrag (wie cancel_run).
            IndexRun.objects.select_for_update().filter(pk=run.pk).first()
        job = (
            Job.objects.select_for_update(of=("self",))
            .filter(
                kind=Job.Kind.INDEX_DOCUMENT,
                status=Job.Status.PENDING,
                payload__document_id=document.pk,
            )
            .first()
        )
        added = run is not None
        if job is None:
            job = Job.objects.create(
                kind=Job.Kind.INDEX_DOCUMENT, payload={"document_id": document.pk}, run=run
            )
        else:
            job.attempts = 0
            job.run_after = timezone.now()
            job.last_error = ""
            fields = ["attempts", "run_after", "last_error"]
            added = run is not None and job.run_id != run.pk
            if added:
                moved_from = job.run_id
                job.run = run
                fields.append("run")
            job.save(update_fields=fields)
        if added:
            IndexRun.objects.filter(pk=run.pk).update(docs_queued=F("docs_queued") + 1)
        Document.objects.filter(pk=document.pk).update(
            status=Document.Status.PENDING, error_text=""
        )
    if moved_from is not None:
        IndexRun.objects.filter(pk=moved_from, docs_queued__gt=0).update(
            docs_queued=F("docs_queued") - 1
        )
        check_run(moved_from)
    document.status, document.error_text = Document.Status.PENDING, ""
    return job


# --- Abholen -------------------------------------------------------------------


def claim_next() -> Job | None:
    """Nächsten fälligen Job sperren, auf „läuft“ setzen und zurückgeben."""
    now = timezone.now()
    with transaction.atomic():
        job = (
            Job.objects.select_for_update(skip_locked=True, of=("self",))
            .filter(status=Job.Status.PENDING, run_after__lte=now)
            # Nachzügler eines Laufs, der gerade abgebrochen wird, nicht mehr starten.
            .exclude(run__status=IndexRun.Status.CANCELLING)
            .order_by("run_after", "id")
            .first()
        )
        if job is None:
            return None
        job.status = Job.Status.RUNNING
        job.attempts += 1
        job.locked_at = now
        job.save(update_fields=["status", "attempts", "locked_at"])
    return job


def requeue_stale(now=None) -> int:
    """Jobs ohne Lebenszeichen (Worker abgestürzt) wieder einreihen bzw. aufgeben."""
    now = now or timezone.now()
    count = 0
    ended = []
    with transaction.atomic():
        stale = Job.objects.select_for_update(skip_locked=True).filter(
            status=Job.Status.RUNNING, locked_at__lt=now - STALE_AFTER
        )
        for job in stale:
            if job.cancel_requested:
                _cancel(job, now)
                ended.append((job, "cancelled", ""))
            else:
                _fail(job, "Die Verarbeitung wurde unterbrochen (Worker beendet).", True, now)
                if job.status == Job.Status.FAILED:
                    ended.append((job, "failed", job.last_error))
            count += 1
    for job, outcome, error in ended:
        job_ended(job, outcome, error)
    if count:
        logger.warning("%d verwaiste Jobs neu eingereiht", count)
    return count


# --- Ausführen -----------------------------------------------------------------


def _document_for(job: Job) -> Document | None:
    doc_id = (job.payload or {}).get("document_id")
    if not isinstance(doc_id, int):
        return None
    return Document.objects.select_related("collection").filter(pk=doc_id).first()


def _set_document_error(job: Job, message: str, status: str) -> None:
    doc_id = (job.payload or {}).get("document_id")
    if isinstance(doc_id, int):
        Document.objects.filter(pk=doc_id).update(status=status, error_text=message[:2000])


def _wait_for_provider(job: Job, message: str, now=None) -> None:
    """Anbieter offline: später erneut versuchen, ohne den Versuch zu zählen."""
    now = now or timezone.now()
    job.status = Job.Status.PENDING
    job.attempts = max(0, job.attempts - 1)
    job.locked_at = None
    job.last_error = message[:2000]
    job.run_after = now + OFFLINE_RETRY
    local = timezone.localtime(job.run_after)
    _set_document_error(
        job,
        f"Wartet: {message} Neuer Versuch ab {local:%H:%M} Uhr.",
        Document.Status.PENDING,
    )
    _store(job, ["status", "attempts", "run_after", "last_error", "locked_at"])


def _store(job: Job, fields: list[str]) -> None:
    """Felder speichern; ist der Auftrag inzwischen entfernt (abgebrochen), nichts tun."""
    Job.objects.filter(pk=job.pk).update(**{f: getattr(job, f) for f in fields})


def _fail(job: Job, message: str, retryable: bool, now=None, *, unreachable=False) -> None:
    """Fehler verbuchen: neuer Versuch mit Backoff oder endgültig fehlgeschlagen.

    ``unreachable``: Anbieter nicht erreichbar -> warten statt aufgeben.
    """
    if unreachable:
        _wait_for_provider(job, message, now)
        return
    now = now or timezone.now()
    job.last_error = message[:2000]
    job.locked_at = None
    if retryable and job.attempts < max_attempts():
        job.status = Job.Status.PENDING
        job.run_after = now + backoff(job.attempts)
        local = timezone.localtime(job.run_after)
        _set_document_error(
            job,
            f"Versuch {job.attempts} fehlgeschlagen: {message} Neuer Versuch ab {local:%H:%M} Uhr.",
            Document.Status.PENDING,
        )
    else:
        job.status = Job.Status.FAILED
        _set_document_error(job, message, Document.Status.ERROR)
    _store(job, ["status", "run_after", "last_error", "locked_at"])


def _finish(job: Job, note: str = "") -> None:
    job.status = Job.Status.DONE
    job.locked_at = None
    job.last_error = note
    _store(job, ["status", "locked_at", "last_error"])


def _release(job: Job) -> None:
    """Unterbrechung (Worker wird beendet): Versuch nicht zählen, sofort wieder fällig."""
    job.status = Job.Status.PENDING
    job.attempts = max(0, job.attempts - 1)
    job.locked_at = None
    job.run_after = timezone.now()
    _store(job, ["status", "attempts", "locked_at", "run_after"])


# --- Abbrechen -----------------------------------------------------------------


def _open_document_jobs(doc_id: int):
    return Job.objects.filter(
        kind=Job.Kind.INDEX_DOCUMENT, status__in=OPEN, payload__document_id=doc_id
    )


def _cancel(job: Job, now=None) -> None:
    """Auftrag entfernen; Dokument bzw. Verzeichnisquelle zeigen den Abbruch.

    Den Lauf berührt das nicht (Zähler über ``job_ended`` bzw. ``cancel_run``).
    """
    now = now or timezone.now()
    was_running = job.status == Job.Status.RUNNING
    Job.objects.filter(pk=job.pk).delete()
    payload = job.payload or {}
    if job.kind == Job.Kind.INDEX_DOCUMENT:
        doc_id = payload.get("document_id")
        if isinstance(doc_id, int) and not _open_document_jobs(doc_id).exists():
            Document.objects.filter(pk=doc_id, status=Document.Status.PENDING).update(
                status=Document.Status.ERROR, error_text=CANCELLED_DOCUMENT
            )
    elif job.kind == Job.Kind.SCAN_DIRECTORY:
        from multigpt.rag.models import DirectorySource

        source_id = payload.get("source_id")
        if isinstance(source_id, int):
            fields = {"last_error": CANCELLED_SCAN}
            if was_running:
                fields["last_scan_finished"] = now
            DirectorySource.objects.filter(pk=source_id).update(**fields)


def is_fresh(job: Job, now=None) -> bool:
    """Läuft und hat ein frisches Lebenszeichen (Worker arbeitet daran)."""
    now = now or timezone.now()
    return (
        job.status == Job.Status.RUNNING
        and job.locked_at is not None
        and job.locked_at >= now - STALE_AFTER
    )


def cancel_jobs(job_ids, *, remove_finished: bool = False) -> tuple[int, int]:
    """Aufträge abbrechen: wartende und hängende sofort entfernen, laufende markieren.

    ``remove_finished``: erledigte/fehlgeschlagene Aufträge ebenfalls löschen
    (Aufräumen im Admin). Rückgabe ``(entfernt, markiert)``.
    """
    now = timezone.now()
    removed = marked = 0
    ended = []
    statuses = None if remove_finished else OPEN
    with transaction.atomic():
        qs = Job.objects.select_for_update(of=("self",)).filter(pk__in=list(job_ids))
        if statuses is not None:
            qs = qs.filter(status__in=statuses)
        for job in qs.order_by("pk"):
            if is_fresh(job, now):
                if not job.cancel_requested:
                    Job.objects.filter(pk=job.pk).update(cancel_requested=True)
                    logger.info("Abbruch für Job %s angefordert", job.pk)
                marked += 1
            elif job.status in OPEN:
                _cancel(job, now)
                ended.append(job)
                removed += 1
                logger.info("Job %s abgebrochen (%s)", job.pk, job.status)
            else:
                job.delete()
                removed += 1
    for job in ended:
        job_ended(job, "cancelled")
    return removed, marked


def cancel_document_jobs(doc_ids) -> tuple[int, int]:
    """Offene Indexierungsaufträge dieser Dokumente abbrechen (z. B. beim Löschen)."""
    ids = list(
        Job.objects.filter(
            kind=Job.Kind.INDEX_DOCUMENT, status__in=OPEN, payload__document_id__in=list(doc_ids)
        ).values_list("pk", flat=True)
    )
    return cancel_jobs(ids) if ids else (0, 0)


def cancel_run(run_id: int) -> tuple[int, int] | None:
    """Lauf abbrechen: wartende Aufträge entfernen, laufende markieren.

    Bereits indexierte Dokumente bleiben. Der Lauf steht danach auf „wird
    abgebrochen“ bzw. – wenn nichts mehr läuft – „abgebrochen“. Rückgabe
    ``(entfernt, markiert)`` oder None, wenn der Lauf nicht (mehr) offen ist.
    """
    now = timezone.now()
    removed = marked = 0
    with transaction.atomic():
        run = IndexRun.objects.select_for_update().filter(pk=run_id).first()
        if run is None or not run.is_open:
            return None
        run.status = IndexRun.Status.CANCELLING
        for job in run.jobs.select_for_update(of=("self",)).filter(status__in=OPEN).order_by("pk"):
            if is_fresh(job, now):
                if not job.cancel_requested:
                    Job.objects.filter(pk=job.pk).update(cancel_requested=True)
                marked += 1
            else:
                _cancel(job, now)
                if job.kind == Job.Kind.INDEX_DOCUMENT:
                    run.docs_cancelled += 1
                removed += 1
        run.save(update_fields=["status", "docs_cancelled"])
        _complete_if_done(run, now)
    logger.info("Lauf %s: Abbruch, %d Aufträge entfernt, %d markiert", run_id, removed, marked)
    return removed, marked


# --- Läufe ---------------------------------------------------------------------

_COUNTERS = {"done": "docs_done", "failed": "docs_failed", "cancelled": "docs_cancelled"}


def _complete_if_done(run: IndexRun, now=None) -> None:
    """Gesperrten Lauf beenden, wenn keiner seiner Aufträge mehr offen ist."""
    if not run.is_open:
        return
    if run.status == IndexRun.Status.CANCELLING:
        # Nachzügler (während des Abbruchs noch eingereiht) entfernen.
        for job in run.jobs.select_for_update(of=("self",), skip_locked=True).filter(
            status=Job.Status.PENDING
        ):
            _cancel(job, now)
            run.docs_cancelled += 1
    if run.jobs.filter(status__in=OPEN).exists():
        run.save(update_fields=["docs_cancelled"])
        return
    if run.status == IndexRun.Status.CANCELLING:
        run.status = IndexRun.Status.CANCELLED
    elif run.error_text:
        run.status = IndexRun.Status.FAILED
    elif run.docs_cancelled and not (run.docs_done or run.docs_failed):
        run.status = IndexRun.Status.CANCELLED  # z. B. Upload einzeln abgebrochen
    else:
        run.status = IndexRun.Status.FINISHED
    run.finished = now or timezone.now()
    run.save(update_fields=["status", "finished", "docs_cancelled"])
    logger.info(
        "Lauf %s %s: %d fertig, %d Fehler, %d abgebrochen",
        run.pk,
        run.status,
        run.docs_done,
        run.docs_failed,
        run.docs_cancelled,
    )


def check_run(run_id: int | None) -> None:
    """Lauf beenden, falls keiner seiner Aufträge mehr offen ist."""
    if run_id is None:
        return
    with transaction.atomic():
        run = IndexRun.objects.select_for_update().filter(pk=run_id).first()
        if run is not None:
            _complete_if_done(run)


def job_ended(job: Job, outcome: str, error: str = "") -> None:
    """Ein Auftrag ist zu Ende (``done``/``failed``/``cancelled``): Lauf mitzählen.

    Wird außerhalb der Transaktion des Auftrags aufgerufen (Sperre Lauf vor Auftrag).
    """
    if job.run_id is None:
        return
    with transaction.atomic():
        run = IndexRun.objects.select_for_update().filter(pk=job.run_id).first()
        if run is None or not run.is_open:
            return  # beendet/abgebrochen: Zähler bleiben stehen
        fields = []
        if job.kind == Job.Kind.INDEX_DOCUMENT and outcome in _COUNTERS:
            name = _COUNTERS[outcome]
            setattr(run, name, getattr(run, name) + 1)
            fields.append(name)
        if job.kind == Job.Kind.SCAN_DIRECTORY and outcome == "failed" and not run.error_text:
            run.error_text = error[:2000] or GENERIC_ERROR
            fields.append("error_text")
        if fields:
            run.save(update_fields=fields)
        _complete_if_done(run)


def _cancel_requested(job: Job) -> bool:
    """Abbruch angefordert oder Auftrag schon entfernt (z. B. hängend gelöscht)."""
    return not Job.objects.filter(pk=job.pk, cancel_requested=False).exists()


def _finish_cancelled(job: Job) -> None:
    with transaction.atomic():
        _cancel(job)
    logger.info("Job %s abgebrochen", job.pk)
    job_ended(job, "cancelled")


def _heartbeat(job: Job, stop: Callable[[], bool], state: dict) -> Callable[[], bool]:
    """Prüfstelle: Lebenszeichen alle ``HEARTBEAT_EVERY`` s, Abbruch-Markierung
    höchstens alle ``CANCEL_CHECK_EVERY`` s (und bei jedem Lebenszeichen)."""
    last = time.monotonic()
    last_check: float | None = None

    def check() -> bool:
        nonlocal last, last_check
        now = time.monotonic()
        if now - last >= HEARTBEAT_EVERY:
            Job.objects.filter(pk=job.pk, status=Job.Status.RUNNING).update(
                locked_at=timezone.now()
            )
            last = now
            last_check = None
        due = last_check is None or now - last_check >= CANCEL_CHECK_EVERY
        if due and not state["cancelled"]:
            last_check = now
            state["cancelled"] = _cancel_requested(job)
        return state["cancelled"] or stop()

    return check


def _run_index_document(job: Job, should_stop: Callable[[], bool]) -> str:
    document = _document_for(job)
    if document is None:
        return "Dokument nicht mehr vorhanden."
    result = ingest.index_document(document, should_stop=should_stop)
    logger.info(
        "Dokument %s indexiert: %d Seiten (%d per OCR), %d Abbildungen, %d Abschnitte, %.1f s",
        document.pk,
        result.pages,
        result.ocr_pages,
        result.figures,
        result.chunks,
        result.seconds,
    )
    return ""


def _run_scan_directory(job: Job, should_stop: Callable[[], bool]) -> str:
    # Verzeichnisquellen (Agent crawler); späte Einbindung gegen Importzyklen.
    from multigpt.rag import crawl

    return crawl.run_scan_job(job, should_stop)


HANDLERS: dict[str, Callable[[Job, Callable[[], bool]], str]] = {
    Job.Kind.INDEX_DOCUMENT: _run_index_document,
    Job.Kind.SCAN_DIRECTORY: _run_scan_directory,
}


def run_job(job: Job, should_stop: Callable[[], bool] | None = None) -> str:
    """Einen abgeholten Job ausführen und das Ergebnis verbuchen.

    Rückgabe: neuer Status des Jobs (``done``/``pending``/``failed``) bzw.
    ``cancelled``, wenn der Auftrag abgebrochen und entfernt wurde.
    """
    state = {"cancelled": False}
    stop = _heartbeat(job, should_stop or (lambda: False), state)
    handler = HANDLERS.get(job.kind)
    logger.info("Job %s (%s) gestartet, Versuch %d", job.pk, job.kind, job.attempts)
    if handler is None:
        _fail(job, f"Unbekannte Jobart „{job.kind}“.", retryable=False)
        job_ended(job, "failed", job.last_error)
        return job.status
    try:
        note = handler(job, stop)
    except extract.Interrupted:
        if state["cancelled"]:
            _finish_cancelled(job)
            return "cancelled"
        logger.info("Job %s unterbrochen, wird später fortgesetzt", job.pk)
        _release(job)
    except ingest.IngestError as exc:
        if _cancel_requested(job):
            _finish_cancelled(job)
            return "cancelled"
        unreachable = bool(getattr(exc, "unreachable", False))
        logger.warning(
            "Job %s fehlgeschlagen (Versuch %d, %s)",
            job.pk,
            job.attempts,
            "Anbieter nicht erreichbar, wartet"
            if unreachable
            else "wird wiederholt"
            if exc.retryable
            else "endgültig",
        )
        _fail(job, exc.message, exc.retryable, unreachable=unreachable)
        if job.status == Job.Status.FAILED:
            job_ended(job, "failed", exc.message)
    except Exception as exc:
        # Unerwartet: nur der Typ ins Log (Meldungen könnten Inhalte enthalten),
        # Stacktrace nur mit LOG_LEVEL=DEBUG; wiederholbar.
        logger.error("Job %s: unerwarteter Fehler %s", job.pk, type(exc).__name__)
        logger.debug("Stacktrace zu Job %s", job.pk, exc_info=True)
        if _cancel_requested(job):
            _finish_cancelled(job)
            return "cancelled"
        _fail(job, GENERIC_ERROR, retryable=True)
        if job.status == Job.Status.FAILED:
            job_ended(job, "failed", GENERIC_ERROR)
    else:
        # Auch bei einem Abbruch nach dem letzten Schritt: Die Arbeit ist getan.
        _finish(job, note)
        logger.info("Job %s erledigt", job.pk)
        job_ended(job, "done")
    return job.status


def work_once(should_stop: Callable[[], bool] | None = None) -> Job | None:
    """Einen fälligen Job abholen und ausführen; None, wenn keiner fällig ist."""
    job = claim_next()
    if job is not None:
        run_job(job, should_stop)
    return job
