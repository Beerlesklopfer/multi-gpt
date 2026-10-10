"""Anhänge: Upload, Entwurf löschen (JSON) und geschützte Auslieferung.

- ``POST /api/attachments/`` (multipart ``file``, optional ``conversation``)
  -> 201 mit den Daten des Entwurfs; Fehler ``{"error"}`` mit 400/404/413/415.
- ``DELETE /api/attachments/<id>/``: nur eigener, noch nicht gesendeter Entwurf.
- ``GET /anhang/<id>/`` und ``/anhang/<id>/vorschau/``: nur mit READ-Recht auf
  den Chat der Nachricht bzw. als Besitzer des Entwurfs; sonst 404 (Existenz
  fremder Anhänge bleibt verborgen). Bilder ``inline``, alles andere als
  Download; ``nosniff``, ``private, no-store`` und eine CSP ohne Skripte.
"""

import mimetypes
import re
import unicodedata
from pathlib import PurePath

from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils.http import content_disposition_header
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from multigpt.accounts.permissions import Action, can

from . import attachments
from .api import api_login_required
from .models import Attachment, Conversation
from .views_collections import x_accel_path

# Nur diese Typen werden im Browser angezeigt; alles andere ist ein Download.
INLINE_TYPES = set(attachments.IMAGE_FORMATS.values())
CSP = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox"


def _error(message: str, status: int) -> JsonResponse:
    return JsonResponse({"error": message}, status=status)


@require_POST
@api_login_required
def upload(request):
    user = request.user
    if not can(user, Action.CHAT):
        return _error("Chatten ist für dieses Konto nicht freigegeben.", 403)
    conversation = None
    raw = request.POST.get("conversation")
    if raw not in (None, ""):
        try:
            conversation = Conversation.objects.filter(pk=int(raw)).first()
        except ValueError:
            return _error("Ungültige Anfrage.", 400)
        if conversation is None or not can(user, Action.READ, conversation):
            return _error("Chat nicht gefunden.", 404)
        if not can(user, Action.WRITE, conversation):
            return _error("Du darfst in diesem Chat nicht schreiben.", 403)
    try:
        attachment, notice = attachments.handle_upload(request, conversation)
    except attachments.UploadError as exc:
        return _error(exc.message, exc.status)
    return JsonResponse(attachments.serialize_upload(attachment, notice), status=201)


@require_http_methods(["DELETE"])
@api_login_required
def detail(request, pk: int):
    attachment = Attachment.objects.filter(pk=pk, owner=request.user).first()
    if attachment is None:
        return _error(attachments.MSG_NOT_FOUND, 404)
    if not attachment.is_draft:
        return _error("Der Anhang wurde bereits gesendet und kann nicht entfernt werden.", 409)
    attachments.delete_draft(attachment)
    return JsonResponse({"deleted": True, "id": pk})


# --- Auslieferung -------------------------------------------------------------------


def readable_attachment(user, pk: int) -> Attachment:
    """Anhang laden; Entwurf nur für den Besitzer, sonst READ auf den Chat. Sonst 404."""
    attachment = get_object_or_404(
        Attachment.objects.select_related("message__conversation"), pk=pk
    )
    if attachment.message_id is None:
        if attachment.owner_id != user.pk:
            raise Http404
    elif not can(user, Action.READ, attachment.message.conversation):
        raise Http404
    return attachment


def download_name(attachment: Attachment, stored: str) -> str:
    """Dateiname für Content-Disposition: Anzeigename ohne Pfad-/Steuerzeichen,
    Endung der gespeicherten (vom Server erzeugten) Datei."""
    suffix = PurePath(stored).suffix.lower()
    name = unicodedata.normalize("NFC", attachment.display_name)
    name = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]+', " ", name)
    name = " ".join(name.split()).strip(" .")[:150] or f"anhang-{attachment.pk}"
    if suffix and not name.lower().endswith(suffix):
        # z. B. „foto.gif“ wurde als PNG gespeichert -> „foto.png“
        stem = PurePath(name).stem if PurePath(name).suffix else name
        name = (stem or name) + suffix
    return name


def _content_type(attachment: Attachment, stored: str) -> str:
    mime = attachment.mime_type or mimetypes.guess_type(stored)[0] or ""
    if mime.startswith("text/"):
        return f"{mime}; charset=utf-8"
    return mime or "application/octet-stream"


def _serve(attachment: Attachment, field, *, inline: bool, content_type: str, filename: str):
    if not field or not field.name:
        raise Http404
    accel = x_accel_path(field.name)
    if accel:
        response = HttpResponse(content_type=content_type)
        response["X-Accel-Redirect"] = accel
        response["Content-Disposition"] = content_disposition_header(not inline, filename)
    else:
        try:
            handle = field.open("rb")
        except (OSError, ValueError) as exc:
            raise Http404 from exc
        response = FileResponse(
            handle, as_attachment=not inline, filename=filename, content_type=content_type
        )
        if inline:
            response["Content-Disposition"] = content_disposition_header(False, filename)
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = CSP
    return response


@require_GET
@login_required
def serve(request, pk: int):
    attachment = readable_attachment(request.user, pk)
    stored = attachment.file.name or ""
    content_type = _content_type(attachment, stored)
    inline = content_type in INLINE_TYPES
    return _serve(
        attachment,
        attachment.file,
        inline=inline,
        content_type=content_type,
        filename=download_name(attachment, stored),
    )


@require_GET
@login_required
def serve_thumbnail(request, pk: int):
    attachment = readable_attachment(request.user, pk)
    if not attachment.thumbnail:
        raise Http404
    return _serve(
        attachment,
        attachment.thumbnail,
        inline=True,
        content_type="image/webp",
        filename=f"vorschau-{attachment.pk}.webp",
    )
