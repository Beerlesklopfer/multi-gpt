"""Abbildungen indexieren: herauslösen, aussortieren, von einem Vision-Modell beschreiben.

Bisher gingen Bilder in PDFs mit Textebene und in Word-Dokumenten verloren,
Bilddateien wurden gar nicht angenommen. Mit ``RagSettings.describe_figures``
beschreibt ein Vision-Modell (``RagSettings.figure_model``, getrennt vom
OCR-Modell) jede Abbildung; die Beschreibung steht als eigener Absatz
„[Abbildung: …]“ an der Bildposition im Seitentext (wie bei olmOCR) und wird
mit eingebettet und durchsucht.

Ablauf in zwei Schritten:

1. Beim Extrahieren (``extract.extract(…, figures=Collector)``) sammelt der
   ``Collector`` die Bilder und setzt an ihre Stelle einen Platzhalter-Absatz
   (Zeichen aus dem Unicode-Bereich für private Nutzung, übersteht
   ``clean_text``). Aussortiert werden kleine Bilder (kürzere Kante unter
   ``figure_min_edge``, zu kleine Fläche), sehr schmale (Linien, Balken) und
   Wiederholungen (gleiche Bilddaten, z. B. ein Logo auf jeder Seite); danach
   gelten die Obergrenzen je Seite und je Dokument. Jedes Bild wird mit Pillow
   auf ``figure_max_edge`` verkleinert und neu kodiert – PNG bei wenigen Farben
   (Diagramme), sonst JPEG. Dabei fallen EXIF und andere Metadaten weg.
2. ``Collector.describe`` fragt das Modell je Abbildung (Abbruchprüfung und
   Lebenszeichen über ``should_stop`` vor jedem Aufruf) und ersetzt die
   Platzhalter durch die Beschreibung bzw. entfernt sie.

**PDF:** Bilder über pypdf statt ``pdfimages`` (poppler-utils). Gründe: pypdf
liest Breite und Höhe aus dem Bildobjekt, bevor es dekodiert – kleine Bilder
und Dekompressionsbomben fallen ohne Aufwand heraus; dasselbe Bildobjekt
(Logo auf jeder Seite) wird am Verweis erkannt; die Reihenfolge der
Zeichenbefehle (``Do``) im Inhaltsstrom liefert die Position im Text. Das
Programm ``pdfimages`` schreibt dagegen alle Bilder einer Seite ungefiltert als
Dateien und kennt keine Position. Nicht dekodierbare Formate (z. B. JBIG2, meist
gescannte Textseiten, die ohnehin zur OCR gehen) entfallen. Seiten, deren Text
per OCR entstand, bekommen keine Abbildungen (das Bild ist die Seite selbst;
olmOCR beschreibt Abbildungen dort schon).

Position in der PDF-Seite: Während einer zweiten Textextraktion zählt ein
Besucher die Buchstaben und Ziffern bis zu jedem ``Do``-Befehl; der Platzhalter
kommt ans Ende der Zeile, in der diese Zahl im aufbereiteten Seitentext
erreicht ist (Buchstaben und Ziffern ändert ``clean_text`` nicht).

**DOCX:** Bilder (``a:blip``, alt ``v:imagedata``) je Absatz bzw. Tabelle in
Dokumentreihenfolge, aufgelöst über die Beziehungen des Dokumentteils
(``word/media``). Die Größe des entpackten Dokuments prüft ``detect_kind``
(``MAX_DOCX_UNCOMPRESSED``), je Bild gelten ``MAX_SOURCE_BYTES`` und
``MAX_PIXELS``.

**Bilddateien** (JPG, PNG, TIFF auch mehrseitig, WEBP; HEIC bräuchte eine
zusätzliche Bibliothek und wird nicht angenommen): Jede Seite wird als
einseitiges PDF an die bestehende OCR gegeben (olmOCR bzw. Tesseract,
``ocr.page_reader``) und – wenn eingeschaltet – zusätzlich beschrieben.

**Bildunterschrift:** Beginnt eine Zeile nahe dem Platzhalter (zuerst darunter,
dann darüber) mit „Abb.“, „Abbildung“, „Fig.“ oder „Figure“, geht sie als
Hinweis an das Modell und steht vorn in der Beschreibung.

**Fehler** wie bei der OCR: Anbieter nicht erreichbar -> ``FigureError`` mit
``unreachable`` (der Auftrag wartet), vorübergehende Fehler -> ``retryable``.
Lehnt das Modell ein Bild ab (HTTP 400/413/415/422, abgeschnittene Antwort),
entfällt nur diese Abbildung (Log nur mit IDs).
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import tempfile
import warnings
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..models import AIModel, RagSettings
from ..providers import registry
from ..providers.base import ProviderError
from . import extract
from .ocr import OcrError

logger = logging.getLogger(__name__)

# Aussortieren: Fläche mindestens Faktor x Mindestkante², Seitenverhältnis höchstens …
MIN_AREA_FACTOR = 1.5
MAX_ASPECT = 6.0
# Schutz vor Dekompressionsbomben: Pixel je Bild (vor dem Dekodieren geprüft)
# und Größe der Bilddaten in DOCX.
MAX_PIXELS = 40_000_000
MAX_SOURCE_BYTES = 30 * 1024 * 1024
# Bilddateien: höchstens so viele Seiten (TIFF); für die OCR auf diese Kante verkleinert.
MAX_IMAGE_PAGES = 500
OCR_MAX_EDGE = 4000
OCR_PDF_DPI = 300
# Verschachtelte Formular-Objekte im PDF höchstens so tief durchsuchen.
MAX_FORM_DEPTH = 5
JPEG_QUALITY = 85
PNG_MAX_COLORS = 256

DESCRIBE_TEMPERATURE = 0.1
DESCRIBE_MAX_TOKENS = 700
MAX_DESCRIPTION_CHARS = 2000
MAX_CAPTION_CHARS = 300
CAPTION_WINDOW = 2  # nicht leere Zeilen unter bzw. über dem Platzhalter
# Ablehnung eines einzelnen Bildes (kein Vision-Modell, Bild zu groß, Inhalt abgelehnt).
REJECTED_STATUS = (400, 413, 415, 422)

FIGURE_PROMPT = (
    "Beschreibe die Abbildung aus einem Dokument sachlich, damit sie über eine Textsuche "
    "gefunden werden kann. Sage, was sie zeigt. Bei Diagrammen nenne die Art des "
    "Diagramms, die Achsen mit Größen und Einheiten, die wichtigsten Werte und erkennbare "
    "Trends. Gib Text, der in der Abbildung steht, wörtlich wieder. Beschreibe nur, was zu "
    "sehen ist: keine Vermutungen, keine Bewertung, nichts hinzuerfinden. Antworte mit 2 "
    "bis 6 Sätzen als Fließtext, ohne Überschrift, ohne Aufzählung und ohne Markdown. "
    "Schreibe in der Sprache des Dokuments: {language}."
)
CAPTION_HINT = " Die Bildunterschrift im Dokument lautet: „{caption}“."

MSG_NO_MODEL = "Zum Beschreiben der Abbildungen ist kein Modell eingerichtet (RAG-Einstellungen)."
MSG_PROVIDER_INACTIVE = "Der Anbieter des Modells für Abbildungen ist deaktiviert."
MSG_UNSUPPORTED = "Dieser Anbietertyp unterstützt keine Bild-Eingaben für Abbildungen."
MSG_FAILED = "Das Beschreiben einer Abbildung ist fehlgeschlagen."
MSG_IMAGE_UNREADABLE = "Die Bilddatei ist beschädigt oder kann nicht gelesen werden."
MSG_IMAGE_TOO_LARGE = "Das Bild ist zu groß (höchstens {} Megapixel)."
MSG_IMAGE_PAGES = f"Die Bilddatei hat zu viele Seiten (höchstens {MAX_IMAGE_PAGES})."
MSG_IMAGE_NO_OCR = (
    "Die Texterkennung (OCR) für Bilddateien ist nicht installiert "
    "(Pakete tesseract-ocr, tesseract-ocr-deu, poppler-utils)."
)

_MARKER = "{}"
_MARKER_RE = re.compile(r"(\d+)")
_CAPTION_RE = re.compile(r"^(?:Abb\.|Abbildung\b|Fig\.|Figure\b)\s*\S", re.IGNORECASE)
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_V = "{urn:schemas-microsoft-com:vml}"

# Häufige Wörter je Sprache für die Sprache der Beschreibung (sonst Deutsch).
_STOPWORDS = {
    "Deutsch": "der die das und ist nicht mit ein eine für auf den von zu im sich des dem auch",
    "Englisch": "the and of to is in that for with are this on be as by not",
    "Französisch": "le la les et des est une pour dans que pas sur du au avec",
    "Spanisch": "el los las y es una para que con por del se no lo como",
    "Italienisch": "il di che è per una con sono non del della gli al nel",
    "Niederländisch": "het een en van is dat op te voor met niet zijn ook bij",
}
_STOPWORD_SETS = {lang: set(words.split()) for lang, words in _STOPWORDS.items()}
LANGUAGE_SAMPLE = 20_000
LANGUAGE_MIN_HITS = 5


class FigureError(OcrError):
    """Abbildungen nicht beschreibbar; wie ``OcrError`` (``unreachable``/``retryable``)."""


@dataclass
class Figure:
    index: int
    page: int | None
    image: bytes
    mime_type: str


# --- Bilder prüfen und aufbereiten -------------------------------------------------


def _pil():
    from PIL import Image, ImageOps

    return Image, ImageOps


def open_image(data: bytes):
    """Bild mit Pillow öffnen und laden; None bei Fehler oder zu vielen Pixeln."""
    Image, _ = _pil()
    with warnings.catch_warnings():
        # Pillow warnt ab Image.MAX_IMAGE_PIXELS (bzw. wirft ab dem Doppelten);
        # hier gilt die strengere eigene Grenze, die Warnung wird zum Fehler.
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            image = Image.open(io.BytesIO(data))
            width, height = image.size
            if width * height > MAX_PIXELS:
                return None
            image.load()
        except Exception as exc:  # noqa: BLE001 - Pillow wirft je Format vielerlei
            logger.info("Abbildung nicht lesbar (%s)", type(exc).__name__)
            return None
    return image


def _flatten(image):
    """Ausrichtung (EXIF) anwenden, Transparenz auf Weiß, Modus RGB oder L."""
    Image, ImageOps = _pil()
    image = ImageOps.exif_transpose(image)
    if image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        image = background
    elif image.mode not in ("RGB", "L"):
        image = image.convert("L" if image.mode in ("1", "I", "I;16", "F") else "RGB")
    return image


def prepare(image, max_edge: int) -> tuple[bytes, str]:
    """Verkleinern und neu kodieren (ohne EXIF/Metadaten): (Bytes, MIME-Typ)."""
    Image, _ = _pil()
    image = _flatten(image)
    if max(image.size) > max_edge:
        image = image.copy()
        image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    image.info = {}  # keine Metadaten (EXIF, Text-Chunks, ICC) übernehmen
    out = io.BytesIO()
    if image.getcolors(PNG_MAX_COLORS) is not None:
        image.save(out, "PNG", optimize=True)
        return out.getvalue(), "image/png"
    image.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue(), "image/jpeg"


def _language(text: str) -> str:
    """Sprache des Dokuments grob an häufigen Wörtern; im Zweifel Deutsch."""
    words = re.findall(r"[^\W\d_]+", text[:LANGUAGE_SAMPLE].lower())
    hits = {lang: sum(w in stop for w in words) for lang, stop in _STOPWORD_SETS.items()}
    best = max(hits, key=lambda lang: hits[lang])
    if hits[best] < LANGUAGE_MIN_HITS or hits[best] == hits["Deutsch"]:
        return "Deutsch"
    return best


# --- Sammeln ---------------------------------------------------------------------------


class Collector:
    """Sammelt Abbildungen eines Dokuments beim Extrahieren (Schritt 1) und
    beschreibt sie danach (Schritt 2, ``describe``)."""

    def __init__(
        self,
        *,
        max_per_document: int = 50,
        max_per_page: int = 10,
        min_edge: int = 150,
        max_edge: int = 1024,
        document_id: int | None = None,
    ):
        self.max_per_document = max_per_document
        self.max_per_page = max_per_page
        self.min_edge = min_edge
        self.max_edge = max_edge
        self.document_id = document_id
        self.figures: list[Figure] = []
        self.skipped = 0
        self._hashes: set[str] = set()
        self._sources: set = set()
        self._per_page: Counter = Counter()

    @classmethod
    def from_settings(cls, cfg: RagSettings, document_id: int | None = None) -> Collector:
        return cls(
            max_per_document=cfg.figure_max_per_document,
            max_per_page=cfg.figure_max_per_page,
            min_edge=cfg.figure_min_edge,
            max_edge=cfg.figure_max_edge,
            document_id=document_id,
        )

    @property
    def full(self) -> bool:
        return len(self.figures) >= self.max_per_document

    def _page_full(self, page: int | None) -> bool:
        return page is not None and self._per_page[page] >= self.max_per_page

    def size_ok(self, width: int, height: int) -> bool:
        """Mindestkante, Mindestfläche, kein extremes Seitenverhältnis (Linie)."""
        short, long = sorted((int(width), int(height)))
        if short < self.min_edge or width * height < MIN_AREA_FACTOR * self.min_edge**2:
            return False
        return long <= MAX_ASPECT * short

    def _seen(self, key) -> bool:
        """Quelle (Bildobjekt, Beziehung) schon verarbeitet? Merkt sie sich."""
        if key is None:
            return False
        if key in self._sources:
            return True
        self._sources.add(key)
        return False

    def add(self, image, page: int | None) -> str | None:
        """Bild prüfen und übernehmen: Platzhalter-Absatz oder None (aussortiert)."""
        if self.full or self._page_full(page) or not self.size_ok(*image.size):
            self.skipped += 1
            return None
        try:
            data, mime = prepare(image, self.max_edge)
        except Exception as exc:  # noqa: BLE001 - seltene Bildmodi, kaputte Daten
            logger.info("Abbildung nicht aufbereitet (%s)", type(exc).__name__)
            self.skipped += 1
            return None
        digest = hashlib.sha256(data).hexdigest()
        if digest in self._hashes:
            self.skipped += 1
            return None
        self._hashes.add(digest)
        figure = Figure(len(self.figures), page, data, mime)
        self.figures.append(figure)
        if page is not None:
            self._per_page[page] += 1
        return _MARKER.format(figure.index)

    def add_bytes(self, data: bytes, page: int | None, key=None) -> str | None:
        if self.full or self._seen(key) or len(data) > MAX_SOURCE_BYTES:
            self.skipped += 1
            return None
        image = open_image(data)
        if image is None:
            self.skipped += 1
            return None
        return self.add(image, page)

    # --- PDF ---

    def pdf_page(self, page, number: int, text: str) -> str:
        """Bilder einer PDF-Seite (pypdf ``PageObject``) als Platzhalter einfügen."""
        if self.full or self._page_full(number):
            return text
        try:
            images = _pdf_images(page)
        except Exception as exc:  # noqa: BLE001 - kaputte Ressourcen
            logger.info("PDF: Bilder von Seite %s nicht lesbar (%s)", number, type(exc).__name__)
            return text
        if not images:
            return text
        try:
            order = _pdf_draw_order(page)
        except Exception as exc:  # noqa: BLE001 - dann alle Bilder ans Seitenende
            logger.info("PDF: Bildposition auf Seite %s unklar (%s)", number, type(exc).__name__)
            order = []
        placed: list[tuple[int | None, str]] = []
        for xobj, position in _in_draw_order(images, order):
            if self.full or self._page_full(number):
                break
            ref = getattr(xobj, "indirect_reference", None)
            if self._seen(("pdf", ref.idnum, ref.generation) if ref is not None else None):
                self.skipped += 1
                continue
            try:
                width, height = int(xobj.get("/Width", 0)), int(xobj.get("/Height", 0))
            except (TypeError, ValueError):
                continue
            if not self.size_ok(width, height) or width * height > MAX_PIXELS:
                self.skipped += 1
                continue
            try:
                image = xobj.decode_as_image()
            except Exception as exc:  # noqa: BLE001 - z. B. JBIG2, JPX, kaputte Daten
                logger.info(
                    "PDF: Bild auf Seite %s nicht dekodierbar (%s)", number, type(exc).__name__
                )
                self.skipped += 1
                continue
            if image is None:
                continue
            marker = self.add(image, number)
            if marker is not None:
                placed.append((position, marker))
        return insert_markers(text, placed)

    # --- DOCX ---

    def docx_block(self, doc, block) -> list[str]:
        """Bilder eines Absatzes bzw. einer Tabelle (python-docx) als Platzhalter."""
        markers: list[str] = []
        related = doc.part.related_parts
        for element in block._element.iter(f"{_A}blip", f"{_V}imagedata"):
            if self.full:
                break
            rid = element.get(f"{_R}embed") or element.get(f"{_R}id")
            part = related.get(rid) if rid else None
            blob = getattr(part, "blob", None)
            if not blob:
                continue  # verknüpftes (externes) Bild oder fehlender Teil
            marker = self.add_bytes(blob, None, key=("docx", getattr(part, "partname", rid)))
            if marker is not None:
                markers.append(marker)
        return markers

    # --- Beschreiben ---

    def describe(
        self,
        pages: list[extract.Page],
        should_stop: Callable[[], bool] | None = None,
        cfg: RagSettings | None = None,
    ) -> int:
        """Platzhalter durch Beschreibungen ersetzen; Rückgabe: Anzahl beschriebener.

        Wirft ``FigureError`` (Anbieter, Einrichtung) oder ``extract.Interrupted``.
        """
        should_stop = should_stop or (lambda: False)
        descriptions: dict[int, str] = {}
        if self.figures:
            ai_model = figure_model(cfg)
            adapter = registry.get_adapter(ai_model.provider)
            language = _language("\n".join(p.text for p in pages))
            captions = _captions(pages)
            for figure in self.figures:
                if should_stop():
                    raise extract.Interrupted
                caption = captions.get(figure.index, "")
                text = self._describe_one(adapter, ai_model, figure, caption, language)
                if text:
                    lead = f"{caption} – " if caption else ""
                    descriptions[figure.index] = f"[Abbildung: {lead}{text}]"
        for page in pages:
            page.text = replace_markers(page.text, descriptions)
        if self.figures:
            logger.info(
                "Dokument %s: %d von %d Abbildungen beschrieben, %d aussortiert",
                self.document_id,
                len(descriptions),
                len(self.figures),
                self.skipped,
            )
        return len(descriptions)

    def _describe_one(self, adapter, ai_model: AIModel, figure: Figure, caption, language) -> str:
        prompt = FIGURE_PROMPT.format(language=language)
        if caption:
            prompt += CAPTION_HINT.format(caption=caption)
        try:
            answer = adapter.describe_image(
                ai_model.model_id,
                figure.image,
                prompt,
                mime_type=figure.mime_type,
                temperature=DESCRIBE_TEMPERATURE,
                max_tokens=DESCRIBE_MAX_TOKENS,
            )
        except NotImplementedError:
            raise FigureError(MSG_UNSUPPORTED) from None
        except ProviderError as exc:
            status = getattr(exc, "status", None)
            if getattr(exc, "truncated", False) or status in REJECTED_STATUS:
                logger.warning(
                    "Dokument %s: Abbildung %s (Seite %s) von Modell %s abgelehnt (%s), entfällt",
                    self.document_id,
                    figure.index,
                    figure.page,
                    ai_model.pk,
                    f"HTTP {status}" if status else "abgeschnitten",
                )
                return ""
            raise FigureError(
                f"Modell für Abbildungen: {exc or MSG_FAILED}",
                retryable=exc.retryable,
                unreachable=getattr(exc, "unreachable", False),
            ) from None
        except Exception as exc:
            logger.error("Abbildung mit Modell %s: %s", ai_model.pk, type(exc).__name__)
            raise FigureError(MSG_FAILED, retryable=True) from None
        return clean_description(answer)


def collector_for(cfg: RagSettings | None = None, document_id: int | None = None):
    """``Collector`` nach den RAG-Einstellungen oder None (Beschreibung aus)."""
    cfg = cfg or RagSettings.load()
    if not cfg.describe_figures:
        return None
    return Collector.from_settings(cfg, document_id)


def figure_model(cfg: RagSettings | None = None) -> AIModel:
    """Das eingestellte Modell für Abbildungen samt Anbieter oder ``FigureError``.

    ``AIModel.active`` wird wie beim OCR-Modell nicht geprüft.
    """
    cfg = cfg or RagSettings.load()
    if cfg.figure_model_id is None:
        raise FigureError(MSG_NO_MODEL)
    ai_model = AIModel.objects.select_related("provider").filter(pk=cfg.figure_model_id).first()
    if ai_model is None:
        raise FigureError(MSG_NO_MODEL)
    if not ai_model.provider.active:
        raise FigureError(MSG_PROVIDER_INACTIVE)
    return ai_model


# --- Text: Platzhalter, Bildunterschrift, Beschreibung ---------------------------------


def clean_description(answer: str) -> str:
    """Eine Zeile ohne Markdown-Reste und eckige Klammern, gekürzt."""
    text = re.sub(r"[*_#`]+", "", answer or "")
    text = text.replace("[", "(").replace("]", ")")
    text = _MARKER_RE.sub("", text).replace("", "").replace("", "")
    text = " ".join(text.split())
    if len(text) > MAX_DESCRIPTION_CHARS:
        text = text[: MAX_DESCRIPTION_CHARS - 1].rsplit(" ", 1)[0] + " …"
    return text


def insert_markers(text: str, placed: list[tuple[int | None, str]]) -> str:
    """Platzhalter als eigene Absätze einfügen.

    ``position`` = Zahl der Buchstaben/Ziffern vor dem Bild; eingefügt wird am
    Ende der Zeile, in der sie erreicht ist (0 = Seitenanfang, None = Seitenende).
    """
    if not placed:
        return text
    offsets: list[tuple[int, int, str]] = []
    for order, (position, marker) in enumerate(placed):
        if position is None:
            offset = len(text)
        elif position <= 0:
            offset = 0
        else:
            seen = 0
            offset = len(text)
            for i, ch in enumerate(text):
                if ch.isalnum():
                    seen += 1
                    if seen >= position:
                        end = text.find("\n", i)
                        offset = len(text) if end < 0 else end
                        break
        offsets.append((offset, order, marker))
    out, last = [], 0
    for offset, _order, marker in sorted(offsets):
        out.append(text[last:offset])
        out.append(f"\n\n{marker}\n\n")
        last = offset
    out.append(text[last:])
    return re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()


def replace_markers(text: str, descriptions: dict[int, str]) -> str:
    """Platzhalter-Absätze durch Beschreibungen ersetzen bzw. samt Leerzeilen entfernen."""
    if "" not in text:
        return text
    text = _MARKER_RE.sub(lambda m: descriptions.get(int(m.group(1)), ""), text)
    return re.sub(r"\n[ \t]*(?:\n[ \t]*)+", "\n\n", text).strip()


def _caption_at(lines: list[str], index: int, used: set) -> int | None:
    """Zeile mit Bildunterschrift nahe ``index``: erst darunter, dann darüber."""
    for step in (1, -1):
        i, checked = index + step, 0
        while 0 <= i < len(lines) and checked < CAPTION_WINDOW:
            line = lines[i].strip()
            if line and not _MARKER_RE.search(line):
                checked += 1
                if i not in used and _CAPTION_RE.match(line):
                    return i
            i += step
    return None


def _captions(pages: list[extract.Page]) -> dict[int, str]:
    """Bildunterschrift je Abbildung (Index -> Text); jede Zeile nur einmal."""
    result: dict[int, str] = {}
    for page in pages:
        if "" not in page.text:
            continue
        lines = page.text.split("\n")
        used: set = set()
        for index, line in enumerate(lines):
            match = _MARKER_RE.search(line)
            if not match:
                continue
            found = _caption_at(lines, index, used)
            if found is not None:
                used.add(found)
                caption = " ".join(lines[found].split())
                if len(caption) > MAX_CAPTION_CHARS:
                    caption = caption[: MAX_CAPTION_CHARS - 1] + "…"
                result[int(match.group(1))] = caption.replace("[", "(").replace("]", ")")
    return result


# --- PDF: Bildobjekte und Zeichenreihenfolge -------------------------------------------


def _pdf_images(page) -> list[tuple[str, object]]:
    """Bildobjekte der Seite samt verschachtelter Formulare: [(Name, Objekt)]."""
    found: list[tuple[str, object]] = []
    visited: set[int] = set()

    def walk(resources, depth: int) -> None:
        if resources is None:
            return
        resources = resources.get_object()
        xobjects = resources.get("/XObject")
        if xobjects is None:
            return
        for name, ref in xobjects.get_object().items():
            obj = ref.get_object()
            subtype = obj.get("/Subtype")
            if subtype == "/Image":
                if not obj.get("/ImageMask"):  # Schablonenmasken sind keine Bilder
                    found.append((str(name), obj))
            elif subtype == "/Form" and depth < MAX_FORM_DEPTH and id(obj) not in visited:
                visited.add(id(obj))
                walk(obj.get("/Resources"), depth + 1)

    walk(page.get("/Resources"), 0)
    return found


def _pdf_draw_order(page) -> list[tuple[str, int]]:
    """Reihenfolge der ``Do``-Befehle mit der Zahl der Buchstaben/Ziffern davor."""
    count = 0
    order: list[tuple[str, int]] = []

    def on_text(text, *_args):
        nonlocal count
        count += sum(ch.isalnum() for ch in text or "")

    def after(operator, operands, *_args):
        if operator == b"Do" and operands:
            order.append((str(operands[0]), count))

    page.extract_text(visitor_operand_after=after, visitor_text=on_text)
    return order


def _in_draw_order(images, order) -> list[tuple[object, int | None]]:
    """Bilder in Zeichenreihenfolge mit Position; nie gezeichnete ans Seitenende."""
    pending = list(images)
    result: list[tuple[object, int | None]] = []
    for name, position in order:
        for i, (image_name, obj) in enumerate(pending):
            if image_name == name:
                result.append((obj, position))
                del pending[i]
                break
    result.extend((obj, None) for _name, obj in pending)
    return result


# --- Bilddateien als Dokument --------------------------------------------------------------


def _frame_pdf(frame, target: Path) -> None:
    """Eine Bildseite als einseitiges PDF (300 dpi) für die bestehende OCR."""
    Image, _ = _pil()
    image = _flatten(frame)
    if max(image.size) > OCR_MAX_EDGE:
        image = image.copy()
        image.thumbnail((OCR_MAX_EDGE, OCR_MAX_EDGE), Image.Resampling.LANCZOS)
    image.info = {}
    image.save(target, "PDF", resolution=OCR_PDF_DPI, quality=95)


def extract_image(
    path: str,
    should_stop: Callable[[], bool],
    ocr: Callable[[str, int], str],
    figures: Collector | None = None,
) -> list[extract.Page]:
    """Bilddatei lesen: je Seite (TIFF) OCR-Text, mit ``figures`` auch die Abbildung.

    Seitenzahlen nur bei mehrseitigen Dateien (sonst None wie bei DOCX).
    """
    Image, _ = _pil()
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            image = Image.open(path)
            frames = int(getattr(image, "n_frames", 1) or 1)
        except Exception as exc:  # noqa: BLE001 - Pillow wirft je Format vielerlei
            logger.info("Bilddatei nicht lesbar: %s", type(exc).__name__)
            raise extract.ExtractionError(MSG_IMAGE_UNREADABLE) from exc
        if frames > MAX_IMAGE_PAGES:
            raise extract.ExtractionError(MSG_IMAGE_PAGES)
        pages: list[extract.Page] = []
        ocr_missing = False
        with image, tempfile.TemporaryDirectory(prefix="mgpt-img-") as tmp:
            for index in range(frames):
                if should_stop():
                    raise extract.Interrupted
                try:
                    image.seek(index)
                    width, height = image.size
                    if width * height > MAX_PIXELS:
                        raise extract.ExtractionError(
                            MSG_IMAGE_TOO_LARGE.format(MAX_PIXELS // 1_000_000)
                        )
                    frame = image.copy()  # lädt nur diese Seite
                except extract.ExtractionError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.info("Bildseite %s nicht lesbar: %s", index + 1, type(exc).__name__)
                    raise extract.ExtractionError(MSG_IMAGE_UNREADABLE) from exc
                number = index + 1 if frames > 1 else None
                text = ""
                if not ocr_missing:
                    target = Path(tmp) / "page.pdf"
                    _frame_pdf(frame, target)
                    try:
                        text = extract.clean_text(ocr(str(target), 1))
                    except extract.OcrUnavailable:
                        ocr_missing = True
                        logger.warning("OCR nicht verfügbar (tesseract/pdftoppm fehlt)")
                parts = []
                if figures is not None:
                    marker = figures.add(frame, number)
                    if marker is not None:
                        parts.append(marker)
                if text:
                    parts.append(text)
                pages.append(extract.Page(number, "\n\n".join(parts), bool(text)))
    if ocr_missing and not any(p.text for p in pages):
        raise extract.ExtractionError(MSG_IMAGE_NO_OCR)
    return pages
