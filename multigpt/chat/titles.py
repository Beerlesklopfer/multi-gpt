"""Automatischer Chattitel aus der ersten Nachricht (M5-02, ohne Modellaufruf).

Erste nicht leere Zeile, Markdown-Auszeichnung entfernt, Leerraum
zusammengefasst, auf ``length`` Zeichen gekürzt (mit "…"). Codeblöcke werden
übersprungen, solange danach noch Text kommt. Das Gegenstück in chat.js
(``titleFrom``) zeigt den Titel sofort in der Seitenleiste an; maßgeblich ist
dieser hier.
"""

import re

DEFAULT_LENGTH = 60

_FENCE = re.compile(r"^\s*(```|~~~)")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_PREFIX = re.compile(r"^\s*(?:#{1,6}\s+|>\s*|[-*+]\s+(?:\[[ xX]\]\s+)?|\d{1,3}[.)]\s+)+")
# Unterstriche nur an Wortgrenzen (snake_case bleibt erhalten).
_EMPHASIS = re.compile(r"\*+|~~|`+|(?<!\w)_+|_+(?!\w)")
_TAG = re.compile(r"</?[A-Za-z][^>]*>")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SPACE = re.compile(r"\s+")


def clean_line(line: str) -> str:
    """Eine Zeile von Markdown-Zeichen befreien."""
    line = _CONTROL.sub(" ", line)
    line = _IMAGE.sub(r"\1", line)
    line = _LINK.sub(r"\1", line)
    line = _PREFIX.sub("", line)
    line = _TAG.sub("", line)
    line = _EMPHASIS.sub("", line)
    return _SPACE.sub(" ", line).strip()


def title_from(content: str, length: int = DEFAULT_LENGTH) -> str:
    """Titel aus dem Nachrichtentext; leer, wenn nichts Lesbares übrig bleibt."""
    in_fence = False
    fallback = ""
    for raw in (content or "").splitlines():
        if _FENCE.match(raw):
            in_fence = not in_fence
            continue
        if in_fence:
            if not fallback:
                fallback = clean_line(raw)
            continue
        line = clean_line(raw)
        if line:
            break
    else:
        line = fallback
    if len(line) > length:
        line = line[: length - 1].rstrip() + "…"
    return line
