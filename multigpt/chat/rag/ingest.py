"""Indexierung eines Dokuments (M7-03, Agent ingest).

Ablauf: Typ am Inhalt prüfen → Text je Seite extrahieren (Seiten ohne
Textebene per OCR) → in überlappende Abschnitte teilen → Embeddings
(``rag.embeddings.embed_texts``; eingebettet wird Kontextkopf + Leerzeile +
Text, siehe ``chunking.heading``) → alte Abschnitte ersetzen →
``Document.status`` = indexiert. Jeder Abschnitt trägt seine Fundstelle (Seite
und Absatz von–bis, ``chunking.TextChunk``); leere Literaturangaben
(``Document.bib_*``) werden aus den Datei-Metadaten vorbelegt.

Fehler kommen als ``IngestError`` mit deutscher Meldung für die Oberfläche und
``retryable`` für den Worker. Logs enthalten nur IDs und Zahlen, keine Inhalte
oder Dateinamen.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from django.db import transaction

from .. import citations
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
    figures: int = 0  # beschriebene Abbildungen (rag.figures)


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


def _figure_collector(document: Document):
    """Sammler für Abbildungen (``rag.figures``) oder None, wenn ausgeschaltet."""
    from .figures import collector_for

    return collector_for(document_id=document.pk)


def _page_reader() -> Callable[[str, int], str]:
    """OCR je Seite nach den RAG-Einstellungen (olmOCR oder Tesseract)."""
    from .ocr import page_reader

    return page_reader()


def chunk_heading(
    document: Document,
    page: int | None,
    page_end: int | None = None,
    section: str = "",
    section_title: str = "",
) -> str:
    """Kontextkopf eines Abschnitts (Titel, bei Verzeichnisquellen Pfad, Seite(n),
    Gliederungsabschnitt)."""
    source_path = document.source_path if document.source_id is not None else ""
    return chunking.heading(document.title, source_path, page, page_end, section, section_title)


# Seiten, auf denen DOI bzw. Normnummer gesucht werden (Titelseite, Impressum).
DETECT_PAGES = 2


def detect_metadata(
    document: Document, meta: extract.Metadata, pages: list[extract.Page]
) -> citations.Reference:
    """Vorschlag für die Literaturangaben: Datei-Metadaten, erkannte DOI und
    Normnummer, bei eingeschalteter Abfrage die Angaben von Crossref.

    Rangfolge je Feld: Crossref vor Normerkennung vor Datei-Metadaten. Normen
    werden nur mit Ausgabedatum erkannt (lieber nichts als falsch).
    """
    from ..models import RagSettings

    head = "\n".join(p.text for p in pages[:DETECT_PAGES])
    values: dict = {
        "title": meta.title,
        "authors": list(meta.authors or []),
        "date": str(meta.year) if meta.year else "",
        "doi": meta.doi or citations.find_doi(head),
    }
    norm = citations.find_norm(document.title) or citations.find_norm(head[:3000])
    if norm:
        # Normnummer statt Autor/Jahr der PDF-Metadaten (meist Ersteller und Druckdatum).
        values |= {
            "type": citations.TYPE_STANDARD,
            "number": norm.number,
            "date": norm.date,
            "institution": norm.institution,
            "authors": [],
        }
    cfg = RagSettings.load()
    if values["doi"] and cfg.crossref_enabled:
        from .crossref import lookup

        found = lookup(values["doi"], cfg.crossref_mailto)
        if found is not None:
            values |= {k: v for k, v in found.to_dict().items() if v}
    return citations.Reference.from_dict(values)


# Felder, die die Vorbelegung füllen darf (Reference-Feld -> Document-Feld).
PREFILL_FIELDS = (
    "title",
    "date",
    "container",
    "publisher",
    "place",
    "edition",
    "series",
    "series_number",
    "isbn",
    "isbn_e",
    "doi",
    "journal",
    "volume",
    "issue",
    "pages",
    "number",
    "institution",
)


def prefill_metadata(document: Document, found: citations.Reference) -> list[str]:
    """Leere Literaturangaben aus ``found`` vorbelegen – nie nach Bearbeitung von
    Hand (``bib_edited``) und nie über vorhandene Werte. Rückgabe: geänderte
    Felder fürs ``save``."""
    if document.bib_edited:
        return []
    changed = []
    for name in PREFILL_FIELDS:
        value = getattr(found, name)
        attr = f"bib_{name}"
        if value and not getattr(document, attr):
            limit = Document._meta.get_field(attr).max_length or len(value)
            setattr(document, attr, value[:limit])
            changed.append(attr)
    for name in ("authors", "editors"):
        people = getattr(found, name)
        attr = f"bib_{name}"
        if people and not getattr(document, attr).strip():
            setattr(document, attr, "\n".join(people))
            changed.append(attr)
    if found.type != citations.TYPE_OTHER and document.bib_type == citations.TYPE_OTHER:
        document.bib_type = found.type
        changed.append("bib_type")
    return changed


def embedding_input(heading: str, text: str) -> str:
    """Eingabe für das Embedding: Kopf, Leerzeile, Text (Präfix setzt embed_texts)."""
    return f"{heading}\n\n{text}" if heading else text


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

    if ocr is None and kind in (extract.KIND_PDF, extract.KIND_IMAGE):
        ocr = _page_reader()
    figures = _figure_collector(document)
    try:
        pages = extract.extract(path, kind, should_stop=should_stop, ocr=ocr, figures=figures)
        # Abbildungen beschreiben (Vision-Modell); Fehler wie bei der OCR.
        described = figures.describe(pages, should_stop) if figures is not None else 0
    except extract.ExtractionError as exc:
        raise IngestError(str(exc)) from exc
    except Exception as exc:
        mapped = _embedding_error(exc)  # auch OCR-Fehler (olmOCR nicht erreichbar)
        if mapped is None:
            raise
        raise mapped from exc
    except MemoryError as exc:
        raise IngestError("Das Dokument ist zu umfangreich für die Verarbeitung.") from exc
    # Literaturangaben vorschlagen (Datei-Metadaten, DOI, Norm, ggf. Crossref);
    # Fehler ergeben leere Angaben.
    meta = extract.extract_metadata(path, kind)
    found = detect_metadata(document, meta, pages)

    chunk_tokens, overlap_tokens = _rag_params()
    pieces = chunking.split_pages(pages, chunk_tokens, overlap_tokens)
    if not pieces:
        raise IngestError("Im Dokument wurde kein Text gefunden.")

    headings = [
        chunk_heading(document, p.page, p.page_end, p.section, p.section_title) for p in pieces
    ]
    vectors: list[list[float]] = []
    for i in range(0, len(pieces), EMBED_BATCH):
        if should_stop():
            raise extract.Interrupted
        batch = [
            embedding_input(h, p.text)
            for h, p in zip(headings[i : i + EMBED_BATCH], pieces[i : i + EMBED_BATCH], strict=True)
        ]
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
                Chunk(
                    document=locked,
                    position=p.position,
                    text=p.text,
                    heading=h,
                    page=p.page,
                    page_end=p.page_end,
                    paragraph=p.paragraph,
                    paragraph_end=p.paragraph_end,
                    section=p.section,
                    section_title=p.section_title,
                    section_end=p.section_end,
                    embedding=v,
                )
                for p, h, v in zip(pieces, headings, vectors, strict=True)
            ],
            batch_size=500,
        )
        locked.status = Document.Status.INDEXED
        locked.error_text = ""
        locked.figures_described = described
        locked.save(
            update_fields=[
                "status",
                "error_text",
                "figures_described",
                *prefill_metadata(locked, found),
            ]
        )

    document.status, document.error_text = locked.status, locked.error_text
    return IngestResult(
        pages=len(pages),
        ocr_pages=sum(1 for p in pages if p.ocr),
        chunks=len(pieces),
        seconds=time.monotonic() - started,
        figures=described,
    )
