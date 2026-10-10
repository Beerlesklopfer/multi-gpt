"""Seitenabruf mit SSRF-Schutz (M8-02, Plan 8d/9).

**Verfahren (Entscheidung):** Wir lösen den Rechnernamen selbst auf
(``socket.getaddrinfo``), prüfen *alle* gelieferten Adressen und verbinden uns
dann mit httpx **direkt mit der geprüften IP**: Die Anfrage geht an
``https://<IP>:<Port>/<Pfad>``, der ursprüngliche Name steht im ``Host``-Header
und – bei HTTPS – in der httpx-Erweiterung ``sni_hostname``. httpcore nutzt
diesen Namen als ``server_hostname`` für SNI *und* für die
Zertifikatsprüfung, TLS bleibt also vollständig gegen den echten Namen
geprüft. Weil httpx selbst keinen Namen mehr auflöst, gibt es kein Fenster
zwischen Prüfung und Verbindung (kein DNS-Rebinding: ein zweites
``getaddrinfo`` mit anderer Antwort findet nicht statt).

Verworfene Alternativen: Prüfen und danach httpx die URL normal auflösen
lassen (Rebinding-Lücke); ein eigenes httpcore-``NetworkBackend`` (bräuchte
private httpx-Interna, weil ``HTTPTransport`` kein Backend annimmt);
ein Proxy (zusätzlicher Dienst).

Weitere Regeln:

- nur ``http``/``https``, keine Zugangsdaten in der URL;
- abgelehnt wird, wenn *irgendeine* aufgelöste Adresse nicht öffentlich ist:
  privat (RFC 1918, ULA ``fc00::/7``), Loopback, link-local (inkl.
  ``169.254.169.254`` Metadaten-Dienst), Multicast, unspezifiziert,
  reserviert, CGNAT ``100.64/10`` sowie IPv6-Hüllen mit eingebetteter IPv4
  (``::ffff:a.b.c.d``, 6to4, Teredo, NAT64);
- Weiterleitungen folgt httpx nicht selbst; jeder Schritt (höchstens 3) wird
  erneut geprüft und neu aufgelöst;
- je Schritt ein eigener Client ohne Umgebungsproxys (``trust_env=False``),
  damit keine Verbindung für einen anderen Namen wiederverwendet wird;
- Gesamtzeitlimit über alle Schritte und das Lesen, Größenlimit auf den
  (entpackten) Inhalt, nur ``text/html``, ``application/xhtml+xml`` und
  ``text/plain`` (``fetch_raw`` mit eigener Liste: ``pages`` erlaubt zusätzlich
  JSON und PDF).

Die SearXNG-URL selbst ist ausgenommen; sie wird in ``searxng.py`` ohne diesen
Schutz angesprochen, weil sie nur aus der Verwalter-Konfiguration kommt.
"""

from __future__ import annotations

import codecs
import ipaddress
import re
import socket
import time
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx
from django.conf import settings

from .base import USER_AGENT, FetchError
from .extract import clip, html_to_text, normalize_text

MAX_REDIRECTS = 3
MAX_BYTES = 2 * 1024 * 1024
MAX_URL_LENGTH = 2000
HTML_TYPES = {"text/html", "application/xhtml+xml"}
TEXT_TYPES = HTML_TYPES | {"text/plain"}
REDIRECT_CODES = {301, 302, 303, 307, 308}

MSG_BAD_URL = "Ungültige Adresse."
MSG_SCHEME = "Nur http- und https-Adressen sind erlaubt."
MSG_BLOCKED = "Adresse im lokalen Netz – Abruf gesperrt."
MSG_DNS = "Rechnername nicht auflösbar."
MSG_TIMEOUT = "Zeitüberschreitung beim Abruf."
MSG_UNREACHABLE = "Seite nicht erreichbar."
MSG_TOO_LARGE = "Seite zu groß."
MSG_TYPE = "Kein Text (Inhaltstyp {type})."
MSG_HTTP = "Seite meldet HTTP {status}."
MSG_REDIRECTS = "Zu viele Weiterleitungen."

_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))
_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_\-:.]+)""", re.I)


@dataclass(frozen=True)
class Page:
    """Abgerufene Seite: endgültige URL, Titel (ggf. leer) und Text."""

    url: str
    title: str
    text: str


def _allow_private() -> bool:
    """Nur für Browsertests in der Entwicklung (DEBUG und eigene Einstellung)."""
    return bool(settings.DEBUG and getattr(settings, "WEBSEARCH_ALLOW_PRIVATE", False))


def is_public_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True nur für global erreichbare Unicast-Adressen."""
    if ip.version == 6:
        embedded = []
        if ip.ipv4_mapped is not None:
            embedded.append(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            embedded.append(ip.sixtofour)
        if ip.teredo is not None:
            embedded.extend(ip.teredo)
        if any(ip in net for net in _NAT64):
            embedded.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        if any(not is_public_address(e) for e in embedded):
            return False
    if (
        ip.is_multicast
        or ip.is_unspecified
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_reserved
    ):
        return False
    return ip.is_global


def resolve_public(host: str, port: int) -> str:
    """Rechnernamen auflösen; erste Adresse, wenn *alle* öffentlich sind."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        raise FetchError(MSG_DNS) from None
    addresses = []
    for info in infos:
        raw = str(info[4][0]).split("%", 1)[0]  # Zonen-ID (fe80::1%eth0) abtrennen
        try:
            addresses.append(ipaddress.ip_address(raw))
        except ValueError:
            raise FetchError(MSG_BLOCKED, blocked=True) from None
    if not addresses:
        raise FetchError(MSG_DNS)
    if not _allow_private() and not all(is_public_address(a) for a in addresses):
        raise FetchError(MSG_BLOCKED, blocked=True)
    return str(addresses[0])


def _parse(url: str) -> httpx.URL:
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LENGTH:
        raise FetchError(MSG_BAD_URL)
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, ValueError, TypeError):
        raise FetchError(MSG_BAD_URL) from None
    if parsed.scheme not in ("http", "https"):
        raise FetchError(MSG_SCHEME, blocked=True)
    if not parsed.raw_host or parsed.userinfo:
        raise FetchError(MSG_BAD_URL)
    return parsed


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _charset(content_type: str, head: bytes) -> str:
    candidates = []
    for param in content_type.split(";")[1:]:
        key, _, value = param.partition("=")
        if key.strip().lower() == "charset":
            candidates.append(value.strip().strip("\"'"))
    match = _META_CHARSET.search(head[:4096])
    if match:
        candidates.append(match.group(1).decode("ascii", "ignore"))
    for name in candidates:
        try:
            return codecs.lookup(name).name
        except LookupError:
            continue
    return "utf-8"


def _read_limited(response: httpx.Response, deadline: float, max_bytes: int) -> bytes:
    length = response.headers.get("content-length")
    if length and length.isdigit() and int(length) > max_bytes:
        raise FetchError(MSG_TOO_LARGE)
    chunks, total = [], 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise FetchError(MSG_TOO_LARGE)
        if time.monotonic() > deadline:
            raise FetchError(MSG_TIMEOUT)
        chunks.append(chunk)
    return b"".join(chunks)


def _request(parsed: httpx.URL, ip: str, remaining: float, user_agent: str = USER_AGENT):
    """Client und Anfrage an die geprüfte IP (Host-Header und SNI = Originalname)."""
    host = parsed.raw_host.decode("ascii")
    target = parsed.copy_with(host=ip)
    headers = {
        "Host": parsed.netloc.decode("ascii"),
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
        "Accept-Language": "de,en;q=0.7",
    }
    extensions = {}
    if parsed.scheme == "https" and not _is_ip_literal(host):
        extensions["sni_hostname"] = host
    client = httpx.Client(
        timeout=httpx.Timeout(max(remaining, 0.1)),
        follow_redirects=False,
        trust_env=False,
    )
    request = client.build_request("GET", target, headers=headers, extensions=extensions)
    return client, request


@dataclass(frozen=True)
class RawPage:
    """Rohinhalt einer Seite: endgültige URL, Inhaltstyp (ohne Parameter), Bytes."""

    url: str
    mime: str
    content_type: str
    body: bytes

    def decoded(self) -> str:
        return self.body.decode(_charset(self.content_type, self.body), errors="replace")


def fetch_raw(
    url: str,
    *,
    timeout: float,
    types=TEXT_TYPES,
    max_bytes: int = MAX_BYTES,
    user_agent: str = USER_AGENT,
    guard=None,
) -> RawPage:
    """Seite SSRF-geschützt abrufen (nur Inhaltstypen aus ``types``);
    ``FetchError`` bei jedem Problem. Grundlage für ``fetch_text`` und
    ``pages`` (fetch_url, crawl_site). ``guard(host)`` prüft jeden Schritt
    (auch nach Weiterleitungen) vor der Auflösung, z. B. gesperrte Domains."""
    deadline = time.monotonic() + timeout
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        parsed = _parse(current)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if guard is not None:
            guard(parsed.host)
        ip = resolve_public(parsed.raw_host.decode("ascii"), port)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchError(MSG_TIMEOUT)
        client, request = _request(parsed, ip, remaining, user_agent)
        try:
            with client:
                response = client.send(request, stream=True)
                try:
                    if response.status_code in REDIRECT_CODES:
                        location = response.headers.get("location")
                        if not location:
                            raise FetchError(MSG_HTTP.format(status=response.status_code))
                        current = urljoin(str(parsed), location)
                        continue
                    if not 200 <= response.status_code < 300:
                        raise FetchError(MSG_HTTP.format(status=response.status_code))
                    content_type = response.headers.get("content-type", "")
                    mime = content_type.split(";", 1)[0].strip().lower()
                    if mime not in types:
                        raise FetchError(MSG_TYPE.format(type=mime or "unbekannt"))
                    body = _read_limited(response, deadline, max_bytes)
                finally:
                    response.close()
        except httpx.TimeoutException:
            raise FetchError(MSG_TIMEOUT) from None
        except httpx.HTTPError:
            raise FetchError(MSG_UNREACHABLE) from None
        return RawPage(url=str(parsed), mime=mime, content_type=content_type, body=body)
    raise FetchError(MSG_REDIRECTS)


def fetch_text(url: str, *, timeout: float, max_chars: int, max_bytes: int = MAX_BYTES) -> Page:
    """Seite abrufen und auf Text reduzieren; ``FetchError`` bei jedem Problem."""
    raw = fetch_raw(url, timeout=timeout, max_bytes=max_bytes)
    decoded = raw.decoded()
    if raw.mime in HTML_TYPES:
        title, text = html_to_text(decoded)
    else:
        title, text = "", normalize_text(decoded)
    return Page(url=raw.url, title=title[:300], text=clip(text, max_chars))
