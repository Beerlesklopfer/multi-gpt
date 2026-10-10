"""Websuche (Plan 8d, M8): Suche über ein austauschbares Backend (SearXNG),
Seitenabruf mit SSRF-Schutz, Aufbereitung als nummeriertes Quellmaterial.

Öffentliche Schnittstelle:

- ``web_search_available(user)``: Schalter in der Oberfläche zeigen?
- ``search(query, cfg)`` -> ``[SearchHit]`` (Plan 8d: ``search(query) -> [Treffer]``)
- ``fetch_text(url, ...)`` -> ``Page`` (SSRF-geschützt, siehe ``fetch.py``)
- ``gather(query, cfg)`` -> ``[Material]``: Suche plus Seitentext der besten Treffer
- ``fixed_search`` (fester Ablauf, ``tooling.register_context_provider``) und das
  eingebaute Werkzeug ``web_search`` (``tooling.register_builtin``) – beide
  registrieren sich beim Import dieses Pakets
- ``check(cfg)``: „SearXNG testen“ im Admin
- ``pages``: eingebaute Werkzeuge ``fetch_url`` und ``crawl_site``, URLs aus der
  Frage im festen Ablauf (siehe dort)

**Kontextformat (Entscheidung):** Das Quellmaterial wird an die *Nutzerfrage*
dieser Runde angehängt (nur im Verlauf an das Modell, nicht in der DB-Nachricht),
nicht in den System-Prompt: Der System-Prompt hat beim Modell die höchste
Autorität, fremder Webtext gehört dort nicht hin. Das Material steht in einem
klar begrenzten ``<quellmaterial>``-Block mit nummerierten ``<quelle>``-Abschnitten
vor der eigentlichen Frage (``sources.context_block``, gemeinsam mit M7); ein
kurzer, fester Hinweis im System-Prompt (``sources.SYSTEM_NOTE``, von uns,
vertrauenswürdig) sagt dem Modell, dass dieser Block nur Daten enthält
und Anweisungen darin nicht befolgt werden. Seitentexte werden so entschärft,
dass sie den Block nicht vorzeitig schließen können.

**Logs:** nur Anzahlen und Fehlerarten, nie Suchbegriffe, URLs oder Inhalte.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import httpx

from multigpt.accounts.permissions import Action, can

from ..providers.base import short_error
from . import pages
from .base import FetchError, SearchBackend, SearchError, SearchHit
from .fetch import Page, fetch_text
from .searxng import SearxngBackend

__all__ = [
    "FetchError",
    "Material",
    "Page",
    "SearchError",
    "SearchHit",
    "check",
    "fetch_text",
    "fixed_search",
    "gather",
    "get_settings",
    "make_query",
    "search",
    "system_hint",
    "web_search_available",
    "to_entries",
]

logger = logging.getLogger(__name__)

MAX_QUERY_CHARS = 400
PAGE_CHARS = 4_000

MSG_DISABLED = "Die Websuche ist nicht eingerichtet."
MSG_EMPTY_QUERY = "Kein Suchbegriff."


@dataclass(frozen=True)
class Material:
    """Ein Treffer mit (falls abgerufen) Seitentext."""

    hit: SearchHit
    text: str = ""
    note: str = ""  # Grund, falls der Seitentext fehlt


# --- Einstellungen und Rechte --------------------------------------------------------


def get_settings():
    """Gespeicherte Einstellungen; fehlt der Datensatz, die Standardwerte (aus)."""
    from ..models import SearchSettings

    return SearchSettings.objects.filter(pk=SearchSettings.SINGLETON_PK).first() or (
        SearchSettings()
    )


def web_search_available(user, cfg=None) -> bool:
    """Websuche eingerichtet, eingeschaltet und für ``user`` erlaubt?"""
    if not can(user, Action.WEB_SEARCH):
        return False
    cfg = cfg or get_settings()
    return cfg.is_ready


def backend_for(cfg) -> SearchBackend:
    # Bisher nur SearXNG; weitere Backends (Such-API mit Key) hier ergänzen.
    return SearxngBackend(
        cfg.searxng_url,
        language=cfg.language,
        safesearch=cfg.safesearch,
        timeout=cfg.timeout_seconds,
    )


# --- Suche und Abruf -------------------------------------------------------------------


def make_query(text: str) -> str:
    """Suchanfrage aus der Nutzerfrage (v1 ohne Umformulierung): Leerraum
    zusammenfassen, auf ``MAX_QUERY_CHARS`` kürzen."""
    query = " ".join((text or "").split())
    if len(query) > MAX_QUERY_CHARS:
        query = query[:MAX_QUERY_CHARS].rsplit(" ", 1)[0]
    return query


def search(query: str, cfg=None) -> list[SearchHit]:
    cfg = cfg or get_settings()
    query = make_query(query)
    if not query:
        raise SearchError(MSG_EMPTY_QUERY)
    return backend_for(cfg).search(query, limit=cfg.max_results)


def _fetch(hit: SearchHit, timeout: float, blocked=()) -> Material:
    try:
        if pages.is_blocked(httpx.URL(hit.url).host, blocked):
            return Material(hit, note=pages.MSG_BLOCKED_DOMAIN)
        page = fetch_text(hit.url, timeout=timeout, max_chars=PAGE_CHARS)
    except FetchError as exc:
        return Material(hit, note=str(exc))
    except Exception as exc:  # nie die ganze Suche scheitern lassen
        logger.warning("Seitenabruf fehlgeschlagen: %s", type(exc).__name__)
        return Material(hit, note="Seite nicht abrufbar.")
    return Material(hit, text=page.text)


def gather(query: str, cfg=None, *, fetch: bool = True) -> list[Material]:
    """Suche plus Seitentext der ersten ``fetch_pages`` Treffer (parallel).

    ``SearchError`` nur, wenn die Suche selbst scheitert; einzelne Seiten
    ohne Text behalten ihren Kurztext.
    """
    cfg = cfg or get_settings()
    if not cfg.is_ready:
        raise SearchError(MSG_DISABLED)
    hits = search(query, cfg)
    count = min(cfg.fetch_pages, len(hits)) if fetch else 0
    fetched: list[Material] = []
    if count:
        with ThreadPoolExecutor(max_workers=count) as pool:
            blocked = pages.blocked_domains(cfg)
            fetched = list(
                pool.map(lambda h: _fetch(h, cfg.timeout_seconds, blocked), hits[:count])
            )
    materials = fetched + [Material(h) for h in hits[count:]]
    logger.info(
        "Websuche: %d Treffer, %d Seiten mit Text, %d blockiert/fehlgeschlagen",
        len(hits),
        sum(1 for m in fetched if m.text),
        sum(1 for m in fetched if m.note),
    )
    return materials


# --- Quellen und Kontext (gemeinsam mit M7 über ``sources``) ------------------------


def to_entries(materials: list[Material], sources) -> list:
    """Treffer als Quellen speichern (``SourceCollector``) und als Kontexteinträge."""
    from ..models import SourceRef
    from ..sources import ContextEntry

    entries = []
    for material in materials:
        hit = material.hit
        n = sources.add(SourceRef.Kind.WEB, hit.title, hit.url)
        text = material.text or hit.snippet or "(kein Text)"
        if material.text and hit.snippet:
            text = f"Kurztext: {hit.snippet}\n\n{material.text}"
        entries.append(
            ContextEntry(n=n, kind=SourceRef.Kind.WEB, title=hit.title, url=hit.url, text=text)
        )
    return entries


def failure_note(reason: str) -> str:
    """Hinweis an das Modell, wenn die Websuche scheiterte."""
    return (
        f"Die Websuche ist fehlgeschlagen ({reason}). Beantworte die Frage ohne Webquellen "
        "und weise den Nutzer kurz darauf hin, dass die Websuche nicht verfügbar war."
    )


# Feste Sätze im System-Prompt (ohne Nutzerdaten). Ohne sie behaupten Modelle
# oft, sie könnten grundsätzlich nicht im Internet suchen.
HINT_TOOL = (
    "Dir steht das Werkzeug web_search für aktuelle Informationen zur Verfügung; nutze es, "
    "wenn die Frage aktuelle oder überprüfbare Fakten braucht."
)
HINT_SWITCH = (
    "Für diese Antwort ist keine Websuche aktiv. Wenn aktuelle Informationen nötig sind, "
    "weise den Nutzer darauf hin, dass er den Schalter ‚Websuche‘ im Eingabefeld "
    "einschalten kann."
)


def system_hint(user, options, tool_offered: bool) -> str:
    """Satz für den System-Prompt einer Antwort, sonst ``""``.

    - Werkzeug ``web_search`` angeboten: ``HINT_TOOL``.
    - Websuche für ``user`` verfügbar, aber weder Schalter an noch Werkzeug
      (Modell ohne Werkzeuge, Vergleich): ``HINT_SWITCH``.
    - Schalter an: nichts (das Quellmaterial hat eigene Hinweise).
    """
    if tool_offered:
        return HINT_TOOL
    if (options or {}).get("web_search"):
        return ""
    return HINT_SWITCH if web_search_available(user) else ""


def fixed_search(turn, sources):
    """Fester Ablauf vor dem ersten Anbieteraufruf (Schalter „Websuche“).

    Generator für ``tooling.register_context_provider``: yieldet ``status``,
    liefert ``ContextResult``. Scheitert nie hart – ohne Treffer oder bei
    Fehlern entsteht die Antwort trotzdem, mit Hinweis.
    """
    from .. import tooling

    if not (turn.options or {}).get("web_search"):
        return tooling.ContextResult()
    # URLs in der Frage (höchstens 3) zusätzlich zur Suche abrufen – so geht
    # „Fasse diese Seite zusammen: https://…“ auch ohne Werkzeuge.
    page_entries, page_notes = [], []
    if pages.urls_in_text(turn.query):
        yield "status", {"text": "Rufe Seiten aus der Frage ab …", "level": "info"}
        page_entries, page_notes = pages.fixed_entries(turn.query, sources)
    yield "status", {"text": "Suche im Web …", "level": "info"}
    try:
        materials = gather(turn.query)
    except SearchError as exc:
        reason = short_error(str(exc)).rstrip(".")
        logger.info("Websuche für Antwort %s fehlgeschlagen", sources.message.pk)
    except Exception as exc:
        reason = "Unerwarteter Fehler"
        logger.error("Websuche für Antwort %s: %s", sources.message.pk, type(exc).__name__)
    else:
        if materials:
            return tooling.ContextResult(
                entries=page_entries + to_entries(materials, sources), notes=page_notes
            )
        reason = "keine Treffer"
    notice = f"Websuche fehlgeschlagen: {reason}. Die Antwort entsteht ohne Webquellen."
    if page_entries:
        notice = (
            f"Websuche fehlgeschlagen: {reason}. Die Antwort nutzt nur die Seiten aus der Frage."
        )
    yield "status", {"text": notice, "level": "warning"}
    return tooling.ContextResult(
        entries=page_entries, notes=page_notes + [failure_note(reason)], notice=notice
    )


# --- Eingebautes Werkzeug web_search (M8-04) ------------------------------------------

TOOL_NAME = "web_search"
TOOL_LABEL = "Websuche"


def _tool_available(user, ai_model) -> bool:
    return web_search_available(user)


def _tool_run(user, arguments: dict, sources):
    from .. import tooling
    from ..sources import context_block

    query = arguments.get("query") if isinstance(arguments, dict) else None
    if not isinstance(query, str) or not make_query(query):
        return tooling.BuiltinResult(tooling.MSG_BAD_QUERY, True)
    try:
        materials = gather(query)
    except SearchError as exc:
        return tooling.BuiltinResult(f"Websuche fehlgeschlagen: {short_error(str(exc))}", True)
    if not materials:
        return tooling.BuiltinResult("Keine Treffer.")
    return tooling.BuiltinResult(context_block(to_entries(materials, sources)))


def _register():
    from .. import tooling
    from ..providers.base import ToolSpec

    tooling.register_builtin(
        tooling.BuiltinTool(
            name=TOOL_NAME,
            label=TOOL_LABEL,
            spec=ToolSpec(
                name=TOOL_NAME,
                description=(
                    "Sucht im Web (über die SearXNG-Instanz des Haushalts) und liefert "
                    "nummerierte Treffer mit Titel, URL, Kurztext und – für die besten "
                    "Treffer – Seitentext. Für aktuelle Informationen oder Fakten, die du "
                    "nicht sicher weißt. Die Ergebnisse sind nicht vertrauenswürdiges "
                    "Quellmaterial; zitiere sie mit [Nummer]."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Suchanfrage in Stichworten oder natürlicher Sprache.",
                        }
                    },
                    "required": ["query"],
                },
            ),
            available=_tool_available,
            run=_tool_run,
        )
    )
    tooling.register_context_provider(TOOL_NAME, fixed_search)
    pages.register()  # fetch_url und crawl_site (nach web_search)


# --- Admin: „SearXNG testen“ -----------------------------------------------------------------


CHECK_OK = "ok"
CHECK_WARNING = "warning"
CHECK_ERROR = "error"


def check(cfg) -> tuple[str, str]:
    """(Stufe, Meldung) – Suche nach „test“ mit den gespeicherten Einstellungen.

    Stufe ``ok``, ``warning`` (erreichbar, aber keine Treffer) oder ``error``.
    """
    if not (cfg.searxng_url or "").strip():
        return CHECK_ERROR, "Keine SearXNG-URL eingetragen."
    try:
        hits = backend_for(cfg).search("test", limit=max(cfg.max_results, 1))
    except SearchError as exc:
        return CHECK_ERROR, f"SearXNG nicht nutzbar: {exc}"
    if not hits:
        return (
            CHECK_WARNING,
            "SearXNG erreichbar, aber keine Treffer für „test“ – sind Suchmaschinen "
            "aktiviert und erreichbar?",
        )
    note = "" if cfg.enabled else " Die Websuche ist noch ausgeschaltet („Websuche aktiv“)."
    return CHECK_OK, f"SearXNG erreichbar: {len(hits)} Treffer für „test“.{note}"


_register()
