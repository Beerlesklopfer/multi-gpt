"""MCP-Werkzeuge für Sammlungen und Dokumente (M15): lesen (``docs.read``) und
schreiben (``docs.write``).

Lesend werden die vorhandenen Dokument-Werkzeuge des Chats wiederverwendet
(``search_documents``, ``list_documents``, ``document_info``,
``read_document``); ihre Rechte gelten unverändert (Zugriffsfilter in SQL).
Ein Key mit eingeschränkten Sammlungen sieht nur diese: Argumente mit fremden
Sammlungen bzw. Dokumenten ergeben „nicht gefunden“, ohne Angabe gilt die
Einschränkung als Auswahl.

Hochladen prüft wie die Oberfläche: Rechte (``check_upload_permission``),
Größe (``DOCUMENT_MAX_UPLOAD_MB``) und Typ am Inhalt. ``url`` lädt nur über den
SSRF-geschützten Abruf der Websuche (keine Intranet-Adressen, Weiterleitungen
geprüft, Größen- und Zeitgrenze).
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import mimetypes
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction

from multigpt.accounts.permissions import Action, can
from multigpt.chat import (
    services,  # noqa: F401 - registriert die eingebauten Werkzeuge
    tooling,
)
from multigpt.chat.api_collections import (
    delete_document_files,
    readable_collections,
    serialize_collection,
    with_counts,
    writable_collection_ids,
)
from multigpt.chat.models import Document
from multigpt.chat.rag import chat as rag_chat
from multigpt.chat.rag import extract, jobs, upload
from multigpt.chat.websearch import fetch as web_fetch
from multigpt.chat.websearch.base import FetchError

from . import runs
from . import scopes as S
from .tools import (
    MemorySources,
    NodeTool,
    ToolFailure,
    ToolOutput,
    arg_text,
    json_output,
    register,
    schema,
)

logger = logging.getLogger(__name__)

URL_TIMEOUT = 60.0
MSG_BOTH = "Bitte genau eines von „content_base64“ und „url“ angeben."
MSG_SEARCH_OFF = "Die Dokumentsuche ist derzeit nicht eingerichtet."
# Inhaltstypen, die ``url`` annimmt; maßgeblich ist danach die Prüfung am Inhalt.
URL_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
    "text/markdown",
    "text/x-markdown",
    "image/jpeg",
    "image/png",
    "image/tiff",
    "image/webp",
    "application/octet-stream",
}


def _builtin_result(name: str, ctx, args: dict, collections=None) -> ToolOutput:
    builtin = tooling.get_builtin(name)
    if builtin is None or not builtin.available(ctx.user, None):
        raise ToolFailure("Dieses Werkzeug steht dem Konto nicht zur Verfügung.")
    sources = MemorySources(ctx.user, collections)
    result = builtin.run(ctx.user, args, sources)
    data = {"sources": sources.numbered()} if sources.items else None
    return ToolOutput(result.text, data, bool(result.is_error))


def _builtin_schema(name: str, extra: dict | None = None) -> dict:
    builtin = tooling.get_builtin(name)
    params = json.loads(json.dumps(builtin.spec.parameters)) if builtin else schema()
    if extra:
        params.setdefault("properties", {}).update(extra)
    return params


def _builtin_description(name: str) -> str:
    builtin = tooling.get_builtin(name)
    return builtin.spec.description if builtin else name


def _has_documents(ctx) -> bool:
    qs = Document.objects.filter(collection__in=readable_collections(ctx.user))
    if ctx.collection_ids is not None:
        qs = qs.filter(collection_id__in=ctx.collection_ids)
    return qs.exists()


# --- docs.read ---------------------------------------------------------------------


def list_collections(ctx, args) -> ToolOutput:
    qs = readable_collections(ctx.user)
    if ctx.collection_ids is not None:
        qs = qs.filter(pk__in=ctx.collection_ids)
    writable = writable_collection_ids(ctx.user)
    items = []
    for collection in with_counts(qs).order_by("name", "pk"):
        data = serialize_collection(collection, ctx.user, collection.pk in writable)
        data.pop("url", None)
        items.append(data)
    return json_output({"collections": items}, f"{len(items)} Sammlung(en).")


def _collection_ids(ctx, raw) -> list[int] | None:
    if raw in (None, []):
        return ctx.collection_ids
    if not isinstance(raw, list) or len(raw) > 50:
        raise ToolFailure("„collections“ muss eine Liste von Sammlungen (ID oder Name) sein.")
    return [runs.find_collection(ctx.user, ref, allowed_ids=ctx.collection_ids).pk for ref in raw]


def search_documents(ctx, args) -> ToolOutput:
    if not rag_chat.search_ready():
        raise ToolFailure(MSG_SEARCH_OFF)
    query = arg_text(args, "query", required=True, max_len=2000)
    ids = _collection_ids(ctx, args.get("collections"))
    return _builtin_result(rag_chat.SEARCH_DOCUMENTS, ctx, {"query": query}, ids)


def list_documents(ctx, args) -> ToolOutput:
    if ctx.collection_ids is not None and args.get("collection") not in (None, ""):
        runs.find_collection(ctx.user, args["collection"], allowed_ids=ctx.collection_ids)
    return _builtin_result("list_documents", ctx, args, ctx.collection_ids)


def _document_tool(name: str):
    def handler(ctx, args) -> ToolOutput:
        if ctx.collection_ids is not None:
            runs.find_document(ctx.user, args.get("document_id"), allowed_ids=ctx.collection_ids)
        return _builtin_result(name, ctx, args, ctx.collection_ids)

    return handler


# --- docs.write --------------------------------------------------------------------


def _filename_from_url(url: str, mime: str) -> str:
    name = unquote(PurePosixPath(urlsplit(url).path).name)
    if PurePosixPath(name).suffix.lower() not in extract.ALLOWED_EXTENSIONS:
        ext = {"text/markdown": ".md", "text/x-markdown": ".md"}.get(mime) or (
            mimetypes.guess_extension(mime or "") or ""
        )
        name = f"{PurePosixPath(name).stem or 'dokument'}{ext}"
    return name


def upload_document(ctx, args) -> ToolOutput:
    collection = runs.find_collection(
        ctx.user, args.get("collection"), allowed_ids=ctx.collection_ids
    )
    denied = upload.check_upload_permission(ctx.user, collection)
    if denied is not None:
        raise ToolFailure(json.loads(denied.content)["error"])
    content = args.get("content_base64")
    url = args.get("url")
    if bool(content) == bool(url):
        raise ToolFailure(MSG_BOTH)
    limit = upload.max_upload_bytes()
    too_big = f"Die Datei ist zu groß (höchstens {limit // (1024 * 1024)} MB)."
    filename = arg_text(args, "filename", max_len=300)
    if content:
        if not isinstance(content, str):
            raise ToolFailure("„content_base64“ muss ein Text sein.")
        if len(content) // 4 * 3 > limit + 3:
            raise ToolFailure(too_big)
        try:
            data = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError):
            raise ToolFailure("„content_base64“ ist kein gültiges base64.") from None
        if not filename:
            raise ToolFailure("Bitte das Argument „filename“ angeben (mit Endung).")
    else:
        if not isinstance(url, str) or len(url) > web_fetch.MAX_URL_LENGTH:
            raise ToolFailure(web_fetch.MSG_BAD_URL)
        try:
            page = web_fetch.fetch_raw(url, timeout=URL_TIMEOUT, types=URL_TYPES, max_bytes=limit)
        except FetchError as exc:
            raise ToolFailure(f"Abruf fehlgeschlagen: {exc}") from None
        data = page.body
        filename = filename or _filename_from_url(page.url, page.mime)
    if len(data) > limit:
        raise ToolFailure(too_big)
    if not data:
        raise ToolFailure("Die Datei ist leer.")
    document, error = upload.store_upload(ctx.user, collection, SimpleUploadedFile(filename, data))
    if error is not None:
        raise ToolFailure(json.loads(error.content)["error"])
    run = document.index_run
    return json_output(
        {
            "document_id": document.pk,
            "title": document.title,
            "collection_id": collection.pk,
            "status": document.status,
            "run_id": run.pk,
            "size": len(data),
        },
        f"Dokument #{document.pk} hochgeladen; Indexierung läuft als Lauf #{run.pk} "
        "(Fortschritt mit run_status).",
    )


def delete_document(ctx, args) -> ToolOutput:
    document = runs.find_document(ctx.user, args.get("document_id"), allowed_ids=ctx.collection_ids)
    if not can(ctx.user, Action.WRITE, document.collection):
        raise ToolFailure(runs.MSG_READ_ONLY)
    if document.source_id is not None:
        raise ToolFailure(
            "Das Dokument stammt aus einer Verzeichnisquelle und lässt sich nicht einzeln löschen."
        )
    pk = document.pk
    jobs.cancel_document_jobs([pk])
    with transaction.atomic():
        delete_document_files([document])
        document.delete()
    logger.info("Dokument %s über API-Key %s gelöscht", pk, ctx.key.pk)
    return json_output({"deleted": True, "document_id": pk}, f"Dokument #{pk} gelöscht.")


def reindex(ctx, args) -> ToolOutput:
    has_collection = args.get("collection") not in (None, "")
    doc_refs = args.get("documents")
    if args.get("document") not in (None, ""):
        doc_refs = [args["document"], *(doc_refs or [])]
    if has_collection == bool(doc_refs):
        raise ToolFailure("Bitte entweder „collection“ oder „document“ angeben.")
    if has_collection:
        collection = runs.find_collection(
            ctx.user, args["collection"], allowed_ids=ctx.collection_ids
        )
        run = runs.reindex_collection(ctx.user, collection)
    else:
        if not isinstance(doc_refs, list) or len(doc_refs) > 500:
            raise ToolFailure("„documents“ muss eine Liste von Dokument-IDs sein.")
        documents = [
            runs.find_document(ctx.user, ref, allowed_ids=ctx.collection_ids) for ref in doc_refs
        ]
        run = runs.reindex_documents(ctx.user, documents)
    return json_output(
        runs.serialize_run(run), f"Lauf #{run.pk} gestartet (Fortschritt mit run_status)."
    )


# --- Verzeichnis --------------------------------------------------------------------

_DOC_READ = (S.DOCS_READ,)
_DOC_WRITE = (S.DOCS_WRITE,)
_REF = {"type": ["integer", "string"], "description": "ID oder Name der Sammlung."}

register(
    NodeTool(
        name="list_collections",
        title="Sammlungen auflisten",
        description="Listet die Dokumentsammlungen, die das Konto lesen darf, mit Zahl der "
        "Dokumente je Status und Schreibrecht.",
        scopes=_DOC_READ,
        input_schema=schema(),
        handler=list_collections,
        read_only=True,
    )
)
register(
    NodeTool(
        name="search_documents",
        title="Dokumente durchsuchen",
        description=_builtin_description(rag_chat.SEARCH_DOCUMENTS)
        + " Optional „collections“: nur diese Sammlungen (ID oder Name).",
        scopes=_DOC_READ,
        input_schema=_builtin_schema(
            rag_chat.SEARCH_DOCUMENTS,
            {"collections": {"type": "array", "items": _REF, "description": "Sammlungen."}},
        ),
        handler=search_documents,
        read_only=True,
        available=_has_documents,
    )
)
for _name, _title in (
    ("list_documents", "Dokumente auflisten"),
    ("document_info", "Dokumentangaben"),
    ("read_document", "Dokument lesen"),
):
    register(
        NodeTool(
            name=_name,
            title=_title,
            description=_builtin_description(_name),
            scopes=_DOC_READ,
            input_schema=_builtin_schema(_name),
            handler=list_documents if _name == "list_documents" else _document_tool(_name),
            read_only=True,
            available=_has_documents,
        )
    )

register(
    NodeTool(
        name="upload_document",
        title="Dokument hochladen",
        description="Lädt eine Datei (PDF, DOCX, TXT, MD, JPG, PNG, TIFF, WEBP) in eine "
        "Sammlung und startet die Indexierung. Inhalt als „content_base64“ (mit "
        "„filename“) oder „url“ (öffentlich erreichbar, keine Intranet-Adressen). Liefert "
        "document_id und run_id; den Fortschritt zeigt run_status. Größe und Typ werden wie "
        "in der Oberfläche geprüft.",
        scopes=_DOC_WRITE,
        input_schema=schema(
            {
                "collection": _REF,
                "filename": {"type": "string", "description": "Dateiname mit Endung."},
                "content_base64": {"type": "string", "description": "Dateiinhalt (base64)."},
                "url": {"type": "string", "description": "Alternativ: Adresse der Datei."},
            },
            required=["collection"],
        ),
        handler=upload_document,
        open_world=True,
    )
)
register(
    NodeTool(
        name="delete_document",
        title="Dokument löschen",
        description="Löscht ein Dokument samt Datei und Abschnitten (nur mit Schreibrecht auf "
        "die Sammlung; nicht für Dokumente aus Verzeichnisquellen).",
        scopes=_DOC_WRITE,
        input_schema=schema({"document_id": {"type": "integer"}}, required=["document_id"]),
        handler=delete_document,
        destructive=True,
    )
)
register(
    NodeTool(
        name="reindex",
        title="Neu indexieren",
        description="Indexiert eine Sammlung („collection“) oder einzelne Dokumente "
        "(„document“ bzw. „documents“) neu. Liefert einen Lauf; Fortschritt mit run_status.",
        scopes=_DOC_WRITE,
        input_schema=schema(
            {
                "collection": _REF,
                "document": {"type": "integer", "description": "Dokument-ID."},
                "documents": {"type": "array", "items": {"type": "integer"}},
            }
        ),
        handler=reindex,
    )
)
