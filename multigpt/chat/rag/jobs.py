"""Job-Warteschlange in PostgreSQL für den Worker (M7-02, Agent ingest).

- Abholen mit ``SELECT … FOR UPDATE SKIP LOCKED``: Mehrere Worker bekommen nie
  denselben Job.
- Wiederholung mit exponentiellem Backoff bis ``settings.JOB_MAX_ATTEMPTS``.
- Lebenszeichen: Während der Arbeit wird ``locked_at`` regelmäßig erneuert.
  Jobs, deren Worker verschwunden ist (Absturz, kill -9), werden nach
  ``STALE_AFTER`` neu eingereiht.
- Sauberes Beenden: ``should_stop`` liefert True -> der laufende Job wird ohne
  Zählung des Versuchs zurückgestellt.
"""

import logging
import time
from collections.abc import Callable
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..models import Document, Job
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


def max_attempts() -> int:
    return max(1, int(getattr(settings, "JOB_MAX_ATTEMPTS", 5)))


def backoff(attempts: int) -> timedelta:
    """Wartezeit nach dem n-ten fehlgeschlagenen Versuch: 30 s, 2 min, 8 min, … ≤ 1 h."""
    return min(BACKOFF_BASE * (4 ** max(0, attempts - 1)), BACKOFF_MAX)


# --- Einreihen -----------------------------------------------------------------


def enqueue_index(document: Document) -> Job:
    """Indexierungsjob für ``document`` anlegen (oder einen wartenden auffrischen).

    Setzt das Dokument auf „wartet“. Läuft gerade ein Job für das Dokument,
    entsteht ein neuer, damit Änderungen (z. B. neues Embedding-Modell) nicht
    verloren gehen.
    """
    with transaction.atomic():
        job = (
            Job.objects.select_for_update()
            .filter(
                kind=Job.Kind.INDEX_DOCUMENT,
                status=Job.Status.PENDING,
                payload__document_id=document.pk,
            )
            .first()
        )
        if job is None:
            job = Job.objects.create(
                kind=Job.Kind.INDEX_DOCUMENT, payload={"document_id": document.pk}
            )
        else:
            job.attempts = 0
            job.run_after = timezone.now()
            job.last_error = ""
            job.save(update_fields=["attempts", "run_after", "last_error"])
        Document.objects.filter(pk=document.pk).update(
            status=Document.Status.PENDING, error_text=""
        )
    document.status, document.error_text = Document.Status.PENDING, ""
    return job


# --- Abholen -------------------------------------------------------------------


def claim_next() -> Job | None:
    """Nächsten fälligen Job sperren, auf „läuft“ setzen und zurückgeben."""
    now = timezone.now()
    with transaction.atomic():
        job = (
            Job.objects.select_for_update(skip_locked=True)
            .filter(status=Job.Status.PENDING, run_after__lte=now)
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
    with transaction.atomic():
        stale = Job.objects.select_for_update(skip_locked=True).filter(
            status=Job.Status.RUNNING, locked_at__lt=now - STALE_AFTER
        )
        for job in stale:
            _fail(job, "Die Verarbeitung wurde unterbrochen (Worker beendet).", True, now)
            count += 1
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
    job.save(update_fields=["status", "attempts", "run_after", "last_error", "locked_at"])


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
    job.save(update_fields=["status", "run_after", "last_error", "locked_at"])


def _finish(job: Job, note: str = "") -> None:
    job.status = Job.Status.DONE
    job.locked_at = None
    job.last_error = note
    job.save(update_fields=["status", "locked_at", "last_error"])


def _release(job: Job) -> None:
    """Abbruch auf Wunsch: Versuch nicht zählen, sofort wieder fällig."""
    job.status = Job.Status.PENDING
    job.attempts = max(0, job.attempts - 1)
    job.locked_at = None
    job.run_after = timezone.now()
    job.save(update_fields=["status", "attempts", "locked_at", "run_after"])


def _heartbeat(job: Job, stop: Callable[[], bool]) -> Callable[[], bool]:
    last = time.monotonic()

    def check() -> bool:
        nonlocal last
        if time.monotonic() - last >= HEARTBEAT_EVERY:
            Job.objects.filter(pk=job.pk, status=Job.Status.RUNNING).update(
                locked_at=timezone.now()
            )
            last = time.monotonic()
        return stop()

    return check


def _run_index_document(job: Job, should_stop: Callable[[], bool]) -> str:
    document = _document_for(job)
    if document is None:
        return "Dokument nicht mehr vorhanden."
    result = ingest.index_document(document, should_stop=should_stop)
    logger.info(
        "Dokument %s indexiert: %d Seiten (%d per OCR), %d Abschnitte, %.1f s",
        document.pk,
        result.pages,
        result.ocr_pages,
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

    Rückgabe: neuer Status des Jobs (``done``/``pending``/``failed``).
    """
    stop = _heartbeat(job, should_stop or (lambda: False))
    handler = HANDLERS.get(job.kind)
    logger.info("Job %s (%s) gestartet, Versuch %d", job.pk, job.kind, job.attempts)
    if handler is None:
        _fail(job, f"Unbekannte Jobart „{job.kind}“.", retryable=False)
        return job.status
    try:
        note = handler(job, stop)
    except extract.Interrupted:
        logger.info("Job %s unterbrochen, wird später fortgesetzt", job.pk)
        _release(job)
    except ingest.IngestError as exc:
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
    except Exception as exc:
        # Unerwartet: nur der Typ ins Log (Meldungen könnten Inhalte enthalten),
        # Stacktrace nur mit LOG_LEVEL=DEBUG; wiederholbar.
        logger.error("Job %s: unerwarteter Fehler %s", job.pk, type(exc).__name__)
        logger.debug("Stacktrace zu Job %s", job.pk, exc_info=True)
        _fail(job, GENERIC_ERROR, retryable=True)
    else:
        _finish(job, note)
        logger.info("Job %s erledigt", job.pk)
    return job.status


def work_once(should_stop: Callable[[], bool] | None = None) -> Job | None:
    """Einen fälligen Job abholen und ausführen; None, wenn keiner fällig ist."""
    job = claim_next()
    if job is not None:
        run_job(job, should_stop)
    return job
