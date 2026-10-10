"""Literaturangaben per DOI bei Crossref nachschlagen (optional, standardmäßig aus).

``lookup(doi)`` fragt ``https://api.crossref.org/works/{doi}`` ab und liefert
die Angaben als ``citations.Reference`` (nur, was Crossref kennt) oder None.

Datenschutz: Die Abfrage verrät Crossref, welche Dokumente hier liegen. Sie
läuft deshalb nur mit ``RagSettings.crossref_enabled`` (Admin, Voreinstellung
aus). Ein User-Agent mit ``mailto:`` aus den Einstellungen ordnet die Abfrage
dem „polite pool“ zu (Crossref-Empfehlung).

Sicherheit (SSRF): Ziel ist fest ``https://api.crossref.org``; die DOI geht
nur URL-kodiert in den Pfad, Weiterleitungen werden nicht verfolgt, die Antwort
ist größenbegrenzt. Jeder Fehler (Netz, Zeitüberschreitung, kaputtes JSON)
ergibt None – die Indexierung hängt nie davon ab.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

from .. import citations

logger = logging.getLogger(__name__)

API_BASE = "https://api.crossref.org/works/"
TIMEOUT = 5.0  # Sekunden
MAX_BYTES = 512 * 1024
USER_AGENT = "MultiGPT/1.0 (Familien-Chat; Literaturangaben)"

# Crossref-Typ -> citations-Art.
TYPES = {
    "journal-article": citations.TYPE_ARTICLE,
    "book-chapter": citations.TYPE_CHAPTER,
    "book-section": citations.TYPE_CHAPTER,
    "book-part": citations.TYPE_CHAPTER,
    "reference-entry": citations.TYPE_CHAPTER,
    "proceedings-article": citations.TYPE_CONFERENCE,
    "book": citations.TYPE_BOOK,
    "monograph": citations.TYPE_BOOK,
    "reference-book": citations.TYPE_BOOK,
    "edited-book": citations.TYPE_EDITED,
    "report": citations.TYPE_REPORT,
    "standard": citations.TYPE_STANDARD,
}


def _first(value) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    return " ".join(str(value or "").split())


def _people(items) -> tuple[str, ...]:
    people = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        family, given = _first(item.get("family")), _first(item.get("given"))
        name = _first(item.get("name"))
        if family:
            people.append(f"{family}, {given}" if given else family)
        elif name:
            people.append(name)
    return tuple(people[:50])


def _date(message: dict) -> str:
    for key in ("published-print", "published-online", "issued", "published"):
        parts = (message.get(key) or {}).get("date-parts") or []
        if parts and isinstance(parts[0], list) and parts[0] and parts[0][0]:
            values = [int(x) for x in parts[0][:3] if isinstance(x, int)]
            if values and 1450 <= values[0] <= 2200:
                return "-".join([f"{values[0]:04d}", *(f"{v:02d}" for v in values[1:])])
    return ""


def _isbns(message: dict) -> tuple[str, str]:
    """(Print, eBook) aus ``isbn-type``, sonst die erste ISBN als Print."""
    printed = electronic = ""
    for item in message.get("isbn-type") or []:
        if not isinstance(item, dict):
            continue
        value = _first(item.get("value"))
        if item.get("type") == "print" and not printed:
            printed = value
        elif item.get("type") == "electronic" and not electronic:
            electronic = value
    if not (printed or electronic):
        printed = _first(message.get("ISBN"))
    return printed, electronic


def parse(message: dict, doi: str) -> citations.Reference:
    """Crossref-``message`` -> Reference (unbekannte Felder bleiben leer)."""
    kind = TYPES.get(_first(message.get("type")), citations.TYPE_OTHER)
    container = _first(message.get("container-title"))
    printed, electronic = _isbns(message)
    series = ""
    if kind in (citations.TYPE_BOOK, citations.TYPE_EDITED, *citations.IN_CONTAINER):
        # Bei Kapiteln ist container-title oft [Buch, Reihe]; die Reihe steht hinten.
        titles = message.get("container-title") or []
        if isinstance(titles, list) and len(titles) > 1:
            series = _first(titles[-1])
    return citations.Reference(
        title=_first(message.get("title")),
        type=kind,
        authors=_people(message.get("author")),
        editors=_people(message.get("editor")),
        date=_date(message),
        container=container if kind != citations.TYPE_ARTICLE else "",
        journal=container if kind == citations.TYPE_ARTICLE else "",
        publisher=_first(message.get("publisher")),
        place=_first(message.get("publisher-location")),
        edition=_first(message.get("edition-number")),
        series=series,
        volume=_first(message.get("volume")),
        issue=_first(message.get("issue")),
        pages=_first(message.get("page")),
        isbn=printed,
        isbn_e=electronic,
        doi=doi,
    )


def lookup(doi: str, mailto: str = "") -> citations.Reference | None:
    """Angaben zu ``doi`` bei Crossref; None bei jedem Fehler."""
    doi = citations.normalize_doi(doi)
    try:
        citations.validate_doi(doi)
    except Exception:  # noqa: BLE001 - ungültige DOI: gar nicht erst fragen
        return None
    agent = USER_AGENT[:-1] + (f"; mailto:{mailto})" if mailto else ")")
    url = API_BASE + quote(doi, safe="")
    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=False) as client:
            response = client.get(url, headers={"User-Agent": agent, "Accept": "application/json"})
        if response.status_code != 200 or len(response.content) > MAX_BYTES:
            logger.info("Crossref: Antwort %s", response.status_code)
            return None
        message = response.json().get("message")
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        logger.info("Crossref nicht erreichbar oder Antwort unlesbar (%s)", type(exc).__name__)
        return None
    if not isinstance(message, dict):
        return None
    return parse(message, doi)
