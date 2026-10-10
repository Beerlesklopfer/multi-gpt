"""Dokumente einer Sammlung: Statusliste und Upload (M7, Agent ingest).

``GET  /api/collections/<pk>/documents/`` -> ``{documents: [...], can_write, can_upload}``
``POST /api/collections/<pk>/documents/`` (multipart, Feld ``file``) -> 201 Dokument

Rechte: kein Lesezugriff -> 404 (Existenz bleibt verborgen); POST ohne
Schreibrecht oder ohne Upload-Recht -> 403. Fehler als ``{"error": "<Text>"}``.
"""

from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from multigpt.accounts.permissions import Action, can

from .api import api_login_required
from .api_collections import cancelling_document_ids, run_progress, serialize_document
from .models import Collection
from .rag import upload


@require_http_methods(["GET", "POST"])
@api_login_required
def collection_documents(request, pk: int):
    collection = Collection.objects.filter(pk=pk).first()
    if collection is None or not can(request.user, Action.READ, collection):
        return JsonResponse({"error": upload.MSG_NOT_FOUND}, status=404)

    if request.method == "POST":
        document, error = upload.upload_document(request, collection)
        if error is not None:
            return error
        return JsonResponse(serialize_document(document), status=201)

    can_write = can(request.user, Action.WRITE, collection)
    documents = list(collection.documents.order_by("title", "id"))
    cancelling = cancelling_document_ids(documents)
    return JsonResponse(
        {
            "documents": [serialize_document(d, d.pk in cancelling) for d in documents],
            # Fortschritt offener Läufe (z. B. Verzeichnis einlesen) für Schreibberechtigte.
            "runs": run_progress(collection) if can_write else [],
            "can_write": can_write,
            "can_upload": can_write and can(request.user, Action.UPLOAD_DOCUMENTS),
            "max_upload_mb": upload.max_upload_bytes() // (1024 * 1024),
            "allowed_extensions": list(upload.ALLOWED_EXTENSIONS),
        }
    )
