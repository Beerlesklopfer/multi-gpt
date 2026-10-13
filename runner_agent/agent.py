"""Verbindung zum Runner-Dienst (M14-08): Anmeldung, Heartbeat, Rahmen, Backoff.

Der Agent baut nur ausgehend eine WebSocket-Verbindung auf (``wss://…/runner/ws``,
ohne ``Origin``-Header). TLS wird immer geprüft – gegen die System-CAs oder,
bei selbst signiertem Zertifikat, gegen den festgehaltenen Fingerprint, und zwar
bevor der Token den Rechner verlässt. Der Token steht nur in der Secret-Datei.

Nach den Close-Codes 4401/4403 (und 4409/4426) verbindet sich der Agent nicht
neu: Ein Mensch muss handeln. Er endet dann mit Exit-Code 3 (Einstellungsfehler:
2); die Quadlet-Datei verhindert mit ``RestartPreventExitStatus=2 3`` eine
Neustart-Schleife. Nach neuem Token startet der Nutzer den Dienst neu.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time

import websockets
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI

from runner_agent import VERSION, protocol, tools
from runner_agent.backoff import Backoff
from runner_agent.config import Config, ConfigError, from_env
from runner_agent.engine import Engine, EngineError
from runner_agent.session import SessionError, SessionManager
from runner_agent.tls import PinError, check_pin, client_context

logger = logging.getLogger("runner_agent")

CLOSE_TLS = 1015  # TLS bzw. Fingerprint passt nicht (lokal, kein Close-Rahmen)
# Diese Rahmen werden in Empfangsreihenfolge verarbeitet (kurz, reihenfolgekritisch);
# alle übrigen laufen als eigene Aufgabe.
ORDERED = frozenset(
    {"mcp", "bundle_chunk", "bundle_ack", "pty_input", "pty_resize", "pty_close", "close", "error"}
)


class Agent:
    """Ein Agent mit einer Engine; ``run()`` läuft bis ``stop()``.

    ``sleep``, ``clock`` und ``backoff`` sind im Test ersetzbar (gefälschte Uhr).
    """

    def __init__(
        self,
        config: Config,
        engine: Engine,
        *,
        sleep=asyncio.sleep,
        clock=time.monotonic,
        backoff: Backoff | None = None,
        connect=ws_connect,
    ):
        self.config = config
        self.engine = engine
        self.sleep = sleep
        self.clock = clock
        self.backoff = backoff or Backoff()
        self.connect = connect
        self.manager = SessionManager(engine, config, self.send, clock=clock)
        self.ws = None
        self.welcome: dict | None = None
        self.close_reason: str | None = None
        self.last_close: int | None = None
        self.delays: list[float] = []  # Wartezeiten (für Tests und Log)
        self._send_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._stopping = asyncio.Event()

    # --- Senden ------------------------------------------------------------------------

    async def send(self, kind: str, **fields) -> None:
        text = protocol.encode(kind, protocol.AGENT, **fields)
        ws = self.ws
        if ws is None:
            raise ConnectionError("Keine Verbindung zum Server.")
        async with self._send_lock:
            await ws.send(text)

    # --- Hauptschleife -------------------------------------------------------------------

    def stop(self) -> None:
        self._stopping.set()
        if self.ws is not None:
            asyncio.ensure_future(self.ws.close())

    async def run(self) -> int | None:
        """Verbinden, bei Abbruch mit Backoff neu verbinden; liefert den letzten Code."""
        await self.manager.adopt()
        while not self._stopping.is_set():
            code = await self.run_once()
            self.last_close = code
            if self._stopping.is_set():
                break
            if not protocol.should_reconnect(code):
                logger.error(
                    "Server hat die Verbindung abgelehnt (%s: %s); kein Neuversuch.",
                    code,
                    protocol.CLOSE_REASONS.get(code, "?"),
                )
                break
            longest = code not in protocol.RECONNECT
            delay = self.backoff.next(longest=longest)
            self.delays.append(delay)
            logger.info("Verbindung beendet (%s), neuer Versuch in %.1f s.", code, delay)
            await self._sleep_or_stop(delay)
        return self.last_close

    async def _sleep_or_stop(self, delay: float) -> None:
        stop = asyncio.ensure_future(self._stopping.wait())
        pause = asyncio.ensure_future(self.sleep(delay))
        await asyncio.wait({stop, pause}, return_when=asyncio.FIRST_COMPLETED)
        for task in (stop, pause):
            task.cancel()

    async def run_once(self) -> int:
        """Eine Verbindung: liefert den Close-Code (1006 ohne Close-Rahmen)."""
        try:
            token = self.config.read_token()
        except ConfigError as exc:
            logger.error("%s", exc)
            return protocol.CLOSE_TOKEN
        secure = self.config.url.startswith("wss://")
        kwargs = dict(
            origin=None,
            max_size=protocol.MAX_FRAME_BYTES,
            ping_interval=20,
            ping_timeout=20,
            open_timeout=15,
            close_timeout=5,
            compression=None,
            user_agent_header=f"multigpt-runner/{VERSION}",
        )
        if secure:
            kwargs["ssl"] = client_context(self.config.ca_sha256)
        try:
            ws = await self.connect(self.config.url, **kwargs)
        except (OSError, TimeoutError, InvalidHandshake, InvalidURI) as exc:
            logger.warning("Verbindung fehlgeschlagen: %s", exc.__class__.__name__)
            return protocol.CLOSE_ABNORMAL
        try:
            if secure:
                check_pin(ws.transport.get_extra_info("ssl_object"), self.config.ca_sha256)
        except PinError as exc:
            logger.error("%s", exc)
            await ws.close()
            return CLOSE_TLS
        self.ws = ws
        self.welcome = None
        self.close_reason = None
        try:
            return await self._session(ws, token)
        finally:
            self.ws = None
            await self._disconnected()

    async def _session(self, ws, token: str) -> int:
        heartbeat = None
        try:
            info = await self.engine.info()
            await self.send(
                "hello",
                token=token,
                protocol=protocol.PROTOCOL_VERSION,
                agent_version=VERSION,
                engine={"name": info.name, "version": info.version, "rootless": info.rootless},
                hostname=self.config.hostname or "runner",
                sessions=self.manager.infos(),
            )
            del token
            async for text in ws:
                try:
                    frame = protocol.parse(text, protocol.SERVER)
                except protocol.ProtocolError as exc:
                    logger.warning("Ungültiger Rahmen vom Server: %s", exc.message)
                    await self.send("error", code="invalid_frame", message=exc.message)
                    continue
                if frame["type"] == "welcome":
                    self._on_welcome(frame)
                    if heartbeat is None:
                        heartbeat = asyncio.create_task(self._heartbeat(frame))
                    continue
                if self.welcome is None:
                    logger.warning("Rahmen %s vor welcome ignoriert.", frame["type"])
                    continue
                if frame["type"] in ORDERED:
                    await self._dispatch(frame)
                else:
                    self._spawn(self._dispatch(frame))
        except ConnectionClosed:
            pass
        except EngineError as exc:
            logger.error("Engine nicht erreichbar: %s", exc)
            await ws.close(protocol.CLOSE_TRY_AGAIN)
            return protocol.CLOSE_TRY_AGAIN
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
        rcvd = getattr(ws, "close_code", None)
        return rcvd if rcvd is not None else protocol.CLOSE_ABNORMAL

    def _on_welcome(self, frame: dict) -> None:
        self.welcome = frame
        self.manager.server_images = list(frame["images"])
        self.manager.chunk_bytes = frame["chunk_bytes"]
        self.backoff.reset()
        logger.info("Angemeldet als Runner %s.", frame["runner"])

    async def _heartbeat(self, welcome: dict) -> None:
        interval = welcome["heartbeat_seconds"]
        while True:
            await asyncio.sleep(interval)
            with contextlib.suppress(Exception):
                await self.manager.check_idle()
            try:
                await self.send("heartbeat", sessions=self.manager.infos())
            except (ConnectionError, ConnectionClosed):
                return

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _disconnected(self) -> None:
        """Verbindung weg: MCP-Pipes und Terminals schließen; Container bleiben.

        Laufende Aufgaben (z. B. ein Sitzungsstart) enden von selbst: Senden
        schlägt fehl, Downloads laufen in ihr Zeitlimit und räumen auf.
        """
        for session in list(self.manager.sessions.values()):
            await self.manager.close_session_io(session)

    # --- Rahmen ---------------------------------------------------------------------------

    async def _dispatch(self, frame: dict) -> None:
        kind = frame["type"]
        try:
            if kind == "session_start":
                await self.manager.start(frame)
            elif kind == "session_stop":
                await self.manager.stop(frame["session"], frame["reason"])
            elif kind == "mcp":
                await tools.on_mcp(self.manager, frame)
            elif kind == "bundle_chunk":
                await self.manager.on_bundle_chunk(frame)
            elif kind == "bundle_ack":
                self.manager.on_bundle_ack(frame)
            elif kind == "pty_open":
                await self.manager.pty_open(frame)
            elif kind == "pty_input":
                await self.manager.pty_input(frame)
            elif kind == "pty_resize":
                await self.manager.pty_resize(frame)
            elif kind == "pty_close":
                await self.manager.pty_close(frame)
            elif kind == "preview_request":
                await self.manager.preview_request(frame)
            elif kind == "close":
                self.close_reason = frame["reason"]
                logger.warning("Server beendet die Verbindung: %s", frame["reason"])
            elif kind == "error":
                logger.warning("Fehler vom Server (%s): %s", frame["code"], frame["message"])
        except SessionError as exc:
            await self._error(exc.code, exc.message, frame)
        except EngineError as exc:
            await self._error("engine", str(exc), frame)
        except (ConnectionError, ConnectionClosed):
            pass
        except Exception:  # noqa: BLE001 - ein Rahmen darf den Agent nicht beenden
            logger.exception("Fehler bei Rahmen %s", kind)
            await self._error("internal", "Interner Fehler im Agent.", frame)

    async def _error(self, code: str, message: str, frame: dict) -> None:
        ref = frame.get("pipe") or frame.get("pty") or frame.get("request") or frame.get("transfer")
        with contextlib.suppress(Exception):
            await self.send(
                "error", code=code, message=message[:1000], session=frame.get("session"), ref=ref
            )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr
    )
    try:
        config = from_env()
        config.read_token()
    except ConfigError as exc:
        logger.error("Einstellungen: %s", exc)
        return 2
    if not config.images:
        logger.error("MGPT_IMAGES ist leer: Ohne Positivliste startet kein Arbeitscontainer.")
        return 2
    from runner_agent.podman import PodmanEngine

    engine = PodmanEngine(config)
    agent = Agent(config, engine)
    logger.info("MultiGPT-Runner %s (websockets %s) startet.", VERSION, websockets.__version__)
    try:
        code = asyncio.run(agent.run())
    except KeyboardInterrupt:
        return 0
    return 0 if code in (None, protocol.CLOSE_NORMAL) else 3
