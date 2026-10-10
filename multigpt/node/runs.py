"""Läufe (Indexierung, Verzeichnisquellen) steuern und beobachten – gemeinsam
für den MCP-Server, ``mgpt-ctl index`` und die Chat-Werkzeuge (M15).

Rechte wie im Admin bzw. bei den Sammlungen:

- **Sehen** (``run_status``, ``list_runs``): Verwalter alle Läufe; sonst Läufe
  von Sammlungen, die das Konto schreiben darf, und selbst gestartete.
- **Neu indexieren:** Schreibrecht auf die Sammlung (``can(WRITE)``).
- **Abbrechen:** Verwalter jeden offenen Lauf; sonst nur Läufe „Hochladen“ bzw.
  „neu indexieren“ einer schreibbaren Sammlung.
- **Verzeichnisquelle einlesen:** nur Verwalter (wie „Jetzt einlesen“ im Admin).

Fremde und unsichtbare Läufe, Sammlungen und Quellen ergeben dieselbe Meldung
„nicht gefunden“. Logs nur mit IDs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db.models import Count, Max, Q
from django.utils import timezone

from multigpt.accounts.permissions import Action, can
from multigpt.chat.api_collections import readable_collections, writable_collection_ids
from multigpt.chat.models import Collection, Document, IndexRun, Job
from multigpt.chat.rag import jobs
from multigpt.rag import crawl, services
from multigpt.rag import paths as source_paths
from multigpt.rag.models import DirectorySource

logger = logging.getLogger(__name__)

MSG_RUN_NOT_FOUND = "Lauf nicht gefunden."
MSG_COLLECTION_NOT_FOUND = "Sammlung nicht gefunden."
MSG_DOCUMENT_NOT_FOUND = "Dokument nicht gefunden."
MSG_SOURCE_NOT_FOUND = "Verzeichnisquelle nicht gefunden."
MSG_READ_ONLY = "Diese Sammlung darf nur gelesen werden."
MSG_ADMIN_ONLY = "Verzeichnisquellen einlesen dürfen nur Verwalter."
MSG_SOURCES_OFF = "Verzeichnisquellen sind nicht eingerichtet (RAG_SOURCE_ROOTS)."
MSG_CANNOT_CANCEL = "Diesen Lauf darf nur ein Verwalter abbrechen."
MSG_NOT_OPEN = "Der Lauf ist schon beendet."

# Läufe, die Nicht-Verwalter (mit Schreibrecht auf die Sammlung) abbrechen dürfen.
_USER_KINDS = (
    IndexRun.Kind.UPLOAD,
    IndexRun.Kind.REINDEX_COLLECTION,
    IndexRun.Kind.REINDEX_DOCUMENTS,
)


class RunError(Exception):
    """Ablehnung mit deutscher Meldung (für Modell, CLI und MCP-Client)."""


def is_admin(user) -> bool:
    return can(user, Action.ADMIN)


# --- Auflösen ------------------------------------------------------------------------


def _as_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def find_collection(user, ref, *, allowed_ids=None) -> Collection:
    """Lesbare Sammlung per ID oder Name (eigene vor geteilten). ``allowed_ids``:
    zusätzliche Einschränkung (API-Key); sonst ``RunError``."""
    qs = readable_collections(user)
    if allowed_ids is not None:
        qs = qs.filter(pk__in=allowed_ids)
    pk = _as_int(ref)
    if pk is not None:
        collection = qs.filter(pk=pk).first()
    elif isinstance(ref, str) and ref.strip():
        matches = qs.filter(name__iexact=ref.strip()).order_by("pk")
        collection = matches.filter(owner=user).first() or matches.first()
    else:
        collection = None
    if collection is None:
        raise RunError(MSG_COLLECTION_NOT_FOUND)
    return collection


def find_document(user, ref, *, allowed_ids=None) -> Document:
    pk = _as_int(ref)
    qs = Document.objects.select_related("collection").filter(
        collection__in=readable_collections(user)
    )
    if allowed_ids is not None:
        qs = qs.filter(collection_id__in=allowed_ids)
    document = qs.filter(pk=pk).first() if pk is not None else None
    if document is None:
        raise RunError(MSG_DOCUMENT_NOT_FOUND)
    return document


def find_source(user, ref, *, allowed_ids=None) -> DirectorySource:
    """Verzeichnisquelle per ID (nur Verwalter; sonst „nur Verwalter“)."""
    if not is_admin(user):
        raise RunError(MSG_ADMIN_ONLY)
    pk = _as_int(ref)
    qs = DirectorySource.objects.select_related("collection")
    if allowed_ids is not None:
        qs = qs.filter(pk__in=allowed_ids)
    source = qs.filter(pk=pk).first() if pk is not None else None
    if source is None:
        raise RunError(MSG_SOURCE_NOT_FOUND)
    return source


# --- Sehen ---------------------------------------------------------------------------


def visible_runs(user):
    qs = IndexRun.objects.select_related("collection", "source")
    if is_admin(user):
        return qs
    writable = writable_collection_ids(user)
    return qs.filter(Q(collection_id__in=writable) | Q(started_by=user))


def get_run(user, run_id) -> IndexRun:
    pk = _as_int(run_id)
    run = visible_runs(user).filter(pk=pk).first() if pk is not None else None
    if run is None:
        raise RunError(MSG_RUN_NOT_FOUND)
    return run


def serialize_run(run: IndexRun) -> dict:
    return {
        "id": run.pk,
        "kind": run.kind,
        "kind_label": run.get_kind_display(),
        "status": run.status,
        "status_label": run.get_status_display(),
        "open": run.is_open,
        "collection_id": run.collection_id,
        "collection": run.collection.name if run.collection_id else None,
        "source_id": run.source_id,
        "files_found": run.files_found,
        "files_checked": run.files_checked,
        "files_new": run.files_new,
        "files_changed": run.files_changed,
        "files_deleted": run.files_deleted,
        "files_skipped": run.files_skipped,
        "docs_queued": run.docs_queued,
        "docs_done": run.docs_done,
        "docs_failed": run.docs_failed,
        "docs_cancelled": run.docs_cancelled,
        "error": run.error_text,
        "started": run.started.isoformat(),
        "finished": run.finished.isoformat() if run.finished else None,
        "duration_seconds": int(run.duration.total_seconds()),
        "progress": run.progress_text(),
    }


def list_runs(user, *, open_only: bool = False, limit: int = 20) -> list[IndexRun]:
    qs = visible_runs(user)
    if open_only:
        qs = qs.filter(status__in=IndexRun.OPEN)
    return list(qs.order_by("-pk")[: max(1, min(int(limit), 200))])


@dataclass(frozen=True)
class QueueState:
    """Warteschlange des Workers (nur Zahlen), für ``mgpt-ctl index status``."""

    due: int
    waiting: int
    running: int
    failed: int
    cancelling: int
    stale: int
    last_heartbeat: object
    worker_warning: bool


def queue_state(now=None) -> QueueState:
    now = now or timezone.now()
    counts = Job.objects.aggregate(
        due=Count("pk", filter=Q(status=Job.Status.PENDING, run_after__lte=now)),
        waiting=Count("pk", filter=Q(status=Job.Status.PENDING, run_after__gt=now)),
        running=Count("pk", filter=Q(status=Job.Status.RUNNING)),
        failed=Count("pk", filter=Q(status=Job.Status.FAILED)),
        cancelling=Count("pk", filter=Q(status=Job.Status.RUNNING, cancel_requested=True)),
        stale=Count(
            "pk", filter=Q(status=Job.Status.RUNNING, locked_at__lt=now - jobs.STALE_AFTER)
        ),
        heartbeat=Max("locked_at", filter=Q(status=Job.Status.RUNNING)),
    )
    oldest_due = (
        Job.objects.filter(status=Job.Status.PENDING, run_after__lte=now)
        .order_by("run_after")
        .values_list("run_after", flat=True)
        .first()
    )
    heartbeat = counts["heartbeat"]
    fresh = heartbeat is not None and heartbeat >= now - jobs.STALE_AFTER
    warning = bool(counts["stale"]) or (
        oldest_due is not None and oldest_due <= now - services.WORKER_SILENT_AFTER and not fresh
    )
    return QueueState(
        due=counts["due"],
        waiting=counts["waiting"],
        running=counts["running"],
        failed=counts["failed"],
        cancelling=counts["cancelling"],
        stale=counts["stale"],
        last_heartbeat=heartbeat,
        worker_warning=warning,
    )


# --- Steuern -------------------------------------------------------------------------


def _require_write(user, collection: Collection) -> None:
    if not can(user, Action.WRITE, collection):
        raise RunError(MSG_READ_ONLY)


def reindex_collection(user, collection: Collection) -> IndexRun:
    _require_write(user, collection)
    run = IndexRun.objects.create(
        kind=IndexRun.Kind.REINDEX_COLLECTION, collection=collection, started_by=user
    )
    count = services.reindex_documents(list(collection.documents.order_by("pk")), run)
    logger.info("Lauf %s: Sammlung %s neu indexieren (%d Dokumente)", run.pk, collection.pk, count)
    run.refresh_from_db()
    return run


def reindex_documents(user, documents: list[Document]) -> IndexRun:
    if not documents:
        raise RunError(MSG_DOCUMENT_NOT_FOUND)
    collections = {d.collection_id: d.collection for d in documents}
    for collection in collections.values():
        _require_write(user, collection)
    run = IndexRun.objects.create(
        kind=IndexRun.Kind.REINDEX_DOCUMENTS,
        collection=next(iter(collections.values())) if len(collections) == 1 else None,
        started_by=user,
    )
    services.reindex_documents(documents, run)
    logger.info("Lauf %s: %d Dokument(e) neu indexieren", run.pk, len(documents))
    run.refresh_from_db()
    return run


def start_scan(user, source: DirectorySource) -> tuple[IndexRun, bool]:
    """Verzeichnisquelle einlesen. Rückgabe ``(Lauf, neu)``; läuft schon ein Lauf
    der Quelle, kommt dieser mit ``neu=False``."""
    if not is_admin(user):
        raise RunError(MSG_ADMIN_ONLY)
    if not source_paths.enabled():
        raise RunError(MSG_SOURCES_OFF)
    job = crawl.enqueue_scan(source, user)
    if job is not None:
        logger.info("Lauf %s: Verzeichnisquelle %s einlesen", job.run_id, source.pk)
        return job.run, True
    run = crawl.open_run(source)
    if run is None:  # nur Scan-Job ohne Lauf (Altbestand)
        raise RunError("Für diese Quelle läuft schon ein Einlesevorgang.")
    return run, False


def can_cancel(user, run: IndexRun) -> bool:
    if is_admin(user):
        return True
    if run.kind not in _USER_KINDS or run.collection_id is None:
        return False
    return run.collection_id in writable_collection_ids(user)


def cancel(user, run: IndexRun) -> dict:
    """Lauf abbrechen; Rückgabe ``{"removed": n, "marked": m, "status": …}``."""
    if not can_cancel(user, run):
        raise RunError(MSG_CANNOT_CANCEL)
    outcome = jobs.cancel_run(run.pk)
    if outcome is None:
        raise RunError(MSG_NOT_OPEN)
    run.refresh_from_db()
    logger.info("Lauf %s abgebrochen (Konto %s)", run.pk, user.pk)
    return {"removed": outcome[0], "marked": outcome[1], "status": run.status}


def list_sources(user, *, allowed_ids=None) -> list[DirectorySource]:
    if not is_admin(user):
        return []
    qs = DirectorySource.objects.select_related("collection").order_by("pk")
    if allowed_ids is not None:
        qs = qs.filter(pk__in=allowed_ids)
    return list(qs)


def serialize_source(source: DirectorySource) -> dict:
    run = crawl.open_run(source)
    return {
        "id": source.pk,
        "collection_id": source.collection_id,
        "collection": source.collection.name,
        "active": source.active,
        "interval_minutes": source.interval_minutes,
        "last_scan_started": source.last_scan_started.isoformat()
        if source.last_scan_started
        else None,
        "last_scan_finished": source.last_scan_finished.isoformat()
        if source.last_scan_finished
        else None,
        "last_result": source.last_result or {},
        "last_error": source.last_error,
        "open_run_id": run.pk if run else None,
    }


def reindex_selection(
    *, collections=None, documents=None, errors_only=False
) -> tuple[int, IndexRun]:
    """Betrieb (``mgpt-ctl reindex`` bzw. ``index reindex``, ohne Konto): Auswahl
    wie bisher, aber als Lauf, damit sie sich beobachten und abbrechen lässt.
    Rückgabe ``(Anzahl Dokumente, Lauf)``; ein leerer Lauf endet sofort."""
    qs = Document.objects.select_related("collection").order_by("pk")
    if collections:
        qs = qs.filter(collection_id__in=collections)
    if documents:
        qs = qs.filter(pk__in=documents)
    if errors_only:
        qs = qs.filter(status=Document.Status.ERROR)
    if not (collections or documents or errors_only):
        kind, collection = IndexRun.Kind.REINDEX_ALL, None
    elif collections and len(collections) == 1 and not documents:
        kind = IndexRun.Kind.REINDEX_COLLECTION
        collection = Collection.objects.filter(pk=collections[0]).first()
    else:
        kind, collection = IndexRun.Kind.REINDEX_DOCUMENTS, None
    run = IndexRun.objects.create(kind=kind, collection=collection)
    count = services.reindex_documents(list(qs), run)
    run.refresh_from_db()
    return count, run
