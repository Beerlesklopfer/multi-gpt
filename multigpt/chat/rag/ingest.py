"""Indexierung eines Dokuments (M7-03, Agent ingest).

Ablauf: Typ am Inhalt prüfen → Text je Seite extrahieren (Seiten ohne
Textebene per OCR) → in überlappende Abschnitte teilen → Embeddings
(``rag.embeddings.embed_texts``) → alte Abschnitte ersetzen →
``Document.status`` = indexiert.

Fehler kommen als ``IngestError`` mit deutscher Meldung für die Oberfläche und
``retryable`` für den Worker. Logs enthalten nur IDs und Zahlen, keine Inhalte
oder Dateinamen.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from django.db import transaction

from ..models import Chunk, Document
from . import chunking, extract

logger = logging.getLogger(__name__)

# Abschnitte je Aufruf von embed_texts (zwischen den Aufrufen wird
# ``should_stop`` geprüft; embed_texts batcht intern selbst noch einmal).
EMBED_BATCH = 64


class IngestError(Exception):
    """``unreachable``: Ein Anbieter (Embedding/OCR, z. B. LM Studio) war nicht
    erreichbar – der Worker wartet dann ohne Obergrenze der Versuche."""

    def __init__(self, message: str, *, retryable: bool = False, unreachable: bool = False):
        super().__init__(message)
        self.message = message
        self.retryable = retryable or unreachable
        self.unreachable = unreachable


@dataclass
class IngestResult:
    pages: int
    ocr_pages: int
    chunks: int
    seconds: float


def _rag_params() -> tuple[int, int]:
    from ..models import RagSettings

    cfg = RagSettings.load()
    return cfg.chunk_tokens, cfg.overlap_tokens


def _embed(texts: list[str]) -> list[list[float]]:
    from .embeddings import embed_texts

    return embed_texts(texts)


def _embedding_error(exc: Exception) -> IngestError | None:
    from .embeddings import EmbeddingError
    from .ocr import OcrError

    if isinstance(exc, EmbeddingError | OcrError):
        message = getattr(exc, "message", "") or "Fehler beim Berechnen der Embeddings."
        return IngestError(
            message,
            retryable=bool(getattr(exc, "retryable", True)),
            unreachable=bool(getattr(exc, "unreachable", False)),
        )
    return None


def _page_reader() -> Callable[[str, int], str]:
    """OCR je Seite nach den RAG-Einstellungen (olmOCR oder Tesseract)."""
    from .ocr import page_reader

    return page_reader()


def _document_file(document: Document):
    """Pfad und Name zum Lesen: hochgeladene Datei oder Datei einer Verzeichnisquelle.

    Quelldokumente (Agent crawler) werden über einen ohne Symlinks geöffneten
    Deskriptor in eine Temp-Datei kopiert (``multigpt.rag.crawl``), damit die
    Extraktion nie einem ausgetauschten Pfad folgt. Rückgabe ``(pfad, name,
    temp_objekt_oder_None)``.
    """
    if document.source_id is None:
        return document.file.path, document.file.name, None
    from multigpt.rag import crawl

    try:
        tmp = crawl.copy_to_temp(document)
    except crawl.SourcePathError as exc:
        raise IngestError(exc.message) from exc
    return tmp.name, document.source_path, tmp


def index_document(
    document: Document,
    *,
    should_stop: Callable[[], bool] | None = None,
    ocr: Callable[[str, int], str] | None = None,
) -> IngestResult:
    """Dokument (neu) indexieren. Wirft ``IngestError`` oder ``extract.Interrupted``."""
    should_stop = should_stop or (lambda: False)
    started = time.monotonic()

    try:
        # Bei Verzeichnisquellen eine geprüfte Temp-Kopie; ``_keep`` hält sie
        # bis zum Ende der Funktion am Leben (danach automatisch gelöscht).
        path, name, _keep = _document_file(document)
        with open(path, "rb") as fh:
            kind = extract.detect_kind(fh, name)
    except FileNotFoundError as exc:
        raise IngestError("Die Datei des Dokuments fehlt auf dem Server.") from exc
    except extract.ExtractionError as exc:
        raise IngestError(str(exc)) from exc

    if ocr is None and kind == extract.KIND_PDF:
        ocr = _page_reader()
    try:
        pages = extract.extract(path, kind, should_stop=should_stop, ocr=ocr)
    except extract.ExtractionError as exc:
        raise IngestError(str(exc)) from exc
    except Exception as exc:
        mapped = _embedding_error(exc)  # auch OCR-Fehler (olmOCR nicht erreichbar)
        if mapped is None:
            raise
        raise mapped from exc
    except MemoryError as exc:
        raise IngestError("Das Dokument ist zu umfangreich für die Verarbeitung.") from exc

    chunk_tokens, overlap_tokens = _rag_params()
    pieces = chunking.split_pages(pages, chunk_tokens, overlap_tokens)
    if not pieces:
        raise IngestError("Im Dokument wurde kein Text gefunden.")

    vectors: list[list[float]] = []
    for i in range(0, len(pieces), EMBED_BATCH):
        if should_stop():
            raise extract.Interrupted
        batch = [p.text for p in pieces[i : i + EMBED_BATCH]]
        try:
            result = _embed(batch)
        except Exception as exc:
            mapped = _embedding_error(exc)
            if mapped is None:
                raise
            raise mapped from exc
        if len(result) != len(batch):
            raise IngestError(
                "Der Embedding-Dienst lieferte eine unerwartete Antwort.", retryable=True
            )
        vectors.extend(result)

    with transaction.atomic():
        # Sperrt das Dokument; ist es inzwischen gelöscht, gibt es nichts zu tun.
        locked = Document.objects.select_for_update().filter(pk=document.pk).first()
        if locked is None:
            logger.info("Dokument %s wurde während der Indexierung gelöscht", document.pk)
            return IngestResult(len(pages), 0, 0, time.monotonic() - started)
        Chunk.objects.filter(document=locked).delete()
        Chunk.objects.bulk_create(
            [
                Chunk(document=locked, position=p.position, text=p.text, page=p.page, embedding=v)
                for p, v in zip(pieces, vectors, strict=True)
            ],
            batch_size=500,
        )
        locked.status = Document.Status.INDEXED
        locked.error_text = ""
        locked.save(update_fields=["status", "error_text"])

    document.status, document.error_text = locked.status, locked.error_text
    return IngestResult(
        pages=len(pages),
        ocr_pages=sum(1 for p in pages if p.ocr),
        chunks=len(pieces),
        seconds=time.monotonic() - started,
    )
