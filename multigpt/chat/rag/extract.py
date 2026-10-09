"""Dateityp-Erkennung und Textextraktion für die Indexierung (M7-03, Agent ingest).

Unterstützt PDF (Textebene, Seiten ohne Text per OCR), DOCX, TXT und MD.

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

logger = logging.getLogger(__name__)

KIND_PDF = "pdf"
KIND_DOCX = "docx"
KIND_TEXT = "text"

# Endungen je erkanntem Inhalt.
EXTENSIONS = {
    KIND_PDF: (".pdf",),
    KIND_DOCX: (".docx",),
    KIND_TEXT: (".txt", ".md"),
}
ALLOWED_EXTENSIONS = tuple(ext for exts in EXTENSIONS.values() for ext in exts)
ALLOWED_LABEL = "PDF, DOCX, TXT, MD"

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


def detect_kind(fileobj, filename: str) -> str:
    """Art des Inhalts (``pdf``/``docx``/``text``) prüfen; muss zur Endung passen.

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

    if b"%PDF-" in head[:1024]:
        kind = KIND_PDF
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


def _extract_pdf(path: str, should_stop: Callable[[], bool], ocr) -> list[Page]:
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
        text = clean_text(text)
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
        pages.append(Page(number, text, used_ocr))

    if ocr_missing and not any(_meaningful_chars(p.text) for p in pages):
        raise ExtractionError(
            "Das PDF enthält keine Textebene (gescannt), und die Texterkennung (OCR) "
            "ist nicht installiert (Pakete tesseract-ocr, tesseract-ocr-deu, poppler-utils)."
        )
    return pages


def _extract_docx(path: str) -> list[Page]:
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

    parts: list[str] = []
    for block in doc.iter_inner_content():
        if isinstance(block, Paragraph):
            parts.append(block.text)
        elif isinstance(block, Table):
            for row in block.rows:
                cells: list[str] = []
                for cell in row.cells:
                    value = cell.text.strip()
                    # Verbundene Zellen erscheinen mehrfach.
                    if value and (not cells or cells[-1] != value):
                        cells.append(value)
                if cells:
                    parts.append(" | ".join(cells))
            parts.append("")
    return [Page(None, clean_text("\n".join(parts)))]


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
) -> list[Page]:
    """Text eines Dokuments je Seite. ``should_stop`` wird zwischen PDF-Seiten
    abgefragt (Abbruch -> ``Interrupted``)."""
    should_stop = should_stop or (lambda: False)
    if kind == KIND_PDF:
        return _extract_pdf(path, should_stop, ocr or ocr_pdf_page)
    if kind == KIND_DOCX:
        return _extract_docx(path)
    if kind == KIND_TEXT:
        return _extract_text(path)
    raise UnsupportedFile(f"Dateityp nicht unterstützt. Erlaubt sind: {ALLOWED_LABEL}.")
