"""Online-Status und Werkzeugliste je MCP-Server (M4a-09).

Prüfen heißt: verbinden (bei ``stdio`` den Prozess starten), ``initialize``,
``tools/list``. Das läuft wie im Betrieb über den Loop-Thread und die
Sitzungen aus :mod:`.client` (gleiches Server-Lock, ein ``stdio``-Prozess je
Server und Prozess), mit kurzem Zeitlimit ``min(timeout_seconds, 10 s)``.
Deaktivierte Server werden nie geprüft – auch kein Prozessstart.

Das Ergebnis steht am ``McpServer`` (``online``, ``last_checked``,
``last_online``, ``last_error``, ``reported_tools``), damit es für alle
gunicorn-Worker gilt. Geprüft wird

* nach dem Speichern im Admin und nach dem JSON-Import (nach dem Commit),
* auf Wunsch im Admin („Jetzt prüfen“, Aktion „Ausgewählte prüfen“),
* periodisch im Worker (``run_worker``): online-Server alle 5 Minuten,
  offline-Server jede Minute, noch nie geprüfte sofort.

Seitenaufrufe prüfen nie; sie lesen nur den gespeicherten Stand.

Sperre gegen Doppelprüfung wie bei den Anbietern (``chat/status.py``): bedingtes
UPDATE von ``last_checked`` als Anspruch; nur wer genau eine Zeile ändert,
prüft. Die Netzprüfung läuft danach ohne offene Transaktion.

Fehlerursachen sind deutsche Texte der Form „Kurzursache: Einzelheiten“ ohne
Zugangsdaten, ohne Pfad/Query der URL und ohne Rohtext des Servers. Das Log
nennt nur Server-IDs und die Kurzursache.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import socket
import ssl
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from django.db.models import Q
from django.utils import timezone
from django.utils.text import Truncator

from multigpt.chat.models import McpServer
from multigpt.chat.providers.base import endpoint_of, short_error

from .errors import McpError

logger = logging.getLogger(__name__)

CHECK_TIMEOUT_MAX = 10.0
ONLINE_RECHECK_SECONDS = 300
OFFLINE_RECHECK_SECONDS = 60
# Anspruch für Prüfungen von Hand: zwei Klicks kurz nacheinander prüfen einmal.
CLAIM_SECONDS = 15

DESCRIPTION_MAX = 300
NAME_MAX = 200
PARAMS_MAX = 30
TOOLS_MAX = 500
HINT_KEYS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")

ERR_REFUSED = (
    "Verbindung abgelehnt: {endpoint} nimmt keine Verbindung an. Läuft der Server, stimmt der Port?"
)
ERR_DNS = "Rechnername unbekannt: {host} lässt sich nicht auflösen. Bitte die URL prüfen."
ERR_UNREACHABLE = "Rechner nicht erreichbar: Keine Verbindung zu {endpoint}."
ERR_TLS = (
    "TLS-Fehler: Die sichere Verbindung zu {endpoint} ist fehlgeschlagen "
    "(Zertifikat oder http/https verwechselt?)."
)
ERR_CONNECT = "Keine Verbindung: {endpoint} ist nicht erreichbar."
ERR_TIMEOUT = "Zeitüberschreitung: Der Server hat nicht innerhalb von {seconds:g} s geantwortet."
ERR_AUTH = "Zugang abgelehnt (HTTP {status}): Token prüfen."
ERR_NOT_FOUND = (
    "Adresse nicht gefunden (HTTP 404): Pfad prüfen (z. B. …/mcp bzw. …/http, je nach Server)."
)
ERR_SERVER = "Serverfehler (HTTP {status}): Später erneut prüfen."
ERR_HTTP = "Anfrage abgelehnt (HTTP {status}): Adresse und Zugangsdaten prüfen."
ERR_CLOSED_HTTP = "Verbindung abgebrochen: {endpoint} hat die Verbindung beendet."
ERR_NOT_FOUND_CMD = (
    "Programm nicht gefunden: „{program}“ lässt sich nicht starten. Befehl und Installation prüfen."
)
ERR_NOT_EXECUTABLE = "Programm nicht ausführbar: „{program}“ darf nicht gestartet werden."
ERR_EXITED = (
    "Programm beendet sich sofort: „{program}“ hat die Verbindung geschlossen, bevor es "
    "geantwortet hat. Befehl, Argumente und Umgebungsvariablen prüfen."
)
ERR_PROTOCOL = "Protokollfehler: Der Server antwortet nicht wie ein MCP-Server (Code {code})."
ERR_SETUP = "Falsch eingerichtet: {detail}"
ERR_FAILED_IN_USE = (
    "Nicht erreichbar: Die Werkzeugliste ließ sich im Chat nicht abrufen. Genaue Ursache nach "
    "der nächsten Prüfung."
)
ERR_UNEXPECTED = "Unerwarteter Fehler: Die Prüfung ist fehlgeschlagen ({kind})."

_UNREACHABLE_ERRNOS = {errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EHOSTDOWN, errno.ENETDOWN}


@dataclass
class CheckOutcome:
    server_id: int
    online: bool
    error: str = ""
    tools: list[dict[str, Any]] = field(default_factory=list)
    skipped: str = ""  # nicht geprüft (deaktiviert, Anspruch verloren)

    @property
    def short_error(self) -> str:
        return short_error(self.error)


# --- Werkzeugliste -----------------------------------------------------------


def _clip(text, limit: int) -> str:
    return Truncator(" ".join(str(text or "").split())).chars(limit)


def summarize_tool(tool) -> dict[str, Any]:
    """Gekürzte, JSON-taugliche Beschreibung eines Werkzeugs für ``reported_tools``.

    Alles hier kommt vom Server und ist nicht vertrauenswürdig: Texte werden
    gekürzt (im Admin zusätzlich escaped), vom Eingabeschema bleiben nur die
    Parameternamen, von den annotations nur die bekannten Wahrheitswerte.
    """
    schema = tool.input_schema if isinstance(tool.input_schema, dict) else {}
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    params = [_clip(name, 60) for name in list(props)[:PARAMS_MAX]]
    entry: dict[str, Any] = {
        "name": str(tool.name)[:NAME_MAX],
        "description": _clip(tool.description or tool.title or "", DESCRIPTION_MAX),
        "params": params,
        "required": [name for name in params if name in required],
    }
    annotations = getattr(tool, "annotations", None)
    if annotations is not None:
        data = annotations.model_dump(by_alias=True, exclude_none=True)
        hints = {k: data[k] for k in HINT_KEYS if isinstance(data.get(k), bool)}
        if hints:
            entry["annotations"] = hints
    return entry


def suggestion(tool: dict) -> str:
    """Vorschlag aus den annotations: ``auto``, ``confirm`` oder ``""``.

    Nur Vorbelegung im Admin; der Server kann sich irren oder lügen.
    ``destructiveHint`` hat Vorrang.
    """
    hints = tool.get("annotations") or {}
    if hints.get("destructiveHint") is True:
        return "confirm"
    if hints.get("readOnlyHint") is True:
        return "auto"
    return ""


def rating(server: McpServer, name: str) -> str:
    """Einstufung eines Werkzeugs: ``auto`` (ohne Rückfrage), ``confirm`` oder ``""``."""
    if name in (server.tools_requiring_confirmation or []):
        return "confirm"
    if name in (server.known_tools or []):
        return "auto"
    return ""


def tool_changes(server: McpServer) -> tuple[list[str], list[str]]:
    """(neu, verschwunden) seit der letzten Einstufung.

    Neu: gemeldet, aber nicht eingestuft. Verschwunden: eingestuft, aber vom
    Server nicht mehr gemeldet (nur, wenn es eine Meldung gibt).
    """
    reported = [t.get("name") for t in server.reported_tools or [] if isinstance(t, dict)]
    rated = set(server.known_tools or []) | set(server.tools_requiring_confirmation or [])
    new = [name for name in reported if name not in rated]
    gone = sorted(rated - set(reported)) if server.last_online else []
    return new, gone


def unrated_count(server: McpServer) -> int:
    return len(tool_changes(server)[0])


# --- Fehlerursachen ------------------------------------------------------------


def _chain(exc: BaseException):
    seen = set()
    stack = [exc]
    while stack:
        item = stack.pop(0)
        if item is None or id(item) in seen:
            continue
        seen.add(id(item))
        yield item
        if isinstance(item, BaseExceptionGroup):
            stack.extend(item.exceptions)
        stack.append(item.__cause__ or item.__context__)


def _program(config) -> str:
    return Truncator(config.argv[0]).chars(80) if config.argv else "?"


def describe_error(exc: BaseException, config, seconds: float, http_status: int | None) -> str:
    """Deutsche Ursache zu einer gescheiterten Prüfung (ohne Zugangsdaten/Rohtext)."""
    from mcp import MCPError
    from mcp_types import CONNECTION_CLOSED, REQUEST_TIMEOUT

    from .errors import McpTimeout

    host, endpoint = endpoint_of(config.url) if config.transport == "http" else ("", "")
    if config.transport == "http" and http_status:
        if http_status in (401, 403):
            return ERR_AUTH.format(status=http_status)
        if http_status == 404:
            return ERR_NOT_FOUND
        if http_status >= 500:
            return ERR_SERVER.format(status=http_status)
        return ERR_HTTP.format(status=http_status)
    items = list(_chain(exc))
    for item in items:
        if isinstance(item, TimeoutError | McpTimeout):
            return ERR_TIMEOUT.format(seconds=seconds)
        if isinstance(item, MCPError) and item.code == REQUEST_TIMEOUT:
            return ERR_TIMEOUT.format(seconds=seconds)
    if config.transport == "stdio":
        for item in items:
            if isinstance(item, FileNotFoundError):
                return ERR_NOT_FOUND_CMD.format(program=_program(config))
            if isinstance(item, PermissionError):
                return ERR_NOT_EXECUTABLE.format(program=_program(config))
    for item in items:
        if isinstance(item, ssl.SSLError):
            return ERR_TLS.format(endpoint=endpoint)
        if isinstance(item, socket.gaierror):
            return ERR_DNS.format(host=host)
        if isinstance(item, ConnectionRefusedError):
            return ERR_REFUSED.format(endpoint=endpoint)
        if isinstance(item, OSError) and item.errno in _UNREACHABLE_ERRNOS:
            return ERR_UNREACHABLE.format(endpoint=endpoint)
    for item in items:
        if isinstance(item, MCPError):
            if item.code == CONNECTION_CLOSED:
                if config.transport == "stdio":
                    return ERR_EXITED.format(program=_program(config))
                return ERR_CLOSED_HTTP.format(endpoint=endpoint)
            return ERR_PROTOCOL.format(code=item.code)
    import anyio
    import httpx2

    for item in items:
        if isinstance(item, httpx2.ConnectError):
            return ERR_CONNECT.format(endpoint=endpoint)
        if isinstance(item, anyio.EndOfStream | anyio.ClosedResourceError | ConnectionError):
            if config.transport == "stdio":
                return ERR_EXITED.format(program=_program(config))
            return ERR_CLOSED_HTTP.format(endpoint=endpoint)
        if isinstance(item, httpx2.TransportError):
            return ERR_CLOSED_HTTP.format(endpoint=endpoint)
    return ERR_UNEXPECTED.format(kind=type(exc).__name__)


# --- Prüfung im Loop -----------------------------------------------------------


async def _probe(config, seconds: float, close_after: bool) -> tuple[list[dict] | None, str]:
    """Läuft im Loop-Thread (kein ORM). Ergebnis: (Werkzeuge, "") oder (None, Ursache)."""
    from . import client

    client.http_errors.pop(config.pk, None)
    conn = client.get_connection(config)
    was_connected = conn.connected
    try:
        async with asyncio.timeout(seconds):
            try:
                tools = await conn.list_tools(refresh=True)
            except Exception as exc:
                # Eine alte Sitzung kann tot sein: dann einmal frisch verbinden.
                if not (was_connected and client._is_connection_failure(exc)):
                    raise
                tools = await conn.list_tools(refresh=True)
        return [summarize_tool(t) for t in tools[:TOOLS_MAX]], ""
    except Exception as exc:
        return None, describe_error(exc, config, seconds, client.http_errors.pop(config.pk, None))
    finally:
        if close_after and not was_connected:
            # Worker: keine Prozesse für die nächste Prüfung offen halten.
            await client.close_server(config.pk)


async def _probe_all(jobs, close_after: bool):
    return await asyncio.gather(
        *(_probe(config, seconds, close_after) for config, seconds in jobs),
        return_exceptions=True,
    )


# --- Synchrone API ---------------------------------------------------------------


def check_timeout(server: McpServer) -> float:
    return float(min(server.timeout_seconds or CHECK_TIMEOUT_MAX, CHECK_TIMEOUT_MAX))


def _store(server: McpServer, tools: list[dict] | None, error: str) -> CheckOutcome:
    online = tools is not None
    was_offline = not server.online and bool(server.last_error)
    fields: dict[str, Any] = {"online": online, "last_error": "" if online else error}
    if online:
        fields["last_online"] = timezone.now()
        fields["reported_tools"] = tools
    McpServer.objects.filter(pk=server.pk).update(**fields)
    if online == was_offline:  # nur beim Wechsel (auch ungeprüft -> offline)
        logger.info(
            "MCP-Server %s ist jetzt %s%s",
            server.pk,
            "online" if online else "offline",
            "" if online else f" ({short_error(error)})",
        )
    for key, value in fields.items():
        setattr(server, key, value)
    return CheckOutcome(server.pk, online, "" if online else error, tools or [])


def _run_checks(servers: list[McpServer], close_after: bool) -> list[CheckOutcome]:
    """Prüft ``servers`` parallel im Loop und speichert die Ergebnisse."""
    from . import bridge
    from .config import ServerConfig

    outcomes: dict[int, CheckOutcome] = {}
    jobs, pending = [], []
    for server in servers:
        try:
            config = ServerConfig.from_model(server)
        except McpError as exc:
            # Formfehler (z. B. Befehl unlesbar): deutsch, ohne Zugangsdaten.
            detail = str(exc).split(": ", 1)[-1]
            outcomes[server.pk] = _store(server, None, ERR_SETUP.format(detail=detail))
            continue
        jobs.append((config, check_timeout(server)))
        pending.append(server)
    if jobs:
        limit = max(seconds for _, seconds in jobs) + bridge.GRACE_SECONDS + 3
        try:
            results = bridge.run(_probe_all(jobs, close_after), limit)
        except McpError:
            results = [TimeoutError()] * len(jobs)
        for server, (config, seconds), result in zip(pending, jobs, results, strict=True):
            if isinstance(result, BaseException):
                result = (None, describe_error(result, config, seconds, None))
            tools, error = result
            outcomes[server.pk] = _store(server, tools, error)
    return [outcomes[s.pk] for s in servers]


def _stamp(server: McpServer) -> None:
    now = timezone.now()
    McpServer.objects.filter(pk=server.pk).update(last_checked=now)
    server.last_checked = now


def force_check(server: McpServer, *, close_after: bool = False) -> CheckOutcome:
    """Prüft sofort (Admin „Jetzt prüfen“, nach Speichern/Import).

    Deaktivierte Server werden nicht geprüft (kein Prozessstart). Nicht in
    einer Transaktion aufrufen (Netzaufruf bis 10 s).
    """
    return force_check_many([server], close_after=close_after)[0]


def force_check_many(servers, *, close_after: bool = False) -> list[CheckOutcome]:
    servers = list(servers)
    active = [s for s in servers if s.active]
    for server in active:
        _stamp(server)
    done = {o.server_id: o for o in _run_checks(active, close_after)}
    return [
        done.get(s.pk)
        or CheckOutcome(s.pk, s.online, s.last_error, skipped="Server ist deaktiviert.")
        for s in servers
    ]


def _due(now) -> Q:
    online_stale = Q(online=True, last_checked__lt=now - timedelta(seconds=ONLINE_RECHECK_SECONDS))
    offline_stale = Q(
        online=False, last_checked__lt=now - timedelta(seconds=OFFLINE_RECHECK_SECONDS)
    )
    return Q(last_checked__isnull=True) | online_stale | offline_stale


def claim(server: McpServer, now=None) -> bool:
    """Prüfrecht per bedingtem UPDATE (siehe Moduldoku); setzt ``last_checked``."""
    now = now or timezone.now()
    won = McpServer.objects.filter(_due(now), pk=server.pk, active=True).update(last_checked=now)
    if won:
        server.last_checked = now
    return won == 1


def check_due(*, close_after: bool = True) -> list[CheckOutcome]:
    """Periodische Prüfung (Worker): fällige aktive Server, je Server höchstens
    eine Prüfung gleichzeitig über alle Prozesse."""
    now = timezone.now()
    servers = [s for s in McpServer.objects.filter(_due(now), active=True) if claim(s, now)]
    if not servers:
        return []
    return _run_checks(servers, close_after)


def invalidate(server_id: int) -> None:
    """Nächster Worker-Durchlauf prüft den Server sofort (z. B. nach Import/Update)."""
    McpServer.objects.filter(pk=server_id).update(last_checked=None)


def mark_failed(server: McpServer, exc: McpError) -> None:
    """Im Betrieb gescheitert (Werkzeugliste im Chat): als offline vermerken.

    Der Text von ``exc`` kann eine Meldung des Servers enthalten und wird
    deshalb nicht übernommen. Die genaue Ursache liefert die nächste Prüfung
    des Workers (nach einer Minute).
    """
    from .errors import McpTimeout

    if isinstance(exc, McpTimeout):
        error = ERR_TIMEOUT.format(seconds=float(server.timeout_seconds or 0))
    else:
        error = ERR_FAILED_IN_USE
    _stamp(server)
    _store(server, None, error)


# --- Anzeige ---------------------------------------------------------------------


def known_offline(server: McpServer) -> bool:
    """Zuletzt geprüft und dabei nicht erreichbar (ungeprüft gilt als verfügbar)."""
    return server.last_checked is not None and not server.online and bool(server.last_error)


def state_label(server: McpServer) -> str:
    if not server.active:
        return "deaktiviert"
    if server.last_checked is None or (not server.online and not server.last_error):
        return "ungeprüft"
    return "online" if server.online else "offline"


def offline_hint(names: list[str]) -> str:
    """Kurzer Hinweis für die Statuszeile im Chat."""
    quoted = ", ".join(f"„{n}“" for n in names)
    if len(names) == 1:
        return f"MCP-Server {quoted} ist offline – seine Werkzeuge stehen nicht zur Verfügung."
    return f"MCP-Server {quoted} sind offline – ihre Werkzeuge stehen nicht zur Verfügung."
