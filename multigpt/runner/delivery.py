"""Zustellung Web/Worker ↔ Runner-Dienst über PostgreSQL LISTEN/NOTIFY (M14-05).

Muster wie ``node/sse.py`` (M15-11): Der Absender schreibt eine
``RunnerMessage`` und ruft in derselben Transaktion ``pg_notify(kanal, id)``
auf; die Nutzlast (ein Rahmen aus ``protocol.py``) liegt wegen der Grenze von
knapp 8000 Byte immer in der Tabelle. Der Empfänger holt sie mit
``DELETE … RETURNING`` in ID-Reihenfolge.

**Kanäle:**

- ``mgpt_runner_<runner_id>``: Rahmen an den Runner. Der Runner-Dienst hört
  darauf, solange der Runner bei ihm verbunden ist (``listener.py``).
- ``mgpt_runner_reply_<prozess>``: Antworten an diesen Prozess (gunicorn bzw.
  Worker). Ein Daemon-Thread je Prozess (``_Dispatcher``) hält die
  LISTEN-Verbindung, erst ab dem ersten Runner-Aufruf, und verteilt die Rahmen
  an die Abonnenten (``Subscription``).
- ``mgpt_runner_ctl``: Steuerereignisse (``control.py``).

**Zuordnung der Antworten:** Der Runner-Dienst merkt sich je Verbindung, welcher
Prozess zuletzt einen Rahmen mit einer bestimmten pipe, pty, request bzw. session
geschickt hat, und schickt die Antworten dorthin (``route_key``):
``mcp`` → pipe, ``pty_*`` → pty, ``preview_*`` mit request → request,
``session_*``/``preview`` → session, ``file_chunk`` → transfer (angemeldet mit
``claim_route``/``subscribe_transfer``). ``pty_data``/``pty_exit`` ohne
``pty_open`` (Werkzeugläufe des Agents, ``run-<n>``) gehen an die Sitzung.
``error`` geht an ``ref`` (pipe, pty, request), sonst an die Sitzung.

**Fehler:** Ist der Runner nicht verbunden, wirft ``send`` sofort
``RunnerOffline``. Trennt er sich, während Antworten ausstehen, schickt der
Runner-Dienst ``error`` mit Code ``offline`` an alle bekannten Abnehmer; die
Abonnements werfen dann ``RunnerOffline``. Geht eine Benachrichtigung verloren,
holt der Dispatcher die Warteschlange spätestens nach ``POLL_SECONDS`` ab.

**API für Werkzeuge (``tools``):**

- sync (Request- bzw. Worker-Thread): ``send``, ``subscribe`` →
  ``Subscription.get(timeout)``, ``request`` (senden und auf die erste Antwort
  warten).
- async (MCP-Loop-Thread, ohne ORM im Loop): ``Pipe`` (``send``, ``receive``,
  ``aclose``) und ``asubscribe`` → ``await Subscription.aget(timeout)``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import json
import logging
import os
import queue
import threading
import time
import uuid
from datetime import timedelta

import psycopg
from django.db import DEFAULT_DB_ALIAS, close_old_connections, connections, transaction
from django.utils import timezone
from psycopg import sql

from . import protocol
from .models import Runner, RunnerMessage

logger = logging.getLogger(__name__)

POLL_SECONDS = 5.0  # Nachholen ohne Benachrichtigung
READY_TIMEOUT = 10.0  # Warten auf die LISTEN-Verbindung des Dispatchers
OFFLINE_MESSAGE = "Der Runner ist nicht verbunden."
TIMEOUT_MESSAGE = "Der Runner hat nicht rechtzeitig geantwortet."

# Rahmen, die nur der Runner-Dienst selbst an den Agent schickt.
GATEWAY_ONLY = frozenset({"welcome", "close", "bundle_chunk", "bundle_ack", "error"})
ROUTE_KINDS = ("pipe", "pty", "request", "session", "transfer")
# Interne Nachricht an den Runner-Dienst (nicht an den Agent): Zuordnung anmelden.
ROUTE_MESSAGE = "route"
# Agent-eigene Terminals (Werkzeugläufe ``run-<n>``) ohne ``pty_open``: an die Sitzung.
_SESSION_FALLBACK = frozenset({"pty_data", "pty_exit"})


class RunnerError(Exception):
    """Fehler bei der Zustellung; ``message`` ist deutsch und ohne Geheimnisse."""

    def __init__(self, message: str, code: str = "internal"):
        super().__init__(message)
        self.message = message
        self.code = code


class RunnerOffline(RunnerError):
    def __init__(self, message: str = OFFLINE_MESSAGE):
        super().__init__(message, "offline")


class RunnerTimeout(RunnerError):
    def __init__(self, message: str = TIMEOUT_MESSAGE):
        super().__init__(message, "timeout")


def runner_channel(runner_id: int) -> str:
    return f"mgpt_runner_{int(runner_id)}"


def stale_after() -> timedelta:
    """Ohne Lebenszeichen seit dieser Zeit gilt ein Runner als getrennt."""
    return timedelta(seconds=3 * protocol.HEARTBEAT_SECONDS)


def route_key(frame: dict) -> tuple[str, str] | None:
    """Zuordnung eines Rahmens zu seinem Abnehmer (siehe Moduldoku)."""
    kind = frame.get("type")
    if kind == "mcp":
        return ("pipe", frame["pipe"])
    if kind in ("pty_open", "pty_input", "pty_resize", "pty_close", "pty_data", "pty_exit"):
        return ("pty", frame["pty"])
    if kind in ("preview_request", "preview_response"):
        return ("request", frame["request"])
    if kind in ("session_start", "session_stop", "session_state", "preview"):
        return ("session", frame["session"])
    if kind == "file_chunk":
        return ("transfer", frame["transfer"])
    return None


def runner_online(runner_id: int) -> bool:
    return Runner.objects.filter(
        pk=runner_id,
        status=Runner.Status.ONLINE,
        last_seen__gte=timezone.now() - stale_after(),
    ).exists()


def _compact(frame: dict) -> dict:
    return {key: value for key, value in frame.items() if value is not None}


def send(runner_id: int, frame: dict, *, session=None, reply: bool = True) -> int:
    """Rahmen an den Runner schicken (sync, ORM). Liefert die ID der Nachricht.

    ``reply=False``: keine Antwort erwartet (z. B. ``session_stop`` aus dem
    Worker); dann wird kein Antwortkanal eingetragen. Wirft ``RunnerOffline``,
    wenn der Runner nicht verbunden ist, und ``protocol.ProtocolError`` bei
    ungültigem Rahmen.
    """
    frame = protocol.validate(frame, protocol.SERVER)
    if frame["type"] in GATEWAY_ONLY:
        raise ValueError(f"Rahmen {frame['type']} schickt nur der Runner-Dienst")
    if not runner_online(runner_id):
        raise RunnerOffline()
    reply_channel = _dispatcher().channel if reply and route_key(frame) else ""
    pipe = frame.get("pipe")
    session_id = getattr(session, "pk", session)
    with transaction.atomic():
        message = RunnerMessage.objects.create(
            runner_id=runner_id,
            direction=RunnerMessage.Direction.TO_RUNNER,
            pipe=_uuid_or_none(pipe),
            session_id=session_id,
            reply_channel=reply_channel,
            kind=frame["type"],
            payload=_compact(frame),
        )
        with connections[DEFAULT_DB_ALIAS].cursor() as cursor:
            cursor.execute("SELECT pg_notify(%s, %s)", [runner_channel(runner_id), str(message.pk)])
    return message.pk


def claim_route(runner_id: int, kind: str, key: str) -> None:
    """Antworten zu (``kind``, ``key``) an diesen Prozess leiten, ohne einen Rahmen
    an den Agent zu schicken – z. B. ``transfer`` für ``file_chunk`` (publish),
    dessen ID nur im MCP-Aufruf steht. Wirft ``RunnerOffline`` wie ``send``."""
    if kind not in ROUTE_KINDS:
        raise ValueError(f"unbekannte Zuordnung {kind!r}")
    if not protocol.ID_RE.match(str(key)):
        raise ValueError("ungültige ID")
    if not runner_online(runner_id):
        raise RunnerOffline()
    with transaction.atomic():
        message = RunnerMessage.objects.create(
            runner_id=runner_id,
            direction=RunnerMessage.Direction.TO_RUNNER,
            reply_channel=_dispatcher().channel,
            kind=ROUTE_MESSAGE,
            payload={"type": ROUTE_MESSAGE, "kind": kind, "key": str(key)},
        )
        with connections[DEFAULT_DB_ALIAS].cursor() as cursor:
            cursor.execute("SELECT pg_notify(%s, %s)", [runner_channel(runner_id), str(message.pk)])


def subscribe_transfer(runner_id: int, transfer: str) -> Subscription:
    """Abonnement für ``file_chunk`` einer Übertragung (sync); vor dem MCP-Aufruf."""
    sub = subscribe("transfer", transfer)
    try:
        claim_route(runner_id, "transfer", transfer)
    except Exception:
        sub.close()
        raise
    return sub


def _uuid_or_none(value) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


# --- Abonnements --------------------------------------------------------------


class Subscription:
    """Empfang der Rahmen zu einem Schlüssel (``kind``, ``id``) in diesem Prozess.

    Sync (``get``) oder, mit ``loop``, async (``aget``) – je nachdem, wo der
    Abnehmer läuft. Der Dispatcher-Thread liefert über ``_put``.
    """

    def __init__(self, kind: str, key: str, loop: asyncio.AbstractEventLoop | None = None):
        if kind not in ROUTE_KINDS:
            raise ValueError(f"unbekannte Zuordnung {kind!r}")
        self.key = (kind, str(key))
        self._loop = loop
        self._queue: queue.Queue | None = None if loop else queue.Queue()
        self._aqueue: asyncio.Queue | None = asyncio.Queue() if loop else None
        self.closed = False

    def _put(self, frame: dict) -> None:
        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._aqueue.put_nowait, frame)
            except RuntimeError:  # Loop schon zu
                pass
        else:
            self._queue.put(frame)

    @staticmethod
    def _check(frame: dict) -> dict:
        if frame.get("type") == "error" and frame.get("code") == "offline":
            raise RunnerOffline(frame.get("message") or OFFLINE_MESSAGE)
        return frame

    def get(self, timeout: float | None = None) -> dict:
        """Nächsten Rahmen holen (sync); ``RunnerTimeout`` nach ``timeout`` s."""
        try:
            return self._check(self._queue.get(timeout=timeout))
        except queue.Empty:
            raise RunnerTimeout() from None

    async def aget(self, timeout: float | None = None) -> dict:
        """Nächsten Rahmen holen (async); ``RunnerTimeout`` nach ``timeout`` s."""
        try:
            async with asyncio.timeout(timeout):
                return self._check(await self._aqueue.get())
        except TimeoutError:
            raise RunnerTimeout() from None

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            _dispatcher(start=False).remove(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def subscribe(kind: str, key: str) -> Subscription:
    """Abonnement (sync) anlegen; vor dem ``send`` aufrufen, sonst droht ein Wettlauf."""
    sub = Subscription(kind, key)
    _dispatcher().add(sub)
    return sub


async def asubscribe(kind: str, key: str) -> Subscription:
    """Abonnement (async, im laufenden Loop) anlegen."""
    sub = Subscription(kind, key, asyncio.get_running_loop())
    dispatcher = _dispatcher(start=False)
    dispatcher.add(sub)
    await asyncio.get_running_loop().run_in_executor(_db_executor(), dispatcher.ensure_started)
    return sub


def request(runner_id: int, frame: dict, *, timeout: float, session=None, accept=None) -> dict:
    """Rahmen senden und auf die erste passende Antwort warten (sync).

    ``accept(frame) -> bool`` filtert (z. B. ``session_state`` mit Zustand
    ``running``); ohne Filter zählt die erste Antwort mit demselben Schlüssel.
    ``error``-Rahmen werden als ``RunnerError`` ausgelöst.
    """
    checked = protocol.validate(frame, protocol.SERVER)
    key = route_key(checked)
    if key is None:
        raise ValueError(f"Auf {checked['type']} gibt es keine Antwort")
    deadline = time.monotonic() + timeout
    with subscribe(*key) as sub:
        send(runner_id, frame, session=session)
        while True:
            answer = sub.get(max(0.0, deadline - time.monotonic()))
            if answer.get("type") == "error":
                raise RunnerError(answer.get("message") or "Fehler im Runner", answer["code"])
            if accept is None or accept(answer):
                return answer


class Pipe:
    """Eine MCP-Verbindung (pipe) zu einer Sitzung, für den MCP-Loop-Thread.

    Im laufenden Event-Loop anlegen. ``send`` schreibt über einen eigenen
    DB-Thread (kein ORM im Loop), ``receive`` liefert die JSON-RPC-Nachrichten
    des Agents.
    """

    def __init__(self, runner_id: int, session, pipe: str | None = None):
        self.runner_id = int(runner_id)
        self.session = str(session)
        self.pipe = str(pipe or uuid.uuid4())
        self._sub = Subscription("pipe", self.pipe, asyncio.get_running_loop())
        _dispatcher(start=False).add(self._sub)

    async def send(self, message: dict) -> None:
        frame = {"type": "mcp", "session": self.session, "pipe": self.pipe, "message": message}
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            _db_executor(), functools.partial(_send_from_loop, self.runner_id, frame)
        )

    async def receive(self, timeout: float | None = None) -> dict:
        frame = await self._sub.aget(timeout)
        if frame.get("type") == "error":
            raise RunnerError(frame.get("message") or "Fehler im Runner", frame["code"])
        return frame["message"]

    async def aclose(self) -> None:
        self._sub.close()


def _send_from_loop(runner_id: int, frame: dict) -> int:
    close_old_connections()
    _dispatcher().ensure_started()
    return send(runner_id, frame)


_executor: concurrent.futures.ThreadPoolExecutor | None = None
_executor_pid: int | None = None
_executor_lock = threading.Lock()


def _db_executor() -> concurrent.futures.ThreadPoolExecutor:
    """Ein Thread je Prozess für DB-Zugriffe aus dem Event-Loop (eine Verbindung)."""
    global _executor, _executor_pid
    with _executor_lock:
        if _executor is None or _executor_pid != os.getpid():
            _executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="mgpt-runner-db"
            )
            _executor_pid = os.getpid()
        return _executor


# --- Dispatcher (Antwort-Listener je Prozess) -----------------------------------


class _Dispatcher:
    def __init__(self):
        self.pid = os.getpid()
        self.channel = f"mgpt_runner_reply_{uuid.uuid4().hex}"
        self._subs: dict[tuple[str, str], list[Subscription]] = {}
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._conn = None
        self._take = sql.SQL(
            "DELETE FROM {} WHERE reply_channel = %s AND direction = %s RETURNING id, payload"
        ).format(sql.Identifier(RunnerMessage._meta.db_table))

    # Abonnenten
    def add(self, sub: Subscription) -> None:
        with self._lock:
            self._subs.setdefault(sub.key, []).append(sub)

    def remove(self, sub: Subscription) -> None:
        with self._lock:
            subs = self._subs.get(sub.key, [])
            if sub in subs:
                subs.remove(sub)
            if not subs:
                self._subs.pop(sub.key, None)

    def _targets(self, frame: dict) -> list[Subscription]:
        keys = []
        if frame.get("type") == "error":
            ref = frame.get("ref")
            if ref:
                keys += [(kind, ref) for kind in ("pipe", "pty", "request", "transfer")]
            if frame.get("session"):
                keys.append(("session", frame["session"]))
        else:
            key = route_key(frame)
            if key is not None:
                keys.append(key)
            if frame.get("type") in _SESSION_FALLBACK and frame.get("session"):
                keys.append(("session", frame["session"]))
        with self._lock:
            for key in keys:
                subs = self._subs.get(key)
                if subs:
                    return list(subs)
        return []

    def dispatch(self, frame: dict) -> int:
        targets = self._targets(frame)
        for sub in targets:
            sub._put(frame)
        if not targets:
            logger.debug("Runner-Rahmen %s ohne Abnehmer verworfen", frame.get("type"))
        return len(targets)

    # Thread
    def ensure_started(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._ready.clear()
                self._thread = threading.Thread(
                    target=self._run, name="mgpt-runner-replies", daemon=True
                )
                self._thread.start()
        if not self._ready.wait(READY_TIMEOUT):
            raise RunnerError("Die Antwortverbindung zum Runner-Dienst fehlt.")

    def _connect(self):
        wrapper = connections[DEFAULT_DB_ALIAS]
        conn = wrapper.Database.connect(**wrapper.get_connection_params())
        conn.autocommit = True
        conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(self.channel)))
        return conn

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._conn = self._connect()
                self._ready.set()
                while not self._stop.is_set():
                    for _notify in self._conn.notifies(timeout=POLL_SECONDS, stop_after=1):
                        pass
                    if self._stop.is_set():
                        break
                    self._drain()
            except psycopg.Error as exc:
                if self._stop.is_set():
                    break
                logger.warning("Runner-Antwortverbindung unterbrochen: %s", type(exc).__name__)
                self._stop.wait(1.0)
            finally:
                self._close_conn()

    def _drain(self) -> None:
        rows = self._conn.execute(
            self._take, [self.channel, RunnerMessage.Direction.TO_SERVER]
        ).fetchall()
        for _pk, payload in sorted(rows, key=lambda row: row[0]):
            # Djangos Verbindungs-Kontext lädt jsonb als Text.
            if isinstance(payload, str | bytes):
                try:
                    payload = json.loads(payload)
                except ValueError:
                    continue
            if isinstance(payload, dict):
                self.dispatch(payload)

    def _close_conn(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # pragma: no cover - Verbindung schon weg
                pass

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            # notifies() wartet höchstens POLL_SECONDS; ein NOTIFY weckt sofort.
            try:
                with connections[DEFAULT_DB_ALIAS].cursor() as cursor:
                    cursor.execute("SELECT pg_notify(%s, '')", [self.channel])
            except Exception:  # pragma: no cover
                pass
            thread.join(timeout)


_instance: _Dispatcher | None = None
_instance_lock = threading.Lock()


def _dispatcher(start: bool = True) -> _Dispatcher:
    global _instance
    with _instance_lock:
        # Nach einem Fork gehört der Thread dem Elternprozess: neu anlegen.
        if _instance is None or _instance.pid != os.getpid():
            _instance = _Dispatcher()
        dispatcher = _instance
    if start:
        dispatcher.ensure_started()
    return dispatcher


def reply_channel() -> str:
    """Antwortkanal dieses Prozesses (startet den Dispatcher nicht)."""
    return _dispatcher(start=False).channel


def shutdown() -> None:
    """Dispatcher-Thread beenden (Tests, Prozessende)."""
    global _instance
    with _instance_lock:
        dispatcher, _instance = _instance, None
    if dispatcher is not None and dispatcher.pid == os.getpid():
        dispatcher.stop()
    global _executor
    with _executor_lock:
        executor, _executor = _executor, None
    if executor is not None and _executor_pid == os.getpid():
        # DB-Verbindung des Threads schließen, dann den Thread beenden.
        executor.submit(connections.close_all).result(timeout=10)
        executor.shutdown(wait=True)


def cleanup(now=None, minutes: int = 10) -> int:
    """Verwaiste Nachrichten (älter als ``minutes``) löschen (Worker)."""
    now = now or timezone.now()
    deleted, _ = RunnerMessage.objects.filter(created__lt=now - timedelta(minutes=minutes)).delete()
    return deleted
