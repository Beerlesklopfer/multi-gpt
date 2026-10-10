"""MCP-Sitzungen im Loop-Thread (M4a-03).

Läuft ausschließlich im Loop-Thread aus :mod:`.bridge`. Je Server gibt es eine
:class:`Connection`:

* Ein langlebiger **Halte-Task** betritt und verlässt den ``mcp.Client``-
  Context-Manager. Das SDK nutzt anyio-TaskGroups und Cancel-Scopes, die im
  selben Task betreten und verlassen werden müssen; deshalb besitzt genau dieser
  Task die Sitzung (und bei ``stdio`` den Kindprozess) und wartet, bis er zum
  Schließen aufgefordert wird.
* Aufrufe (``tools/list``, ``tools/call``) laufen in den Tasks der Aufrufer
  direkt über die Sitzung. JSON-RPC ordnet Antworten über die Request-ID zu, und
  der Dispatcher des SDK verwaltet beliebig viele gleichzeitige Anfragen; die
  Spezifikation 2026-07-28 kennt zudem keinen Sitzungszustand zwischen Aufrufen.
  Parallele Aufrufe auf einer Sitzung sind daher erlaubt und werden nicht
  serialisiert.
* Ein ``asyncio.Lock`` je Server schützt Verbindungsaufbau und Neuverbindung:
  Brauchen mehrere Threads gleichzeitig denselben Server, wird der
  ``stdio``-Prozess nur einmal gestartet.

Bricht die Verbindung ab (Prozess beendet, Netzfehler), wird die Sitzung
geschlossen; der nächste Aufruf verbindet neu. Unbenutzte Sitzungen werden nach
:data:`IDLE_SECONDS` geschlossen.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import time
from collections.abc import AsyncIterator
from typing import Any

import anyio
import httpx2
from mcp import Client, MCPError, StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import CONNECTION_CLOSED, REQUEST_TIMEOUT, Implementation

from . import bridge
from .config import ServerConfig
from .errors import McpError, McpTimeout
from .results import ToolResult, convert_result

logger = logging.getLogger(__name__)

TOOLS_TTL_SECONDS = 300.0
IDLE_SECONDS = 600.0
REAP_INTERVAL = 60.0
CLOSE_TIMEOUT = 8.0
MAX_LIST_PAGES = 100
CLIENT_INFO = Implementation(name="multi-gpt", version="0.1.0")

_connections: dict[int, Connection] = {}
# Letzter HTTP-Fehlerstatus je Server (für die Statusprüfung, chat/mcp/status.py):
# Das SDK meldet z. B. 401 und 500 nur als allgemeinen JSON-RPC-Fehler.
http_errors: dict[int, int] = {}
_background: set[asyncio.Task] = set()
_reaper: asyncio.Task | None = None


def _keep(task: asyncio.Task) -> None:
    _background.add(task)
    task.add_done_callback(_background.discard)


def _errlog():
    """Ziel für stderr des Kindprozesses (braucht einen echten Dateideskriptor)."""
    stream = sys.__stderr__
    try:
        if stream is not None:
            stream.fileno()
            return stream
    except (OSError, ValueError, AttributeError):
        pass
    return open(os.devnull, "w")  # noqa: SIM115 - lebt so lange wie der Prozess


@contextlib.asynccontextmanager
async def _http_transport(config: ServerConfig) -> AsyncIterator[Any]:
    timeout = httpx2.Timeout(30.0, read=300.0)

    async def remember_status(response) -> None:
        if response.status_code >= 400:
            http_errors[config.pk] = response.status_code

    async with httpx2.AsyncClient(
        headers=dict(config.headers),
        timeout=timeout,
        event_hooks={"response": [remember_status]},
    ) as http:
        async with streamable_http_client(config.url, http_client=http) as streams:
            yield streams


def _make_client(config: ServerConfig) -> Client:
    if config.transport == "stdio":
        params = StdioServerParameters(
            command=config.argv[0], args=list(config.argv[1:]), env=dict(config.env)
        )
        transport = stdio_client(params, errlog=_errlog())
    else:
        transport = _http_transport(config)
    return Client(transport, cache=None, client_info=CLIENT_INFO)


def _leaf(exc: BaseException) -> BaseException:
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


_BROKEN = (
    anyio.ClosedResourceError,
    anyio.BrokenResourceError,
    anyio.EndOfStream,
    ConnectionError,
    httpx2.TransportError,
)


def translate(exc: BaseException, config: ServerConfig) -> McpError:
    """Deutsche Meldung ohne Zugangsdaten (keine Texte aus Transportfehlern)."""
    exc = _leaf(exc)
    name = config.name
    if isinstance(exc, McpError):
        return exc
    if isinstance(exc, TimeoutError):
        return McpTimeout(f"MCP-Server „{name}“: Zeitüberschreitung.")
    if isinstance(exc, MCPError):
        if exc.code == REQUEST_TIMEOUT:
            return McpTimeout(f"MCP-Server „{name}“: Zeitüberschreitung.")
        if exc.code == CONNECTION_CLOSED:
            return McpError(f"Verbindung zum MCP-Server „{name}“ wurde unterbrochen.")
        return McpError(f"MCP-Server „{name}“ meldet einen Fehler: {exc.message}")
    if isinstance(exc, FileNotFoundError | PermissionError) and config.transport == "stdio":
        return McpError(f"Der Befehl für den MCP-Server „{name}“ konnte nicht gestartet werden.")
    if isinstance(exc, httpx2.HTTPStatusError):
        return McpError(
            f"MCP-Server „{name}“ antwortet mit HTTP-Status {exc.response.status_code}."
        )
    if isinstance(exc, _BROKEN):
        return McpError(
            f"MCP-Server „{name}“ ist nicht erreichbar oder hat die Verbindung beendet."
        )
    return McpError(f"Verbindung zum MCP-Server „{name}“ fehlgeschlagen ({type(exc).__name__}).")


def _is_connection_failure(exc: BaseException) -> bool:
    exc = _leaf(exc)
    if isinstance(exc, MCPError):
        return exc.code == CONNECTION_CLOSED
    return isinstance(exc, (*_BROKEN, OSError))


class Connection:
    """Eine Sitzung zu einem MCP-Server, gehalten von einem eigenen Task."""

    def __init__(self, config: ServerConfig):
        self.config = config
        self.lock = asyncio.Lock()
        self.client: Client | None = None
        self.connect_count = 0
        self.last_used = time.monotonic()
        self._owner: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None
        self._tools: list[Any] | None = None
        self._tools_at = 0.0

    @property
    def connected(self) -> bool:
        return (
            self.client is not None
            and self._owner is not None
            and not self._owner.done()
            and self._stop is not None
            and not self._stop.is_set()
        )

    async def _hold(self, ready: asyncio.Future, stop: asyncio.Event) -> None:
        try:
            async with _make_client(self.config) as client:
                if self._owner is not asyncio.current_task():
                    if not ready.done():  # inzwischen verworfen
                        ready.cancel()
                    return
                self.client = client
                if not ready.done():
                    ready.set_result(None)
                await stop.wait()
        except asyncio.CancelledError:
            if not ready.done():
                ready.cancel()
            raise
        except Exception as exc:
            if not ready.done():
                ready.set_exception(exc)
            else:
                logger.warning(
                    "MCP-Server %s: Verbindung beendet (%s)",
                    self.config.name,
                    type(_leaf(exc)).__name__,
                )
        finally:
            # Ein verworfener alter Halte-Task darf die neue Sitzung nicht löschen.
            if self._owner is None or self._owner is asyncio.current_task():
                self.client = None
                self._tools = None

    async def ensure(self) -> Client:
        """Verbindet bei Bedarf; unter dem Server-Lock genau ein Aufbau zur Zeit."""
        self.last_used = time.monotonic()
        if self.connected:
            return self.client  # type: ignore[return-value]
        async with self.lock:
            if self.connected:
                return self.client  # type: ignore[return-value]
            await self._discard()
            loop = asyncio.get_running_loop()
            ready = loop.create_future()
            stop = asyncio.Event()
            owner = loop.create_task(self._hold(ready, stop), name=f"mcp:{self.config.name}")
            self._owner, self._stop = owner, stop
            self.connect_count += 1
            try:
                await asyncio.shield(ready)
            except BaseException:
                # Aufbau gescheitert oder Aufrufer abgebrochen (Zeitlimit): Versuch
                # beenden, ohne auf das Aufräumen des Prozesses zu warten.
                owner.cancel()
                _keep(owner)
                self._owner = self._stop = None
                raise
            _ensure_reaper()
            return self.client  # type: ignore[return-value]

    async def _discard(self) -> None:
        """Schließt eine tote oder halb offene Sitzung im Hintergrund."""
        owner, stop = self._owner, self._stop
        self._owner = self._stop = None
        self.client = None
        self._tools = None
        if owner is not None and not owner.done():
            if stop is not None:
                stop.set()
            _keep(owner)

    def mark_broken(self) -> None:
        if self._stop is not None:
            self._stop.set()
        self._tools = None

    async def close(self) -> None:
        owner, stop = self._owner, self._stop
        self._owner = self._stop = None
        self._tools = None
        if owner is None:
            return
        if stop is not None:
            stop.set()
        try:
            await asyncio.wait_for(asyncio.shield(owner), CLOSE_TIMEOUT)
        except (TimeoutError, asyncio.CancelledError, Exception):
            owner.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(owner, CLOSE_TIMEOUT)

    async def list_tools(self, refresh: bool = False) -> list[Any]:
        now = time.monotonic()
        if not refresh and self._tools is not None and now - self._tools_at < TOOLS_TTL_SECONDS:
            self.last_used = now
            return self._tools
        client = await self.ensure()
        tools: list[Any] = []
        cursor = None
        try:
            for _ in range(MAX_LIST_PAGES):
                page = await client.list_tools(cursor=cursor)
                tools.extend(page.tools)
                cursor = page.next_cursor
                if not cursor:
                    break
        except BaseException as exc:
            if _is_connection_failure(exc):
                self.mark_broken()
            raise
        self._tools, self._tools_at = tools, time.monotonic()
        return tools

    async def call_tool(self, name: str, arguments: dict, timeout: float) -> ToolResult:
        client = await self.ensure()
        try:
            result = await client.call_tool(name, arguments, read_timeout_seconds=timeout)
        except BaseException as exc:
            if _is_connection_failure(exc):
                self.mark_broken()
            raise
        finally:
            self.last_used = time.monotonic()
        return convert_result(result)


def get_connection(config: ServerConfig) -> Connection:
    """Liefert die Verbindung zu ``config``; bei geänderter Konfiguration eine neue."""
    conn = _connections.get(config.pk)
    if conn is not None and conn.config.fingerprint != config.fingerprint:
        _connections.pop(config.pk, None)
        _keep(asyncio.get_running_loop().create_task(conn.close()))
        conn = None
    if conn is None:
        conn = Connection(config)
        _connections[config.pk] = conn
    else:
        conn.config = config  # Name/Zeitlimit können sich ohne Neuverbindung ändern
    return conn


# --- Einstiegspunkte für die Sync-API (laufen im Loop) -----------------------


async def _guarded(config: ServerConfig, timeout: float, coro_fn):
    try:
        async with asyncio.timeout(timeout):
            return await coro_fn()
    except McpError:
        raise
    except TimeoutError:
        raise McpTimeout(
            f"MCP-Server „{config.name}“ hat nicht innerhalb von {timeout:g} s geantwortet."
        ) from None
    except Exception as exc:
        raise translate(exc, config) from None


async def list_tools(config: ServerConfig, timeout: float, refresh: bool = False) -> list[Any]:
    conn = get_connection(config)

    async def run():
        try:
            return await conn.list_tools(refresh=refresh)
        except Exception as exc:
            # Lesend und wiederholbar: nach Verbindungsabbruch einmal neu verbinden.
            if not _is_connection_failure(exc):
                raise
            return await conn.list_tools(refresh=True)

    return await _guarded(config, timeout, run)


async def call_tool(config: ServerConfig, name: str, arguments: dict, timeout: float) -> ToolResult:
    conn = get_connection(config)
    return await _guarded(config, timeout, lambda: conn.call_tool(name, arguments, timeout))


async def close_server(pk: int) -> None:
    conn = _connections.pop(pk, None)
    if conn is not None:
        await conn.close()


async def close_all() -> None:
    global _reaper
    if _reaper is not None:
        _reaper.cancel()
        _reaper = None
    conns = list(_connections.values())
    _connections.clear()
    await asyncio.gather(*(c.close() for c in conns), return_exceptions=True)
    pending = [t for t in _background if not t.done()]
    if pending:
        await asyncio.wait(pending, timeout=CLOSE_TIMEOUT)


async def _reap_idle() -> None:
    while True:
        await asyncio.sleep(REAP_INTERVAL)
        now = time.monotonic()
        for pk, conn in list(_connections.items()):
            if conn.connected and now - conn.last_used > IDLE_SECONDS and not conn.lock.locked():
                logger.info("MCP-Server %s: unbenutzte Sitzung geschlossen", conn.config.name)
                _connections.pop(pk, None)
                _keep(asyncio.get_running_loop().create_task(conn.close()))


def _ensure_reaper() -> None:
    global _reaper
    if _reaper is None or _reaper.done():
        _reaper = asyncio.get_running_loop().create_task(_reap_idle(), name="mcp:reaper")


def connection_stats() -> dict[int, dict[str, Any]]:
    """Für Tests und Diagnose (im Loop aufrufen)."""
    return {
        pk: {"connected": c.connected, "connect_count": c.connect_count}
        for pk, c in _connections.items()
    }


def _reset_after_fork() -> None:
    """Zustand des Elternprozesses verwerfen (Tasks gehören dessen Loop)."""
    global _reaper
    _connections.clear()
    _background.clear()
    http_errors.clear()
    _reaper = None


bridge.register_shutdown_hook(close_all)
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)
