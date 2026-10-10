"""Legacy-Transport HTTP+SSE (MCP 2024-11-05) für ältere Clients (M15).

Manche Clients sprechen nur den alten Transport, z. B. der n8n-Knoten
„MCP Client Tool“ in Version 1 bzw. mit „Server Sent Events (Deprecated)“,
mcp-remote mit ``--transport sse-only`` und einige Desktop-Clients:

1. ``GET /mcp/sse`` öffnet einen dauerhaften Event-Stream. Zuerst kommt
   ``event: endpoint`` mit ``data: /mcp/messages?session_id=<128 Bit hex>``,
   danach alle ``MCP_SSE_KEEPALIVE_SECONDS`` (15 s) ein Kommentar als Keepalive.
2. Der Client POSTet jede JSON-RPC-Nachricht an diese URL. Der Handler prüft
   Key und Sitzung (die Sitzung gehört zum Key, sonst 404), verarbeitet die
   Nachricht mit demselben Dispatch wie ``/mcp/`` (``views.dispatch``) und
   antwortet mit 202. Die Antwort geht als ``event: message`` an den Stream.

Authentifizierung, Drosselung, Origin-Prüfung, Scopes und Audit-Log sind
dieselben wie bei ``/mcp/`` (``views.authorize``); jeder POST prüft den Key neu.

**Zustellung zwischen Prozessen (Entscheidung LISTEN/NOTIFY):** gunicorn hat
mehrere Prozesse, GET und POST landen oft in verschiedenen. Der POST legt die
Antwort in ``SseMessage`` ab (der NOTIFY-Payload ist auf knapp 8000 Byte
begrenzt, Werkzeugergebnisse mit Dateien sind größer) und sendet in derselben
Transaktion ``pg_notify('mcp_sse_<sitzung>', <id>)``; PostgreSQL stellt das erst
nach dem Commit zu, die Zeile ist dann sichtbar. Der Stream-Thread hält eine
eigene Verbindung mit ``LISTEN`` auf den Kanal seiner Sitzung, wartet mit
``notifies(timeout=…)``, holt dann alle Nachrichten der Sitzung mit
``DELETE … RETURNING`` (lesen und löschen in einem Schritt) und sendet sie in
ID-Reihenfolge. Gegenüber reinem Polling: keine Verzögerung und keine Abfragen
im Sekundentakt je offenem Stream, und keine zusätzliche Infrastruktur (Redis).
Nach jedem Keepalive-Intervall holt der Stream die Warteschlange ohnehin ab;
eine verlorene Benachrichtigung verzögert also höchstens um dieses Intervall.

**Grenzen (Ressourcen):** Jeder offene Stream belegt einen gthread-Thread
(Standard 8 je Prozess) und zwei Datenbankverbindungen (LISTEN und die normale
des Threads). Deshalb höchstens ``MCP_SSE_PER_PROCESS`` (4) Streams je Prozess
und ``MCP_SSE_PER_KEY`` (2) je Key über alle Prozesse (gezählt über die
Sitzungen in der Datenbank, mit Zeilensperre auf dem Key); darüber 503 mit
``Retry-After``. Ein Stream endet nach ``MCP_SSE_MAX_MINUTES`` (30) Minuten,
die Clients verbinden sich neu. gthread-Worker brechen lange Requests nicht ab
(der Heartbeat an den Arbiter läuft im Hauptthread des Workers, unabhängig von
den Request-Threads); ``MULTI_GPT_TIMEOUT`` gilt also nicht für Streams. Beim
Neustart bzw. Reload von gunicorn enden offene Streams nach
``graceful_timeout`` (30 s).

**Widerruf:** Bei jedem Keepalive prüft der Stream den Key frisch (aktiv, nicht
abgelaufen, Konto aktiv, Rolle mit Recht) und endet sonst.

**Aufräumen:** Beim Ende des Streams wird die Sitzung gelöscht (samt offener
Nachrichten). Sitzungen, deren Stream nicht mehr lebt (Prozess beendet), löscht
der Worker (``cleanup``), sobald ``last_seen`` älter als ``stale_after()`` ist.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
from datetime import timedelta

import psycopg
from django.conf import settings
from django.db import DEFAULT_DB_ALIAS, DatabaseError, IntegrityError, connections, transaction
from django.http import HttpResponse, StreamingHttpResponse
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from psycopg import sql

from . import keys, protocol
from .models import ApiCall, ApiKey, SseMessage, SseSession
from .protocol import RpcError
from .views import (
    SSE_TYPE,
    _Call,
    _json,
    _origin_ok,
    _plain,
    _rpc_error,
    authorize,
    dispatch,
    read_message,
)

logger = logging.getLogger(__name__)

SESSION_RE = re.compile(r"^[0-9a-f]{32}$")
# Audit-Log: ein Eintrag je Stream (beim Ende, mit Dauer und gesendeten Bytes).
STREAM_METHOD = "sse"

_slots_lock = threading.Lock()
_open_streams = 0


def _number(name: str, default: float) -> float:
    try:
        value = float(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def keepalive_seconds() -> float:
    return _number("MCP_SSE_KEEPALIVE_SECONDS", 15)


def max_seconds() -> float:
    return _number("MCP_SSE_MAX_MINUTES", 30) * 60


def per_key() -> int:
    return int(_number("MCP_SSE_PER_KEY", 2))


def per_process() -> int:
    return int(_number("MCP_SSE_PER_PROCESS", 4))


def stale_after() -> timedelta:
    """Ab wann eine Sitzung als verwaist gilt (kein Keepalive mehr)."""
    return timedelta(seconds=max(60.0, 4 * keepalive_seconds()))


def channel(session_id: str) -> str:
    return f"mcp_sse_{session_id}"


def open_streams() -> int:
    """Offene Streams dieses Prozesses (für Tests und Diagnose)."""
    return _open_streams


def _take_slot() -> bool:
    global _open_streams
    with _slots_lock:
        if _open_streams >= per_process():
            return False
        _open_streams += 1
        return True


def _release_slot() -> None:
    global _open_streams
    with _slots_lock:
        _open_streams = max(0, _open_streams - 1)


def open_session(key: ApiKey) -> SseSession | None:
    """Sitzung anlegen; None, wenn der Key schon ``per_key()`` lebende Streams hat.

    Die Zeilensperre auf dem Key serialisiert gleichzeitige Verbindungsaufbauten
    desselben Keys über alle Prozesse.
    """
    now = timezone.now()
    with transaction.atomic():
        ApiKey.objects.select_for_update().filter(pk=key.pk).exists()
        alive = SseSession.objects.filter(key=key, last_seen__gte=now - stale_after()).count()
        if alive >= per_key():
            return None
        return SseSession.objects.create(id=secrets.token_hex(16), key=key)


def deliver(session_id: str, payload: dict) -> int:
    """Nachricht in die Warteschlange der Sitzung legen und den Stream wecken.

    Liefert die Größe in Bytes; 0, wenn die Sitzung inzwischen weg ist.
    """
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    try:
        with transaction.atomic():
            message = SseMessage.objects.create(session_id=session_id, payload=data)
            with connections[DEFAULT_DB_ALIAS].cursor() as cursor:
                cursor.execute("SELECT pg_notify(%s, %s)", [channel(session_id), str(message.pk)])
    except IntegrityError:
        return 0
    return len(data.encode())


def cleanup(now=None) -> int:
    """Verwaiste Sitzungen (ohne Keepalive seit ``stale_after()``) löschen (Worker)."""
    now = now or timezone.now()
    deleted, _ = SseSession.objects.filter(last_seen__lt=now - stale_after()).delete()
    return deleted


class Listener:
    """Eigene Datenbankverbindung (autocommit) mit ``LISTEN`` auf eine Sitzung.

    Bewusst außerhalb der Verbindungsverwaltung von Django: Sie lebt so lange wie
    der Stream und wird nur von ihm benutzt.
    """

    def __init__(self, session_id: str):
        wrapper = connections[DEFAULT_DB_ALIAS]
        self.session_id = session_id
        self.conn = wrapper.Database.connect(**wrapper.get_connection_params())
        self.conn.autocommit = True
        self.conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(channel(session_id))))
        self._take = sql.SQL("DELETE FROM {} WHERE session_id = %s RETURNING id, payload").format(
            sql.Identifier(SseMessage._meta.db_table)
        )

    def wait(self, timeout: float) -> bool:
        """Bis zu ``timeout`` Sekunden auf eine Benachrichtigung warten."""
        for _notify in self.conn.notifies(timeout=max(0.0, timeout), stop_after=1):
            return True
        return False

    def take(self) -> list[str]:
        """Alle wartenden Nachrichten der Sitzung lesen und löschen (ID-Reihenfolge)."""
        rows = self.conn.execute(self._take, [self.session_id]).fetchall()
        return [payload for _pk, payload in sorted(rows)]

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # pragma: no cover - Verbindung schon weg
            pass


def _event(name: str, data: str) -> bytes:
    return f"event: {name}\ndata: {data}\n\n".encode()


def _key_usable(key_pk: int) -> bool:
    key = ApiKey.objects.select_related("owner", "owner__role").filter(pk=key_pk).first()
    return key is not None and key.owner.is_active and bool(keys.effective_scopes(key))


class Stream:
    """Inhalt der Stream-Antwort. ``close()`` ruft Django beim Ende der Antwort
    immer auf (auch wenn der Inhalt nie gelesen wurde); es gibt Thread-Platz,
    LISTEN-Verbindung und Sitzung frei und schreibt das Audit-Log."""

    def __init__(self, call: _Call, session: SseSession, listener: Listener, endpoint: str):
        self.call = call
        self.session_id = session.pk
        self.key_pk = session.key_id
        self.listener = listener
        self.endpoint = endpoint
        self.sent = 0
        self.reason = "Client getrennt"
        self._closed = False

    def __iter__(self):
        return self._events()

    def _emit(self, chunk: bytes) -> bytes:
        self.sent += len(chunk)
        return chunk

    def _alive(self) -> bool:
        if not _key_usable(self.key_pk):
            self.reason = "Key ungültig"
            return False
        touched = SseSession.objects.filter(pk=self.session_id).update(last_seen=timezone.now())
        if not touched:
            self.reason = "Sitzung entfernt"
            return False
        return True

    def _events(self):
        try:
            yield self._emit(_event("endpoint", self.endpoint))
            keepalive = keepalive_seconds()
            deadline = time.monotonic() + max_seconds()
            next_check = time.monotonic() + keepalive
            while True:
                now = time.monotonic()
                if now >= deadline:
                    self.reason = "Höchstdauer erreicht"
                    break
                self.listener.wait(min(next_check, deadline) - now)
                for payload in self.listener.take():
                    yield self._emit(_event("message", payload))
                if time.monotonic() >= next_check:
                    if not self._alive():
                        break
                    yield self._emit(b": keepalive\n\n")
                    next_check = time.monotonic() + keepalive
        except (DatabaseError, psycopg.Error) as exc:
            # LISTEN-Verbindung bzw. Datenbank weg: Stream beenden, Client verbindet neu.
            self.reason = f"Datenbankfehler ({type(exc).__name__})"
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.listener.close()
        _release_slot()
        # Audit vor dem Löschen der Sitzung: Ist die Sitzung weg, steht der Eintrag.
        self.call.record(ApiCall.Status.OK, 200, self.sent)
        try:
            SseSession.objects.filter(pk=self.session_id).delete()
        except DatabaseError as exc:
            logger.warning("SSE-Sitzung nicht gelöscht: %s", type(exc).__name__)
        logger.info("MCP-SSE-Stream (Key %s) beendet: %s", self.key_pk, self.reason)


def _unavailable(call: _Call, retry_after: int) -> HttpResponse:
    call.record(ApiCall.Status.RATE_LIMITED, 503)
    return _plain(503, Retry_After=str(retry_after))


@csrf_exempt
def sse_endpoint(request):
    """``GET /mcp/sse``: Event-Stream öffnen (Legacy-Transport)."""
    call = _Call(request)
    call.method = STREAM_METHOD
    if not _origin_ok(request.headers.get("Origin")):
        return _json(_rpc_error(None, protocol.INVALID_REQUEST, "Forbidden origin"), 403)
    if request.method != "GET":
        return _plain(405, Allow="GET")
    ctx = authorize(request, call)
    if isinstance(ctx, HttpResponse):
        return ctx
    if not _take_slot():
        return _unavailable(call, 30)
    session = None
    try:
        session = open_session(ctx.key)
        if session is None:
            _release_slot()
            return _unavailable(call, 60)
        listener = Listener(session.pk)
    except Exception:
        _release_slot()
        if session is not None:
            SseSession.objects.filter(pk=session.pk).delete()
        raise
    endpoint = f"{reverse('mcp_messages')}?session_id={session.pk}"
    stream = Stream(call, session, listener, endpoint)
    logger.info("MCP-SSE-Stream (Key %s) geöffnet", ctx.key.pk)
    response = StreamingHttpResponse(stream, content_type=SSE_TYPE)
    response["Cache-Control"] = "no-cache, no-transform"
    response["X-Accel-Buffering"] = "no"
    return response


@csrf_exempt
def messages_endpoint(request):
    """``POST /mcp/messages?session_id=…``: Nachricht annehmen (202), Antwort per Stream."""
    call = _Call(request)
    if not _origin_ok(request.headers.get("Origin")):
        return _json(_rpc_error(None, protocol.INVALID_REQUEST, "Forbidden origin"), 403)
    if request.method != "POST":
        return _plain(405, Allow="POST")
    ctx = authorize(request, call)
    if isinstance(ctx, HttpResponse):
        return ctx
    # session_id (Python-SDK) bzw. sessionId (ältere TypeScript-Server).
    session_id = request.GET.get("session_id") or request.GET.get("sessionId") or ""
    fresh = timezone.now() - stale_after()
    if not (
        SESSION_RE.match(session_id)
        and SseSession.objects.filter(pk=session_id, key=ctx.key, last_seen__gte=fresh).exists()
    ):
        # Unbekannt, beendet oder Sitzung eines anderen Keys: keine Auskunft.
        call.record(ApiCall.Status.INVALID, 404)
        return _plain(404)
    body = read_message(request, call)
    if isinstance(body, HttpResponse):
        return body
    if "id" not in body:
        # Benachrichtigung bzw. Antwort des Clients: annehmen, nichts tun.
        call.record(ApiCall.Status.OK, 202)
        return _plain(202)
    request_id = body.get("id")
    params = body.get("params") if isinstance(body.get("params"), dict) else {}
    headers = {name.lower(): value for name, value in request.headers.items()}
    try:
        env = protocol.envelope(body, headers)
    except RpcError as exc:
        payload = _rpc_error(request_id, exc.code, exc.message, exc.data)
        status = ApiCall.Status.INVALID
    else:
        token = protocol.progress_token(params)
        progress = None
        if token is not None:
            steps = iter(range(1, 1_000_000))

            def progress(item):
                deliver(session_id, protocol.progress_notification(token, next(steps), item))

        payload, status, _http_status = dispatch(call, ctx, env, request_id, params, progress)
    size = deliver(session_id, payload)
    call.record(status if size else ApiCall.Status.ERROR, 202, size)
    return _plain(202)
