"""Werkzeug ``create_pdf``: Blätter zum Ausdrucken als PDF (Arbeitsblätter,
Lineaturen, Briefe, Übersichten, Tabellen, Formulare).

Bittet jemand um ein *Blatt*, soll ein druckfertiges PDF entstehen, kein Bild
(``generate_image``) und kein Diagramm (``run_python``). Vorlagen (``layout``):

- ``text`` und ``arbeitsblatt``: Markdown vom Modell (Überschriften, Listen,
  Tabellen, einfache Formeln ``$…$``, Ankreuzfelder ``[ ]``, Lücken ``___``,
  Schreiblinien ``[linien:3]``, Kästen ``[kasten:50 Beschriftung]``, Bilder
  dieser Antwort ``![](anhang:ID)``). ``arbeitsblatt`` setzt alles kindgerecht
  (Schrift nach ``grade``, nummerierte Aufgaben, Name/Datum).
- ``lineatur``: Schreiblern-Lineaturen 0–4, liniert, Karo, blanko
  (``documents_sheets``, Maße und Quellen dort).
- ``aufgaben``, ``rechenkaestchen``, ``einmaleins``, ``zahlenstrahl``, ``uhr``:
  Mathe-Blätter; Aufgaben und Lösungen erzeugt der Server per ``seed``.

Entscheidungen:

- **Markdown -> HTML** mit markdown-it-py (CommonMark plus Tabellen), ``html``
  aus: Rohes HTML vom Modell erscheint als Text. Links werden Text mit
  sichtbarer Adresse (kein ``href``), Bilder nur über ``anhang:ID`` – sonst
  nur der Alternativtext. Jede URL im fertigen HTML stammt also vom Server.
- **HTML -> PDF** mit WeasyPrint in einem eigenen Prozess (``pdf_render``):
  echtes Zeitlimit (``RENDER_TIMEOUT``), Speicher- und CPU-Grenze, Umgebung
  ohne Schlüssel. Ein ``url_fetcher`` lehnt alles ab außer
  ``data:image/…;base64`` (die eingesetzten Anhänge): kein Netz, kein
  ``file://``. Eine Sandbox wie bei ``run_python`` braucht es nicht: Hier
  läuft kein Code des Modells, nur Daten durch einen Renderer, der nichts
  nachladen kann (``html=False`` plus Fetcher).
- **Grenzen:** ``MAX_CONTENT_BYTES`` Markdown, ``MAX_PAGES`` Seiten (geprüft
  vor dem Schreiben), ``MAX_PDFS_PER_ANSWER``, ``MAX_PARALLEL`` gleichzeitige
  Läufe je Prozess.
- **Ergebnis:** Anhang der Antwort (``owner`` leer = erzeugt, wie Bilder aus
  ``run_python``), Typ ``application/pdf``; im Chat als Datei-Chip mit
  „Ansehen“ (inline) und Download. Das Modell bekommt nur Dateiname,
  Seitenzahl und Anhang-Nummer.
- **Angeboten** bei ``supports_tools``, Recht ``CREATE_DOCUMENTS`` („Dokumente
  erzeugen (PDF)“) und installiertem WeasyPrint (Python-Paket und Pango);
  ohne Rückfrage (lokal, kostenlos, nur lesend). Keine Kostenbuchung.
- **Logs:** nur IDs, Seitenzahl, Dauer, Fehlerart – nie Inhalte.
"""

from __future__ import annotations

import base64
import ctypes.util
import functools
import importlib.util
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from xml.sax.saxutils import escape

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils.text import slugify

from multigpt.accounts.permissions import Action, can

from . import attachments as chat_attachments
from . import documents_sheets as sheets
from . import tooling
from .models import Attachment
from .providers.base import ToolSpec

logger = logging.getLogger(__name__)

TOOL_NAME = "create_pdf"
TOOL_LABEL = "Dokumente"
MAX_CONTENT_BYTES = 200 * 1024
MAX_TITLE = 200
MAX_PAGES = 50
MAX_PDFS_PER_ANSWER = 5
MAX_EMBED_BYTES = 10 * 1024 * 1024
MAX_EMBED_TOTAL = 20 * 1024 * 1024
RENDER_TIMEOUT = 60
MAX_PARALLEL = 2
PDF_MIME = "application/pdf"
EMBED_TYPES = {"image/png", "image/jpeg", "image/webp", "image/svg+xml"}

LAYOUTS = (
    "text",
    "arbeitsblatt",
    "lineatur",
    "aufgaben",
    "rechenkaestchen",
    "einmaleins",
    "zahlenstrahl",
    "uhr",
)
MARKDOWN_LAYOUTS = ("text", "arbeitsblatt")
# Ränder des Markdown-Layouts (oben, rechts, unten, links) in mm.
TEXT_MARGINS = (20.0, 18.0, 18.0, 18.0)
# Klassenstufe -> (Schrift Markdown in pt, Schrift Vorlagen in mm, Zahlenraum).
GRADES = {
    1: (16, 6.0, (0, 20)),
    2: (16, 6.0, (0, 100)),
    3: (14, 5.0, (0, 1000)),
    4: (14, 5.0, (0, 1_000_000)),
}
DEFAULT_GRADE = (12, 4.5, (0, 100))

MSG_UNAVAILABLE = "PDF-Erzeugung ist auf diesem Server nicht eingerichtet (WeasyPrint fehlt)."
MSG_NO_CONTENT = "Bitte den Inhalt als Markdown im Argument „content“ angeben."
MSG_TOO_LONG = f"Der Inhalt ist zu lang (höchstens {MAX_CONTENT_BYTES // 1024} KB Markdown)."
MSG_TOO_MANY_PAGES = "Das Dokument hätte {pages} Seiten, erlaubt sind höchstens {limit}."
MSG_TOO_MANY = f"Höchstens {MAX_PDFS_PER_ANSWER} PDF-Dokumente je Antwort."
MSG_TIMEOUT = f"Abgebrochen: Das PDF war nach {RENDER_TIMEOUT} s nicht fertig."
MSG_BUSY = "Gerade entstehen zu viele PDFs. Bitte gleich noch einmal versuchen."
MSG_FAILED = "Das PDF konnte nicht erzeugt werden."

DESCRIPTION = (
    "Erzeugt ein druckfertiges PDF-Dokument und hängt es an die Antwort (Ansehen und "
    "Herunterladen). Nutze es, wenn der Nutzer ein Blatt bzw. Dokument zum Ausdrucken "
    "möchte: Arbeitsblatt, Schreibblatt, Lineatur, Schreibheft-Seite, Karopapier, "
    "Rechenblatt, Brief, Einladung, Rezept, Übersicht, Tabelle, Formular, Checkliste. "
    "Nicht für Bilder, Fotos, Zeichnungen und Grafiken (dafür generate_image, falls "
    "vorhanden) und nicht für Diagramme aus Daten (dafür run_python, falls vorhanden). "
    "Vorlagen (layout): text (Markdown, Standard); arbeitsblatt (Markdown, kindgerecht "
    "groß, nummerierte Aufgaben mit ## Überschrift, Name/Datum); lineatur (Schreiblern-"
    "Lineatur 0–4 mit Häuschen, liniert, karo5, karo10, blanko_rand – bei „Schreibblatt“, "
    "„Lineatur“, „Schreibheft“, „Karopapier“, „Schreiben lernen“); aufgaben, "
    "rechenkaestchen, einmaleins (Rechenaufgaben mit Lösungsblatt), zahlenstrahl, uhr "
    "(Uhrzeiten ablesen bzw. einzeichnen). Rechenaufgaben und Lösungen erzeugt der Server "
    "(operations, range, count, seed) – keine Aufgaben oder Lösungen selbst ausdenken. "
    "Markdown-Bausteine: Tabellen, Listen, $Formel$, [ ] Ankreuzfeld, ___ Lücke bzw. "
    "Schreiblinie, eigene Zeile [linien:3] bzw. [linien:3 lineatur:1] für Schreiblinien, "
    "[kasten:50 Zeichne hier] für ein Zeichenfeld (Höhe in mm), ![](anhang:ID) für ein "
    "Bild aus dieser Antwort (z. B. von generate_image). Kein HTML. Den Inhalt des PDFs "
    "danach nicht noch einmal als Text ausgeben."
)

HINT = (
    "Wünscht der Nutzer ein Blatt oder Dokument zum Ausdrucken (Arbeitsblatt, "
    "Schreibblatt mit Lineatur, Karopapier, Rechenblatt, Brief, Einladung, Tabelle, "
    "Formular), erzeuge es mit create_pdf statt eines Bildes. Bei Arbeitsblättern für "
    "Mathe, Deutsch und Sachunterricht die passende Vorlage wählen; Rechenaufgaben und "
    "Lösungen erzeugt der Server – nicht selbst rechnen oder raten. Diagramme aus Daten "
    "und Blätter mit freier Geometrie, die keine Vorlage abdeckt, gehören zu run_python "
    "(falls angeboten; dort als PDF speichern)."
)


class DocumentError(Exception):
    """Abgelehnt oder fehlgeschlagen; ``str(exc)`` ist ein deutscher Text für das Modell."""


# --- Verfügbarkeit ----------------------------------------------------------------


@functools.cache
def weasyprint_installed() -> bool:
    """WeasyPrint und Pango vorhanden (ohne Import: der lädt Bibliotheken in jeden
    Serverprozess; gerendert wird ohnehin im Kindprozess)."""
    if importlib.util.find_spec("weasyprint") is None:
        return False
    return all(ctypes.util.find_library(lib) for lib in ("pango-1.0", "pangoft2-1.0"))


def available(user) -> bool:
    return can(user, Action.CREATE_DOCUMENTS) and weasyprint_installed()


def _tool_available(user, ai_model) -> bool:
    return available(user)


def system_hint(bindings) -> str:
    """Satz für den System-Prompt, nur wenn ``create_pdf`` angeboten wird."""
    return HINT if TOOL_NAME in (bindings or {}) else ""


# --- Markdown -> HTML ---------------------------------------------------------------

_TEX = {
    r"\cdot": "·",
    r"\times": "×",
    r"\div": "÷",
    r"\pm": "±",
    r"\mp": "∓",
    r"\le": "≤",
    r"\leq": "≤",
    r"\ge": "≥",
    r"\geq": "≥",
    r"\neq": "≠",
    r"\ne": "≠",
    r"\approx": "≈",
    r"\infty": "∞",
    r"\to": "→",
    r"\rightarrow": "→",
    r"\Rightarrow": "⇒",
    r"\degree": "°",
    r"\circ": "°",
    r"\%": "%",
    r"\,": " ",
    r"\;": " ",
    r"\ ": " ",
    r"\alpha": "α",
    r"\beta": "β",
    r"\gamma": "γ",
    r"\delta": "δ",
    r"\Delta": "Δ",
    r"\pi": "π",
    r"\lambda": "λ",
    r"\mu": "μ",
    r"\sigma": "σ",
    r"\Sigma": "Σ",
    r"\phi": "φ",
    r"\omega": "ω",
    r"\Omega": "Ω",
    r"\sum": "∑",
    r"\int": "∫",
    r"\sqrt": "√",
    r"\cdots": "⋯",
    r"\ldots": "…",
    r"\dots": "…",
}
_SUP = str.maketrans("0123456789+-=()n", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿ")
_SUB = str.maketrans("0123456789+-=()", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎")


def tex_to_text(tex: str) -> str:
    """Einfache Formeln lesbar setzen (ohne KaTeX): Symbole, Brüche, Hochzahlen."""
    text = re.sub(r"\\(?:text|mathrm|mathbf|operatorname)\{([^{}]*)\}", r"\1", tex)
    text = re.sub(r"\\[dt]?frac\{(\w+)\}\{(\w+)\}", r"\1/\2", text)
    text = re.sub(r"\\[dt]?frac\{([^{}]*)\}\{([^{}]*)\}", r"(\1)/(\2)", text)
    text = re.sub(r"\\sqrt\{([^{}]*)\}", r"√(\1)", text)
    for command in sorted(_TEX, key=len, reverse=True):
        text = re.sub(re.escape(command) + r"(?![A-Za-z])", _TEX[command], text)

    def script(match, table, mark):
        body = match.group(1) or match.group(2)
        mapped = body.translate(table)
        return (
            mapped
            if all(ch != orig or ch == " " for ch, orig in zip(mapped, body, strict=True))
            else f"{mark}({body})"
        )

    text = re.sub(r"\^(?:\{([^{}]*)\}|(\w))", lambda m: script(m, _SUP, "^"), text)
    text = re.sub(r"_(?:\{([^{}]*)\}|(\w))", lambda m: script(m, _SUB, "_"), text)
    text = re.sub(r"\\([A-Za-z]+)", r"\1", text)
    return text.replace("{", "").replace("}", "").replace("\\", "")


def _math_rule(state, silent) -> bool:
    src, pos = state.src, state.pos
    if src[pos] != "$":
        return False
    delim = "$$" if src.startswith("$$", pos) else "$"
    start = pos + len(delim)
    end = src.find(delim, start)
    if end <= start:
        return False
    body = src[start:end]
    if delim == "$" and (body[0].isspace() or body[-1].isspace() or "\n" in body):
        return False
    if not silent:
        token = state.push("math_inline", "span", 0)
        token.content = body
        token.markup = delim
    state.pos = end + len(delim)
    return True


_WIDGET = re.compile(r"^\[(linien|kasten)(?::\s*([^\]]*))?\]$", re.IGNORECASE)
_TASK_ITEM = re.compile(r"^\[[ xX]\]\s")
_GAP = re.compile(r"_{3,}")
_BOX = re.compile(r"\[( |x|X)\]")


@dataclass
class MarkdownOptions:
    content_width: float  # mm
    lineatur: str = "4"
    images: dict | None = None  # Anhang-ID -> data-URI


def _widget_html(kind: str, raw: str, opts: MarkdownOptions) -> str:
    args = (raw or "").split()
    if kind == "linien":
        rows = int(args[0]) if args and args[0].isdigit() else 2
        key = opts.lineatur
        for arg in args[1:]:
            name, _, value = arg.partition(":" if ":" in arg else "=")
            if name.lower() == "lineatur" and value in sheets.LINEATUREN:
                key = value
        return (
            '<div class="lines">'
            + sheets.writing_lines_svg(opts.content_width, min(rows, 20), key)
            + "</div>\n"
        )
    height = int(args[0]) if args and args[0].isdigit() else 50
    label = " ".join(args[1:] if args and args[0].isdigit() else args)
    height = max(10, min(height, 250))
    inner = f'<div class="label">{escape(label)}</div>' if label else ""
    return f'<div class="draw-box" style="height:{height}mm">{inner}</div>\n'


def _core_rule(state) -> None:
    """Bausteine als eigene Absätze ersetzen; Listenpunkte mit [ ] markieren."""
    from markdown_it.token import Token

    tokens, out, i = state.tokens, [], 0
    opts: MarkdownOptions = state.env["opts"]
    while i < len(tokens):
        tok = tokens[i]
        if (
            tok.type == "paragraph_open"
            and i + 2 < len(tokens)
            and tokens[i + 1].type == "inline"
            and (match := _WIDGET.match(tokens[i + 1].content.strip()))
        ):
            block = Token("html_block", "", 0)
            block.content = _widget_html(match.group(1).lower(), match.group(2), opts)
            out.append(block)
            i += 3
            continue
        if (
            tok.type == "list_item_open"
            and i + 2 < len(tokens)
            and tokens[i + 2].type == "inline"
            and _TASK_ITEM.match(tokens[i + 2].content)
        ):
            tok.attrSet("class", "task")
        out.append(tok)
        i += 1
    state.tokens = out


def _render_text(self, tokens, idx, options, env) -> str:
    text = escape(tokens[idx].content)

    def gap(match):
        width = max(15, min(150, len(match.group(0)) * 3))
        return f'<span class="gap" style="width:{width}mm"></span>'

    text = _GAP.sub(gap, text)
    return _BOX.sub(
        lambda m: '<span class="check">' + ("✗" if m.group(1) in "xX" else "") + "</span>", text
    )


def _render_math(self, tokens, idx, options, env) -> str:
    token = tokens[idx]
    cls = "math display" if token.markup == "$$" else "math"
    return f'<span class="{cls}">{escape(tex_to_text(token.content))}</span>'


def _render_link_open(self, tokens, idx, options, env) -> str:
    href = tokens[idx].attrGet("href") or ""
    label = []
    for tok in tokens[idx + 1 :]:
        if tok.type == "link_close":
            break
        label.append(tok.content)
    env.setdefault("links", []).append("" if "".join(label).strip() == href else href)
    return '<span class="link">'


def _render_link_close(self, tokens, idx, options, env) -> str:
    href = env.get("links", [""]).pop() if env.get("links") else ""
    return "</span>" + (f' <span class="url">({escape(href)})</span>' if href else "")


def _render_image(self, tokens, idx, options, env) -> str:
    token = tokens[idx]
    alt = self.renderInlineAsText(token.children or [], options, env)
    match = re.fullmatch(r"anhang:(\d+)", (token.attrGet("src") or "").strip())
    data = (env["opts"].images or {}).get(int(match.group(1))) if match else None
    if data:
        return f'<img class="embed" src="{escape(data, {chr(34): "&quot;"})}" alt="{escape(alt)}">'
    return f'<span class="missing-image">[Bild: {escape(alt or "nicht verfügbar")}]</span>'


@functools.cache
def _markdown():
    from markdown_it import MarkdownIt

    md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
    md.enable(["table", "strikethrough"])
    md.inline.ruler.before("escape", "math", _math_rule)
    md.core.ruler.push("worksheet_widgets", _core_rule)
    md.add_render_rule("text", _render_text)
    md.add_render_rule("math_inline", _render_math)
    md.add_render_rule("link_open", _render_link_open)
    md.add_render_rule("link_close", _render_link_close)
    md.add_render_rule("image", _render_image)
    return md


def markdown_html(content: str, opts: MarkdownOptions) -> str:
    return _markdown().render(content, {"opts": opts})


# --- HTML-Gerüste --------------------------------------------------------------------

TEXT_CSS = """
@page { size: %(w)smm %(h)smm; margin: %(mt)smm %(mr)smm %(mb)smm %(ml)smm;
  @top-left { content: string(doctitle); font-size: 8pt; color: #666; }
  @bottom-right { content: "Seite " counter(page) " von " counter(pages);
    font-size: 8pt; color: #666; } }
@page :first { @top-left { content: none; } }
html { font-family: "DejaVu Sans", sans-serif; font-size: %(pt)spt; line-height: 1.45;
  color: #111; }
body { margin: 0; }
h1.doc-title { string-set: doctitle content(); font-size: 1.6em; margin: 0 0 0.5em; }
h1, h2, h3, h4 { line-height: 1.25; break-after: avoid; margin: 0.9em 0 0.4em; }
p { orphans: 2; widows: 2; margin: 0 0 0.6em; }
table { border-collapse: collapse; width: 100%%; margin: 0.6em 0; }
th, td { border: 0.3mm solid #444; padding: 1.5mm 2mm; vertical-align: top;
  text-align: left; }
thead { display: table-header-group; }
th { background: #eeeeee; }
tr, img, .lines, .draw-box, li { break-inside: avoid; }
img.embed { display: block; max-width: 100%%; max-height: 120mm; margin: 2mm auto; }
.gap { display: inline-block; border-bottom: 0.35mm solid #222; height: 1em;
  vertical-align: baseline; }
.check { display: inline-block; width: 0.85em; height: 0.85em; border: 0.35mm solid #222;
  vertical-align: -0.1em; text-align: center; line-height: 0.85em; margin-right: 0.25em; }
li.task { list-style: none; margin-left: -1.1em; }
.lines { margin: 1mm 0 3mm; }
.lines svg { display: block; }
.draw-box { border: 0.4mm solid #333; border-radius: 2mm; margin: 2mm 0 4mm; }
.draw-box .label { font-size: 0.8em; color: #444; padding: 1mm 2mm; }
.math { font-family: "DejaVu Serif", serif; font-style: italic; }
.math.display { display: block; text-align: center; margin: 0.5em 0; }
code, pre { font-family: "DejaVu Sans Mono", monospace; font-size: 0.9em; }
pre { white-space: pre-wrap; background: #f4f4f4; padding: 2mm; border-radius: 1mm; }
.url { color: #444; font-size: 0.85em; }
.missing-image { color: #666; font-style: italic; }
.namefield { text-align: right; margin: 0 0 4mm; }
.namefield .gap { min-width: 0; }
hr { border: 0; border-top: 0.3mm solid #888; margin: 1em 0; }
"""
WORKSHEET_CSS = """
html { line-height: 1.6; }
body { counter-reset: task; }
h2 { counter-increment: task; font-size: 1.1em; }
h2::before { content: counter(task); display: inline-block; width: 1.5em; height: 1.5em;
  line-height: 1.5em; border: 0.4mm solid #222; border-radius: 50%; text-align: center;
  margin-right: 0.5em; font-size: 0.9em; }
.gap { height: 1.3em; }
td, th { padding: 2.5mm 3mm; }
"""


def _fmt(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def text_document(title: str, body: str, *, width, height, pt, worksheet, name_field) -> str:
    mt, mr, mb, ml = TEXT_MARGINS
    css = TEXT_CSS % {
        "w": _fmt(width),
        "h": _fmt(height),
        "mt": _fmt(mt),
        "mr": _fmt(mr),
        "mb": _fmt(mb),
        "ml": _fmt(ml),
        "pt": pt,
    }
    if worksheet:
        css += WORKSHEET_CSS
    head = ""
    if name_field:
        head += (
            '<div class="namefield">Name: <span class="gap" style="width:60mm"></span>'
            ' &nbsp; Datum: <span class="gap" style="width:30mm"></span></div>'
        )
    if title:
        head += f'<h1 class="doc-title">{escape(title)}</h1>'
    return (
        f'<!doctype html><html lang="de"><head><meta charset="utf-8"><title>{escape(title)}'
        f"</title><style>{css}</style></head><body>{head}{body}</body></html>"
    )


def svg_document(title: str, pages: list[str], width: float, height: float) -> str:
    w, h = _fmt(width), _fmt(height)
    css = (
        f"@page {{ size: {w}mm {h}mm; margin: 0; }} html, body {{ margin: 0; padding: 0; }}"
        f" .page {{ width: {w}mm; height: {h}mm; overflow: hidden; break-after: page; }}"
        " .page:last-child { break-after: auto; } svg { display: block; }"
    )
    body = "".join(f'<div class="page">{page}</div>' for page in pages)
    return (
        f'<!doctype html><html lang="de"><head><meta charset="utf-8"><title>{escape(title)}'
        f"</title><style>{css}</style></head><body>{body}</body></html>"
    )


# --- Rendern im Kindprozess --------------------------------------------------------------

_SLOTS = threading.BoundedSemaphore(MAX_PARALLEL)
_ENV_KEYS = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "XDG_CACHE_HOME",
    "FONTCONFIG_FILE",
    "FONTCONFIG_PATH",
)


def render(html: str, max_pages: int = MAX_PAGES) -> tuple[bytes, int]:
    """HTML -> (PDF, Seitenzahl) in ``pdf_render``; Fehler als ``DocumentError``."""
    from pypdf import PdfReader

    if not _SLOTS.acquire(blocking=False):
        raise DocumentError(MSG_BUSY)
    env = {key: os.environ[key] for key in _ENV_KEYS if key in os.environ}
    env.setdefault("LANG", "C.UTF-8")
    job = json.dumps({"html": html, "max_pages": max_pages}).encode()
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-m", "multigpt.chat.pdf_render"],
            input=job,
            capture_output=True,
            timeout=RENDER_TIMEOUT,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise DocumentError(MSG_TIMEOUT) from None
    finally:
        _SLOTS.release()
    if proc.returncode == 3:
        pages = int(proc.stdout or b"0")
        raise DocumentError(MSG_TOO_MANY_PAGES.format(pages=pages, limit=max_pages))
    if proc.returncode != 0 or not proc.stdout.startswith(b"%PDF-"):
        reason = proc.stderr.decode("utf-8", "replace").strip()[:60] or f"Exit {proc.returncode}"
        logger.warning("PDF-Erzeugung fehlgeschlagen: %s", reason)
        raise DocumentError(MSG_FAILED)
    import io

    return proc.stdout, len(PdfReader(io.BytesIO(proc.stdout)).pages)


# --- Auftrag aus den Argumenten -----------------------------------------------------------


@dataclass
class Job:
    html: str
    title: str
    filename: str
    note: str = ""


def _int(value, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _bool(value, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _range(value, default: tuple[int, int]) -> tuple[int, int]:
    if isinstance(value, list | tuple) and len(value) == 2:
        try:
            lo, hi = int(value[0]), int(value[1])
        except (TypeError, ValueError):
            return default
        if 0 <= lo < hi <= 1_000_000:
            return lo, hi
        raise DocumentError("Zahlenraum ungültig: range = [von, bis] mit 0 ≤ von < bis ≤ 1000000.")
    return default


def _background(value) -> str:
    text = str(value or "").strip().lower()
    if re.fullmatch(r"#[0-9a-f]{6}", text):
        return text
    return sheets.BACKGROUNDS.get(text, "#ffffff")


def filename_for(raw, title: str, layout: str) -> str:
    stem = slugify(str(raw or "").removesuffix(".pdf") or title or layout)[:60].strip("-")
    return (stem or "dokument") + ".pdf"


def _choice(value, options, default: str) -> str:
    """Auswahl aus ``options`` (nur Text; Listen o. Ä. vom Modell -> Standard)."""
    return value if isinstance(value, str) and value in options else default


def _page(args, default: tuple[str, str]) -> tuple[str, str]:
    """Format und Ausrichtung; ohne Angabe die Vorgabe der Vorlage (Lineatur 0: quer)."""
    size = _choice(args.get("page_size"), sheets.PAGE_SIZES, default[0])
    return size, _choice(args.get("orientation"), ("portrait", "landscape"), default[1])


def embedded_images(user, message, content: str) -> dict[int, str]:
    """``![](anhang:ID)``: Bilder aus diesem Chat (Leserecht) als data-URI."""
    ids = {int(i) for i in re.findall(r"\]\(\s*anhang:(\d+)\s*\)", content)}
    if not ids or message is None or not can(user, Action.READ, message.conversation):
        return {}
    found, total = {}, 0
    rows = Attachment.objects.filter(
        pk__in=list(ids)[:20],
        kind=Attachment.Kind.IMAGE,
        message__conversation_id=message.conversation_id,
    )
    for attachment in rows:
        if attachment.mime_type not in EMBED_TYPES or attachment.size > MAX_EMBED_BYTES:
            continue
        try:
            with attachment.file.open("rb") as handle:
                data = handle.read(MAX_EMBED_BYTES + 1)
        except (OSError, ValueError):
            continue
        total += len(data)
        if len(data) > MAX_EMBED_BYTES or total > MAX_EMBED_TOTAL:
            break
        encoded = base64.b64encode(data).decode("ascii")
        found[attachment.pk] = f"data:{attachment.mime_type};base64,{encoded}"
    return found


def build(user, message, args: dict) -> Job:
    """Argumente prüfen und HTML bauen; ungültig -> ``DocumentError``."""
    layout = _choice(args.get("layout"), LAYOUTS, "text")
    title = " ".join(str(args.get("title") or "").split())[:MAX_TITLE]
    grade = _int(args.get("grade"), 0, 0, 13)
    pt, font, default_range = GRADES.get(grade, DEFAULT_GRADE)
    seed = _int(args.get("seed"), secrets.randbelow(10_000) + 1, 0, 1_000_000_000)
    with_solutions = _bool(args.get("with_solutions"), True)
    name_field = _bool(args.get("name_field"), layout != "text")
    filename = filename_for(args.get("filename"), title, layout)
    content = args.get("content") if isinstance(args.get("content"), str) else ""
    if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise DocumentError(MSG_TOO_LONG)

    if layout in MARKDOWN_LAYOUTS:
        if not content.strip():
            raise DocumentError(MSG_NO_CONTENT)
        size, orientation = _page(args, ("A4", "portrait"))
        width, height = sheets.page_dims(size, orientation)
        worksheet = layout == "arbeitsblatt"
        if not worksheet:
            pt = 11 if not grade else pt
        lineatur = str(args.get("lineatur") or "")
        if lineatur not in sheets.LINEATUREN:
            lineatur = {1: "1", 2: "2", 3: "3"}.get(grade, "4")
        opts = MarkdownOptions(
            content_width=width - TEXT_MARGINS[1] - TEXT_MARGINS[3],
            lineatur=lineatur,
            images=embedded_images(user, message, content),
        )
        body = markdown_html(content, opts)
        html = text_document(
            title,
            body,
            width=width,
            height=height,
            pt=pt,
            worksheet=worksheet,
            name_field=name_field,
        )
        return Job(html, title, filename)

    instruction = " ".join(content.split())[:150]
    if layout == "lineatur":
        key = str(args.get("lineatur") if args.get("lineatur") is not None else "0")
        lin = sheets.LINEATUREN.get(key)
        if lin is None:
            raise DocumentError(
                "Unbekannte Lineatur. Möglich: " + ", ".join(sheets.LINEATUREN) + "."
            )
        size, orientation = _page(args, lin.default_page)
        width, height = sheets.page_dims(size, orientation)
        frame = sheets.Frame(
            width,
            height,
            title=title,
            name_field=_bool(args.get("name_field"), False),
            background=_background(args.get("background")),
            label=lin.label,
            instruction=instruction,
        )
        opts = {
            "rows": _int(args["rows"], 1, 1, 60) if args.get("rows") is not None else None,
            "house_symbols": _bool(args.get("house_symbols"), lin.houses),
            "baseline_bold": _bool(args.get("baseline_bold"), lin.band is not None),
            "contrast": _bool(args.get("contrast"), lin.contrast),
            "sample_words": str(args.get("sample_words") or "")[:80],
        }
        pages, info = [], {}
        for _ in range(_int(args.get("pages"), 1, 1, 10)):
            svg, info = sheets.lineatur_page(frame, lin, opts)
            pages.append(svg)
        note = ""
        if opts["rows"] and info.get("fit") and opts["rows"] > info["fit"]:
            note = f" Es passen nur {info['fit']} Zeilen auf die Seite."
        title = title or lin.label
        return Job(
            svg_document(title, pages, width, height),
            title,
            filename_for(args.get("filename"), title, layout),
            note,
        )

    size, orientation = _page(args, ("A4", "portrait"))
    width, height = sheets.page_dims(size, orientation)
    frame = sheets.Frame(
        width, height, title=title, name_field=name_field, font=font, instruction=instruction
    )
    columns = _int(args.get("columns"), 2 if width < 250 else 3, 1, 4)
    note = f" Startwert (seed) {seed}."
    try:
        if layout in ("aufgaben", "rechenkaestchen"):
            lo, hi = _range(args.get("range"), default_range)
            operations = (
                args.get("operations") if isinstance(args.get("operations"), list) else ["+", "-"]
            )
            limit = 60 if layout == "rechenkaestchen" else 100
            tasks = sheets.make_tasks(
                operations,
                lo,
                hi,
                _int(args.get("count"), 20, 1, limit),
                seed,
                args.get("blank")
                if args.get("blank") in ("result", "a", "b", "mixed")
                else "result",
            )
            draw = sheets.task_pages if layout == "aufgaben" else sheets.grid_task_pages
            pages = draw(frame, tasks, columns, False)
            if with_solutions:
                pages += draw(frame, tasks, columns, True)
        elif layout == "einmaleins":
            reihen = args.get("reihen") if isinstance(args.get("reihen"), list) else [2, 5, 10]
            ops = args.get("operations") if isinstance(args.get("operations"), list) else []
            count = _int(args.get("count"), 0, 0, 100) or None
            tasks = sheets.times_table_tasks(
                reihen, _bool(args.get("mixed"), True), ":" in ops, count, seed
            )
            pages = sheets.task_pages(frame, tasks, columns, False)
            if with_solutions:
                pages += sheets.task_pages(frame, tasks, columns, True)
        elif layout == "zahlenstrahl":
            lo, hi = _range(args.get("range"), (0, 20))
            step = _int(args.get("step"), 1, 1, 100_000)
            count = _int(args.get("count"), 4, 1, 8)
            gaps = _int(args.get("gaps"), 4, 0, 30)
            pages = sheets.number_line_pages(frame, lo, hi, step, count, gaps, seed, False)
            if with_solutions and gaps:
                pages += sheets.number_line_pages(frame, lo, hi, step, count, gaps, seed, True)
        else:  # uhr
            mode = _choice(args.get("mode"), ("ablesen", "einzeichnen"), "ablesen")
            precision = _choice(args.get("precision"), sheets.PRECISIONS, "halb")
            times = sheets.make_times(precision, _int(args.get("count"), 6, 1, 12), seed)
            pages = sheets.clock_pages(frame, times, mode, False)
            if with_solutions:
                pages += sheets.clock_pages(frame, times, mode, True)
    except sheets.SheetError as exc:
        raise DocumentError(str(exc)) from None
    title = (
        title
        or {
            "aufgaben": "Rechenaufgaben",
            "rechenkaestchen": "Rechenkästchen",
            "einmaleins": "Einmaleins",
            "zahlenstrahl": "Zahlenstrahl",
            "uhr": "Uhrzeiten",
        }[layout]
    )
    return Job(
        svg_document(title, pages, width, height),
        title,
        filename_for(args.get("filename"), title, layout),
        note,
    )


# --- Anhang ---------------------------------------------------------------------------


def pdf_count(message) -> int:
    return Attachment.objects.filter(
        message=message, owner__isnull=True, mime_type=PDF_MIME
    ).count()


def store_pdf(message, data: bytes, filename: str) -> Attachment:
    """PDF als erzeugten Anhang der Antwort speichern (wie Bilder aus ``run_python``)."""
    attachment = Attachment(
        message=message,
        owner=None,  # erzeugt, nicht hochgeladen
        conversation=message.conversation,
        kind=Attachment.Kind.FILE,
        mime_type=PDF_MIME,
        size=len(data),
        original_name=filename,
    )
    try:
        with transaction.atomic():
            attachment.file.save("dokument.pdf", ContentFile(data), save=False)
            attachment.save()
    except Exception:
        chat_attachments.delete_files([attachment], immediately=True)
        raise
    return attachment


# --- Werkzeug --------------------------------------------------------------------------


def _tool_run(user, arguments: dict, sources) -> tooling.BuiltinResult:
    args = arguments if isinstance(arguments, dict) else {}
    if not can(user, Action.CREATE_DOCUMENTS):
        return tooling.BuiltinResult(tooling.MSG_NOT_ALLOWED, True)
    if not weasyprint_installed():
        return tooling.BuiltinResult(MSG_UNAVAILABLE, True)
    message = sources.message
    if pdf_count(message) >= MAX_PDFS_PER_ANSWER:
        return tooling.BuiltinResult(MSG_TOO_MANY, True)
    started = time.monotonic()
    try:
        job = build(user, message, args)
        data, pages = render(job.html)
    except DocumentError as exc:
        return tooling.BuiltinResult(str(exc), True)
    attachment = store_pdf(message, data, job.filename)
    logger.info(
        "PDF %s erzeugt (Nachricht %s, Konto %s): %d Seite(n), %d Bytes, %d ms",
        attachment.pk,
        message.pk,
        user.pk,
        pages,
        len(data),
        int((time.monotonic() - started) * 1000),
    )
    seiten = "Seite" if pages == 1 else "Seiten"
    return tooling.BuiltinResult(
        f"PDF erzeugt: {job.filename}, {pages} {seiten} (Anhang #{attachment.pk}).{job.note}"
    )


TOOL_SPEC = ToolSpec(
    name=TOOL_NAME,
    description=DESCRIPTION,
    parameters={
        "type": "object",
        "properties": {
            "layout": {
                "type": "string",
                "enum": list(LAYOUTS),
                "description": "Vorlage, Standard text.",
            },
            "title": {"type": "string", "description": "Titel (Kopf des Blattes)."},
            "content": {
                "type": "string",
                "description": "Bei text und arbeitsblatt: Inhalt als Markdown. Bei den "
                "anderen Vorlagen optional: kurze Arbeitsanweisung.",
            },
            "page_size": {"type": "string", "enum": list(sheets.PAGE_SIZES)},
            "orientation": {"type": "string", "enum": ["portrait", "landscape"]},
            "filename": {"type": "string", "description": "Dateiname (optional)."},
            "grade": {
                "type": "integer",
                "description": "Klassenstufe 1–13: steuert Schriftgröße und Zahlenraum.",
            },
            "name_field": {"type": "boolean", "description": "Kopf mit Name und Datum."},
            "lineatur": {
                "type": "string",
                "enum": list(sheets.LINEATUREN),
                "description": "lineatur: 0 (Klasse 1, A5 quer, 6/6/6 mm), 1 (5/5/5), "
                "2 (4/4/4), 3, 4, liniert, karo5, karo10, blanko_rand. Bei arbeitsblatt "
                "für [linien:N].",
            },
            "rows": {
                "type": "integer",
                "description": "lineatur: Zeilenzahl (Standard: so viele wie passen).",
            },
            "pages": {"type": "integer", "description": "lineatur: Anzahl gleicher Seiten."},
            "house_symbols": {
                "type": "boolean",
                "description": "lineatur: Häuschen (Dach, Haus, Keller) links und rechts.",
            },
            "baseline_bold": {"type": "boolean", "description": "Grundlinie verstärkt."},
            "background": {
                "type": "string",
                "description": "lineatur: Hintergrund, z. B. "
                "hellgruen oder #eef6d0; Standard weiß.",
            },
            "sample_words": {
                "type": "string",
                "description": "lineatur: Vorlagewörter in der ersten Zeile.",
            },
            "operations": {
                "type": "array",
                "items": {"type": "string", "enum": list(sheets.OPERATIONS)},
                "description": "Rechenarten: + - * (mal) : (geteilt).",
            },
            "range": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 2,
                "maxItems": 2,
                "description": "Zahlenraum [von, bis], z. B. [0, 20].",
            },
            "count": {
                "type": "integer",
                "description": "Anzahl Aufgaben, Uhren bzw. Zahlenstrahle.",
            },
            "columns": {"type": "integer", "description": "Spalten (1–4)."},
            "blank": {
                "type": "string",
                "enum": ["result", "a", "b", "mixed"],
                "description": "aufgaben: Lücke beim Ergebnis oder einer Zahl.",
            },
            "with_solutions": {
                "type": "boolean",
                "description": "Lösungsseite anhängen (Standard ja).",
            },
            "seed": {"type": "integer", "description": "Startwert für dieselben Aufgaben."},
            "reihen": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "einmaleins: Reihen, z. B. [2, 5, 10].",
            },
            "mixed": {
                "type": "boolean",
                "description": "einmaleins: gemischt statt der Reihe nach.",
            },
            "step": {"type": "integer", "description": "zahlenstrahl: Schrittweite."},
            "gaps": {"type": "integer", "description": "zahlenstrahl: Zahl der Lücken."},
            "mode": {
                "type": "string",
                "enum": ["ablesen", "einzeichnen"],
                "description": "uhr: Zeit ablesen oder Zeiger einzeichnen.",
            },
            "precision": {
                "type": "string",
                "enum": list(sheets.PRECISIONS),
                "description": "uhr: voll, halb, viertel, 5min, 1min.",
            },
        },
        "required": ["layout"],
    },
)


def register() -> None:
    tooling.register_builtin(
        tooling.BuiltinTool(
            name=TOOL_NAME,
            label=TOOL_LABEL,
            spec=TOOL_SPEC,
            available=_tool_available,
            run=_tool_run,
        )
    )


register()
