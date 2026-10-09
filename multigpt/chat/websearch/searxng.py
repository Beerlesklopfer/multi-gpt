"""SearXNG als Such-Backend (M8-01).

Such-API laut https://docs.searxng.org/dev/search_api.html: ``GET /search``
mit ``q``, ``format=json``, ``language``, ``pageno``, ``safesearch``
(optional ``categories``). Die Antwort enthält ``results[]`` mit ``title``,
``url`` und ``content`` (Kurztext). Ist das Format nicht in
``search.formats`` freigegeben, antwortet SearXNG mit 403.

Die URL kommt aus ``SearchSettings`` (nur Verwalter) und liegt im Intranet;
sie ist deshalb vom SSRF-Schutz des Seitenabrufs ausgenommen.
"""

from __future__ import annotations

import httpx

from ..providers.base import check_error_message
from .base import USER_AGENT, SearchError, SearchHit
from .extract import normalize_text

MAX_TITLE = 300
MAX_SNIPPET = 600

MSG_NO_URL = "Keine SearXNG-URL eingetragen."
MSG_FORMAT = (
    "JSON-Format nicht freigegeben (HTTP 403): In settings.yml unter search.formats "
    "„json“ ergänzen und SearXNG neu starten."
)
MSG_RATE_LIMIT = (
    "Zu viele Anfragen (HTTP 429): Der Bot-Schutz (Limiter) von SearXNG blockiert MultiGPT. "
    "Limiter abschalten (server.limiter: false) oder die IP des MultiGPT-Servers in "
    "botdetection pass_ip eintragen."
)
MSG_NOT_FOUND = "Adresse nicht gefunden (HTTP 404): Bitte die SearXNG-URL prüfen."
MSG_SERVER = "Serverfehler bei SearXNG (HTTP {status}): Später erneut versuchen."
MSG_REDIRECT = (
    "Weiterleitung (HTTP {status}): Bitte die SearXNG-URL genau angeben (http/https, Pfad)."
)
MSG_HTTP = "SearXNG hat die Anfrage abgelehnt (HTTP {status})."
MSG_INVALID = (
    "Ungültige Antwort: SearXNG hat kein JSON geliefert. Ist die URL die Basisadresse der Instanz?"
)
MSG_BAD_URL = "Ungültige SearXNG-URL: Bitte die Adresse prüfen (z. B. http://searx.intern:8888)."


def search_url(base_url: str) -> str:
    """``<Basis>/search`` – mit oder ohne Pfadpräfix und Schrägstrich am Ende."""
    return base_url.strip().rstrip("/") + "/search"


def _http_message(status: int) -> str:
    if status == 403:
        return MSG_FORMAT
    if status == 429:
        return MSG_RATE_LIMIT
    if status == 404:
        return MSG_NOT_FOUND
    if 300 <= status < 400:
        return MSG_REDIRECT.format(status=status)
    if status >= 500:
        return MSG_SERVER.format(status=status)
    return MSG_HTTP.format(status=status)


def _text(value, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return normalize_text(value).replace("\n", " ")[:limit]


def parse_results(data, limit: int) -> list[SearchHit]:
    """Treffer aus der JSON-Antwort; nur http(s)-URLs, doppelte URLs einmal."""
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise SearchError(MSG_INVALID)
    hits: list[SearchHit] = []
    seen = set()
    for item in data["results"]:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
            continue
        if len(url) > 2000 or url in seen:
            continue
        seen.add(url)
        title = _text(item.get("title"), MAX_TITLE) or url[:MAX_TITLE]
        hits.append(
            SearchHit(title=title, url=url, snippet=_text(item.get("content"), MAX_SNIPPET))
        )
        if len(hits) >= limit:
            break
    return hits


class SearxngBackend:
    def __init__(
        self, base_url: str, *, language: str = "de", safesearch: int = 1, timeout: float = 10
    ):
        self.base_url = (base_url or "").strip()
        self.language = (language or "").strip() or "all"
        self.safesearch = safesearch
        self.timeout = timeout

    def search(self, query: str, *, limit: int) -> list[SearchHit]:
        if not self.base_url:
            raise SearchError(MSG_NO_URL)
        url = search_url(self.base_url)
        params = {
            "q": query,
            "format": "json",
            "language": self.language,
            "pageno": 1,
            "safesearch": self.safesearch,
        }
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": self.language if self.language != "all" else "de",
        }
        try:
            with httpx.Client(timeout=self.timeout, trust_env=False) as client:
                response = client.get(url, params=params, headers=headers)
        except (httpx.InvalidURL, httpx.UnsupportedProtocol):
            raise SearchError(MSG_BAD_URL) from None
        except httpx.HTTPError as exc:
            raise SearchError(check_error_message(exc, url)) from None
        if response.status_code != 200:
            raise SearchError(_http_message(response.status_code))
        try:
            data = response.json()
        except ValueError:
            raise SearchError(MSG_INVALID) from None
        return parse_results(data, limit)
