"""Verzeichnisquellen einlesen (Agent crawler).

Ein Verwalter verbindet eine Sammlung mit einem Verzeichnis auf dem Server/NAS
(``DirectorySource``). Der Worker liest es periodisch ein (Job
``scan_directory``):

- Verzeichnis durchlaufen – Komponente für Komponente über Deskriptoren, ohne
  Symlinks zu folgen, versteckte Dateien/Ordner überspringen, Muster anwenden,
  höchstens ``RAG_SOURCE_MAX_FILES`` Dateien je Lauf.
- Abgleich über (Pfad, mtime, Größe); nur bei Änderung wird die Datei gelesen,
  am Inhalt typgeprüft (wie beim Upload) und per SHA-256 verglichen.
- Neue/geänderte Dateien -> Dokument anlegen/aktualisieren + Indexierungsjob.
  Verschwundene Dateien -> Dokument samt Abschnitten entfernen.
- Fehler einzelner Dateien werden gezählt, der Lauf geht weiter.

Die Dateien bleiben am Ort (``Document.file`` ist leer). Logs enthalten nur
IDs und Zahlen, nie Datei- oder Pfadnamen.

Periodik: ``enqueue_due_scans`` prüft im Worker einmal je Minute, welche aktiven
Quellen fällig sind, und reiht je Quelle höchstens einen offenen Scan-Job ein.
"""

import fnmatch
import hashlib
import logging
import os
import stat
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from multigpt.chat.models import Document, Job
from multigpt.chat.rag import extract, jobs, upload

from . import paths
from .models import DirectorySource
from .paths import SourcePathError

__all__ = ["SourcePathError"]

logger = logging.getLogger(__name__)

MAX_DEPTH = 32
READ_CHUNK = 1024 * 1024
MSG_EMPTY_DIR = (
    "Das Verzeichnis ist leer – ist die Freigabe eingehängt? Die vorhandenen Dokumente "
    "bleiben erhalten."
)
MSG_TRUNCATED = (
    "Mehr als {limit} Dateien: Nur die ersten {limit} wurden berücksichtigt, nichts wurde "
    "entfernt. Bitte Muster einschränken oder RAG_SOURCE_MAX_FILES erhöhen."
)
MSG_SOURCE_GONE = "Verzeichnisquelle nicht mehr vorhanden."
MSG_TOO_BIG = "Die Datei ist zu groß (höchstens {mb} MB)."


def max_files() -> int:
    return max(1, int(getattr(settings, "RAG_SOURCE_MAX_FILES", 5000)))


def parse_patterns(text: str) -> list[str]:
    return [p.strip() for p in (text or "").replace(";", ",").split(",") if p.strip()]


def _matches(name: str, rel: str, patterns: list[str]) -> bool:
    name, rel = name.lower(), rel.lower()
    return any(
        fnmatch.fnmatchcase(name, p.lower()) or fnmatch.fnmatchcase(rel, p.lower())
        for p in patterns
    )


def mtime_of(st: os.stat_result) -> datetime:
    """mtime auf Mikrosekunden (Auflösung der Datenbank), immer gleich gerundet."""
    ns = st.st_mtime_ns
    return datetime.fromtimestamp(ns // 1_000_000_000, UTC) + timedelta(
        microseconds=(ns % 1_000_000_000) // 1000
    )


@dataclass
class ScanResult:
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    deleted: int = 0
    skipped: int = 0
    errors: int = 0
    truncated: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class _Entry:
    rel: str
    name: str
    size: int
    mtime: datetime


class _EmptyDirectory(Exception):
    pass


# --- Durchlaufen ------------------------------------------------------------------


def _walk(
    dir_fd: int,
    prefix: str,
    *,
    recursive: bool,
    exclude: list[str],
    depth: int = 0,
) -> Iterator[tuple[str, str, os.stat_result | None]]:
    """Reguläre Dateien unterhalb von ``dir_fd`` (sortiert, ohne Symlinks).

    Liefert ``(relativer_pfad, name, stat)``; bei nicht lesbaren Unterordnern
    ``(relativer_pfad, name, None)`` als Fehler.
    """
    with os.scandir(dir_fd) as it:
        entries = sorted(it, key=lambda e: e.name)
    for entry in entries:
        name = entry.name
        if name.startswith("."):
            continue  # versteckt
        rel = f"{prefix}{name}"
        if exclude and _matches(name, rel, exclude):
            continue
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            yield rel, name, None
            continue
        if stat.S_ISDIR(st.st_mode):
            if not recursive or depth >= MAX_DEPTH:
                continue
            try:
                sub = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=dir_fd,
                )
            except OSError:
                yield rel + "/", name, None
                continue
            try:
                yield from _walk(sub, rel + "/", recursive=True, exclude=exclude, depth=depth + 1)
            except OSError:
                yield rel + "/", name, None
            finally:
                os.close(sub)
        elif stat.S_ISREG(st.st_mode):
            yield rel, name, st
        # Symlinks, FIFOs, Geräte, Sockets: still übergehen.


def list_files(source: DirectorySource, base: str) -> tuple[list[_Entry], int, bool, bool]:
    """Kandidaten der Quelle: (Einträge, Fehler, abgeschnitten, Verzeichnis_leer)."""
    include = parse_patterns(source.include_patterns) or ["*"]
    exclude = parse_patterns(source.exclude_patterns)
    limit = max_files()
    result: list[_Entry] = []
    errors = 0
    truncated = False
    fd = paths.open_under_root(base, directory=True)
    try:
        with os.scandir(fd) as it:
            empty = next(it, None) is None
        for rel, name, st in _walk(fd, "", recursive=source.recursive, exclude=exclude):
            if st is None:
                errors += 1
                continue
            if not _matches(name, rel, include):
                continue
            if PurePosixPath(name).suffix.lower() not in extract.ALLOWED_EXTENSIONS:
                continue
            if len(result) >= limit:
                truncated = True
                break
            result.append(_Entry(rel=rel, name=name, size=st.st_size, mtime=mtime_of(st)))
    finally:
        os.close(fd)
    return result, errors, truncated, empty


# --- Abgleich -----------------------------------------------------------------------


def _hash_and_check(fh, name: str) -> tuple[str, int, datetime]:
    """Typprüfung am Inhalt (wie beim Upload) und SHA-256 aus demselben Deskriptor."""
    st = os.fstat(fh.fileno())
    limit = upload.max_upload_bytes()
    if st.st_size > limit:
        raise extract.UnsupportedFile("zu groß")
    extract.detect_kind(fh, name)
    fh.seek(0)
    digest = hashlib.sha256()
    read = 0
    while chunk := fh.read(READ_CHUNK):
        read += len(chunk)
        if read > limit:
            raise extract.UnsupportedFile("zu groß")
        digest.update(chunk)
    return digest.hexdigest(), st.st_size, mtime_of(st)


def scan_source(
    source: DirectorySource, should_stop: Callable[[], bool] | None = None
) -> ScanResult:
    """Quelle einlesen. Wirft ``SourcePathError`` (Quelle unzulässig/unlesbar),
    ``_EmptyDirectory`` oder ``extract.Interrupted``."""
    should_stop = should_stop or (lambda: False)
    result = ScanResult()
    base = paths.check_directory(source.path)
    entries, walk_errors, truncated, empty = list_files(source, base)
    result.errors += walk_errors
    result.truncated = truncated

    existing = {d.source_path: d for d in Document.objects.filter(source=source)}
    if empty and existing:
        raise _EmptyDirectory

    limit = upload.max_upload_bytes()
    seen: set[str] = set()
    for entry in entries:
        if should_stop():
            raise extract.Interrupted
        doc = existing.get(entry.rel)
        if entry.size > limit or entry.size == 0:
            result.skipped += 1
            continue
        if doc is not None and doc.source_size == entry.size and doc.source_mtime == entry.mtime:
            seen.add(entry.rel)
            result.unchanged += 1
            continue
        try:
            with paths.open_file(base, entry.rel) as fh:
                sha, size, mtime = _hash_and_check(fh, entry.name)
        except extract.ExtractionError:
            # Typ passt nicht, leer oder zu groß: wie beim Upload abgelehnt.
            result.skipped += 1
            continue
        except (SourcePathError, OSError):
            result.errors += 1
            if doc is not None:
                seen.add(entry.rel)  # vorübergehend? Dokument behalten.
            continue
        seen.add(entry.rel)
        if doc is not None and doc.source_sha256 == sha:
            Document.objects.filter(pk=doc.pk).update(source_size=size, source_mtime=mtime)
            result.unchanged += 1
            continue
        with transaction.atomic():
            if doc is None:
                doc = Document.objects.create(
                    collection_id=source.collection_id,
                    title=upload.title_from_filename(entry.name),
                    status=Document.Status.PENDING,
                    source=source,
                    source_path=entry.rel,
                    source_size=size,
                    source_mtime=mtime,
                    source_sha256=sha,
                )
                result.new += 1
            else:
                Document.objects.filter(pk=doc.pk).update(
                    source_size=size, source_mtime=mtime, source_sha256=sha
                )
                result.changed += 1
            jobs.enqueue_index(doc)

    if not truncated:
        gone = [d.pk for rel, d in existing.items() if rel not in seen]
        if gone:
            with transaction.atomic():
                Job.objects.filter(
                    kind=Job.Kind.INDEX_DOCUMENT,
                    status=Job.Status.PENDING,
                    payload__document_id__in=gone,
                ).delete()
                result.deleted = (
                    Document.objects.filter(pk__in=gone, source=source)
                    .delete()[1]
                    .get(Document._meta.label, 0)
                )
    return result


def run_scan(source: DirectorySource, should_stop: Callable[[], bool] | None = None) -> str:
    """Einlesen mit Buchführung an der Quelle; Rückgabe: Notiz für den Job."""
    DirectorySource.objects.filter(pk=source.pk).update(last_scan_started=timezone.now())
    error = ""
    result = None
    try:
        result = scan_source(source, should_stop)
    except SourcePathError as exc:
        error = exc.message
    except _EmptyDirectory:
        error = MSG_EMPTY_DIR
    if result is not None:
        if result.truncated:
            error = MSG_TRUNCATED.format(limit=max_files())
        elif result.errors:
            error = (
                f"{result.errors} Datei(en) bzw. Ordner konnten nicht gelesen werden "
                "(Rechte des Dienstnutzers prüfen)."
            )
    DirectorySource.objects.filter(pk=source.pk).update(
        last_scan_finished=timezone.now(),
        last_result=result.as_dict() if result is not None else {},
        last_error=error,
    )
    if result is not None:
        logger.info(
            "Quelle %s eingelesen: %d neu, %d geändert, %d unverändert, %d entfernt, "
            "%d übersprungen, %d Fehler%s",
            source.pk,
            result.new,
            result.changed,
            result.unchanged,
            result.deleted,
            result.skipped,
            result.errors,
            ", abgeschnitten" if result.truncated else "",
        )
    else:
        logger.warning(
            "Quelle %s nicht eingelesen (Verzeichnis unzulässig, fehlt oder leer)", source.pk
        )
    return error


def run_scan_job(job: Job, should_stop: Callable[[], bool]) -> str:
    """Handler für ``Job.Kind.SCAN_DIRECTORY`` (aus ``chat.rag.jobs``)."""
    source_id = (job.payload or {}).get("source_id")
    source = (
        DirectorySource.objects.filter(pk=source_id).first() if isinstance(source_id, int) else None
    )
    if source is None:
        return MSG_SOURCE_GONE
    return run_scan(source, should_stop)


# --- Einreihen und Periodik -------------------------------------------------------


def open_scan_job(source: DirectorySource) -> Job | None:
    return (
        Job.objects.filter(
            kind=Job.Kind.SCAN_DIRECTORY,
            status__in=[Job.Status.PENDING, Job.Status.RUNNING],
            payload__source_id=source.pk,
        )
        .order_by("pk")
        .first()
    )


def enqueue_scan(source: DirectorySource) -> Job | None:
    """Scan-Job anlegen, sofern für die Quelle keiner offen ist (sonst None)."""
    with transaction.atomic():
        # Zeilensperre auf die Quelle: parallele Aufrufe (Worker-Periodik und
        # „Jetzt einlesen“ im Admin) legen nie zwei offene Jobs an.
        locked = DirectorySource.objects.select_for_update().filter(pk=source.pk).first()
        if locked is None or open_scan_job(locked) is not None:
            return None
        return Job.objects.create(kind=Job.Kind.SCAN_DIRECTORY, payload={"source_id": source.pk})


def is_due(source: DirectorySource, now=None) -> bool:
    now = now or timezone.now()
    if not source.active:
        return False
    if source.last_scan_started is None:
        return True
    interval = max(source.interval_minutes or 0, 5)
    return source.last_scan_started <= now - timedelta(minutes=interval)


def enqueue_due_scans(now=None) -> int:
    """Fällige aktive Quellen einreihen (Worker, etwa einmal je Minute)."""
    if not paths.enabled():
        return 0
    now = now or timezone.now()
    count = 0
    for source in DirectorySource.objects.filter(active=True).order_by("pk"):
        if is_due(source, now) and enqueue_scan(source) is not None:
            count += 1
    if count:
        logger.info("%d Verzeichnisquelle(n) zum Einlesen eingereiht", count)
    return count


# --- Lesen einzelner Quelldokumente (Indexierung, Download) ------------------------


def open_document(document: Document):
    """Datei eines Quelldokuments sicher öffnen (Wurzelprüfung, ohne Symlinks)."""
    source = document.source
    if source is None or not document.source_path:
        raise SourcePathError(paths.MSG_FILE_MISSING)
    return paths.open_file(source.path, document.source_path)


def copy_to_temp(document: Document):
    """Quelldokument in eine Temp-Datei kopieren (für die Extraktion per Pfad).

    Rückgabe: ``NamedTemporaryFile`` (wird beim Schließen/Aufräumen gelöscht).
    """
    limit = upload.max_upload_bytes()
    with open_document(document) as fh:
        if os.fstat(fh.fileno()).st_size > limit:
            raise SourcePathError(MSG_TOO_BIG.format(mb=settings.DOCUMENT_MAX_UPLOAD_MB))
        suffix = PurePosixPath(document.source_path).suffix.lower()
        tmp = tempfile.NamedTemporaryFile(prefix="mgpt-src-", suffix=suffix)  # noqa: SIM115
        try:
            copied = 0
            while chunk := fh.read(READ_CHUNK):
                copied += len(chunk)
                if copied > limit:
                    raise SourcePathError(MSG_TOO_BIG.format(mb=settings.DOCUMENT_MAX_UPLOAD_MB))
                tmp.write(chunk)
            tmp.flush()
        except BaseException:
            tmp.close()
            raise
    return tmp
