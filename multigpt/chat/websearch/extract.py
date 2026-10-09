"""HTML auf lesbaren Text reduzieren (M8-03) – nur Standardbibliothek.

**Entscheidung:** ``html.parser`` aus der Standardbibliothek statt einer neuen
Abhängigkeit. Der Parser ist fehlertolerant, braucht keine C-Erweiterung (dh-
virtualenv-Paket bleibt schlank) und reicht für den Zweck: Das Modell braucht
den Fließtext, kein exaktes DOM. Gegenüber ``trafilatura`` (beste
Boilerplate-Erkennung, aber lxml und ein halbes Dutzend weiterer Pakete) und
``selectolax``/``beautifulsoup4`` (zusätzliche Pakete ohne Mehrwert für reinen
Text) genügt eine einfache Heuristik:

- Skripte, Styles, Formulare, Navigation, Kopf-/Fußbereiche, ``aside``,
  ``svg`` usw. werden komplett übersprungen, ebenso versteckte Elemente
  (``hidden``, ``aria-hidden="true"``) – dort steht gern unsichtbarer Text,
  der dem Modell Anweisungen unterschieben soll.
- Gibt es ``<main>`` oder ``<article>``, zählt nur deren Text (sofern er
  nicht fast leer ist), sonst der ganze ``<body>``.
- Block-Elemente erzeugen Zeilenumbrüche, Leerraum wird zusammengefasst.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

# Inhalt dieser Elemente wird nie übernommen.
SKIP_TAGS = {
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "math",
    "canvas",
    "iframe",
    "object",
    "embed",
    "form",
    "button",
    "select",
    "textarea",
    "nav",
    "header",
    "footer",
    "aside",
    "dialog",
    "head",
}
VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
BLOCK_TAGS = {
    "p",
    "div",
    "section",
    "article",
    "main",
    "br",
    "hr",
    "li",
    "ul",
    "ol",
    "dl",
    "dt",
    "dd",
    "table",
    "tr",
    "td",
    "th",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "blockquote",
    "pre",
    "figure",
    "figcaption",
    "address",
    "details",
    "summary",
}
CONTENT_TAGS = {"main", "article"}
MIN_MAIN_CHARS = 200

_SPACES = re.compile(r"[ \t\r\f\v ]+")
_BLANK_LINES = re.compile(r"\n\s*\n+")


def _hidden(attrs) -> bool:
    values = {k.lower(): (v or "") for k, v in attrs}
    if "hidden" in values:
        return True
    if values.get("aria-hidden", "").strip().lower() == "true":
        return True
    style = values.get("style", "").replace(" ", "").lower()
    return "display:none" in style or "visibility:hidden" in style


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool]] = []  # (Tag, überspringen)
        self.skip_depth = 0
        self.content_depth = 0
        self.title_parts: list[str] = []
        self.in_title = False
        self.all_parts: list[str] = []
        self.main_parts: list[str] = []

    # Elemente ohne Endtag (p, li, …) schließt HTML implizit; der Stapel wird
    # beim passenden Endtag bis dorthin abgebaut, unbekannte Endtags ignoriert.

    def handle_starttag(self, tag, attrs):
        if tag == "title" and not self.stack_has("body"):
            self.in_title = True
            return
        if tag in VOID_TAGS:
            if tag in ("br", "hr"):
                self._newline()
            return
        skip = tag in SKIP_TAGS or _hidden(attrs)
        self.stack.append((tag, skip))
        if skip:
            self.skip_depth += 1
        if tag in CONTENT_TAGS:
            self.content_depth += 1
        if tag in BLOCK_TAGS:
            self._newline()

    def handle_startendtag(self, tag, attrs):
        if tag in ("br", "hr"):
            self._newline()

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
            return
        if not any(t == tag for t, _ in self.stack):
            return
        while self.stack:
            open_tag, skip = self.stack.pop()
            if skip:
                self.skip_depth -= 1
            if open_tag in CONTENT_TAGS:
                self.content_depth -= 1
            if open_tag == tag:
                break
        if tag in BLOCK_TAGS:
            self._newline()

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)
            return
        if self.skip_depth:
            return
        self.all_parts.append(data)
        if self.content_depth:
            self.main_parts.append(data)

    def stack_has(self, tag: str) -> bool:
        return any(t == tag for t, _ in self.stack)

    def _newline(self):
        if self.skip_depth:
            return
        self.all_parts.append("\n")
        if self.content_depth:
            self.main_parts.append("\n")


def normalize_text(text: str) -> str:
    lines = [_SPACES.sub(" ", line).strip() for line in text.split("\n")]
    joined = "\n".join(lines)
    return _BLANK_LINES.sub("\n\n", joined).strip()


def html_to_text(html: str) -> tuple[str, str]:
    """(Titel, Text) aus HTML. Fehlerhaftes HTML führt nie zu einer Ausnahme."""
    parser = _TextParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # html.parser ist tolerant; sicherheitshalber
        pass
    title = normalize_text(" ".join(parser.title_parts)).replace("\n", " ")
    main = normalize_text("".join(parser.main_parts))
    text = main if len(main) >= MIN_MAIN_CHARS else normalize_text("".join(parser.all_parts))
    return title, text


def clip(text: str, limit: int) -> str:
    """Auf ``limit`` Zeichen kürzen, möglichst an einer Wortgrenze."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ", int(limit * 0.8))
    if space > 0:
        cut = cut[:space]
    return cut.rstrip() + " […]"
