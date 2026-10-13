"""LISTEN-Verbindung des Runner-Dienstes (M14-05).

Eine ``psycopg.AsyncConnection`` (autocommit) je Prozess des Dienstes hört auf
``mgpt_runner_ctl`` und auf ``mgpt_runner_<id>`` jedes Runners, der bei diesem
Prozess verbunden ist. ``notifies()`` hält die Verbindung für sich, solange es
wartet; deshalb wartet die Schleife in kurzen Fenstern (``WAIT_SECONDS``) und
führt dazwischen die angeforderten ``LISTEN``/``UNLISTEN`` aus. ``listen``
kehrt erst zurück, wenn das ``LISTEN`` aktiv ist: Danach holt der Aufrufer die
Warteschlange einmal ab, so geht nichts zwischen Anmeldung und ``LISTEN``
verloren.

Bricht die Verbindung ab, baut die Schleife sie neu auf, meldet alle Kanäle neu
an und ruft ``on_reconnect`` (der Dienst holt dann alle Warteschlangen ab).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import psycopg
from psycopg import sql

from .control import CTL_CHANNEL

logger = logging.getLogger(__name__)

WAIT_SECONDS = 0.2
RECONNECT_SECONDS = 1.0
START_TIMEOUT = 30.0


class Listener:
    def __init__(
        self,
        conninfo: dict,
        on_notify: Callable[[str, str], Awaitable[None] | None],
        on_reconnect: Callable[[], Awaitable[None] | None] | None = None,
    ):
        self._conninfo = conninfo
        self._on_notify = on_notify
        self._on_reconnect = on_reconnect
        self._channels: set[str] = {CTL_CHANNEL}
        self._pending: list[tuple[str, str, asyncio.Future]] = []
        self._conn: psycopg.AsyncConnection | None = None
        self._task: asyncio.Task | None = None
        self._stopped = False
        self._ready = asyncio.Event()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="runner-listener")
        try:
            await asyncio.wait_for(self._ready.wait(), START_TIMEOUT)
        except TimeoutError:
            await self.stop()
            raise RuntimeError("Keine Datenbankverbindung für LISTEN.") from None

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        await self._close()

    async def listen(self, channel: str) -> None:
        await self._command("LISTEN", channel)

    async def unlisten(self, channel: str) -> None:
        await self._command("UNLISTEN", channel)

    async def _command(self, verb: str, channel: str) -> None:
        if verb == "LISTEN":
            self._channels.add(channel)
        else:
            self._channels.discard(channel)
        if self._stopped:
            return
        future = asyncio.get_running_loop().create_future()
        self._pending.append((verb, channel, future))
        await future

    async def _connect(self) -> None:
        self._conn = await psycopg.AsyncConnection.connect(**self._conninfo, autocommit=True)
        for channel in sorted(self._channels):
            await self._conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(channel)))

    async def _close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                await conn.close()
            except Exception:  # pragma: no cover - Verbindung schon weg
                pass

    async def _flush(self) -> None:
        pending, self._pending = self._pending, []
        for verb, channel, future in pending:
            try:
                await self._conn.execute(sql.SQL(verb + " {}").format(sql.Identifier(channel)))
            except Exception as exc:
                if not future.done():
                    future.set_exception(exc)
                raise
            if not future.done():
                future.set_result(None)

    async def _call(self, callback, *args) -> None:
        try:
            result = callback(*args)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.exception("Fehler bei der Verarbeitung einer Runner-Benachrichtigung")

    async def _run(self) -> None:
        first = True
        while not self._stopped:
            try:
                await self._connect()
                # Vor dem Neuaufbau angeforderte Kanäle sind über _channels schon angemeldet.
                for _verb, _channel, future in self._pending:
                    if not future.done():
                        future.set_result(None)
                self._pending.clear()
                self._ready.set()
                if not first and self._on_reconnect is not None:
                    await self._call(self._on_reconnect)
                first = False
                while not self._stopped:
                    await self._flush()
                    async for notify in self._conn.notifies(timeout=WAIT_SECONDS):
                        await self._call(self._on_notify, notify.channel, notify.payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("LISTEN-Verbindung des Runner-Dienstes verloren: %s", exc)
                await self._close()
                if self._stopped:
                    break
                await asyncio.sleep(RECONNECT_SECONDS)
