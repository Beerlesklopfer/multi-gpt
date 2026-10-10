"""Dateityp-Erkennung und Textextraktion für die Indexierung (M7-03, Agent ingest).

Unterstützt PDF (Textebene, Seiten ohne Text per OCR), DOCX, TXT, MD und
Bilddateien (JPG, PNG, TIFF, WEBP; per OCR, siehe ``rag.figures``).

Bibliotheken:

- ``pypdf`` (BSD-3-Clause, reines Python): Textebene je Seite.
- ``python-docx`` (MIT, nutzt lxml): Absätze und Tabellen in Dokumentreihenfolge.
- OCR über die Programme ``pdftoppm`` (poppler-utils) und ``tesseract``
  (tesseract-ocr, Sprachpakete z. B. tesseract-ocr-deu) als Unterprozesse:
  keine zusätzliche Python-Abhängigkeit mit eigener Binärbibliothek, die
  Programme kommen als Debian-Pakete mit Sicherheitsupdates.

Der Dateityp wird immer am Inhalt erkannt (Magic Bytes, ZIP-Inhalt,
Dekodierbarkeit), die Endung muss nur dazu passen.
"""

import logging
import re
import shutil
import subprocess
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings

from .. import citations

logger = logging.getLogger(__name__)

KIND_PDF = "pdf"
KIND_DOCX = "docx"
KIND_TEXT = "text"
KIND_IMAGE = "image"

# Bilddateien (``rag.figures``): Format an den Magic Bytes erkannt, Endungen je Format.
IMAGE_FORMATS = {
    "jpeg": (".jpg", ".jpeg"),
    "png": (".png",),
    "tiff": (".tif", ".tiff"),
    "webp": (".webp",),
}

# Endungen je erkanntem Inhalt.
EXTENSIONS = {
    KIND_PDF: (".pdf",),
    KIND_DOCX: (".docx",),
    KIND_TEXT: (".txt", ".md"),
    KIND_IMAGE: tuple(ext for exts in IMAGE_FORMATS.values() for ext in exts),
}
ALLOWED_EXTENSIONS = tuple(ext for exts in EXTENSIONS.values() for ext in exts)
ALLOWED_LABEL = "PDF, DOCX, TXT, MD, JPG, PNG, TIFF, WEBP"

# Seiten mit weniger Buchstaben/Ziffern gelten als "ohne Textebene" -> OCR.
MIN_PAGE_CHARS = 20
# Schutz vor riesigen (entpackten) Inhalten: ZIP-Bombe bei DOCX, sehr große
# Inhaltsströme bei PDF (pypdf braucht dafür ein Vielfaches an Speicher).
MAX_DOCX_UNCOMPRESSED = 200 * 1024 * 1024
MAX_PDF_CONTENT_STREAM = 50 * 1024 * 1024
MAX_PDF_PAGES = 3000
OCR_DPI = 300
OCR_TIMEOUT = 180  # Sekunden je Seite und Programm


class ExtractionError(Exception):
    """Dauerhafter Fehler mit verständlicher deutscher Meldung (kein Wiederholen)."""


class UnsupportedFile(ExtractionError):
    """Dateityp nicht unterstützt oder Inhalt passt nicht zur Endung."""


class Interrupted(Exception):
    """Abbruch auf Wunsch (Worker wird beendet); der Job wird neu eingereiht."""


class OcrUnavailable(Exception):
    """tesseract oder pdftoppm fehlt."""


@dataclass
class Page:
    number: int | None  # 1-basiert; None bei Formaten ohne Seiten
    text: str
    ocr: bool = False


# --- Typ-Erkennung -------------------------------------------------------------


def _decode_text(data: bytes) -> str | None:
    """UTF-8 (auch mit BOM), sonst Windows-1252; None bei Binärdaten."""
    if b"\x00" in data:
        return None
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = data.decode("cp1252")
        except UnicodeDecodeError:
            return None
    # Steuerzeichen außer Tab, Zeilenumbruch, Seitenvorschub -> kein Text.
    if re.search(r"[\x00-\x08\x0b\x0e-\x1f\x7f]", text):
        return None
    return text


def _is_text_sample(head: bytes, *, truncated: bool) -> bool:
    """Textprüfung am Dateianfang; bei abgeschnittenem Ausschnitt darf das
    letzte UTF-8-Zeichen unvollständig sein (höchstens 3 Bytes)."""
    if _decode_text(head) is not None:
        return True
    if truncated and b"\x00" not in head:
        for cut in (1, 2, 3):
            try:
                head[:-cut].decode("utf-8")
            except UnicodeDecodeError:
                continue
            return _decode_text(head[:-cut]) is not None
    return False


def _is_docx(fileobj) -> bool:
    try:
        with zipfile.ZipFile(fileobj) as zf:
            names = set(zf.namelist())
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                return False
            if sum(info.file_size for info in zf.infolist()) > MAX_DOCX_UNCOMPRESSED:
                raise UnsupportedFile("Das Word-Dokument ist entpackt zu groß.")
            return True
    except zipfile.BadZipFile:
        return False


def image_format(head: bytes) -> str | None:
    """Bildformat an den Magic Bytes (Schlüssel von ``IMAGE_FORMATS``) oder None."""
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def detect_kind(fileobj, filename: str) -> str:
    """Art des Inhalts (``pdf``/``docx``/``text``/``image``) prüfen; muss zur Endung passen.

    ``fileobj`` ist ein binär lesbares, seekbares Dateiobjekt. Wirft
    ``UnsupportedFile`` mit deutscher Meldung.
    """
    suffix = Path(filename or "").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise UnsupportedFile(f"Dateityp nicht unterstützt. Erlaubt sind: {ALLOWED_LABEL}.")

    fileobj.seek(0)
    head = fileobj.read(64 * 1024)
    fileobj.seek(0)
    if not head:
        raise ExtractionError("Die Datei ist leer.")

    image = image_format(head)
    if b"%PDF-" in head[:1024]:
        kind = KIND_PDF
    elif image is not None:
        kind = KIND_IMAGE
    elif head.startswith(b"PK\x03\x04"):
        kind = KIND_DOCX if _is_docx(fileobj) else None
        fileobj.seek(0)
    elif head.startswith(b"\xd0\xcf\x11\xe0"):
        raise UnsupportedFile(
            "Ältere Word-Dateien (.doc) werden nicht unterstützt. "
            "Bitte als .docx oder PDF speichern."
        )
    else:
        kind = KIND_TEXT if _is_text_sample(head, truncated=len(head) == 64 * 1024) else None

    if kind is None:
        raise UnsupportedFile(f"Dateityp nicht unterstützt. Erlaubt sind: {ALLOWED_LABEL}.")
    if kind == KIND_IMAGE and suffix not in IMAGE_FORMATS[image]:
        raise UnsupportedFile(
            f"Der Inhalt passt nicht zur Dateiendung „{suffix}“ (erkannt: {image.upper()}-Bild)."
        )
    if suffix not in EXTENSIONS[kind]:
        raise UnsupportedFile(
            f"Der Inhalt passt nicht zur Dateiendung „{suffix}“ "
            f"(erkannt: {kind.upper() if kind != KIND_TEXT else 'Text'})."
        )
    return kind


# --- Aufbereitung --------------------------------------------------------------


def clean_text(text: str) -> str:
    """Zeilenenden vereinheitlichen, Silbentrennung am Zeilenende auflösen,
    Leerraum zusammenfassen."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    # "Verbin-\ndung" -> "Verbindung" (nur vor Kleinbuchstaben, "Ein- und" bleibt)
    text = re.sub(r"(\w)-\n(?=[a-zäöüß])", r"\1", text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# --- Absätze in der PDF-Textebene --------------------------------------------------
#
# pypdf liefert die Textebene zeilenweise ohne Leerzeilen zwischen Absätzen
# (Einrückungen fallen in ``clean_text`` weg). Für die Absatzzählung (Zitieren
# mit „Abs.“) setzt eine vorsichtige Heuristik Leerzeilen ein, nur wenn die
# Seite noch keine enthält. Grundlage ist die übliche Satzspiegel-Breite der
# Seite: Die letzte Zeile eines Absatzes ist meist kürzer als eine volle Zeile.
# Lieber ein Absatzwechsel zu wenig als einer zu viel.

# Erst ab so vielen Zeilen und dieser Zeilenbreite lohnt die Schätzung.
PARA_MIN_LINES = 3
PARA_MIN_WIDTH = 40
# Absatzende: Zeile endet mit Satzzeichen und ist kürzer als dieser Anteil der
# vollen Zeilenbreite (Blocksatz/Flattersatz schwanken um etwa 10 %).
PARA_SHORT_LINE = 0.85
# Überschrift: kurze Zeile ohne Satzzeichen am Ende, höchstens so viele Wörter.
HEADING_MAX_LINE = 0.6
HEADING_MAX_WORDS = 10

_LINE_SENTENCE_END = re.compile(r"[.!?:…][\"'»«“”)\]]*$")
_LINE_OPEN_END = re.compile(r"[.,;:!?…\-–]$")
# Beginn eines neuen Absatzes: Großbuchstabe, Ziffer, Aufzählungszeichen, Anführung.
_LINE_STARTS_BLOCK = re.compile(r"^[A-ZÄÖÜ0-9•\-–„“\"»(§]")


def pdf_paragraphs(text: str) -> str:
    """Leerzeilen an wahrscheinlichen Absatzgrenzen einer PDF-Seite einsetzen.

    Grenze nach Zeile *i*, wenn die nächste Zeile wie ein Absatzbeginn aussieht
    (Großbuchstabe, Ziffer, Aufzählung) und Zeile *i* entweder

    - mit Satzende endet und deutlich kürzer als eine volle Zeile ist, oder
    - eine kurze Überschrift ist (wenige Wörter, kein Satzzeichen am Ende,
      davor Seitenanfang oder ein Satzende).

    Seiten, die schon Leerzeilen haben, oder sehr kurze Seiten bleiben, wie sie
    sind. Volle Zeilenbreite = 90-%-Quantil der Zeilenlängen (robust gegen
    einzelne Ausreißer wie lange URLs).
    """
    if "\n\n" in text:
        return text
    lines = text.split("\n")
    if len(lines) < PARA_MIN_LINES:
        return text
    lengths = sorted(len(line) for line in lines)
    width = lengths[min(len(lengths) - 1, int(len(lengths) * 0.9))]
    if width < PARA_MIN_WIDTH:
        return text
    out = [lines[0]]
    for i in range(len(lines) - 1):
        line, following = lines[i].strip(), lines[i + 1].strip()
        if line and following and _LINE_STARTS_BLOCK.match(following):
            sentence_end = bool(_LINE_SENTENCE_END.search(line))
            before = lines[i - 1].strip() if i else ""
            is_heading = (
                not _LINE_OPEN_END.search(line)
                and len(line) < HEADING_MAX_LINE * width
                and len(line.split()) <= HEADING_MAX_WORDS
                and _LINE_STARTS_BLOCK.match(line)
                and (i == 0 or bool(_LINE_SENTENCE_END.search(before)))
            )
            if (sentence_end and len(line) < PARA_SHORT_LINE * width) or is_heading:
                out.append("")
        out.append(lines[i + 1])
    return "\n".join(out)


def _meaningful_chars(text: str) -> int:
    return sum(ch.isalnum() for ch in text)


# --- OCR -------------------------------------------------------------------------


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd, capture_output=True, timeout=OCR_TIMEOUT, check=False, stdin=subprocess.DEVNULL
        )
    except FileNotFoundError as exc:
        raise OcrUnavailable(cmd[0]) from exc


def ocr_available() -> bool:
    return bool(shutil.which("tesseract") and shutil.which("pdftoppm"))


def ocr_pdf_page(pdf_path: str, page_number: int) -> str:
    """Eine PDF-Seite rendern (pdftoppm, Graustufen) und mit tesseract lesen.

    Wirft ``OcrUnavailable``, wenn die Programme fehlen; sonst liefert sie bei
    Fehlern einer einzelnen Seite einen leeren Text (Warnung im Log).
    """
    with tempfile.TemporaryDirectory(prefix="mgpt-ocr-") as tmp:
        base = str(Path(tmp) / "page")
        n = str(page_number)
        try:
            render = _run(
                [
                    "pdftoppm",
                    "-r",
                    str(OCR_DPI),
                    "-f",
                    n,
                    "-l",
                    n,
                    "-gray",
                    "-png",
                    "-singlefile",
                    pdf_path,
                    base,
                ]  # fmt: skip
            )
        except subprocess.TimeoutExpired:
            logger.warning("OCR: Rendern von Seite %s dauerte zu lange", page_number)
            return ""
        image = base + ".png"
        if render.returncode != 0 or not Path(image).exists():
            logger.warning("OCR: pdftoppm-Fehler auf Seite %s (Code %s)", n, render.returncode)
            return ""
        try:
            result = _run(["tesseract", image, "-", "-l", settings.OCR_LANGUAGES])
        except subprocess.TimeoutExpired:
            logger.warning("OCR: Texterkennung von Seite %s dauerte zu lange", page_number)
            return ""
        if result.returncode != 0:
            # stderr enthält keine Dokumentinhalte, nur Meldungen (z. B. fehlendes Sprachpaket).
            msg = result.stderr.decode("utf-8", "replace").strip().splitlines()
            logger.warning(
                "OCR: tesseract-Fehler auf Seite %s: %s", n, msg[-1][:200] if msg else "?"
            )
            return ""
        return result.stdout.decode("utf-8", "replace")


# --- Extraktion je Format --------------------------------------------------------


def _extract_pdf(path: str, should_stop: Callable[[], bool], ocr, figures=None) -> list[Page]:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(path)
        if reader.is_encrypted:
            # Viele PDFs haben nur ein Besitzerpasswort (leeres Nutzerpasswort).
            try:
                ok = reader.decrypt("")
            except Exception:  # noqa: BLE001 - z. B. fehlende Kryptobibliothek
                ok = 0
            if not ok:
                raise ExtractionError(
                    "Das PDF ist passwortgeschützt und kann nicht gelesen werden."
                )
        count = len(reader.pages)
    except ExtractionError:
        raise
    except (PdfReadError, ValueError, OSError, KeyError, TypeError) as exc:
        logger.info("PDF nicht lesbar: %s", type(exc).__name__)
        raise ExtractionError("Das PDF ist beschädigt oder kann nicht gelesen werden.") from exc
    if count == 0:
        raise ExtractionError("Das PDF enthält keine Seiten.")
    if count > MAX_PDF_PAGES:
        raise ExtractionError(f"Das PDF hat zu viele Seiten (höchstens {MAX_PDF_PAGES}).")

    pages: list[Page] = []
    ocr_missing = False
    for index in range(count):
        if should_stop():
            raise Interrupted
        number = index + 1
        text = ""
        try:
            page = reader.pages[index]
            contents = page.get_contents()
            size = len(contents.get_data()) if contents is not None else 0
            if size <= MAX_PDF_CONTENT_STREAM:
                text = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001 - pypdf wirft vielerlei bei kaputten Seiten
            logger.info("PDF: Textebene von Seite %s nicht lesbar (%s)", number, type(exc).__name__)
        text = pdf_paragraphs(clean_text(text))
        used_ocr = False
        if _meaningful_chars(text) < MIN_PAGE_CHARS and not ocr_missing:
            try:
                ocr_text = clean_text(ocr(path, number))
            except OcrUnavailable:
                ocr_missing = True
                logger.warning("OCR nicht verfügbar (tesseract/pdftoppm fehlt)")
            else:
                if _meaningful_chars(ocr_text) > _meaningful_chars(text):
                    text, used_ocr = ocr_text, True
        if figures is not None and not used_ocr:
            # Abbildungen der Seite als eigene Absätze an ihrer Position (rag.figures).
            text = figures.pdf_page(reader.pages[index], number, text)
        pages.append(Page(number, text, used_ocr))

    if ocr_missing and not any(_meaningful_chars(p.text) for p in pages):
        raise ExtractionError(
            "Das PDF enthält keine Textebene (gescannt), und die Texterkennung (OCR) "
            "ist nicht installiert (Pakete tesseract-ocr, tesseract-ocr-deu, poppler-utils)."
        )
    return pages


def _extract_docx(path: str, figures=None) -> list[Page]:
    from docx import Document as DocxDocument
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        doc = DocxDocument(path)
    except Exception as exc:  # noqa: BLE001 - python-docx/lxml/zipfile werfen vielerlei
        logger.info("DOCX nicht lesbar: %s", type(exc).__name__)
        raise ExtractionError(
            "Das Word-Dokument ist beschädigt oder kann nicht gelesen werden."
        ) from exc

    # Ein Absatz je w:p (auch Überschriften), getrennt durch eine Leerzeile;
    # leere Absätze zählen nicht. Eine Tabelle ist ein Absatz mit einer Zeile
    # je Tabellenzeile. Weiche Zeilenumbrüche (w:br) bleiben im Absatz.
    parts: list[str] = []
    for block in doc.iter_inner_content():
        if isinstance(block, Paragraph):
            if block.text.strip():
                parts.append(block.text)
        elif isinstance(block, Table):
            rows: list[str] = []
            for row in block.rows:
                cells: list[str] = []
                for cell in row.cells:
                    value = " ".join(cell.text.split())
                    # Verbundene Zellen erscheinen mehrfach.
                    if value and (not cells or cells[-1] != value):
                        cells.append(value)
                if cells:
                    rows.append(" | ".join(cells))
            if rows:
                parts.append("\n".join(rows))
        if figures is not None:
            # Bilder des Absatzes bzw. der Tabelle als eigene Absätze danach (rag.figures).
            parts.extend(figures.docx_block(doc, block))
    return [Page(None, clean_text("\n\n".join(parts)))]


def _extract_text(path: str) -> list[Page]:
    data = Path(path).read_bytes()
    text = _decode_text(data)
    if text is None:
        raise ExtractionError("Die Textdatei enthält Binärdaten oder eine unbekannte Kodierung.")
    return [Page(None, clean_text(text))]


def extract(
    path: str,
    kind: str,
    *,
    should_stop: Callable[[], bool] | None = None,
    ocr: Callable[[str, int], str] | None = None,
    figures=None,
) -> list[Page]:
    """Text eines Dokuments je Seite. ``should_stop`` wird zwischen PDF-Seiten
    abgefragt (Abbruch -> ``Interrupted``). ``figures`` (``rag.figures.Collector``)
    sammelt Abbildungen und setzt Platzhalter-Absätze an ihre Position."""
    should_stop = should_stop or (lambda: False)
    if kind == KIND_PDF:
        return _extract_pdf(path, should_stop, ocr or ocr_pdf_page, figures)
    if kind == KIND_DOCX:
        return _extract_docx(path, figures)
    if kind == KIND_TEXT:
        return _extract_text(path)
    if kind == KIND_IMAGE:
        from .figures import extract_image

        return extract_image(path, should_stop, ocr or ocr_pdf_page, figures)
    raise UnsupportedFile(f"Dateityp nicht unterstützt. Erlaubt sind: {ALLOWED_LABEL}.")


# --- Bibliografische Metadaten (Vorbelegung fürs Zitieren) -------------------------

# Titel, die Programme selbst eintragen (Vorlagen, Druckertreiber) – kein echter Titel.
_JUNK_TITLE = re.compile(
    r"^(untitled|unbenannt|ohne titel|dokument\d*|document\d*|titel|title|"
    r"präsentation\d*|presentation\d*|powerpoint[- ]präsentation|folie \d+|"
    r"microsoft (word|powerpoint|excel) - .*|.*\.(docx?|pdf|txt|md|rtf|odt|xlsx?|pptx?))$",
    re.IGNORECASE,
)
_JUNK_AUTHOR = re.compile(
    r"^(admin|administrator|user|benutzer|windows-benutzer|microsoft office user|"
    r"owner|besitzer|unknown|unbekannt|author|autor|default|nutzer)$",
    re.IGNORECASE,
)
_AUTHOR_SPLIT = re.compile(r"\s*;\s*|\s+(?:und|and|&)\s+")
MAX_META_CHARS = 300


@dataclass
class Metadata:
    """Angaben aus der Datei selbst; leer, wenn nichts Brauchbares drinsteht."""

    title: str = ""
    authors: list[str] | None = None
    year: int | None = None
    doi: str = ""  # aus /doi, /Subject oder /Keywords (Verlags-PDFs, z. B. Springer)


def _meta_text(value) -> str:
    text = " ".join(str(value or "").replace("\x00", "").split())
    return text[:MAX_META_CHARS]


def plausible_title(value) -> str:
    """Titel aus Metadaten oder "" (leer, zu kurz, Dateiname, Vorlagenname)."""
    text = _meta_text(value)
    if sum(ch.isalpha() for ch in text) < 3 or _JUNK_TITLE.match(text):
        return ""
    return text


def plausible_authors(value) -> list[str]:
    """Autoren aus einem Metadatenfeld („A; B“, „A und B“), ohne Platzhalter."""
    authors = []
    for part in _AUTHOR_SPLIT.split(_meta_text(value)):
        part = part.strip(" ,")
        if sum(ch.isalpha() for ch in part) >= 2 and not _JUNK_AUTHOR.match(part):
            authors.append(part)
    return authors[:20]


def _plausible_year(value) -> int | None:
    year = getattr(value, "year", None)
    return year if isinstance(year, int) and 1450 <= year <= 2200 else None


def extract_metadata(path: str, kind: str) -> Metadata:
    """Titel, Autoren, Jahr (und DOI) aus PDF-Metadaten (Title, Author,
    CreationDate, doi/Subject) bzw. DOCX-Core-Properties (title, creator,
    created). Fehler -> leere Angaben; das Indexieren hängt nie davon ab."""
    try:
        if kind == KIND_PDF:
            from pypdf import PdfReader

            reader = PdfReader(path)
            if reader.is_encrypted:
                reader.decrypt("")
            info = reader.metadata
            if info is None:
                return Metadata()
            try:
                created = info.creation_date
            except Exception:  # noqa: BLE001 - ungültiges Datumsformat
                created = None
            doi_text = " ".join(
                str(info.get(key) or "") for key in ("/doi", "/Subject", "/Keywords")
            )
            return Metadata(
                plausible_title(info.title),
                plausible_authors(info.author),
                _plausible_year(created),
                citations.find_doi(doi_text),
            )
        if kind == KIND_DOCX:
            from docx import Document as DocxDocument

            props = DocxDocument(path).core_properties
            return Metadata(
                plausible_title(props.title),
                plausible_authors(props.author),
                _plausible_year(props.created),
            )
    except Exception as exc:  # noqa: BLE001 - Metadaten sind nur eine Vorbelegung
        logger.info("Metadaten nicht lesbar: %s", type(exc).__name__)
    return Metadata()
