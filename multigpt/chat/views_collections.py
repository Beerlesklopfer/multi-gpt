"""Seiten der Sammlungen (M7, RAG): Liste, Sammlung mit Dokumenten, Download
und Abschnittsansicht (Ziel der Quellen-Links unter Antworten).

Rechte immer über ``can()``: fremde Sammlungen, Dokumente und Abschnitte -> 404,
damit ihre Existenz verborgen bleibt. Literaturangaben eines Dokuments
bearbeiten (Formular ohne JavaScript) darf, wer in die Sammlung schreiben darf;
nur Lesende bekommen 403. Aktionen (anlegen, hochladen, teilen,
löschen) laufen über die JSON-API; die Seiten zeigen Knöpfe nur bei Recht,
geprüft wird serverseitig in der API.
"""

import re
import unicodedata
from pathlib import PurePath

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import content_disposition_header
from django.views.decorators.http import require_GET, require_http_methods

from multigpt.accounts.permissions import Action, can

from . import citations, sources
from .api_collections import (
    cancelling_document_ids,
    collection_shares,
    document_size,
    readable_collections,
    run_progress,
    share_groups,
    with_counts,
    writable_collection_ids,
)
from .forms_citation import DocumentCitationForm
from .models import Chunk, Collection, Document

# Inhaltstyp für den Download nach der (vom Server vergebenen) Dateiendung.
DOWNLOAD_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
}


def max_upload_mb() -> int:
    """Größenlimit je Datei (Setting von ingest, Standard 25 MB)."""
    return int(getattr(settings, "DOCUMENT_MAX_UPLOAD_MB", 25))


def _readable_collection(request, pk) -> Collection:
    collection = get_object_or_404(Collection.objects.select_related("owner"), pk=pk)
    if not can(request.user, Action.READ, collection):
        raise Http404
    return collection


@require_GET
@login_required
def collection_list(request):
    user = request.user
    writable = writable_collection_ids(user)
    items = list(with_counts(readable_collections(user)).order_by("name", "pk"))
    for item in items:
        item.is_owner = item.owner_id == user.pk
        item.user_can_write = item.pk in writable
    context = {
        "collections": items,
        "own_collections": [c for c in items if c.is_owner],
        "shared_collections": [c for c in items if not c.is_owner],
        "can_create": can(user, Action.UPLOAD_DOCUMENTS),
        "active_page": "collections",
    }
    return render(request, "chat/collections/list.html", context)


@require_GET
@login_required
def collection_detail(request, pk):
    user = request.user
    collection = _readable_collection(request, pk)
    can_write = can(user, Action.WRITE, collection)
    is_owner = collection.owner_id == user.pk
    documents = list(collection.documents.order_by("title", "pk"))
    cancelling = cancelling_document_ids(documents)
    for doc in documents:
        doc.size = document_size(doc)
        doc.cancelling = doc.pk in cancelling
    from_directory = collection.directory_sources.exists()
    can_share = is_owner and can(user, Action.SHARE, collection)
    context = {
        "collection": collection,
        "documents": documents,
        "has_pending": any(d.status == Document.Status.PENDING for d in documents),
        "from_directory": from_directory,
        "is_owner": is_owner,
        "can_write": can_write,
        "can_upload": can_write and can(user, Action.UPLOAD_DOCUMENTS),
        "can_share": can_share,
        "runs": run_progress(collection) if can_write else [],
        "shares": collection_shares(collection) if can_share else [],
        "groups": list(share_groups()) if can_share else [],
        "max_upload_mb": max_upload_mb(),
        "max_upload_bytes": max_upload_mb() * 1024 * 1024,
        "active_page": "collections",
    }
    return render(request, "chat/collections/detail.html", context)


def stored_name(document: Document) -> str:
    """Name der gespeicherten Datei (Upload) bzw. Pfad in der Verzeichnisquelle."""
    if document.source_id is not None:
        return document.source_path or ""
    return document.file.name or ""


def download_filename(document: Document) -> str:
    """Dateiname für den Download: Titel (ohne Pfad- und Steuerzeichen) plus die
    Endung der gespeicherten Datei. Nicht-ASCII kodiert FileResponse nach RFC 5987."""
    suffix = PurePath(stored_name(document)).suffix.lower()
    title = unicodedata.normalize("NFC", document.title or "")
    title = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]+', " ", title)
    title = " ".join(title.split()).strip(" .")[:150] or f"dokument-{document.pk}"
    if suffix and not title.lower().endswith(suffix):
        title += suffix
    return title


def download_content_type(document: Document) -> str:
    suffix = PurePath(stored_name(document)).suffix.lower()
    return DOWNLOAD_TYPES.get(suffix, "application/octet-stream")


# Typen, die der Browser selbst anzeigt („Dokument ansehen“). Markdown als
# Text, sonst bieten Browser ihn zum Speichern an. DOCX und TIFF zeigen
# Browser nicht an – dafür gibt es nur den Download.
VIEW_TYPES = {
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
# Text und Bilder ohne aktive Inhalte. PDF bekommt kein ``sandbox``: Die
# PDF-Betrachter von Chrome und Firefox zeigen sandboxed PDFs nicht an.
VIEW_CSP = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox"


def can_view_inline(document: Document) -> bool:
    return PurePath(stored_name(document)).suffix.lower() in VIEW_TYPES


def view_url(document: Document, page: int | None = None) -> str:
    """Adresse für „Dokument ansehen“; bei PDF mit Sprung auf ``page``."""
    url = reverse("chat:document_view", args=[document.pk])
    if page and PurePath(stored_name(document)).suffix.lower() == ".pdf":
        url += f"#page={int(page)}"
    return url


# Vom Server vergebene Upload-Namen (documents/<id>/<hex>.<endung>); alles
# andere liefert Django selbst aus.
_ACCEL_SAFE_NAME = re.compile(r"[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*(\.[A-Za-z0-9]+)?")


def x_accel_path(name: str) -> str | None:
    """Interner nginx-Pfad für eine Datei unter MEDIA_ROOT, wenn
    ``USE_X_ACCEL_REDIRECT`` aktiv ist und der Name unbedenklich ist."""
    if not getattr(settings, "USE_X_ACCEL_REDIRECT", False):
        return None
    if not name or not _ACCEL_SAFE_NAME.fullmatch(name):
        return None
    return settings.X_ACCEL_REDIRECT_PREFIX + name


@require_GET
@login_required
def document_download(request, pk):
    """Originaldatei als Download – nur mit Lesezugriff auf die Sammlung."""
    return serve_document(request, pk, inline=False)


@require_GET
@login_required
def document_view(request, pk):
    """Originaldatei im Browser anzeigen (PDF, Text, Bilder); andere Typen
    werden heruntergeladen."""
    return serve_document(request, pk, inline=True)


def serve_document(request, pk, *, inline: bool):
    document = get_object_or_404(Document.objects.select_related("collection", "source"), pk=pk)
    if not can(request.user, Action.READ, document.collection):
        raise Http404
    inline = inline and can_view_inline(document)
    if inline:
        content_type = VIEW_TYPES[PurePath(stored_name(document)).suffix.lower()]
    else:
        content_type = download_content_type(document)
    if document.source_id is not None:
        # Verzeichnisquelle: vom Pfad lesen, Wurzelprüfung bei jedem Abruf.
        from multigpt.rag import crawl

        try:
            handle = crawl.open_document(document)
        except crawl.SourcePathError as exc:
            raise Http404 from exc
    elif not document.file:
        raise Http404
    elif accel_path := x_accel_path(document.file.name):
        # nginx liefert die Datei aus (interne location auf MEDIA_ROOT).
        response = HttpResponse(content_type=content_type)
        response["Content-Disposition"] = content_disposition_header(
            not inline, download_filename(document)
        )
        response["X-Accel-Redirect"] = accel_path
        return _protect(response, content_type)
    else:
        try:
            handle = document.file.open("rb")
        except OSError as exc:
            raise Http404 from exc
    response = FileResponse(
        handle,
        as_attachment=not inline,
        filename=download_filename(document),
        content_type=content_type,
    )
    if inline:
        response["Content-Disposition"] = content_disposition_header(
            False, download_filename(document)
        )
    return _protect(response, content_type)


def _protect(response, content_type: str):
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    if not content_type.startswith("application/pdf"):
        response["Content-Security-Policy"] = VIEW_CSP
    return response


@require_GET
@login_required
def document_chunk(request, chunk_id):
    """Abschnittsansicht: Text eines Abschnitts mit Seite und Dokument (Ziel der
    Quellen „Dokument, S. x“). Ohne Lesezugriff auf die Sammlung 404."""
    chunk = get_object_or_404(
        Chunk.objects.select_related("document__collection__owner"), pk=chunk_id
    )
    document = chunk.document
    collection = document.collection
    if not can(request.user, Action.READ, collection):
        raise Http404
    neighbours = {
        c.position: c.pk
        for c in Chunk.objects.filter(
            document=document, position__in=[chunk.position - 1, chunk.position + 1]
        ).only("pk", "position")
    }
    context = {
        "chunk": chunk,
        "document": document,
        "collection": collection,
        "prev_chunk_id": neighbours.get(chunk.position - 1),
        "next_chunk_id": neighbours.get(chunk.position + 1),
        "chunk_count": Chunk.objects.filter(document=document).count(),
        "chunk_location": sources.locator(chunk).label(),
        "citation": citations.entry(
            citations.reference_from_document(document), citations.prefs_for(request.user).style
        ),
        "active_page": "collections",
    }
    response = render(request, "chat/collections/chunk.html", context)
    response["Cache-Control"] = "private, no-store"
    return response


@require_http_methods(["GET", "POST"])
@login_required
def document_citation(request, pk):
    """Literaturangaben eines Dokuments ansehen und (mit WRITE) bearbeiten."""
    document = get_object_or_404(Document.objects.select_related("collection"), pk=pk)
    collection = document.collection
    if not can(request.user, Action.READ, collection):
        raise Http404
    if not can(request.user, Action.WRITE, collection):
        raise PermissionDenied
    form = DocumentCitationForm(request.POST or None, instance=document)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"Literaturangaben für „{document.title}“ gespeichert.")
        return redirect("chat:document_citation", pk=document.pk)
    # Vorschau mit den gespeicherten Angaben (ein ungültiges Formular hat die
    # Instanz schon verändert, darum neu laden).
    stored = Document.objects.get(pk=document.pk) if form.is_bound else document
    reference = citations.reference_from_document(stored)
    prefs = citations.prefs_for(request.user)
    preview = [
        {
            "label": label,
            "entry": citations.entry(reference, key),
            "short": citations.short(reference, key),
        }
        for key, label in citations.STYLE_CHOICES
    ]
    context = {
        "document": document,
        "collection": collection,
        "form": form,
        "preview": preview,
        "own_style": prefs.style,
        "bibtex": citations.bibtex(reference),
        "active_page": "collections",
    }
    return render(request, "chat/collections/citation.html", context)
