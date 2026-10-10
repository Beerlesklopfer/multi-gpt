"""Upload eines Dokuments in eine Sammlung (M7-01, Agent ingest).

Prüft Rechte, Größe (``settings.DOCUMENT_MAX_UPLOAD_MB``) und Typ am Inhalt
(nicht nur an der Endung), speichert unter zufälligem Namen
(``models.document_upload_to``) und reiht die Indexierung ein. Der
Originaldateiname dient nur als Titel.
"""

import logging
import re

from django.conf import settings
from django.db import transaction
from django.http import JsonResponse

from multigpt.accounts.permissions import Action, can

from ..models import Collection, Document, IndexRun
from . import extract, jobs

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = extract.ALLOWED_EXTENSIONS
TITLE_MAX = 300
# Overhead des multipart-Rahmens über der eigentlichen Datei.
_MULTIPART_SLACK = 64 * 1024

MSG_NOT_FOUND = "Sammlung nicht gefunden."
MSG_READ_ONLY = "Du darfst diese Sammlung nur lesen."
MSG_NO_UPLOAD = "Du darfst keine Dokumente hochladen."


def _error(message: str, status: int) -> JsonResponse:
    return JsonResponse({"error": message}, status=status)


def max_upload_bytes() -> int:
    return int(settings.DOCUMENT_MAX_UPLOAD_MB) * 1024 * 1024


def title_from_filename(filename: str) -> str:
    """Anzeigetitel aus dem Originalnamen: ohne Pfad, ohne Steuerzeichen, gekürzt."""
    name = re.split(r"[\\/]", filename or "")[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:TITLE_MAX] or "Dokument"


def check_upload_permission(user, collection: Collection | None) -> JsonResponse | None:
    """Rechte prüfen: kein Lesen -> 404, nur Lesen -> 403, kein Upload-Recht -> 403."""
    if collection is None or not can(user, Action.READ, collection):
        return _error(MSG_NOT_FOUND, 404)
    if not can(user, Action.WRITE, collection):
        return _error(MSG_READ_ONLY, 403)
    if not can(user, Action.UPLOAD_DOCUMENTS):
        return _error(MSG_NO_UPLOAD, 403)
    return None


def upload_document(request, collection: Collection) -> tuple[Document | None, JsonResponse | None]:
    """Datei aus ``request.FILES['file']`` prüfen und als Dokument anlegen.

    Rückgabe ``(dokument, None)`` bei Erfolg, sonst ``(None, Fehlerantwort)``.
    """
    denied = check_upload_permission(request.user, collection)
    if denied is not None:
        return None, denied

    limit = max_upload_bytes()
    too_big = f"Die Datei ist zu groß (höchstens {settings.DOCUMENT_MAX_UPLOAD_MB} MB)."
    try:
        content_length = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        content_length = 0
    if content_length > limit + _MULTIPART_SLACK:
        return None, _error(too_big, 413)

    files = request.FILES.getlist("file")
    if not files:
        return None, _error("Bitte eine Datei auswählen.", 400)
    if len(files) > 1:
        return None, _error("Bitte nur eine Datei je Anfrage hochladen.", 400)
    upload = files[0]
    if upload.size > limit:
        return None, _error(too_big, 413)
    if upload.size == 0:
        return None, _error("Die Datei ist leer.", 400)

    try:
        extract.detect_kind(upload, upload.name)
    except extract.UnsupportedFile as exc:
        return None, _error(str(exc), 415)
    except extract.ExtractionError as exc:
        return None, _error(str(exc), 400)
    upload.seek(0)

    document = Document(
        collection=collection,
        title=title_from_filename(upload.name),
        status=Document.Status.PENDING,
    )
    try:
        with transaction.atomic():
            # Der Speichername ist zufällig (document_upload_to), nur die Endung bleibt.
            document.file.save(upload.name, upload, save=False)
            document.save()
            run = IndexRun.objects.create(
                kind=IndexRun.Kind.UPLOAD, collection=collection, started_by=request.user
            )
            jobs.enqueue_index(document, run)
    except Exception:
        if document.file.name:
            document.file.delete(save=False)
        raise
    logger.info(
        "Dokument %s hochgeladen (Sammlung %s, %d Bytes)", document.pk, collection.pk, upload.size
    )
    return document, None
