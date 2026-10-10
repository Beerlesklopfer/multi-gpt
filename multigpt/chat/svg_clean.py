"""SVG serverseitig bereinigen (Diagramme aus ``run_python``, M4a-10).

Das SVG ist nicht vertrauenswürdig, auch wenn matplotlib es erzeugt hat: Der
Code stammt vom Modell und kann beliebiges SVG schreiben. Dieselbe Strenge wie
``static/chat/svg_preview.js`` (DOMPurify im SVG-Profil plus Nacharbeit), hier
ohne neue Abhängigkeit mit der Standardbibliothek:

- **Kein DTD-Inhalt:** ``<!ENTITY`` bzw. eine DOCTYPE mit internem Teil
  (``[``) wird abgelehnt (Entity-Bomben, externe Entities); die übliche
  DOCTYPE-Zeile von matplotlib wird entfernt. Verarbeitungsanweisungen fallen weg.
- **Positivliste** der Elemente (SVG-Namensraum, Zeichnen, Text, Verläufe,
  Muster, Masken, Filter). Alles andere fällt samt Inhalt weg – also auch
  ``script``, ``foreignObject``, ``a``, ``iframe`` und fremde Namensräume
  (z. B. RDF-Metadaten).
- **Attribute:** keine ``on*``; nur Attribute ohne Namensraum bzw. ``xlink:href``,
  ``xml:space`` und ``xml:lang``. ``href``/``xlink:href``/``src`` nur auf ``#…`` bzw. eingebettete
  Rasterbilder (``data:image/png|jpeg|gif|webp``); ``<use>`` nur ``#…``.
- **CSS:** ``url(…)`` nur auf ``#…`` (sonst ``none``), kein ``@import`` – in
  ``style``-Attributen, Präsentationsattributen und ``<style>``.

Ausgeliefert wird das Ergebnis nur über die geschützte Anhang-Auslieferung
(Download mit CSP ``sandbox``, ``nosniff``) und angezeigt nur als ``<img>``
(dort laufen ohnehin keine Skripte und keine externen Ressourcen).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
XML_NS = "http://www.w3.org/XML/1998/namespace"
MAX_BYTES = 2 * 1024 * 1024
MAX_ELEMENTS = 200_000
MAX_DEPTH = 200

ALLOWED_TAGS = frozenset(
    """
    svg g defs symbol use title desc style
    path rect circle ellipse line polyline polygon
    text tspan textPath
    image clipPath mask pattern marker
    linearGradient radialGradient stop
    filter feBlend feColorMatrix feComponentTransfer feComposite feConvolveMatrix
    feDiffuseLighting feDisplacementMap feDistantLight feDropShadow feFlood feFuncA
    feFuncB feFuncG feFuncR feGaussianBlur feImage feMerge feMergeNode feMorphology
    feOffset fePointLight feSpecularLighting feSpotLight feTile feTurbulence
    """.split()
)
SAFE_HREF = re.compile(r"^\s*(?:#|data:image/(?:png|jpe?g|gif|webp);)", re.I)
CSS_URL = re.compile(r"""url\(\s*(['"]?)(?!\s*#)[^)]*\)""", re.I)
CSS_IMPORT = re.compile(r"@import[^;]*;?", re.I)
DOCTYPE = re.compile(r"<!DOCTYPE[^>\[]*>", re.I)
_REFS = {"href", "src"}


class SvgError(ValueError):
    """Kein brauchbares SVG."""


def _clean_css(text: str) -> str:
    return CSS_URL.sub("none", CSS_IMPORT.sub("", text))


def _local(name: str) -> tuple[str | None, str]:
    if name.startswith("{"):
        ns, _, local = name[1:].partition("}")
        return ns, local
    return None, name


def _clean_element(element: ET.Element) -> None:
    ns, tag = _local(element.tag)
    for name, value in list(element.attrib.items()):
        attr_ns, local = _local(name)
        lower = local.lower()
        keep = (
            attr_ns is None
            or (attr_ns == XLINK_NS and lower == "href")
            or (attr_ns == XML_NS and lower in ("space", "lang"))
        )
        if not keep or lower.startswith("on"):
            del element.attrib[name]
        elif lower in _REFS:
            if not SAFE_HREF.match(value) or (tag == "use" and not value.strip().startswith("#")):
                del element.attrib[name]
        elif re.search(r"url\(|@import", value, re.I):
            element.attrib[name] = _clean_css(value)
    if tag == "style" and element.text:
        element.text = _clean_css(element.text)
    for child in list(element):
        child_ns, child_tag = _local(child.tag) if isinstance(child.tag, str) else (None, "")
        if child_ns != SVG_NS or child_tag not in ALLOWED_TAGS:
            # Kommentare, fremde Namensräume, script, foreignObject, a …: samt Inhalt weg.
            # Der Text nach dem Element (tail) bleibt erhalten.
            tail = child.tail
            index = list(element).index(child)
            element.remove(child)
            if tail:
                if index > 0:
                    prev = element[index - 1]
                    prev.tail = (prev.tail or "") + tail
                else:
                    element.text = (element.text or "") + tail
            continue
        _clean_element(child)


def _length(value: str | None) -> float | None:
    match = re.match(r"^\s*([0-9.]+)\s*(px|pt)?\s*$", value or "")
    if not match:
        return None
    number = float(match.group(1))
    return number * 4 / 3 if match.group(2) == "pt" else number


def size(root: ET.Element) -> tuple[int | None, int | None]:
    """Anzeigegröße in Pixeln (width/height bzw. viewBox), sonst (None, None)."""
    width, height = _length(root.get("width")), _length(root.get("height"))
    if not (width and height):
        parts = (root.get("viewBox") or "").replace(",", " ").split()
        try:
            width, height = float(parts[2]), float(parts[3])
        except (IndexError, ValueError):
            return None, None
    if not (0 < width < 100_000 and 0 < height < 100_000):
        return None, None
    return round(width), round(height)


def clean(data: bytes) -> tuple[bytes, int | None, int | None]:
    """Bereinigtes SVG (UTF-8) und Größe in Pixeln; ungültig -> ``SvgError``."""
    if len(data) > MAX_BYTES:
        raise SvgError("Das SVG ist zu groß.")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SvgError("Das SVG ist nicht UTF-8.") from exc
    if re.search(r"<!ENTITY", text, re.I) or re.search(r"<!DOCTYPE[^>]*\[", text, re.I):
        raise SvgError("Das SVG enthält eine DTD.")
    text = DOCTYPE.sub("", text)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise SvgError("Das SVG ist fehlerhaft.") from exc
    if root.tag != f"{{{SVG_NS}}}svg":
        raise SvgError("Kein SVG.")
    if sum(1 for _ in root.iter()) > MAX_ELEMENTS or _depth(root) > MAX_DEPTH:
        raise SvgError("Das SVG hat zu viele Elemente.")
    _clean_element(root)
    width, height = size(root)
    parts = ['<?xml version="1.0" encoding="utf-8"?>\n']
    _serialize(root, parts, top=True)
    return "".join(parts).encode("utf-8"), width, height


def _depth(root: ET.Element) -> int:
    deepest, stack = 0, [(root, 1)]
    while stack:
        element, level = stack.pop()
        deepest = max(deepest, level)
        stack.extend((child, level + 1) for child in element)
    return deepest


def _attr_name(name: str) -> str:
    ns, local = _local(name)
    return {XLINK_NS: f"xlink:{local}", XML_NS: f"xml:{local}"}.get(ns, local)


def _serialize(element: ET.Element, out: list[str], *, top: bool = False) -> None:
    """Eigene Ausgabe: SVG als Standard-Namensraum (ElementTree kann das mit
    Attributen ohne Namensraum nicht), alles maskiert."""
    tag = _local(element.tag)[1]
    attrs = "".join(f" {_attr_name(k)}={quoteattr(v)}" for k, v in element.attrib.items())
    if top:
        attrs = f' xmlns="{SVG_NS}" xmlns:xlink="{XLINK_NS}"' + attrs
    out.append(f"<{tag}{attrs}>")
    if element.text:
        out.append(escape(element.text))
    for child in element:
        _serialize(child, out)
        if child.tail:
            out.append(escape(child.tail))
    out.append(f"</{tag}>")
