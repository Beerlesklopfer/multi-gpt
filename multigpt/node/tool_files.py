"""MCP-Werkzeuge ``get_file`` (``files.read``) und ``usage`` (``usage.read``), M15.

``get_file`` liefert einen Anhang (z. B. ein erzeugtes PDF) oder ein Dokument
als eingebettete Ressource (base64), nur mit denselben Rechten wie der Download
in der Oberfläche (Anhang: Leserecht auf den Chat bzw. eigener Entwurf;
Dokument: Leserecht auf die Sammlung, ggf. Einschränkung des Keys). Grenze:
``API_FILE_MAX_MB`` (Standard 20 MB); größere Dateien nur über die Oberfläche.
"""

from __future__ import annotations

import logging
import mimetypes

from django.conf import settings
from django.http import Http404

from multigpt.accounts.views_usage import usage_payload
from multigpt.chat.api_attachments import _content_type, download_name, readable_attachment
from multigpt.chat.views_collections import download_content_type, download_filename
from multigpt.rag import crawl

from . import runs
from . import scopes as S
from .tools import FileItem, NodeTool, ToolFailure, ToolOutput, arg_int, json_output, register
from .tools import schema as make_schema

logger = logging.getLogger(__name__)

MSG_NOT_FOUND = "Datei nicht gefunden."


def max_bytes() -> int:
    return max(1, int(getattr(settings, "API_FILE_MAX_MB", 20))) * 1024 * 1024


def _read(handle, limit: int) -> bytes:
    data = handle.read(limit + 1)
    if len(data) > limit:
        raise ToolFailure(
            f"Die Datei ist größer als {limit // (1024 * 1024)} MB; bitte in MultiGPT "
            "herunterladen."
        )
    return data


def _attachment(ctx, pk: int) -> ToolOutput:
    try:
        attachment = readable_attachment(ctx.user, pk)
    except Http404:
        raise ToolFailure(MSG_NOT_FOUND) from None
    stored = attachment.file.name or ""
    if not stored:
        raise ToolFailure(MSG_NOT_FOUND)
    try:
        with attachment.file.open("rb") as handle:
            data = _read(handle, max_bytes())
    except OSError:
        raise ToolFailure(MSG_NOT_FOUND) from None
    mime = _content_type(attachment, stored).split(";", 1)[0]
    name = download_name(attachment, stored)
    return _output(f"multigpt://attachment/{attachment.pk}", mime, data, name, "attachment")


def _document(ctx, pk: int) -> ToolOutput:
    try:
        document = runs.find_document(ctx.user, pk, allowed_ids=ctx.collection_ids)
    except runs.RunError:
        raise ToolFailure(MSG_NOT_FOUND) from None
    limit = max_bytes()
    try:
        if document.source_id is not None:
            with crawl.open_document(document) as handle:
                data = _read(handle, limit)
        elif document.file:
            with document.file.open("rb") as handle:
                data = _read(handle, limit)
        else:
            raise ToolFailure(MSG_NOT_FOUND)
    except (OSError, crawl.SourcePathError):
        raise ToolFailure(MSG_NOT_FOUND) from None
    mime = download_content_type(document)
    name = download_filename(document)
    return _output(f"multigpt://document/{document.pk}", mime, data, name, "document")


def _output(uri: str, mime: str, data: bytes, name: str, kind: str) -> ToolOutput:
    mime = mime or mimetypes.guess_type(name)[0] or "application/octet-stream"
    info = {"kind": kind, "uri": uri, "name": name, "mime_type": mime, "size": len(data)}
    return ToolOutput(
        f"Datei „{name}“ ({mime}, {len(data)} Bytes) als eingebettete Ressource.",
        info,
        files=[FileItem(uri=uri, mime_type=mime, data=data, name=name)],
    )


def get_file(ctx, args) -> ToolOutput:
    attachment_id = arg_int(args, "attachment_id")
    document_id = arg_int(args, "document_id")
    if (attachment_id is None) == (document_id is None):
        raise ToolFailure("Bitte genau eines von „attachment_id“ und „document_id“ angeben.")
    if attachment_id is not None:
        return _attachment(ctx, attachment_id)
    return _document(ctx, document_id)


def usage(ctx, args) -> ToolOutput:
    data = usage_payload(ctx.user)
    return json_output(data, data.get("message") or f"Verbrauch {data['month_label']}.")


register(
    NodeTool(
        name="get_file",
        title="Datei abholen",
        description="Holt einen Anhang (z. B. ein mit create_pdf erzeugtes PDF oder ein "
        "erzeugtes Bild, „attachment_id“) oder ein Dokument einer Sammlung („document_id“) "
        "als base64-Ressource. Höchstgröße siehe Fehlermeldung.",
        scopes=(S.FILES_READ,),
        input_schema=make_schema(
            {"attachment_id": {"type": "integer"}, "document_id": {"type": "integer"}}
        ),
        handler=get_file,
        read_only=True,
    )
)
register(
    NodeTool(
        name="usage",
        title="Verbrauch",
        description="Eigener Verbrauch im laufenden Monat: Kosten, Budget, Stand je "
        "Abrechnungskonto und je Modell.",
        scopes=(S.USAGE_READ,),
        input_schema=make_schema(),
        handler=usage,
        read_only=True,
    )
)
