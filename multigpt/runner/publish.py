"""Werkzeug ``publish`` (Scratchpad des Runners, Nutzerentscheidung 2026-10-13).

Eine Datei aus ``/scratch`` oder ``/workspace`` wird als Anhang der Antwort in
den Chat gelegt – nur auf Anfrage (Aufruf des Modells), mit Größengrenze. Der
Inhalt kommt aus dem Container und gilt als nicht vertrauenswürdiges Material:

* Rasterbilder werden neu kodiert (``attachments.process_image``), SVG wird
  bereinigt (``svg_clean``), PDF nur mit gültigem Kopf als ``application/pdf``.
* Alles andere wird ``Attachment.Kind.FILE``; aktive Typen (HTML, SVG, XML,
  JavaScript) werden als ``application/octet-stream`` gespeichert. Ausgeliefert
  wird wie bei anderen erzeugten Anhängen (``api_attachments``: Download,
  ``nosniff``, CSP ``sandbox``).

**Übertragung:** Der Server gibt dem Agent ``transfer`` (ID) und ``max_bytes``
mit. Der Agent schickt die Datei als ``file_chunk``-Rahmen (wie Bundles), die
der Runner-Dienst diesem Prozess zustellt (Abo ``("transfer", id)``); hier
werden die Stücke zusammengesetzt und Größe sowie SHA-256 geprüft. Liefert der
Agent die Datei stattdessen im MCP-Ergebnis (eingebettete Ressource), wird
diese genommen.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import io
import logging
import mimetypes
import re

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction

from multigpt.chat import attachments as chat_attachments
from multigpt.chat import svg_clean
from multigpt.chat.models import Attachment

logger = logging.getLogger(__name__)

DEFAULT_MAX_MB = 20
MAX_FILES_PER_ANSWER = 12
TRANSFER_TIMEOUT = 60.0
RASTER_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif")
# Typen, die ein Browser ausführen bzw. als Dokument deuten würde: nur als Bytes.
ACTIVE_TYPES = {
    "text/html",
    "application/xhtml+xml",
    "image/svg+xml",
    "text/xml",
    "application/xml",
    "text/javascript",
    "application/javascript",
}

MSG_TOO_LARGE = "Die Datei ist zu groß (höchstens {mb} MB)."
MSG_TOO_MANY = f"Höchstens {MAX_FILES_PER_ANSWER} Dateien je Antwort."
MSG_TRANSFER = "Die Datei kam nicht vollständig an."
MSG_CHECKSUM = "Die Prüfsumme der Datei stimmt nicht."
MSG_INVALID = "Die Datei ist kein gültiges {kind}."


class PublishError(Exception):
    """Deutsche Meldung für das Modell."""


def max_bytes(user) -> int:
    mb = getattr(settings, "RUNNER_PUBLISH_MAX_MB", DEFAULT_MAX_MB) or DEFAULT_MAX_MB
    return int(mb) * 1024 * 1024


# --- Übertragung ---------------------------------------------------------------------


@contextlib.contextmanager
def subscribe(runner_id: int, transfer: str):
    """Abo für die Stücke einer Übertragung (vor dem MCP-Aufruf; meldet die
    Zuordnung beim Runner-Dienst an). None, wenn das nicht geht – dann zählt
    nur eine im MCP-Ergebnis eingebettete Datei."""
    from . import delivery

    try:
        sub = delivery.subscribe_transfer(runner_id, transfer)
    except (ValueError, delivery.RunnerError):
        yield None
        return
    try:
        yield sub
    finally:
        sub.close()


def collect(sub, limit: int, timeout: float = TRANSFER_TIMEOUT) -> tuple[bytes, str, str]:
    """Stücke ``file_chunk`` einsammeln: (Daten, Name, Typ). ``PublishError`` bei Fehlern."""
    from . import delivery, protocol

    if sub is None:
        raise PublishError(MSG_TRANSFER)
    parts: list[bytes] = []
    size = 0
    expected = 0
    while True:
        try:
            frame = sub.get(timeout)
        except delivery.RunnerError:
            raise PublishError(MSG_TRANSFER) from None
        if frame.get("type") == "error":
            raise PublishError(frame.get("message") or MSG_TRANSFER)
        if frame.get("type") != "file_chunk" or frame.get("seq") != expected:
            raise PublishError(MSG_TRANSFER)
        try:
            data = protocol.b64decode(frame.get("data") or "")
        except ValueError:
            raise PublishError(MSG_TRANSFER) from None
        size += len(data)
        if size > limit:
            raise PublishError(MSG_TOO_LARGE.format(mb=limit // (1024 * 1024)))
        parts.append(data)
        expected += 1
        if frame.get("last"):
            break
    blob = b"".join(parts)
    digest = hashlib.sha256(blob).hexdigest()
    if frame.get("size") != len(blob):
        raise PublishError(MSG_TRANSFER)
    if not hmac.compare_digest(digest, str(frame.get("sha256") or "").lower()):
        raise PublishError(MSG_CHECKSUM)
    return blob, str(frame.get("name") or ""), str(frame.get("mime") or "")


# --- Speichern -------------------------------------------------------------------------


def _stem(name: str) -> str:
    base = re.split(r"[\\/]", name or "")[-1]
    stem = re.sub(r"[^\w.-]+", "-", base.rsplit(".", 1)[0], flags=re.UNICODE).strip("-.")
    return stem[:80] or "datei"


def _suffix(name: str) -> str:
    base = re.split(r"[\\/]", name or "")[-1]
    if "." not in base:
        return ""
    suffix = "." + re.sub(r"[^A-Za-z0-9]+", "", base.rsplit(".", 1)[1])[:10].lower()
    return suffix if suffix != "." else ""


def _save(attachment: Attachment, name: str, data: bytes, thumbnail: bytes | None) -> None:
    try:
        with transaction.atomic():
            attachment.file.save(name, ContentFile(data), save=False)
            if thumbnail is not None:
                attachment.thumbnail.save("thumb.webp", ContentFile(thumbnail), save=False)
            attachment.save()
    except Exception:
        chat_attachments.delete_files([attachment], immediately=True)
        raise


def store(message, tool_call, name: str, data: bytes, user=None) -> Attachment:
    """Datei prüfen und als Anhang der Antwort speichern (``PublishError`` bei Fehlern)."""
    limit = max_bytes(user)
    if len(data) > limit:
        raise PublishError(MSG_TOO_LARGE.format(mb=limit // (1024 * 1024)))
    already = Attachment.objects.filter(message=message, tool_call__isnull=False).count()
    if already >= MAX_FILES_PER_ANSWER:
        raise PublishError(MSG_TOO_MANY)
    stem, suffix = _stem(name), _suffix(name)
    base = {
        "message": message,
        "tool_call": tool_call,
        "owner": None,  # erzeugt, nicht hochgeladen
        "conversation": message.conversation,
    }
    try:
        if suffix in RASTER_SUFFIXES:
            result = chat_attachments.process_image(io.BytesIO(data))
            attachment = Attachment(
                **base,
                kind=Attachment.Kind.IMAGE,
                mime_type=result["mime_type"],
                width=result["width"],
                height=result["height"],
                size=len(result["data"]),
                original_name=stem + result["extension"],
            )
            _save(attachment, "runner" + result["extension"], result["data"], result["thumbnail"])
            return attachment
        if suffix == ".svg":
            clean, width, height = svg_clean.clean(data)
            attachment = Attachment(
                **base,
                kind=Attachment.Kind.IMAGE,
                mime_type="image/svg+xml",
                width=width,
                height=height,
                size=len(clean),
                original_name=stem + ".svg",
            )
            _save(attachment, "runner.svg", clean, None)
            return attachment
    except (svg_clean.SvgError, chat_attachments.UploadError):
        raise PublishError(MSG_INVALID.format(kind="Bild")) from None
    if suffix == ".pdf":
        if not data.startswith(b"%PDF-"):
            raise PublishError(MSG_INVALID.format(kind="PDF"))
        mime = "application/pdf"
    else:
        mime = mimetypes.guess_type("x" + suffix)[0] if suffix else None
        if not mime or mime in ACTIVE_TYPES or mime.startswith("image/"):
            mime = "application/octet-stream"
    attachment = Attachment(
        **base,
        kind=Attachment.Kind.FILE,
        mime_type=mime,
        size=len(data),
        original_name=stem + suffix,
    )
    # Gespeicherte Endung nur aus harmlosen Zeichen; HTML u. a. bleibt Download.
    _save(attachment, "runner" + (suffix or ".bin"), data, None)
    return attachment
