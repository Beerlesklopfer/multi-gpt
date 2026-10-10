"""Filter für die Quellenliste unter Antworten (M8 Websuche, M7 Dokumente).

Titel und URLs von Webquellen sind nicht vertrauenswürdig: Sie werden nur über
Auto-Escaping ausgegeben, verlinkt werden ausschließlich http(s)-URLs.
Dokumentquellen verlinken immer serverseitig auf den Abschnitt (``chunk_id``).
Gleiche Regeln wie in chat.js (Live-Anzeige aus dem SSE-Event ``sources``).

Zitieren: ``source_items`` formatiert die Quellen im Zitierstil des
Betrachters (``sources.serialize_all``); ``citations_json`` legt Kurzbeleg,
Eintrag und alle Stile für citations.js (Kopieren, Kurzbeleg im Text) als
JSON in ein Datenattribut (Auto-Escaping, kein Skript im HTML)."""

import json
from urllib.parse import urlsplit

from django import template
from django.urls import reverse

from ..sources import serialize_all

register = template.Library()

# So viele Quellen sind immer sichtbar; der Rest steht unter „Weitere Quellen“.
SOURCES_VISIBLE = 3


def _split(url):
    try:
        return urlsplit(str(url or "").strip())
    except ValueError:
        return None


@register.filter
def source_href(url) -> str:
    """Die URL, wenn sie als Link taugt (http/https mit Host), sonst ""."""
    parts = _split(url)
    if parts is None or parts.scheme.lower() not in ("http", "https"):
        return ""
    try:
        host = parts.hostname
    except ValueError:
        return ""
    return str(url).strip() if host else ""


@register.filter
def source_domain(url) -> str:
    """Host ohne „www.“ für die kleine Angabe neben dem Titel ("" ohne http(s))."""
    if not source_href(url):
        return ""
    host = _split(url).hostname or ""
    return host[4:] if host.startswith("www.") else host


def _is_document(source) -> bool:
    return getattr(source, "kind", "") == "document"


@register.filter
def source_link(source) -> str:
    """Ziel des Links: Webquelle nur http(s), Dokument die Abschnittsseite ("" = kein Link)."""
    if _is_document(source):
        chunk_id = getattr(source, "chunk_id", None)
        return reverse("chat:document_chunk", args=[chunk_id]) if chunk_id else ""
    return source_href(getattr(source, "url", ""))


@register.filter
def source_site(source) -> str:
    """Domain neben dem Titel, nur bei Webquellen."""
    return "" if _is_document(source) else source_domain(getattr(source, "url", ""))


@register.filter
def source_removed(source) -> bool:
    """Dokumentquelle, deren Abschnitt gelöscht wurde."""
    return _is_document(source) and not getattr(source, "chunk_id", None)


@register.filter
def source_label(source) -> str:
    """Anzeigetext: Titel, sonst Domain, sonst die URL; bei Dokumenten mit Seite."""
    title = " ".join(str(getattr(source, "title", "") or "").split())
    if _is_document(source):
        label = title or "Dokument"
        page = getattr(source, "page", None)
        return f"{label}, S. {page}" if page else label
    url = getattr(source, "url", "") or ""
    return title or source_domain(url) or str(url) or "Quelle"


@register.filter
def sources_head(sources):
    return list(sources)[:SOURCES_VISIBLE]


@register.filter
def sources_tail(sources):
    return list(sources)[SOURCES_VISIBLE:]


@register.simple_tag
def source_items(sources, user):
    """Quellen als Liste von dicts (wie im SSE-Event) plus Anzeige: ``link``,
    ``site``, ``removed`` und ``label`` (Dokument: Eintrag im Zitierstil)."""
    refs = list(sources)
    items = serialize_all(refs, user)
    for ref, item in zip(refs, items, strict=True):
        # Quelle aus einer Sammlung, die der Betrachter nicht lesen darf: kein Link.
        item["link"] = "" if item.get("restricted") else source_link(ref)
        item["site"] = source_site(ref)
        item["removed"] = source_removed(ref)
        item["label"] = item["entry"] if _is_document(ref) else source_label(ref)
    return items


CITATION_KEYS = ("n", "kind", "short", "entry", "inline", "style", "formats")


@register.filter
def citations_json(items) -> str:
    """Zitierdaten für ``data-citations`` (Escapen übernimmt das Template)."""
    data = [{k: item.get(k) for k in CITATION_KEYS} for item in items]
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
