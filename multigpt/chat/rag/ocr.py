"""Texterkennung gescannter PDF-Seiten: olmOCR oder Tesseract (M7-09).

**olmOCR** (https://github.com/allenai/olmocr, Modell ``allenai/olmOCR-2-7B-1025``,
in LM Studio z. B. ``allenai/olmocr-2-7b``) ist ein Vision-Modell, das eine
Seite als Bild bekommt und den Text als Markdown liefert – mit Tabellen (als
HTML), Spalten in Lesereihenfolge und Formeln (LaTeX). Ablauf wie in der
olmOCR-Pipeline (``olmocr/pipeline.py``, ``build_page_query``):

- Seite mit ``pdftoppm`` rendern, längste Kante 1288 Pixel
  (``--target_longest_image_dim`` Standard 1288, Modellkarte ebenso).
- Eine Nutzernachricht: erst der Prompt ``build_no_anchoring_v4_yaml_prompt``
  (wörtlich in ``OLMOCR_PROMPT``, kein „anchor text“ nötig), dann das Bild als
  ``data:image/png;base64,…``; ``max_tokens`` 8000, Temperatur 0,1 (bei
  abgeschnittener Antwort ein zweiter Versuch mit 0,2, wie die Pipeline).
- Antwort: YAML-Front-Matter (``primary_language``, ``is_rotation_valid``,
  ``rotation_correction``, ``is_table``, ``is_diagram``) zwischen ``---``, danach
  der Text. Meldet das Modell eine falsche Drehung, wird die Seite einmal
  gedreht neu gelesen.

Übernommen wird der Text ohne Front-Matter; HTML-Tabellen werden zu Zeilen
„Zelle | Zelle“, Abbildungs-Platzhalter ``![Beschreibung](page_….png)`` zur
Beschreibung.

**Tesseract** liest die Seite lokal (``extract.ocr_pdf_page``). Mit
``RagSettings.ocr_fallback_tesseract`` springt es ein, wenn das OCR-Modell nicht
erreichbar ist oder einen Fehler meldet; ohne Ersatz wartet die Indexierung
(``OcrError`` mit ``unreachable``/``retryable``).
"""

from __future__ import annotations

import html
import logging
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

from ..models import AIModel, RagSettings
from ..providers import registry
from ..providers.base import ProviderError
from . import extract

logger = logging.getLogger(__name__)

# Wörtlich aus olmocr/prompts/prompts.py, build_no_anchoring_v4_yaml_prompt().
OLMOCR_PROMPT = (
    "Attached is one page of a document that you must process. "
    "Just return the plain text representation of this document as if you were reading it "
    "naturally. Convert equations to LateX and tables to HTML.\n"
    "If there are any figures or charts, label them with the following markdown syntax "
    "![Alt text describing the contents of the figure](page_startx_starty_width_height.png)\n"
    "Return your output as markdown, with a front matter section on top specifying values for "
    "the primary_language, is_rotation_valid, rotation_correction, is_table, and is_diagram "
    "parameters."
)
OLMOCR_LONGEST_DIM = 1288
OLMOCR_MAX_TOKENS = 8000
# Pipeline: TEMPERATURE_BY_ATTEMPT = [0.1, 0.1, 0.2, …]; hier höchstens zwei Versuche.
OLMOCR_TEMPERATURES = (0.1, 0.2)
RENDER_TIMEOUT = 120  # Sekunden

MSG_NO_MODEL = "Für olmOCR ist kein OCR-Modell eingerichtet (RAG-Einstellungen)."
MSG_PROVIDER_INACTIVE = "Der Anbieter des OCR-Modells ist deaktiviert."
MSG_RENDER_MISSING = "Das Programm pdftoppm fehlt (Paket poppler-utils)."
MSG_RENDER_FAILED = "Die Seite konnte nicht als Bild gerendert werden."
MSG_UNSUPPORTED = "Dieser Anbietertyp unterstützt keine Bild-Eingaben."
MSG_FAILED = "Die Texterkennung mit dem OCR-Modell ist fehlgeschlagen."


class OcrError(Exception):
    """OCR über das Vision-Modell nicht möglich; ``message`` deutsch, ohne Key.

    ``page_only``: betrifft nur diese Seite (z. B. abgeschnittene Antwort) –
    ohne Ersatz bleibt die Seite dann leer, statt das Dokument scheitern zu lassen.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        unreachable: bool = False,
        page_only: bool = False,
    ):
        super().__init__(message)
        self.message = message
        self.retryable = retryable or unreachable
        self.unreachable = unreachable
        self.page_only = page_only


# --- Rendern ---------------------------------------------------------------------


def _rotated_copy(pdf_path: str, page_number: int, rotation: int, target: Path) -> str:
    """Einseitiges PDF mit gedrehter Seite (pypdf, ohne Bildbibliothek)."""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(pdf_path)
    if reader.is_encrypted:
        reader.decrypt("")
    writer = PdfWriter()
    page = writer.add_page(reader.pages[page_number - 1])
    # olmOCR dreht mit PIL ``ROTATE_<n>`` (gegen den Uhrzeigersinn); pypdf
    # dreht positiv im Uhrzeigersinn.
    page.rotate((360 - rotation) % 360)
    with open(target, "wb") as fh:
        writer.write(fh)
    return str(target)


def render_page(
    pdf_path: str, page_number: int, *, longest: int = OLMOCR_LONGEST_DIM, rotation: int = 0
) -> bytes:
    """PDF-Seite als PNG, längste Kante ``longest`` Pixel (``pdftoppm -scale-to``)."""
    with tempfile.TemporaryDirectory(prefix="mgpt-olmocr-") as tmp:
        source = pdf_path
        number = page_number
        if rotation % 360:
            try:
                source = _rotated_copy(pdf_path, page_number, rotation, Path(tmp) / "rot.pdf")
            except Exception as exc:  # noqa: BLE001 - pypdf wirft vielerlei
                logger.info("olmOCR: Drehen von Seite %s nicht möglich (%s)", page_number, exc)
                raise OcrError(MSG_RENDER_FAILED, page_only=True) from None
            number = 1
        base = str(Path(tmp) / "page")
        n = str(number)
        cmd = ["pdftoppm", "-png", "-f", n, "-l", n, "-scale-to", str(longest), "-singlefile"]
        try:
            result = subprocess.run(
                [*cmd, source, base],
                capture_output=True,
                timeout=RENDER_TIMEOUT,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            raise OcrError(MSG_RENDER_MISSING) from None
        except subprocess.TimeoutExpired:
            raise OcrError(MSG_RENDER_FAILED, page_only=True) from None
        image = Path(base + ".png")
        if result.returncode != 0 or not image.exists():
            logger.warning("olmOCR: pdftoppm-Fehler auf Seite %s (Code %s)", n, result.returncode)
            raise OcrError(MSG_RENDER_FAILED, page_only=True)
        return image.read_bytes()


# --- Antwort aufbereiten -----------------------------------------------------------


@dataclass
class OlmOcrPage:
    text: str
    front_matter: dict
    rotation_valid: bool = True
    rotation_correction: int = 0


_FRONT_MATTER = re.compile(r"\A\s*---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.DOTALL)
_FIGURE = re.compile(r"!\[([^\]]*)\]\([^)\s]*\)")
_TABLE = re.compile(r"<table\b.*?</table>", re.DOTALL | re.IGNORECASE)


def _yaml_value(raw: str):
    value = raw.strip().strip("'\"")
    lowered = value.lower()
    if lowered in ("true", "yes"):
        return True
    if lowered in ("false", "no"):
        return False
    if lowered in ("null", "none", "~", ""):
        return None
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


class _TableText(HTMLParser):
    """HTML-Tabelle -> Zeilen „Zelle | Zelle“ (Text bleibt, Tags fallen weg)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            if self._row is None:
                self._row = []
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(self._row):
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _table_to_text(match: re.Match) -> str:
    parser = _TableText()
    try:
        parser.feed(match.group(0))
        parser.close()
    except Exception:  # noqa: BLE001 - kaputtes HTML: Tags einfach entfernen
        return html.unescape(re.sub(r"<[^>]+>", " ", match.group(0)))
    # Eigener Absatz (Leerzeilen davor und danach, wichtig für die Absatzzählung).
    return "\n\n" + "\n".join(" | ".join(row) for row in parser.rows) + "\n\n"


def parse_olmocr(raw: str) -> OlmOcrPage:
    """Front-Matter abtrennen und auswerten; Text für die Indexierung aufbereiten."""
    raw = (raw or "").replace("\r\n", "\n")
    front: dict = {}
    body = raw
    match = _FRONT_MATTER.match(raw)
    if match:
        for line in match.group(1).splitlines():
            key, sep, value = line.partition(":")
            if sep and key.strip():
                front[key.strip()] = _yaml_value(value)
        body = raw[match.end() :]
    body = _TABLE.sub(_table_to_text, body)
    body = _FIGURE.sub(
        lambda m: f"[Abbildung: {m.group(1).strip()}]" if m.group(1).strip() else "", body
    )
    rotation = front.get("rotation_correction")
    return OlmOcrPage(
        text=body.strip(),
        front_matter=front,
        rotation_valid=front.get("is_rotation_valid") is not False,
        rotation_correction=rotation if rotation in (0, 90, 180, 270) else 0,
    )


# --- olmOCR über den Anbieter ---------------------------------------------------------


MSG_NO_VISION = (
    " – das Modell hat das Seitenbild abgelehnt; vermutlich kann es keine Bilder lesen "
    "(kein Vision-Modell). Für OCR ein Vision-Modell wie allenai/olmocr-2-7b wählen."
)


def _provider_error(exc: ProviderError) -> OcrError:
    message = f"OCR-Modell: {exc or MSG_FAILED}"
    if getattr(exc, "status", None) == 400:
        message += MSG_NO_VISION
    if getattr(exc, "truncated", False):
        return OcrError(message, page_only=True)
    return OcrError(
        message,
        retryable=exc.retryable,
        unreachable=getattr(exc, "unreachable", False),
    )


def _ask(ai_model: AIModel, image: bytes) -> str:
    adapter = registry.get_adapter(ai_model.provider)
    last: ProviderError | None = None
    for temperature in OLMOCR_TEMPERATURES:
        try:
            return adapter.describe_image(
                ai_model.model_id,
                image,
                OLMOCR_PROMPT,
                temperature=temperature,
                max_tokens=OLMOCR_MAX_TOKENS,
            )
        except NotImplementedError:
            raise OcrError(MSG_UNSUPPORTED) from None
        except ProviderError as exc:
            if not getattr(exc, "truncated", False):
                raise _provider_error(exc) from None
            last = exc  # abgeschnitten (Wiederholschleife?) -> etwas wärmer erneut
        except Exception as exc:
            logger.error("olmOCR mit Modell %s: %s", ai_model.pk, type(exc).__name__)
            raise OcrError(MSG_FAILED, retryable=True) from None
    raise _provider_error(last)  # type: ignore[arg-type]


def olmocr_page(ai_model: AIModel, pdf_path: str, page_number: int) -> str:
    """Eine Seite mit olmOCR lesen; Text ohne Front-Matter. Fehler: ``OcrError``."""
    page = parse_olmocr(_ask(ai_model, render_page(pdf_path, page_number)))
    if not page.rotation_valid and page.rotation_correction:
        try:
            image = render_page(pdf_path, page_number, rotation=page.rotation_correction)
            rotated = parse_olmocr(_ask(ai_model, image))
        except OcrError as exc:
            if exc.unreachable:
                raise
            return page.text
        if rotated.rotation_valid or len(rotated.text) > len(page.text):
            return rotated.text
    return page.text


def ocr_model(cfg: RagSettings | None = None) -> AIModel:
    """Das eingestellte OCR-Modell samt Anbieter oder ``OcrError``.

    ``AIModel.active`` wird bewusst nicht geprüft: Das Modell darf für den Chat
    ausgeblendet sein und trotzdem für OCR dienen.
    """
    cfg = cfg or RagSettings.load()
    if cfg.ocr_model_id is None:
        raise OcrError(MSG_NO_MODEL)
    ai_model = AIModel.objects.select_related("provider").filter(pk=cfg.ocr_model_id).first()
    if ai_model is None:
        raise OcrError(MSG_NO_MODEL)
    if not ai_model.provider.active:
        raise OcrError(MSG_PROVIDER_INACTIVE)
    return ai_model


def page_reader(cfg: RagSettings | None = None) -> Callable[[str, int], str]:
    """OCR-Funktion ``(pdf_path, seite) -> text`` nach den RAG-Einstellungen.

    olmOCR mit Ersatz: Scheitert eine Seite (Anbieter offline, Fehler), liest
    Tesseract sie; nach „nicht erreichbar“ übernimmt Tesseract alle weiteren
    Seiten dieses Dokuments (kein erneutes Warten auf den Verbindungsaufbau).
    Ohne Ersatz: ``OcrError`` (offline -> der Worker wartet und wiederholt);
    nur seitenbezogene Fehler (abgeschnittene Antwort) lassen die Seite leer.
    """
    cfg = cfg or RagSettings.load()
    if cfg.ocr_backend != RagSettings.OcrBackend.OLMOCR:
        return extract.ocr_pdf_page
    fallback = cfg.ocr_fallback_tesseract
    try:
        ai_model = ocr_model(cfg)
        setup_error = None
    except OcrError as exc:
        ai_model, setup_error = None, exc
    state = {"offline": False}

    def read(pdf_path: str, page_number: int) -> str:
        use_tesseract = fallback and extract.ocr_available()
        if state["offline"] and use_tesseract:
            return extract.ocr_pdf_page(pdf_path, page_number)
        try:
            if setup_error is not None:
                raise setup_error
            return olmocr_page(ai_model, pdf_path, page_number)
        except OcrError as exc:
            if use_tesseract:
                logger.warning(
                    "olmOCR für Seite %s nicht möglich, Tesseract springt ein", page_number
                )
                state["offline"] = state["offline"] or exc.unreachable
                return extract.ocr_pdf_page(pdf_path, page_number)
            if exc.page_only:
                logger.warning("olmOCR: Seite %s ohne Text (%s)", page_number, exc.message)
                return ""
            raise

    return read


# --- Test im Admin ----------------------------------------------------------------------

CHECK_LINES = [
    "OCR-Test MultiGPT",
    "Die Kaltmiete beträgt 850 Euro im Monat.",
    "Prüfsumme: Größe 4711",
]
CHECK_EXPECTED = ("850", "4711")


def _check_pdf() -> bytes:
    """Kleines PDF mit Textzeilen (Helvetica, WinAnsi) – wird nur gerendert."""
    ops = ["BT", "/F1 28 Tf", "72 700 Td", "40 TL"]
    for line in CHECK_LINES:
        ops.append("(" + line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ") '")
    ops.append("ET")
    content = "\n".join(ops).encode("cp1252")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>",
        f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(out)


def _short(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


PROGRAM_PACKAGES = {
    "tesseract": "Tesseract ist nicht installiert (Pakete tesseract-ocr, tesseract-ocr-deu)",
    "pdftoppm": "pdftoppm fehlt (Paket poppler-utils)",
}


def missing_programs() -> list[str]:
    """Genau benannte fehlende Programme für Tesseract-OCR (leer = alles da)."""
    return [text for program, text in PROGRAM_PACKAGES.items() if not shutil.which(program)]


def _model_label(ai_model: AIModel) -> str:
    return f"{ai_model.provider.name} · {ai_model.model_id}"


def _fallback_note(cfg: RagSettings) -> str:
    """Hinweis, wenn Tesseract als Ersatz eingestellt, aber nicht nutzbar ist."""
    if not cfg.ocr_fallback_tesseract:
        return ""
    missing = missing_programs()
    if not missing:
        return ""
    return (
        " Hinweis zum Ersatz: Tesseract (Ersatz) ist nicht nutzbar – "
        + "; ".join(missing)
        + ". Fällt olmOCR aus, wartet die Indexierung."
    )


def _result(label: str, text: str, note: str = "") -> tuple[str, str]:
    if not text.strip():
        return "error", f"OCR-Test mit {label}: Es wurde kein Text erkannt.{note}"
    if all(word in text for word in CHECK_EXPECTED):
        level = "warning" if note else "ok"
        return level, f"OCR-Test mit {label} erfolgreich. Erkannt: „{_short(text)}“{note}"
    return "warning", (
        f"OCR-Test mit {label}: Text erkannt, aber unvollständig: „{_short(text)}“{note}"
    )


def check() -> tuple[str, str]:
    """(``ok``/``warning``/``error``, Meldung) für „OCR testen“.

    Rendert eine Testseite mit bekanntem Text und liest sie mit dem
    eingestellten Verfahren (ohne Ersatz, damit genau dieses geprüft wird).
    Die Meldung nennt Verfahren und Modell; ein nicht nutzbarer Ersatz
    (Tesseract) wird getrennt als Hinweis genannt. Der erkannte Text stammt
    aus der Testseite, nicht aus Dokumenten.
    """
    cfg = RagSettings.load()
    with tempfile.TemporaryDirectory(prefix="mgpt-ocrcheck-") as tmp:
        path = str(Path(tmp) / "check.pdf")
        Path(path).write_bytes(_check_pdf())
        if cfg.ocr_backend == RagSettings.OcrBackend.OLMOCR:
            note = _fallback_note(cfg)
            label = "olmOCR (kein Modell gewählt)"
            try:
                ai_model = ocr_model(cfg)
                label = f"olmOCR ({_model_label(ai_model)})"
                text = olmocr_page(ai_model, path, 1)
            except OcrError as exc:
                return "error", f"OCR-Test mit {label} fehlgeschlagen: {exc.message}{note}"
            return _result(label, text, note)
        label = "Tesseract"
        missing = missing_programs()
        if missing:
            return "error", f"OCR-Test mit {label} fehlgeschlagen: " + "; ".join(missing) + "."
        try:
            text = extract.ocr_pdf_page(path, 1)
        except extract.OcrUnavailable as exc:
            program = str(exc) or "tesseract"
            reason = PROGRAM_PACKAGES.get(program, f"{program} fehlt")
            return "error", f"OCR-Test mit {label} fehlgeschlagen: {reason}."
        return _result(label, text)
