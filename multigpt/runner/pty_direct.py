"""Terminal im Browser über einen direkten WebSocket (``/runner/pty/<uuid>``).

Der alte Weg (``views_live.pty_stream``/``pty_input``) schickt jeden Tastendruck
als POST über ``RunnerMessage`` + NOTIFY an den Runner-Dienst und die Ausgabe
über dieselbe Warteschlange und SSE zurück – mehrere Datenbankzugriffe je
Tastendruck. Hier hält der Browser einen WebSocket direkt zum Runner-Dienst; der
Dienst reicht ``pty_open``/``pty_input``/``pty_resize``/``pty_close`` über die
bestehende Agent-Verbindung (``gateway.Connection.send``, signiert) weiter und
leitet ``pty_data``/``pty_exit``/``error`` dieses PTY im Speicher an den Browser
(``Hub.terminals``). Die Datenbank sieht nur Öffnen und Schließen (Prüfung,
Audit) und alle ``RECHECK_SECONDS`` eine Nachprüfung.

**Zugang** (vor dem ersten Rahmen):

- ``Origin`` muss die eigene Adresse sein (Schutz vor Cross-Site-WebSocket):
  ein Eintrag aus ``CSRF_TRUSTED_ORIGINS`` oder derselbe Name wie im ``Host``
  (Port egal), der in ``ALLOWED_HOSTS`` stehen muss. Sonst 403 vor dem Handshake.
- Anmeldung über die Django-Sitzung (Session-Cookie, wie ``login_required``),
  Sitzung des eigenen Kontos, Recht „Runner nutzen“, Sitzung läuft – wie
  ``views_live._usable_session``.
- Der Runner muss an **diesem** Prozess verbunden sein. Sonst ``failure`` mit
  ``fallback: true`` und Close 4503: der Browser nimmt dann den alten Weg.

**Rahmen** (JSON-Text). Browser → Dienst: ``{"type": "input", "data": Text}``
(UTF-8, höchstens ``protocol.PTY_BYTES``), ``{"type": "resize", "cols", "rows"}``,
``{"type": "close"}``. Dienst → Browser: ``open`` {pty}, ``data`` {data b64},
``exit`` {code}, ``end`` {message}, ``failure`` {message, fallback}.

**Grenzen:** Nachricht höchstens ``MAX_MESSAGE`` Byte (sonst 1009), Token-Eimer
je Terminal (sonst 4429), höchstens ``PER_USER`` Terminals je Konto und
``PER_PROCESS`` je Prozess, Leerlauf ``PTY_IDLE_SECONDS`` und Höchstdauer
``PTY_MAX_SECONDS`` wie beim alten Weg, ungelesene Ausgabe höchstens
``MAX_PENDING_BYTES`` (langsamer Browser → Ende statt Speicher ohne Grenze).
Audit: Öffnen und Schließen (Art „Sitzung“, nie Inhalte).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from importlib import import_module
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from django.conf import settings
from django.http.request import split_domain_port, validate_host
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from . import audit, delivery, protocol, store
from .gateway import Closed, TokenBucket, _client_ip, db

logger = logging.getLogger(__name__)

PATH = "/runner/pty/{session_uuid}"
PTY_IDLE_SECONDS = 10 * 60
PTY_MAX_SECONDS = 60 * 60
RECHECK_SECONDS = 30.0  # Sitzung, Konto und Recht erneut prüfen
PER_USER = 4
PER_PROCESS = 64
MAX_MESSAGE = 2 * protocol.PTY_BYTES + 256  # JSON mit Escapes
MAX_PENDING_BYTES = 4 * 1024 * 1024
RATE_PER_SECOND = 200.0
RATE_BURST = 400.0

# Close-Codes zum Browser
CLOSE_NORMAL = 1000
CLOSE_TOO_BIG = 1009
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_NOT_FOUND = 4404
CLOSE_IDLE = 4408
CLOSE_NOT_RUNNING = 4409
CLOSE_RATE_LIMIT = 4429
CLOSE_ELSEWHERE = 4503  # Runner an einem anderen Prozess / nicht verbunden

MSG_LOGIN = "Bitte erneut anmelden."
MSG_NOT_FOUND = "Sitzung nicht gefunden."
MSG_NO_RIGHT = "Der Runner ist für dieses Konto nicht freigegeben."
MSG_NOT_RUNNING = "Die Sitzung läuft nicht."
MSG_ELSEWHERE = "Der Runner ist an einem anderen Prozess verbunden."
MSG_BUSY = "Zu viele offene Terminals. Bitte ein anderes schließen."
MSG_RATE = "Zu viele Eingaben in kurzer Zeit."
MSG_SLOW = "Die Ausgabe kommt schneller, als der Browser sie liest."
MSG_IDLE = "Terminal wegen Leerlauf geschlossen."


# --- Zugang ------------------------------------------------------------------------


def origin_allowed(origin: str | None, host: str | None) -> bool:
    """``Origin`` des Browsers: Eintrag aus ``CSRF_TRUSTED_ORIGINS`` oder die eigene
    Adresse (``Host``, Name aus ``ALLOWED_HOSTS``). Ohne Origin: nein."""
    origin = (origin or "").strip().lower().rstrip("/")
    if not origin or origin == "null":
        return False
    trusted = {
        str(item).strip().lower().rstrip("/")
        for item in getattr(settings, "CSRF_TRUSTED_ORIGINS", [])
        if "*" not in str(item)
    }
    if origin in trusted:
        return True
    parsed = urlsplit(origin)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or not host:
        return False
    # Gleicher Name wie der Dienst; der Port zählt nicht (nginx reicht ``$host``
    # ohne Port weiter, und Cookies gelten ohnehin für alle Ports eines Namens).
    domain, _port = split_domain_port(host.strip().lower())
    if not domain or parsed.hostname != domain.strip("[]"):
        return False
    return validate_host(domain, settings.ALLOWED_HOSTS)


def _user_id_from_cookie(session_key: str) -> int | None:
    """Angemeldetes, aktives Konto zur Django-Sitzung (wie ``AuthenticationMiddleware``)."""
    from django.contrib.auth import get_user

    store.fresh()
    engine = import_module(settings.SESSION_ENGINE)
    request = SimpleNamespace(session=engine.SessionStore(session_key))
    user = get_user(request)
    if not user.is_authenticated or not user.is_active:
        return None
    return user.pk


@dataclass
class Target:
    session_pk: int
    session_uuid: str
    runner_id: int
    owner_id: int


def _target(user_id: int, session_uuid: str) -> tuple[Target | None, int, str]:
    """Wie ``views_live._usable_session``: (Ziel, 0, "") oder (None, Close-Code, Meldung)."""
    from multigpt.accounts.permissions import Action, can

    from .models import RunnerSession

    store.fresh()
    session = (
        RunnerSession.objects.select_related("owner", "owner__role")
        .filter(uuid=session_uuid, owner_id=user_id)
        .first()
    )
    if session is None:
        return None, CLOSE_NOT_FOUND, MSG_NOT_FOUND
    if not session.owner.is_active or not can(session.owner, Action.USE_RUNNER):
        return None, CLOSE_FORBIDDEN, MSG_NO_RIGHT
    if session.status != RunnerSession.Status.RUNNING or session.runner_id is None:
        return None, CLOSE_NOT_RUNNING, MSG_NOT_RUNNING
    return Target(session.pk, str(session.uuid), session.runner_id, user_id), 0, ""


def _runner_online(runner_id: int) -> bool:
    store.fresh()
    return delivery.runner_online(runner_id)


def _audit(target: Target, message: str, ip: str | None) -> None:
    """Öffnen bzw. Schließen ins Audit der Sitzung (nie Inhalte)."""
    from .models import RunnerSession

    store.fresh()
    try:
        session = RunnerSession.objects.select_related("owner").get(pk=target.session_pk)
        audit.record(
            "session",
            owner=session.owner,
            session=session,
            runner=session.runner,
            tool="terminal",
            message=message,
            ip=ip,
        )
    except Exception:  # Audit darf das Terminal nicht anhalten
        logger.exception("Audit des Terminals nicht geschrieben")


# --- Terminal ----------------------------------------------------------------------


class _End(Exception):
    """Terminal endet; ``frame`` geht noch an den Browser, ``code`` als Close-Code."""

    def __init__(self, code: int, frame: dict | None = None):
        super().__init__(code)
        self.code = code
        self.frame = frame


class Terminal:
    """Ein PTY im Browser. Ereignisse (Rahmen des Agents, Eingaben, Ende der
    Agent-Verbindung) laufen über eine Warteschlange; nur ``run`` schreibt an den
    Browser."""

    def __init__(
        self, hub, conn, websocket: WebSocket, target: Target, ip: str | None, cookie: str
    ):
        self.hub = hub
        self.cookie = cookie
        self.conn = conn
        self.ws = websocket
        self.target = target
        self.ip = ip
        self.pty = uuid.uuid4().hex
        self.events: asyncio.Queue = asyncio.Queue()
        self.pending = 0  # Byte ungelesener Ausgabe
        self.overflow = False
        self.exited = False
        self.bucket = TokenBucket(RATE_PER_SECOND, RATE_BURST)
        self.last_activity = time.monotonic()

    # Aus der Empfangsschleife der Agent-Verbindung: darf nie blockieren.
    def deliver(self, frame: dict) -> None:
        if self.overflow:
            return
        size = len(frame.get("data") or "")
        if self.pending + size > MAX_PENDING_BYTES:
            self.overflow = True
            self.events.put_nowait(("end", {"type": "failure", "message": MSG_SLOW}))
            return
        self.pending += size
        self.events.put_nowait(("agent", frame))

    async def _to_agent(self, frame: dict) -> None:
        frame = protocol.validate(frame, protocol.SERVER)
        await self.conn.send(frame)

    async def _reader(self) -> None:
        """Browser → Agent (eigene Task); Ende als Ereignis in die Warteschlange."""
        target = self.target
        try:
            while True:
                message = await self.ws.receive()
                if message["type"] == "websocket.disconnect":
                    self.events.put_nowait(("gone", None))
                    return
                text = message.get("text")
                if text is None or len(text) > MAX_MESSAGE:
                    self.events.put_nowait(("close", CLOSE_TOO_BIG))
                    return
                if not self.bucket.take():
                    self.events.put_nowait(
                        ("end", {"type": "failure", "message": MSG_RATE, "_code": CLOSE_RATE_LIMIT})
                    )
                    return
                try:
                    data = json.loads(text)
                except ValueError:
                    continue
                if not isinstance(data, dict):
                    continue
                kind = data.get("type")
                base = {"session": target.session_uuid, "pty": self.pty}
                if kind == "input":
                    raw = data.get("data")
                    if not isinstance(raw, str) or not raw:
                        continue
                    payload = raw.encode("utf-8", "replace")
                    if len(payload) > protocol.PTY_BYTES:
                        self.events.put_nowait(("close", CLOSE_TOO_BIG))
                        return
                    self.last_activity = time.monotonic()
                    await self._to_agent(
                        {
                            "type": "pty_input",
                            **base,
                            "data": base64.b64encode(payload).decode("ascii"),
                        }
                    )
                elif kind == "resize":
                    try:
                        cols = min(max(int(data.get("cols")), 10), 500)
                        rows = min(max(int(data.get("rows")), 4), 200)
                    except (TypeError, ValueError):
                        continue
                    self.last_activity = time.monotonic()
                    await self._to_agent({"type": "pty_resize", **base, "cols": cols, "rows": rows})
                elif kind == "close":
                    self.events.put_nowait(
                        ("end", {"type": "end", "message": "Terminal geschlossen."})
                    )
                    return
        except Closed:
            self.events.put_nowait(("offline", None))
        except WebSocketDisconnect:
            self.events.put_nowait(("gone", None))
        except protocol.ProtocolError as exc:
            logger.warning("Ungültige Eingabe ins Terminal verworfen: %s", exc)
            self.events.put_nowait(("close", CLOSE_NORMAL))

    async def _send(self, frame: dict) -> None:
        await self.ws.send_text(json.dumps(frame, ensure_ascii=False, separators=(",", ":")))

    async def run(self, cols: int, rows: int) -> int:
        """Terminal öffnen und bedienen; liefert den Close-Code für den Browser."""
        started = time.monotonic()
        await self._to_agent(
            {
                "type": "pty_open",
                "session": self.target.session_uuid,
                "pty": self.pty,
                "cols": cols,
                "rows": rows,
            }
        )
        await self._send({"type": "open", "pty": self.pty})
        reader = asyncio.ensure_future(self._reader())
        offline = asyncio.ensure_future(self.conn.closed_event.wait())
        offline.add_done_callback(
            lambda f: f.cancelled() or self.events.put_nowait(("offline", None))
        )
        next_check = started + RECHECK_SECONDS
        try:
            while True:
                now = time.monotonic()
                if now - started > PTY_MAX_SECONDS or now - self.last_activity > PTY_IDLE_SECONDS:
                    raise _End(CLOSE_IDLE, {"type": "end", "message": MSG_IDLE})
                if now >= next_check:
                    next_check = now + RECHECK_SECONDS
                    await self._recheck()
                timeout = min(
                    next_check - now,
                    started + PTY_MAX_SECONDS - now,
                    self.last_activity + PTY_IDLE_SECONDS - now,
                )
                try:
                    kind, value = await asyncio.wait_for(self.events.get(), max(timeout, 0.01))
                except TimeoutError:
                    continue
                if kind == "agent":
                    await self._from_agent(value)
                elif kind == "end":
                    code = value.pop("_code", CLOSE_NORMAL)
                    raise _End(code, value)
                elif kind == "close":
                    raise _End(value)
                elif kind == "gone":
                    raise _End(CLOSE_NORMAL)
                elif kind == "offline":
                    raise _End(
                        CLOSE_ELSEWHERE,
                        {"type": "failure", "message": delivery.OFFLINE_MESSAGE},
                    )
        except _End as end:
            if end.frame is not None:
                with contextlib.suppress(Exception):
                    await self._send(end.frame)
            return end.code
        finally:
            offline.cancel()
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader

    async def _from_agent(self, frame: dict) -> None:
        kind = frame["type"]
        if kind == "pty_data":
            data = frame.get("data") or ""
            self.pending -= len(data)
            self.last_activity = time.monotonic()
            await self._send({"type": "data", "data": data})
        elif kind == "pty_exit":
            self.exited = True
            raise _End(CLOSE_NORMAL, {"type": "exit", "code": frame.get("code")})
        elif kind == "error":
            raise _End(
                CLOSE_NORMAL,
                {"type": "failure", "message": frame.get("message") or "Fehler im Runner"},
            )

    async def _recheck(self) -> None:
        if await db(_user_id_from_cookie, self.cookie) != self.target.owner_id:
            raise _End(CLOSE_UNAUTHORIZED, {"type": "end", "message": MSG_LOGIN})
        target, code, message = await db(_target, self.target.owner_id, self.target.session_uuid)
        if target is None or target.runner_id != self.target.runner_id:
            raise _End(
                code or CLOSE_NOT_RUNNING, {"type": "end", "message": message or MSG_NOT_RUNNING}
            )

    async def close_pty(self) -> None:
        if self.exited or self.conn.closed:
            return
        with contextlib.suppress(Exception):
            await self._to_agent(
                {"type": "pty_close", "session": self.target.session_uuid, "pty": self.pty}
            )


def _size(websocket: WebSocket) -> tuple[int, int]:
    query = parse_qs(websocket.url.query or "")

    def number(name, fallback, low, high):
        try:
            return min(max(int((query.get(name) or [fallback])[0]), low), high)
        except (TypeError, ValueError):
            return fallback

    return number("cols", 80, 10, 500), number("rows", 24, 4, 200)


async def _refuse(websocket: WebSocket, code: int, message: str, fallback: bool = False) -> None:
    with contextlib.suppress(Exception):
        frame = {"type": "failure", "message": message, "fallback": fallback}
        await websocket.send_text(json.dumps(frame, ensure_ascii=False))
    with contextlib.suppress(Exception):
        if websocket.application_state != WebSocketState.DISCONNECTED:
            await websocket.close(code)


async def endpoint(hub, websocket: WebSocket) -> None:
    """``/runner/pty/<uuid>``: Terminal einer eigenen Sitzung (Moduldoku)."""
    if not origin_allowed(websocket.headers.get("origin"), websocket.headers.get("host")):
        await websocket.close(CLOSE_FORBIDDEN)  # vor dem Handshake: 403
        return
    ip = _client_ip(websocket)
    session_uuid = str(websocket.path_params.get("session_uuid") or "")
    await websocket.accept()
    if not protocol.UUID_RE.match(session_uuid):
        await _refuse(websocket, CLOSE_NOT_FOUND, MSG_NOT_FOUND)
        return
    cookie = websocket.cookies.get(settings.SESSION_COOKIE_NAME) or ""
    user_id = await db(_user_id_from_cookie, cookie) if cookie else None
    if user_id is None:
        await _refuse(websocket, CLOSE_UNAUTHORIZED, MSG_LOGIN)
        return
    target, code, message = await db(_target, user_id, session_uuid)
    if target is None:
        await _refuse(websocket, code, message)
        return
    conn = hub.connections.get(target.runner_id)
    if conn is None or conn.closed:
        if await db(_runner_online, target.runner_id):
            await _refuse(websocket, CLOSE_ELSEWHERE, MSG_ELSEWHERE, fallback=True)
        else:
            await _refuse(websocket, CLOSE_ELSEWHERE, delivery.OFFLINE_MESSAGE)
        return
    mine = sum(1 for item in hub.terminals.values() if item.target.owner_id == user_id)
    if mine >= PER_USER or len(hub.terminals) >= PER_PROCESS:
        await _refuse(websocket, CLOSE_RATE_LIMIT, MSG_BUSY)
        return
    terminal = Terminal(hub, conn, websocket, target, ip, cookie)
    # Erst eintragen, dann pty_open: keine Ausgabe geht verloren.
    hub.terminals[terminal.pty] = terminal
    code = CLOSE_NORMAL
    try:
        await db(_audit, target, "Terminal geöffnet (Browser)", ip)
        code = await terminal.run(*_size(websocket))
    except Closed:
        code = CLOSE_ELSEWHERE
        with contextlib.suppress(Exception):
            await terminal._send({"type": "failure", "message": delivery.OFFLINE_MESSAGE})
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Fehler im Terminal der Sitzung %s", target.session_uuid)
        code = 1011
    finally:
        hub.terminals.pop(terminal.pty, None)
        await terminal.close_pty()
        with contextlib.suppress(Exception):
            if websocket.application_state != WebSocketState.DISCONNECTED:
                await websocket.close(code)
        await asyncio.shield(db(_audit, target, "Terminal geschlossen (Browser)", ip))
