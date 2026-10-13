"""MCP-Transport ``runner`` (M14-06): JSON-RPC über den Rückkanal zum Runner.

Läuft ausschließlich im MCP-Loop-Thread (``chat/mcp/bridge.py``). Für den
MCP-Client des SDK ist der Runner ein normaler MCP-Server: Der Transport ist
ein asynchroner Kontextmanager nach dem ``Transport``-Protokoll des SDK und
liefert ``(read_stream, write_stream)`` mit ``SessionMessage``.

Die Zustellung selbst (``RunnerMessage`` + LISTEN/NOTIFY, Antwortkanal je
Prozess) gehört dem Paket von ``gateway``; hier wird nur eine schmale
Schnittstelle genutzt (:class:`Link`)::

    await link.open(runner_id, pipe, session_id)   # Antwortkanal für pipe belegen
    await link.send(runner_id, pipe, message)      # dict (JSON-RPC) an den Runner
    message = await link.receive(pipe)             # nächste Antwort; None = Ende
    await link.close(pipe)

``RunnerOffline`` (bzw. jede Exception aus ``send``) beendet die Verbindung;
der MCP-Client meldet dann „nicht erreichbar“, die Sync-Schicht
(``runner.chat``) prüft den Status vorher und meldet „Runner nicht verbunden“.
Je Sitzung gibt es eine eigene pipe (``ServerConfig.runner_pipe``), die
Verbindungen in ``chat/mcp/client.py`` sind nach ``(pk, pipe)`` geschlüsselt.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any, Protocol

import anyio
from mcp.shared.message import SessionMessage
from mcp_types import jsonrpc_message_adapter

logger = logging.getLogger(__name__)

STREAM_BUFFER = 16


class RunnerOffline(ConnectionError):
    """Der Runner ist nicht verbunden (oder die Zustellung ist gescheitert)."""


class Link(Protocol):
    async def open(self, runner_id: int, pipe: str, session_id: int | None) -> None: ...

    async def send(self, runner_id: int, pipe: str, message: dict[str, Any]) -> None: ...

    async def receive(self, pipe: str) -> dict[str, Any] | None: ...

    async def close(self, pipe: str) -> None: ...


_link: Link | None = None


def set_link(link: Link | None) -> None:
    """Zustellung festlegen (Tests: Fake; sonst lädt :func:`get_link` die von gateway)."""
    global _link
    _link = link


class DeliveryLink:
    """:class:`Link` über ``runner.delivery`` (RunnerMessage + LISTEN/NOTIFY).

    Je pipe ein ``delivery.Pipe``; Fehler der Zustellung werden zu
    :class:`RunnerOffline` (der MCP-Client sieht das Ende der Verbindung).
    """

    def __init__(self):
        self._pipes: dict[str, Any] = {}

    async def open(self, runner_id, pipe, session_id):
        from . import delivery

        self._pipes[pipe] = delivery.Pipe(runner_id, session_id, pipe)

    def _pipe(self, pipe):
        found = self._pipes.get(pipe)
        if found is None:
            raise RunnerOffline("Die Verbindung zum Runner ist geschlossen.")
        return found

    async def send(self, runner_id, pipe, message):
        from . import delivery

        try:
            await self._pipe(pipe).send(message)
        except delivery.RunnerError as exc:
            raise RunnerOffline(exc.message) from None

    async def receive(self, pipe):
        from . import delivery

        try:
            return await self._pipe(pipe).receive()
        except delivery.RunnerError as exc:
            raise RunnerOffline(exc.message) from None

    async def close(self, pipe):
        found = self._pipes.pop(pipe, None)
        if found is not None:
            await found.aclose()


def get_link() -> Link:
    """Zustellung dieses Prozesses (im MCP-Loop-Thread benutzt)."""
    global _link
    if _link is None:
        _link = DeliveryLink()
    return _link


@contextlib.asynccontextmanager
async def runner_transport(config) -> AsyncIterator[Any]:
    """Transport für ``config`` (``ServerConfig`` mit ``runner_id``/``runner_pipe``)."""
    link = get_link()
    runner_id, pipe = config.runner_id, config.runner_pipe
    await link.open(runner_id, pipe, config.runner_session)
    read_writer, read_stream = anyio.create_memory_object_stream[SessionMessage | Exception](
        STREAM_BUFFER
    )
    write_stream, write_reader = anyio.create_memory_object_stream[SessionMessage](STREAM_BUFFER)

    async def reader() -> None:
        async with read_writer:
            while True:
                try:
                    payload = await link.receive(pipe)
                except (RunnerOffline, anyio.ClosedResourceError, anyio.EndOfStream):
                    return
                if payload is None:
                    return
                try:
                    message = jsonrpc_message_adapter.validate_python(payload, by_name=False)
                except ValueError as exc:
                    # Nur die Fehlerart ins Log, keine Inhalte aus dem Container.
                    logger.warning("Runner %s: ungültige MCP-Nachricht verworfen", runner_id)
                    await read_writer.send(exc)
                    continue
                try:
                    await read_writer.send(SessionMessage(message))
                except (anyio.ClosedResourceError, anyio.BrokenResourceError):
                    return

    async def writer() -> None:
        try:
            async with write_reader:
                async for session_message in write_reader:
                    payload = session_message.message.model_dump(
                        mode="json", by_alias=True, exclude_unset=True
                    )
                    await link.send(runner_id, pipe, payload)
        except Exception as exc:
            if not isinstance(exc, anyio.ClosedResourceError | anyio.BrokenResourceError):
                logger.warning(
                    "Runner %s: Zustellung gescheitert (%s)", runner_id, type(exc).__name__
                )
        finally:
            # Ohne Zustellung keine Antworten: Sitzung sieht das Ende sofort.
            await read_writer.aclose()

    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(reader)
            tg.start_soon(writer)
            try:
                yield read_stream, write_stream
            finally:
                tg.cancel_scope.cancel()
    finally:
        with anyio.CancelScope(shield=True):
            with contextlib.suppress(Exception):
                await link.close(pipe)
            await read_stream.aclose()
            await write_stream.aclose()
