"""Runner-Dienst: WebSocket-Verbindungen der Runner-Agents (M14-04/05).

Je Prozess gibt es einen ``Hub`` mit den Verbindungen dieses Prozesses
(Runner-ID → ``Connection``) und einer LISTEN-Verbindung (``listener.py``).

**Ablauf einer Verbindung:**

1. ``Origin``-Header gesetzt → abgewiesen vor dem Handshake (Browser haben hier
   nichts zu suchen; der Agent sendet keinen). IP mit zu vielen
   Fehlversuchen → Close 4429.
2. Erster Rahmen innerhalb ``HELLO_TIMEOUT`` muss ``hello`` sein (sonst 4408
   bzw. 4400); falsche Protokollversion 4426. Token ungültig, abgelaufen oder
   widerrufen → 4401, Konto ohne Recht → 4403 (Fehlversuche ins Audit, für die
   Drosselung je IP).
3. Installationsschlüssel laden (fehlt er: 1011), Dienstkatalog prüfen
   (``trust``), Runner online setzen, Ist-Zustand abgleichen, ``welcome`` mit
   den freigegebenen Diensten. Ab hier geht jeder Rahmen an den Agent im
   Umschlag ``signed`` hinaus (Signatur über die ``nonce`` aus dem ``hello``).
   Eine ältere Verbindung desselben Runners (auch in einem anderen Prozess,
   über ``mgpt_runner_ctl``) wird mit 4409 geschlossen.
4. ``LISTEN mgpt_runner_<id>``; Rahmen aus der Warteschlange gehen an den Agent.
   Antworten des Agents gehen an den Prozess, der den Rahmen geschickt hat
   (``delivery.route_key``).
5. ``heartbeat``: Token und Recht frisch prüfen, ``last_seen``, Laufzeit
   buchen, Warteschlange nachholen (verlorene Benachrichtigung).
6. Ende: Runner offline, Sitzungen ``unknown``; alle wartenden Abnehmer
   bekommen ``error`` mit Code ``offline``.

**Browser-Terminal** (``/runner/pty/<uuid>``, ``pty_direct.py``): Der Browser
hält einen WebSocket direkt zu diesem Dienst; ``pty_*`` gehen über die
Verbindung des Runners in diesem Prozess, die Ausgabe zurück im Speicher
(``Hub.terminals``) – ohne Warteschlange und NOTIFY.

**Drosselung:** Rahmen je Token über einen Token-Eimer (``RATE_PER_SECOND``,
``RATE_BURST``); darüber Close 4429. Widerruf, Rechteänderung und Stopp wegen
Kontingent kommen über ``mgpt_runner_ctl`` und wirken sofort.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import time
import uuid

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import DEFAULT_DB_ALIAS, connections
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from multigpt.version import app_version

from . import control, protocol, store, trust
from .delivery import OFFLINE_MESSAGE, ROUTE_KINDS, ROUTE_MESSAGE, route_key, runner_channel
from .listener import Listener

logger = logging.getLogger(__name__)

PATH = "/runner/ws"
# Zeiten (Sekunden); in Tests veränderbar.
HELLO_TIMEOUT = protocol.HELLO_TIMEOUT
HEARTBEAT_SECONDS = protocol.HEARTBEAT_SECONDS
QUOTA_GRACE_SECONDS = store.QUOTA_GRACE_SECONDS
MAX_UPLOADS = 2  # gleichzeitige Bundle-Uploads je Verbindung


def idle_timeout() -> float:
    """Ohne Rahmen so lange → Verbindung gilt als tot (3 Heartbeats)."""
    return 3 * HEARTBEAT_SECONDS


def _rate(name: str, default: float) -> float:
    try:
        value = float(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def db(func, *args, **kwargs):
    """ORM-Aufruf aus dem Loop: im ORM-Thread (eine Verbindung)."""
    return sync_to_async(func, thread_sensitive=True)(*args, **kwargs)


def _client_ip(websocket: WebSocket) -> str | None:
    client = websocket.client
    if client is None:
        return None
    try:
        return str(ipaddress.ip_address(client.host))
    except ValueError:
        return None


def _images() -> list[str]:
    """Erlaubte Arbeits-Images für ``welcome`` – nur mit Digest (name@sha256:…)."""
    images = [getattr(settings, "RUNNER_WORK_IMAGE", "") or ""]
    return [image for image in images if image and protocol.IMAGE_RE.match(image)][:20]


class TokenBucket:
    def __init__(self, rate: float, burst: float):
        self.rate = rate
        self.burst = burst
        self.tokens = burst
        self.stamp = time.monotonic()

    def take(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.stamp) * self.rate)
        self.stamp = now
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True


class Closed(Exception):
    """Verbindung wird geschlossen (Code bereits gesendet bzw. gesetzt)."""


class Connection:
    def __init__(self, hub: Hub, websocket: WebSocket, ip: str | None):
        self.hub = hub
        self.ws = websocket
        self.ip = ip
        self.id = uuid.uuid4().hex
        self.runner_id: int | None = None
        self.token_id: int | None = None
        self.owner_id: int | None = None
        self.connected_at = None
        # Aus dem hello: Zufallszahl des Agents und Installationsschlüssel – jeder
        # Rahmen an den Agent geht signiert hinaus (Umschlag ``signed``).
        self.nonce: str | None = None
        self.key: trust.ServerKey | None = None
        self.closed = False
        self.close_code: int | None = None
        # Zuordnung (Art, ID) → Antwortkanal des Prozesses, der zuletzt schickte.
        self.routes: dict[tuple[str, str], str] = {}
        self.uploads: dict[str, tuple[protocol.BundleAssembler, dict]] = {}
        self._send_lock = asyncio.Lock()
        self._drain_lock = asyncio.Lock()
        self._dirty = False
        self._tasks: set[asyncio.Task] = set()
        self.closed_event = asyncio.Event()

    # --- Senden und Schließen -------------------------------------------------

    async def send(self, frame: dict) -> None:
        if self.key is None or self.nonce is None:
            raise RuntimeError("Vor dem hello wird nichts gesendet (keine Signatur möglich).")
        text = trust.sign_frame(self.key, frame, self.nonce)
        async with self._send_lock:
            if self.closed:
                raise Closed()
            await self.ws.send_text(text)

    async def close(self, code: int, reason: str | None = None, message: str = "") -> None:
        if self.closed:
            return
        if reason is not None:
            with contextlib.suppress(Exception):
                await self.send({"type": "close", "reason": reason, "message": message or None})
        async with self._send_lock:
            if self.closed:
                return
            self.closed = True
            self.close_code = code
            self.closed_event.set()
            with contextlib.suppress(Exception):
                if self.ws.application_state != WebSocketState.DISCONNECTED:
                    await self.ws.close(code, protocol.CLOSE_REASONS.get(code, ""))

    def spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def cancel_tasks(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # --- Rahmen an den Agent (aus der Warteschlange) -----------------------------

    async def drain(self) -> None:
        """Warteschlange des Runners abholen und senden (zusammengefasst)."""
        self._dirty = True
        if self._drain_lock.locked():
            return
        async with self._drain_lock:
            while self._dirty and not self.closed:
                self._dirty = False
                rows = await db(store.take_messages, self.runner_id)
                for _pk, channel, payload in rows:
                    await self._forward_to_agent(channel, payload)

    async def _forward_to_agent(self, channel: str, payload: dict) -> None:
        if payload.get("type") == ROUTE_MESSAGE:
            # Nur Zuordnung anmelden (delivery.claim_route), nichts an den Agent.
            kind, key = payload.get("kind"), payload.get("key")
            if channel and kind in ROUTE_KINDS and isinstance(key, str):
                self.routes[(kind, key)] = channel
            return
        try:
            frame = protocol.validate(payload, protocol.SERVER)
        except protocol.ProtocolError as exc:
            logger.warning("Ungültiger Rahmen für Runner %s verworfen: %s", self.runner_id, exc)
            return
        key = route_key(frame)
        if self.closed:
            if channel and key:
                await self._fail(key, channel)
            return
        if channel and key:
            self.routes[key] = channel
        try:
            await self.send(frame)
        except Closed:
            if channel and key:
                await self._fail(key, channel)

    async def _fail(self, key: tuple[str, str], channel: str) -> None:
        kind, ident = key
        frame = {"type": "error", "code": "offline", "message": OFFLINE_MESSAGE}
        if kind == "session":
            frame["session"] = ident
        else:
            frame["ref"] = ident
        await db(store.post_reply, self.runner_id, channel, frame)

    # --- Rahmen vom Agent -----------------------------------------------------

    def _reply_channel(self, frame: dict) -> str:
        if frame["type"] == "error":
            ref = frame.get("ref")
            for kind in ("pipe", "pty", "request", "transfer"):
                channel = self.routes.get((kind, ref)) if ref else None
                if channel:
                    return channel
            if frame.get("session"):
                return self.routes.get(("session", frame["session"]), "")
            return ""
        key = route_key(frame)
        channel = self.routes.get(key, "") if key else ""
        if not channel and frame["type"] in ("pty_data", "pty_exit"):
            # Werkzeugläufe des Agents (eigene pty-IDs ohne pty_open): an die Sitzung.
            channel = self.routes.get(("session", frame["session"]), "")
        return channel

    def _to_terminal(self, frame: dict) -> bool:
        """Ausgabe eines direkt verbundenen Browser-Terminals (``pty_direct``):
        im Speicher weiterreichen, ohne Warteschlange."""
        kind = frame["type"]
        if kind in ("pty_data", "pty_exit"):
            key = frame["pty"]
        elif kind == "error":
            key = frame.get("ref")
        else:
            return False
        terminal = self.hub.terminals.get(key) if key else None
        if terminal is None or terminal.conn is not self:
            return False
        terminal.deliver(frame)
        return True

    async def forward_to_server(self, frame: dict) -> None:
        if self.hub.terminals and self._to_terminal(frame):
            return
        channel = self._reply_channel(frame)
        if channel:
            await db(store.post_reply, self.runner_id, channel, frame)
        else:
            logger.debug("Rahmen %s von Runner %s ohne Abnehmer", frame["type"], self.runner_id)
        if frame["type"] == "pty_exit":
            self.routes.pop(("pty", frame["pty"]), None)
        elif frame["type"] == "preview_response" and frame["last"]:
            self.routes.pop(("request", frame["request"]), None)
        elif frame["type"] == "file_chunk" and frame["last"]:
            self.routes.pop(("transfer", frame["transfer"]), None)

    async def handle(self, frame: dict) -> None:
        kind = frame["type"]
        if kind == "heartbeat":
            beat = await db(store.heartbeat, self.runner_id, self.token_id, frame["sessions"])
            if beat.code is not None:
                reason = "revoked" if beat.code == protocol.CLOSE_TOKEN else "rights"
                await self.close(beat.code, reason)
                raise Closed()
            for item in beat.frames:
                await self.send(item)
            self.spawn(self.drain())
        elif kind == "session_state":
            for item in await db(store.session_state, self.runner_id, frame):
                await self.send(item)
            await self.forward_to_server(frame)
        elif kind == "preview":
            await db(store.preview, self.runner_id, frame)
            await self.forward_to_server(frame)
        elif kind in ("mcp", "pty_data", "pty_exit", "preview_response", "error", "file_chunk"):
            await self.forward_to_server(frame)
        elif kind == "bundle_get":
            self.spawn(self._send_bundle(frame))
        elif kind == "bundle_chunk":
            await self._receive_chunk(frame)
        elif kind == "bundle_ack":
            if not frame["ok"]:
                logger.info(
                    "Runner %s: Bundle %s nicht übernommen: %s",
                    self.runner_id,
                    frame["transfer"],
                    frame.get("error") or "",
                )
        else:  # hello ein zweites Mal
            await self.close(protocol.CLOSE_PROTOCOL, "protocol", f"Rahmen {kind} unerwartet")
            raise Closed()

    # --- Bundles ----------------------------------------------------------------

    async def _send_bundle(self, frame: dict) -> None:
        session, transfer = frame["session"], frame["transfer"]
        out = await db(store.load_bundle, self.runner_id, session)
        try:
            if out.data is None:
                await self.send(
                    {
                        "type": "bundle_ack",
                        "session": session,
                        "transfer": transfer,
                        "ok": False,
                        "error": out.error,
                    }
                )
                return
            for seq, part, last in protocol.split_bundle(out.data):
                chunk = {
                    "type": "bundle_chunk",
                    "session": session,
                    "transfer": transfer,
                    "seq": seq,
                    "data": protocol.b64encode(part),
                    "last": last,
                }
                if last:
                    chunk.update(size=len(out.data), sha256=out.sha256, head=out.head or None)
                await self.send(chunk)
                await asyncio.sleep(0)
        except Closed:
            return

    async def _receive_chunk(self, frame: dict) -> None:
        session, transfer = frame["session"], frame["transfer"]

        async def ack(ok: bool, head: str = "", error: str = "") -> None:
            await self.send(
                {
                    "type": "bundle_ack",
                    "session": session,
                    "transfer": transfer,
                    "ok": ok,
                    "head": head or None,
                    "error": error or None,
                }
            )

        entry = self.uploads.get(transfer)
        if entry is None:
            if frame["seq"] != 0:
                await ack(False, error="Unbekannte Übertragung.")
                return
            if len(self.uploads) >= MAX_UPLOADS:
                await ack(False, error="Zu viele gleichzeitige Übertragungen.")
                return
            limit = await db(store.bundle_limit, self.runner_id, session)
            entry = (protocol.BundleAssembler(transfer, limit), {"session": session})
            self.uploads[transfer] = entry
        assembler, meta = entry
        if meta["session"] != session:
            self.uploads.pop(transfer, None)
            await ack(False, error="Übertragung gehört zu einer anderen Sitzung.")
            return
        try:
            data = assembler.add(frame)
        except protocol.ProtocolError as exc:
            self.uploads.pop(transfer, None)
            await ack(False, error=exc.message)
            return
        if data is None:
            return
        self.uploads.pop(transfer, None)
        result = await db(
            store.accept_bundle,
            self.runner_id,
            session,
            data,
            frame["sha256"],
            frame.get("head") or "",
            frame.get("message") or "",
        )
        await ack(result.ok, result.head, result.error)

    # --- Kontingent -------------------------------------------------------------

    async def quota_stop(self, user_id: int, reason: str) -> None:
        sessions = await db(store.sessions_of_user, self.runner_id, user_id)
        if not sessions:
            return
        await db(store.mark_quota_stop, sessions)
        for session in sessions:
            with contextlib.suppress(Closed):
                await self.send({"type": "session_stop", "session": session, "reason": reason})
        self.spawn(self._quota_deadline(sessions))

    async def _quota_deadline(self, sessions: list[str]) -> None:
        await asyncio.sleep(QUOTA_GRACE_SECONDS)
        if not self.closed and await db(store.still_running, self.runner_id, sessions):
            await self.close(protocol.CLOSE_QUOTA, "quota", "Stopp nicht bestätigt")


class Hub:
    """Verbindungen dieses Prozesses und die LISTEN-Verbindung."""

    def __init__(self):
        self.connections: dict[int, Connection] = {}
        self.buckets: dict[int, TokenBucket] = {}
        # Browser-Terminals mit direktem WebSocket (pty_direct): PTY-ID → Terminal.
        self.terminals: dict = {}
        self.listener: Listener | None = None
        self._lock = asyncio.Lock()
        self._cleanups: set[asyncio.Future] = set()

    # --- Lebenszyklus --------------------------------------------------------------

    async def start(self) -> None:
        conninfo = await db(_conninfo)
        self.listener = Listener(conninfo, self.on_notify, self.on_reconnect)
        await self.listener.start()

    async def stop(self) -> None:
        for conn in list(self.connections.values()):
            await conn.close(protocol.CLOSE_RESTART, "shutdown")
        if self._cleanups:
            await asyncio.wait(list(self._cleanups), timeout=10)
        if self.listener is not None:
            await self.listener.stop()
            self.listener = None
        # DB-Verbindung des ORM-Threads schließen (Dienstende, Tests).
        with contextlib.suppress(Exception):
            await db(connections.close_all)

    @contextlib.asynccontextmanager
    async def lifespan(self, app):
        await self.start()
        try:
            yield
        finally:
            await self.stop()

    def bucket(self, token_id: int) -> TokenBucket:
        bucket = self.buckets.get(token_id)
        if bucket is None:
            bucket = TokenBucket(
                _rate("RUNNER_RATE_PER_SECOND", 100), _rate("RUNNER_RATE_BURST", 1000)
            )
            self.buckets[token_id] = bucket
        return bucket

    # --- Anmelden und Abmelden ---------------------------------------------------------

    async def register(self, conn: Connection) -> None:
        async with self._lock:
            old = self.connections.get(conn.runner_id)
            self.connections[conn.runner_id] = conn
        if old is not None and old is not conn:
            await old.close(protocol.CLOSE_REPLACED, "replaced")
        if self.listener is not None:
            await self.listener.listen(runner_channel(conn.runner_id))
        # Andere Prozesse des Dienstes schließen ihre ältere Verbindung.
        await db(_notify_connected, conn.runner_id, conn.id)

    async def unregister(self, conn: Connection) -> None:
        if conn.runner_id is None:
            return
        current = False
        async with self._lock:
            if self.connections.get(conn.runner_id) is conn:
                del self.connections[conn.runner_id]
                current = True
        if current:
            if self.listener is not None:
                with contextlib.suppress(Exception):
                    await self.listener.unlisten(runner_channel(conn.runner_id))
            await db(store.disconnect, conn.runner_id, conn.connected_at)
            # Was noch wartet, bekommt sofort „nicht verbunden“.
            rows = await db(store.take_messages, conn.runner_id)
            for _pk, channel, payload in rows:
                await conn._forward_to_agent(channel, payload)
        for key, channel in list(conn.routes.items()):
            with contextlib.suppress(Exception):
                await conn._fail(key, channel)
        conn.routes.clear()

    # --- Benachrichtigungen ----------------------------------------------------------

    async def on_notify(self, channel: str, payload: str) -> None:
        if channel == control.CTL_CHANNEL:
            await self.on_ctl(payload)
            return
        prefix = "mgpt_runner_"
        if channel.startswith(prefix):
            try:
                runner_id = int(channel[len(prefix) :])
            except ValueError:
                return
            conn = self.connections.get(runner_id)
            if conn is not None:
                conn.spawn(conn.drain())

    async def on_reconnect(self) -> None:
        for conn in list(self.connections.values()):
            conn.spawn(conn.drain())

    async def on_ctl(self, payload: str) -> None:
        try:
            data = json.loads(payload)
            kind = data["kind"]
        except (ValueError, KeyError, TypeError):
            return
        conns = list(self.connections.values())
        if kind == control.KIND_REVOKE:
            for conn in conns:
                if conn.token_id == data.get("token"):
                    await conn.close(protocol.CLOSE_TOKEN, "revoked", "Token widerrufen")
        elif kind == control.KIND_RIGHTS:
            for conn in conns:
                if conn.owner_id == data.get("user"):
                    code = await db(store.access_code, conn.token_id)
                    if code is not None:
                        reason = "revoked" if code == protocol.CLOSE_TOKEN else "rights"
                        await conn.close(code, reason)
        elif kind == control.KIND_STOP:
            session = str(data.get("session") or "")
            reason = data.get("reason") if data.get("reason") in protocol.STOP_REASONS else "user"
            for conn in conns:
                if await db(store.own_session, conn.runner_id, session) is not None:
                    with contextlib.suppress(Closed, protocol.ProtocolError):
                        await conn.send(
                            {"type": "session_stop", "session": session, "reason": reason}
                        )
        elif kind == control.KIND_QUOTA:
            reason = data.get("reason") if data.get("reason") in protocol.STOP_REASONS else "quota"
            for conn in conns:
                if conn.owner_id == data.get("user"):
                    await conn.quota_stop(conn.owner_id, reason)
        elif kind == control.KIND_CONNECTED:
            conn = self.connections.get(data.get("runner"))
            if conn is not None and conn.id != data.get("conn"):
                await conn.close(protocol.CLOSE_REPLACED, "replaced")

    # --- Endpunkt ----------------------------------------------------------------------

    async def endpoint(self, websocket: WebSocket) -> None:
        if websocket.headers.get("origin"):
            # Vor dem Handshake: der Browser bekommt 403.
            await websocket.close(protocol.CLOSE_FORBIDDEN)
            return
        ip = _client_ip(websocket)
        await websocket.accept()
        conn = Connection(self, websocket, ip)
        try:
            await self._serve(conn)
        except (Closed, WebSocketDisconnect):
            pass
        except Exception:
            logger.exception("Fehler in der Runner-Verbindung %s", conn.runner_id)
            await conn.close(protocol.CLOSE_SERVER_ERROR)
        finally:
            conn.closed = True
            conn.closed_event.set()
            # Aufräumen in eigener Task: läuft auch weiter, wenn diese Task
            # abgebrochen wird (Verbindungsabbruch, Dienstende).
            cleanup = asyncio.ensure_future(self._cleanup(conn))
            self._cleanups.add(cleanup)
            cleanup.add_done_callback(self._cleanups.discard)
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(cleanup)

    async def _cleanup(self, conn: Connection) -> None:
        await conn.cancel_tasks()
        try:
            await self.unregister(conn)
        except Exception:
            logger.exception("Runner %s nicht sauber abgemeldet", conn.runner_id)
        if conn.runner_id is not None:
            logger.info("Runner %s getrennt (Code %s)", conn.runner_id, conn.close_code or "-")

    async def _receive(self, conn: Connection, timeout: float) -> dict:
        receive = asyncio.ensure_future(conn.ws.receive())
        closed = asyncio.ensure_future(conn.closed_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {receive, closed}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            closed.cancel()
        if receive not in done:
            receive.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await receive
            if conn.closed:
                raise Closed()
            raise TimeoutError
        message = receive.result()
        if message["type"] == "websocket.disconnect":
            conn.close_code = message.get("code")
            conn.closed = True
            raise Closed()
        text = message.get("text")
        if text is None:
            await conn.close(protocol.CLOSE_PROTOCOL, "protocol", "Nur Textrahmen erlaubt")
            raise Closed()
        try:
            return protocol.parse(text, protocol.AGENT)
        except protocol.ProtocolError as exc:
            # Nur Feldname und Regel, keine Inhalte (Token steht im hello).
            logger.warning("Rahmen von %s abgewiesen (%s): %s", conn.ip or "?", exc.close_code, exc)
            await conn.close(exc.close_code, "protocol", exc.message)
            raise Closed() from None

    async def _serve(self, conn: Connection) -> None:
        if conn.ip and await db(store.auth_blocked, conn.ip):
            await conn.close(protocol.CLOSE_RATE_LIMIT)
            return
        try:
            hello = await self._receive(conn, HELLO_TIMEOUT)
        except TimeoutError:
            await conn.close(protocol.CLOSE_HELLO_TIMEOUT)
            return
        if hello["type"] != "hello":
            await conn.close(protocol.CLOSE_PROTOCOL, "protocol", "Zuerst hello senden")
            return
        if hello["protocol"] != protocol.PROTOCOL_VERSION:
            await conn.close(protocol.CLOSE_VERSION)
            return
        auth = await db(store.authenticate, hello["token"], conn.ip)
        if auth.code is not None:
            await conn.close(auth.code)
            return
        key = await asyncio.to_thread(trust.server_key_or_none)
        if key is None:
            logger.error(trust.MSG_NO_KEY.format(path=trust.server_key_path()))
            await conn.close(protocol.CLOSE_SERVER_ERROR)
            return
        conn.key, conn.nonce = key, hello["nonce"]
        conn.token_id, conn.owner_id = auth.token_id, auth.owner_id
        connected = await db(store.connect, auth.token_id, hello, conn.ip)
        conn.runner_id, conn.connected_at = connected.runner_id, connected.connected_at
        await self.register(conn)
        await conn.send(
            {
                "type": "welcome",
                "runner": conn.runner_id,
                "protocol": protocol.PROTOCOL_VERSION,
                "server_version": (app_version() or "0")[:64],
                "heartbeat_seconds": int(HEARTBEAT_SECONDS),
                "images": _images(),
                "max_frame_bytes": protocol.MAX_FRAME_BYTES,
                "chunk_bytes": protocol.CHUNK_BYTES,
                "services": connected.services,
                "catalog": connected.catalog_state,
            }
        )
        logger.info("Runner %s verbunden", conn.runner_id)
        for frame in connected.frames:
            await conn.send(frame)
        await conn.drain()
        bucket = self.bucket(conn.token_id)
        while not conn.closed:
            try:
                frame = await self._receive(conn, idle_timeout())
            except TimeoutError:
                await conn.close(protocol.CLOSE_TRY_AGAIN)
                return
            if not bucket.take():
                await conn.close(protocol.CLOSE_RATE_LIMIT)
                return
            await conn.handle(frame)


def _conninfo() -> dict:
    """Verbindungsparameter für die LISTEN-Verbindung (wie Djangos eigene)."""
    params = dict(connections[DEFAULT_DB_ALIAS].get_connection_params())
    params.pop("cursor_factory", None)
    params.pop("context", None)
    params.pop("prepare_threshold", None)
    return params


def _notify_connected(runner_id: int, conn_id: str) -> None:
    control.notify_ctl(control.KIND_CONNECTED, runner=runner_id, conn=conn_id)


def build_app() -> Starlette:
    """ASGI-App des Runner-Dienstes (je Aufruf ein eigener ``Hub``)."""
    from . import pty_direct  # importiert dieses Modul

    hub = Hub()

    async def terminal(websocket: WebSocket) -> None:
        await pty_direct.endpoint(hub, websocket)

    app = Starlette(
        routes=[
            WebSocketRoute(PATH, hub.endpoint),
            WebSocketRoute(pty_direct.PATH, terminal),
        ],
        lifespan=hub.lifespan,
    )
    app.state.hub = hub
    return app
