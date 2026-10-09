"""Zahlen und Aktionen für die RAG-Verwaltung im Admin.

Nur Metadaten: Zählungen, Status, Größen und IDs. Texte von Abschnitten oder
Dokumentinhalte werden hier nie gelesen (Plan 8f, Privatsphäre).
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import Count, Max, Q
from django.utils import timezone

from multigpt.chat.models import Chunk, Collection, Document, Job, RagSettings
from multigpt.chat.rag import extract, jobs
from multigpt.chat.rag import ocr as rag_ocr

from . import paths as source_paths
from .models import DirectorySource

# Ist ein Auftrag so lange überfällig und arbeitet kein Worker sichtbar, läuft
# der Worker vermutlich nicht (er fragt alle paar Sekunden nach Arbeit).
WORKER_SILENT_AFTER = timedelta(minutes=2)

CANCELLED_TEXT = "Indexierung in der Verwaltung abgebrochen."


def document_id_of(job: Job) -> int | None:
    doc_id = (job.payload or {}).get("document_id")
    return doc_id if isinstance(doc_id, int) else None


def file_size(document: Document) -> int | None:
    if document.source_id is not None:
        # Verzeichnisquelle: Größe beim letzten Einlesen (Datei liegt nicht in MEDIA_ROOT).
        return document.source_size
    try:
        return document.file.size if document.file else None
    except (OSError, ValueError):
        return None


# --- Übersicht ---------------------------------------------------------------


@dataclass
class Overview:
    settings: RagSettings
    collections: int
    documents: int
    documents_by_status: dict[str, int]
    chunks: int
    storage_bytes: int
    missing_files: int
    jobs_due: int
    jobs_waiting: int
    jobs_running: int
    jobs_failed: int
    jobs_stale: int
    last_heartbeat: datetime | None
    last_done_job: Job | None
    oldest_due: datetime | None
    worker_warning: bool = False
    document_states: list[tuple[str, str, int]] = field(default_factory=list)
    sources: list = field(default_factory=list)
    sources_enabled: bool = False
    source_documents: int = 0


def storage_usage() -> tuple[int, int]:
    """Summe der Dateigrößen aller hochgeladenen Dokumente und Zahl fehlender
    Dateien (Dokumente aus Verzeichnisquellen belegen keinen Speicher in MEDIA_ROOT)."""
    total = missing = 0
    for document in Document.objects.filter(source__isnull=True).only("pk", "file").iterator():
        size = file_size(document)
        if size is None:
            missing += 1
        else:
            total += size
    return total, missing


def overview(now=None) -> Overview:
    now = now or timezone.now()
    by_status = dict(
        Document.objects.order_by()
        .values_list("status")
        .annotate(n=Count("pk"))
        .values_list("status", "n")
    )
    job_counts = Job.objects.aggregate(
        due=Count("pk", filter=Q(status=Job.Status.PENDING, run_after__lte=now)),
        waiting=Count("pk", filter=Q(status=Job.Status.PENDING, run_after__gt=now)),
        running=Count("pk", filter=Q(status=Job.Status.RUNNING)),
        failed=Count("pk", filter=Q(status=Job.Status.FAILED)),
        stale=Count(
            "pk",
            filter=Q(status=Job.Status.RUNNING, locked_at__lt=now - jobs.STALE_AFTER),
        ),
        heartbeat=Max("locked_at", filter=Q(status=Job.Status.RUNNING)),
    )
    oldest_due = (
        Job.objects.filter(status=Job.Status.PENDING, run_after__lte=now)
        .order_by("run_after")
        .values_list("run_after", flat=True)
        .first()
    )
    storage, missing = storage_usage()
    heartbeat = job_counts["heartbeat"]
    fresh_worker = heartbeat is not None and heartbeat >= now - jobs.STALE_AFTER
    warning = bool(job_counts["stale"]) or (
        oldest_due is not None and oldest_due <= now - WORKER_SILENT_AFTER and not fresh_worker
    )
    result = Overview(
        settings=RagSettings.load(),
        collections=Collection.objects.count(),
        documents=sum(by_status.values()),
        documents_by_status=by_status,
        chunks=Chunk.objects.count(),
        storage_bytes=storage,
        missing_files=missing,
        jobs_due=job_counts["due"],
        jobs_waiting=job_counts["waiting"],
        jobs_running=job_counts["running"],
        jobs_failed=job_counts["failed"],
        jobs_stale=job_counts["stale"],
        last_heartbeat=heartbeat,
        last_done_job=Job.objects.filter(status=Job.Status.DONE).order_by("-pk").first(),
        oldest_due=oldest_due,
        worker_warning=warning,
    )
    result.sources = source_overview()
    result.sources_enabled = source_paths.enabled()
    result.source_documents = Document.objects.filter(source__isnull=False).count()
    result.document_states = [
        (value, label, by_status.get(value, 0)) for value, label in Document.Status.choices
    ]
    return result


def source_overview() -> list[DirectorySource]:
    """Verzeichnisquellen mit Dokumentzahl und offenem Scan (für die Übersicht)."""
    open_ids = {
        (j.payload or {}).get("source_id")
        for j in Job.objects.filter(
            kind=Job.Kind.SCAN_DIRECTORY, status__in=[Job.Status.PENDING, Job.Status.RUNNING]
        ).only("payload")
    }
    items = list(
        DirectorySource.objects.select_related("collection__owner")
        .annotate(document_total=Count("documents"))
        .order_by("collection__name", "pk")
    )
    for item in items:
        item.scan_open = item.pk in open_ids
    return items


@dataclass
class OcrOverview:
    backend: str
    backend_label: str
    olmocr: bool
    fallback: bool
    tesseract_available: bool
    missing_programs: list[str]


def ocr_overview(cfg: RagSettings) -> OcrOverview:
    """OCR-Verfahren und ob Tesseract (als Verfahren oder Ersatz) installiert ist."""
    return OcrOverview(
        backend=cfg.ocr_backend,
        backend_label=cfg.get_ocr_backend_display(),
        olmocr=cfg.ocr_backend == RagSettings.OcrBackend.OLMOCR,
        fallback=cfg.ocr_fallback_tesseract,
        tesseract_available=extract.ocr_available(),
        missing_programs=rag_ocr.missing_programs(),
    )


# --- Aktionen ----------------------------------------------------------------


def reindex_documents(documents) -> int:
    """Dokumente zur Indexierung einreihen (wie ``make reindex``).

    Fehlgeschlagene Aufträge dieser Dokumente sind damit überholt und werden
    entfernt, damit die Warteschlange nur noch Offenes zeigt.
    """
    ids = []
    for document in documents:
        jobs.enqueue_index(document)
        ids.append(document.pk)
    if ids:
        Job.objects.filter(
            kind=Job.Kind.INDEX_DOCUMENT,
            status=Job.Status.FAILED,
            payload__document_id__in=ids,
        ).delete()
    return len(ids)


def reindex_all() -> int:
    return reindex_documents(list(Document.objects.order_by("pk")))


def retry_errors(documents) -> int:
    """Nur Dokumente mit Status „Fehler“ erneut einreihen."""
    return reindex_documents(d for d in documents if d.status == Document.Status.ERROR)


def retry_job(job: Job) -> bool:
    """Fehlgeschlagenen oder wartenden Auftrag sofort neu starten (Versuche auf 0).

    Laufende und erledigte Aufträge bleiben unverändert (Rückgabe False).
    """
    if job.status not in (Job.Status.FAILED, Job.Status.PENDING):
        return False
    doc_id = document_id_of(job) if job.kind == Job.Kind.INDEX_DOCUMENT else None
    if doc_id is not None:
        document = Document.objects.filter(pk=doc_id).first()
        if document is not None:
            reindex_documents([document])
            if job.status == Job.Status.FAILED:
                # Der Auftrag ist durch den neuen ersetzt (oder schon gelöscht).
                Job.objects.filter(pk=job.pk, status=Job.Status.FAILED).delete()
            return True
    with transaction.atomic():
        updated = Job.objects.filter(
            pk=job.pk, status__in=[Job.Status.FAILED, Job.Status.PENDING]
        ).update(
            status=Job.Status.PENDING,
            attempts=0,
            run_after=timezone.now(),
            locked_at=None,
            last_error="",
        )
    return bool(updated)


def retry_failed() -> int:
    count = 0
    for job in Job.objects.filter(status=Job.Status.FAILED).order_by("pk"):
        count += retry_job(job)
    # Dokumente mit Fehler ohne offenen Auftrag (z. B. Auftrag gelöscht).
    open_ids = {
        document_id_of(j)
        for j in Job.objects.filter(
            kind=Job.Kind.INDEX_DOCUMENT,
            status__in=[Job.Status.PENDING, Job.Status.RUNNING],
        )
    }
    orphans = [
        d
        for d in Document.objects.filter(status=Document.Status.ERROR).order_by("pk")
        if d.pk not in open_ids
    ]
    return count + reindex_documents(orphans)


def reset_stale() -> int:
    """Hängende Aufträge (Worker ohne Lebenszeichen) neu einreihen."""
    return jobs.requeue_stale()


def is_stale(job: Job, now=None) -> bool:
    now = now or timezone.now()
    return (
        job.status == Job.Status.RUNNING
        and job.locked_at is not None
        and job.locked_at < now - jobs.STALE_AFTER
    )


def cancel_jobs(queryset) -> tuple[int, int]:
    """Aufträge löschen; laufende (mit frischem Lebenszeichen) bleiben.

    Hat ein Dokument danach keinen offenen Auftrag mehr und wartete es noch,
    bekommt es den Status „Fehler“ mit Hinweis, statt ewig zu warten.
    Rückgabe: (gelöscht, übersprungen).
    """
    now = timezone.now()
    deleted = skipped = 0
    doc_ids = set()
    with transaction.atomic():
        for job in queryset.select_for_update():
            if job.status == Job.Status.RUNNING and not is_stale(job, now):
                skipped += 1
                continue
            if job.kind == Job.Kind.INDEX_DOCUMENT and document_id_of(job) is not None:
                doc_ids.add(document_id_of(job))
            job.delete()
            deleted += 1
        if doc_ids:
            still_open = {
                document_id_of(j)
                for j in Job.objects.filter(
                    kind=Job.Kind.INDEX_DOCUMENT,
                    status__in=[Job.Status.PENDING, Job.Status.RUNNING],
                )
            }
            Document.objects.filter(
                pk__in=doc_ids - still_open, status=Document.Status.PENDING
            ).update(status=Document.Status.ERROR, error_text=CANCELLED_TEXT)
    return deleted, skipped
