"""Runner live: Status, Audit je Sitzung, Terminal und Vorschau im Browser (M14-12, 8p, 8x).

Grundsatz: Jeder Zugriff läuft über MultiGPT, nie direkt zum Runner. Alle Seiten
und Endpunkte zeigen nur das angemeldete Konto – es gibt keinen Parameter für
ein anderes, auch Verwalter sehen hier keine fremden Sitzungen, Ausgaben oder
Audit-Einträge (fremde Sitzung = 404). Metadaten aller Runner zeigt nur der
Admin (``admin.py``, ohne Inhalte).

- **Status** (``GET /runner/status/``): JSON für die Seite „Runner“, die alle
  5 s abfragt: Runner, aktive und letzte Sitzungen, Arbeitsstände, Laufzeit.
- **Audit je Sitzung** (``/runner/sitzung/<uuid>/``): gekürzte Argumente,
  Exit-Code, Dauer, Meldung – nie Ausgaben.
- **Terminal** (PTY, 8p): ``GET …/pty/`` ist ein SSE-Strom. Er legt die
  Kennung des PTY an, abonniert sie (``delivery.subscribe``) und schickt erst
  dann ``pty_open`` – so geht keine Ausgabe verloren, und die Antworten gehen an
  den Prozess, der den Strom hält. Eingaben, Größe und Schließen kommen als
  POST aus beliebigen Prozessen und senden ohne Antwortkanal (``reply=False``).
  Ein Strom endet nach ``PTY_IDLE_SECONDS`` ohne Ausgabe und Eingabe bzw. nach
  ``PTY_MAX_SECONDS``; beim Ende geht ``pty_close`` an den Runner.
- **Vorschau** (8p, 8x): ``GET /runner/vorschau/<uuid>/<token>/<pfad>`` holt die
  Datei über den Rückkanal (``preview_request``/``preview_response``) und
  liefert sie mit strenger CSP aus: ``sandbox allow-scripts`` (eigener,
  undurchsichtiger Ursprung – kein Zugriff auf Cookies und Seiten von MultiGPT),
  kein Netz (``connect-src 'none'``), keine Formulare, keine Einbettung außer
  in MultiGPT. Zugriff nur mit dem zufälligen Sitzungs-Token im Pfad (Vergleich
  in konstanter Zeit): Unterressourcen aus dem abgeschotteten Rahmen haben keine
  Cookies. Keine Verzeichnislisten (``/`` → ``index.html``), ``no-store``,
  ``no-referrer`` (das Token gelangt nicht in fremde Logs).
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
import posixpath
import threading
import time
import uuid
from urllib.parse import unquote

from django.contrib.auth.decorators import login_required
from django.http import (
    Http404,
    HttpResponse,
    JsonResponse,
    StreamingHttpResponse,
)
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from multigpt.accounts.permissions import Action, can

from . import delivery, protocol
from .models import Runner, RunnerAudit, RunnerSession, RunnerToken, RunnerWorkspace

logger = logging.getLogger(__name__)

RECENT_SESSIONS = 10
AUDIT_LIMIT = 500
PTY_KEEPALIVE_SECONDS = 15.0
PTY_IDLE_SECONDS = 10 * 60
PTY_MAX_SECONDS = 60 * 60
PTY_PER_PROCESS = 4  # jeder Strom belegt einen gthread-Thread
PTY_DEFAULT_SIZE = (80, 24)
PREVIEW_TIMEOUT = 20.0
PREVIEW_MAX_BYTES = 8 * 1024 * 1024
PREVIEW_MAX_PATH = 1024

PREVIEW_CSP = (
    "sandbox allow-scripts; default-src 'none'; script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; "
    "media-src 'self' data: blob:; connect-src 'none'; form-action 'none'; "
    "frame-ancestors 'self'; base-uri 'none'"
)
# Kopfzeilen aus der Antwort des Agents, die durchgereicht werden; alle anderen
# (Set-Cookie, CSP, CORS …) fallen weg.
PREVIEW_PASS_HEADERS = {"content-type", "last-modified", "etag"}
PREVIEW_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".woff2": "font/woff2",
    ".wasm": "application/wasm",
}

MSG_NOT_RUNNING = "Die Sitzung läuft nicht."
MSG_OFFLINE = delivery.OFFLINE_MESSAGE
MSG_BUSY = "Zu viele offene Terminals. Bitte ein anderes schließen."
MSG_NO_RIGHT = "Der Runner ist für dieses Konto nicht freigegeben."


def _no_store(response):
    response["Cache-Control"] = "no-store"
    return response


def _json_error(message: str, status: int):
    return _no_store(JsonResponse({"error": message}, status=status))


def _own_session(request, session_uuid) -> RunnerSession:
    """Sitzung des angemeldeten Kontos; fremde und unbekannte gleichermaßen 404."""
    session = (
        RunnerSession.objects.select_related("workspace", "runner", "conversation")
        .filter(uuid=session_uuid, owner=request.user)
        .first()
    )
    if session is None:
        raise Http404
    return session


def _iso(value):
    return value.isoformat() if value else None


# --- Status (Polling) ----------------------------------------------------------------


def status_payload(user, now=None) -> dict:
    """Live-Stand des Kontos – nur Metadaten, keine Inhalte."""
    from multigpt.billing import runtime

    now = now or timezone.now()
    stale = now - delivery.stale_after()
    runners = []
    for runner in Runner.objects.filter(token__owner=user).select_related("token"):
        online = runner.is_online and runner.last_seen is not None and runner.last_seen >= stale
        runners.append(
            {
                "id": runner.pk,
                "name": runner.name or runner.token.name,
                "token": runner.token.name,
                "online": online,
                "status": "verbunden" if online else "getrennt",
                "engine": runner.get_engine_display(),
                "engine_version": runner.engine_version,
                "agent_version": runner.agent_version,
                "rootless": runner.rootless,
                "last_seen": _iso(runner.last_seen),
                "revoked": runner.token.revoked,
            }
        )
    sessions = []
    qs = (
        RunnerSession.objects.filter(owner=user)
        .select_related("workspace", "conversation")
        .order_by("-started", "-pk")
    )
    active = list(qs.filter(status__in=RunnerSession.ACTIVE_STATES))
    recent = list(qs.exclude(status__in=RunnerSession.ACTIVE_STATES)[:RECENT_SESSIONS])
    for session in active + recent:
        conversation = session.conversation
        sessions.append(
            {
                "uuid": str(session.uuid),
                "status": session.status,
                "status_label": session.get_status_display(),
                "active": session.is_active,
                "workspace": session.workspace.name if session.workspace else "gelöscht",
                "conversation": (
                    {
                        "title": conversation.title or "Chat",
                        "url": reverse("chat:conversation", args=[conversation.pk]),
                    }
                    if conversation is not None and conversation.user_id == user.pk
                    else None
                ),
                "via_api_key": session.api_key_id is not None,
                "network": session.network,
                "started": _iso(session.started),
                "ended": _iso(session.ended),
                "minutes": session.minutes,
                "stop_reason": session.stop_reason,
                "url": reverse("runner:session", args=[session.uuid]),
            }
        )
    workspaces = [
        {
            "id": ws.pk,
            "name": ws.name,
            "size": ws.size,
            "head": ws.head_commit[:12],
            "updated": _iso(ws.updated),
            "locked": ws.locked,
            "url": reverse("runner:workspace", args=[ws.pk]),
        }
        for ws in RunnerWorkspace.objects.filter(owner=user).order_by("name")
    ]
    return {
        "now": _iso(now),
        "runners": runners,
        "sessions": sessions,
        "workspaces": workspaces,
        "runtime": runtime.runtime_state(user, now).as_dict(),
        "poll_seconds": 5,
    }


@require_GET
@login_required
def status_api(request):
    return _no_store(JsonResponse(status_payload(request.user)))


# --- Audit je Sitzung --------------------------------------------------------------------


@require_GET
@login_required
def session_page(request, session_uuid):
    session = _own_session(request, session_uuid)
    entries = RunnerAudit.objects.filter(session=session, owner=request.user).order_by(
        "created", "pk"
    )[:AUDIT_LIMIT]
    preview_url = preview_url_for(session)
    return _no_store(
        render(
            request,
            "runner/session.html",
            {
                "session": session,
                "entries": entries,
                "preview_url": preview_url,
                "terminal_allowed": session.status == RunnerSession.Status.RUNNING
                and can(request.user, Action.USE_RUNNER),
            },
        )
    )


@require_GET
@login_required
def session_api(request, session_uuid):
    """Kurzstand einer eigenen Sitzung (für Chat: Terminal/Vorschau anbieten)."""
    session = _own_session(request, session_uuid)
    return _no_store(
        JsonResponse(
            {
                "uuid": str(session.uuid),
                "status": session.status,
                "status_label": session.get_status_display(),
                "running": session.status == RunnerSession.Status.RUNNING,
                "preview_url": preview_url_for(session),
                "pty_url": reverse("runner:pty_stream", args=[session.uuid]),
                "audit_url": reverse("runner:session", args=[session.uuid]),
                "minutes": session.minutes,
            }
        )
    )


# --- Terminal (PTY) --------------------------------------------------------------------


_pty_lock = threading.Lock()
_pty_open = 0


def _acquire_pty_slot() -> bool:
    global _pty_open
    with _pty_lock:
        if _pty_open >= PTY_PER_PROCESS:
            return False
        _pty_open += 1
        return True


def _release_pty_slot() -> None:
    global _pty_open
    with _pty_lock:
        _pty_open = max(_pty_open - 1, 0)


def _usable_session(request, session_uuid) -> RunnerSession:
    session = _own_session(request, session_uuid)
    if not can(request.user, Action.USE_RUNNER):
        raise PermissionError(MSG_NO_RIGHT)
    if session.status != RunnerSession.Status.RUNNING or session.runner_id is None:
        raise LookupError(MSG_NOT_RUNNING)
    return session


def _size(request, default=PTY_DEFAULT_SIZE) -> tuple[int, int]:
    def number(name, fallback, low, high):
        try:
            return min(max(int(request.GET.get(name) or fallback), low), high)
        except (TypeError, ValueError):
            return fallback

    return number("cols", default[0], 10, 500), number("rows", default[1], 4, 200)


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


@require_GET
@login_required
def pty_stream(request, session_uuid):
    """Terminal öffnen: SSE ``open`` {pty}, ``data`` {data b64}, ``exit``, ``end``, ``failure``."""
    try:
        session = _usable_session(request, session_uuid)
    except PermissionError as exc:
        return _json_error(str(exc), 403)
    except LookupError as exc:
        return _json_error(str(exc), 409)
    if not delivery.runner_online(session.runner_id):
        return _json_error(MSG_OFFLINE, 503)
    if not _acquire_pty_slot():
        response = _json_error(MSG_BUSY, 503)
        response["Retry-After"] = "10"
        return response
    cols, rows = _size(request)
    pty = uuid.uuid4().hex
    sub = delivery.subscribe("pty", pty)
    try:
        delivery.send(
            session.runner_id,
            {
                "type": "pty_open",
                "session": str(session.uuid),
                "pty": pty,
                "cols": cols,
                "rows": rows,
            },
            session=session,
        )
    except delivery.RunnerError as exc:
        sub.close()
        _release_pty_slot()
        return _json_error(exc.message, 503)
    except Exception:
        sub.close()
        _release_pty_slot()
        raise
    _remember_pty(pty, session.pk)
    stream = _PtyStream(session, pty, sub)
    response = StreamingHttpResponse(stream, content_type="text/event-stream; charset=utf-8")
    response["Cache-Control"] = "no-store"
    response["X-Accel-Buffering"] = "no"
    return response


class _PtyStream:
    """Iterator des SSE-Stroms; schließt PTY und Abonnement auch beim Abbruch."""

    def __init__(self, session: RunnerSession, pty: str, sub):
        self.session = session
        self.pty = pty
        self.sub = sub
        self.closed = False

    def __iter__(self):
        started = time.monotonic()
        last_activity = started
        try:
            yield _sse("open", {"pty": self.pty})
            while True:
                now = time.monotonic()
                if now - started > PTY_MAX_SECONDS or now - last_activity > PTY_IDLE_SECONDS:
                    yield _sse("end", {"message": "Terminal wegen Leerlauf geschlossen."})
                    return
                if _pty_touched(self.pty, last_activity):
                    last_activity = time.monotonic()
                try:
                    frame = self.sub.get(PTY_KEEPALIVE_SECONDS)
                except delivery.RunnerOffline as exc:
                    yield _sse("failure", {"message": exc.message})
                    return
                except delivery.RunnerTimeout:
                    if not self._still_running():
                        yield _sse("end", {"message": MSG_NOT_RUNNING})
                        return
                    yield b": keepalive\n\n"
                    continue
                kind = frame.get("type")
                if kind == "pty_data":
                    last_activity = time.monotonic()
                    yield _sse("data", {"data": frame.get("data", "")})
                elif kind == "pty_exit":
                    yield _sse("exit", {"code": frame.get("code")})
                    self.closed = True  # der Runner hat es schon beendet
                    return
                elif kind == "error":
                    yield _sse("failure", {"message": frame.get("message") or "Fehler im Runner"})
                    return
        finally:
            self.close()

    def _still_running(self) -> bool:
        return RunnerSession.objects.filter(
            pk=self.session.pk, status=RunnerSession.Status.RUNNING
        ).exists()

    def close(self):
        if getattr(self, "_done", False):
            return
        self._done = True
        try:
            if not self.closed:
                delivery.send(
                    self.session.runner_id,
                    {"type": "pty_close", "session": str(self.session.uuid), "pty": self.pty},
                    session=self.session,
                    reply=False,
                )
        except Exception:  # Runner getrennt: nichts mehr zu schließen
            pass
        finally:
            self.sub.close()
            _forget_pty(self.pty)
            _release_pty_slot()


# Eingaben kommen in beliebigen Prozessen an; die letzte Aktivität je PTY hält der
# Strom nur lokal. Über Prozesse hinweg zählt deshalb auch Ausgabe als Aktivität
# (jede Eingabe erzeugt in der Regel ein Echo).
_pty_activity: dict[str, float] = {}
_pty_sessions: dict[str, int] = {}


def _remember_pty(pty: str, session_pk: int) -> None:
    _pty_sessions[pty] = session_pk


def _forget_pty(pty: str) -> None:
    _pty_sessions.pop(pty, None)
    _pty_activity.pop(pty, None)


def _pty_touched(pty: str, since: float) -> bool:
    return _pty_activity.get(pty, 0) > since


def _pty_body(request) -> dict:
    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _pty_id(value: str) -> str:
    if not protocol.ID_RE.match(value or ""):
        raise Http404
    return value


def _pty_send(request, session_uuid, pty, frame_type: str, **fields):
    pty = _pty_id(pty)
    try:
        session = _usable_session(request, session_uuid)
    except PermissionError as exc:
        return _json_error(str(exc), 403)
    except LookupError as exc:
        return _json_error(str(exc), 409)
    frame = {"type": frame_type, "session": str(session.uuid), "pty": pty, **fields}
    try:
        delivery.send(session.runner_id, frame, session=session, reply=False)
    except delivery.RunnerError as exc:
        return _json_error(exc.message, 503)
    except protocol.ProtocolError as exc:
        return _json_error(exc.message, 400)
    _pty_activity[pty] = time.monotonic()
    return _no_store(JsonResponse({"ok": True}))


@require_POST
@login_required
def pty_input(request, session_uuid, pty):
    """Eingabe ins Terminal: JSON {data: Text} (UTF-8, höchstens 64 KiB)."""
    text = _pty_body(request).get("data")
    if not isinstance(text, str) or not text:
        return _json_error("Keine Eingabe.", 400)
    raw = text.encode("utf-8")
    if len(raw) > protocol.PTY_BYTES:
        return _json_error("Eingabe zu lang.", 400)
    return _pty_send(
        request, session_uuid, pty, "pty_input", data=base64.b64encode(raw).decode("ascii")
    )


@require_POST
@login_required
def pty_resize(request, session_uuid, pty):
    body = _pty_body(request)
    try:
        cols = min(max(int(body.get("cols")), 10), 500)
        rows = min(max(int(body.get("rows")), 4), 200)
    except (TypeError, ValueError):
        return _json_error("Ungültige Größe.", 400)
    return _pty_send(request, session_uuid, pty, "pty_resize", cols=cols, rows=rows)


@require_POST
@login_required
def pty_close(request, session_uuid, pty):
    return _pty_send(request, session_uuid, pty, "pty_close")


# --- Vorschau ---------------------------------------------------------------------------


def preview_url_for(session: RunnerSession, path: str = "") -> str | None:
    """Pfad der Vorschau (mit Token) oder None, wenn keine Vorschau läuft."""
    if session.status != RunnerSession.Status.RUNNING or not session.preview_token:
        return None
    if session.preview_port is None:  # Agent meldet keinen laufenden Vorschau-Server
        return None
    if not path:
        return reverse("runner:preview_root", args=[session.uuid, session.preview_token])
    return reverse("runner:preview", args=[session.uuid, session.preview_token, path])


def clean_preview_path(raw: str) -> str | None:
    """Pfad normalisieren; None bei Ausbruchsversuch oder Unzulässigem.

    Ergebnis beginnt mit ``/``; ein Verzeichnis (leer oder ``/`` am Ende) wird
    zu ``index.html`` – Verzeichnislisten gibt es nicht.
    """
    path = unquote(raw or "")
    if len(path) > PREVIEW_MAX_PATH or "\x00" in path or "\\" in path:
        return None
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if any(p == ".." or p.startswith(".") for p in parts):
        return None  # kein ../ und keine versteckten Dateien (.git)
    if any(ord(c) < 32 for c in path):
        return None
    if not parts or path.endswith("/"):
        parts.append("index.html")
    return "/" + posixpath.join(*parts)


def _preview_error(status: int, text: str) -> HttpResponse:
    response = HttpResponse(text, status=status, content_type="text/plain; charset=utf-8")
    return _preview_headers(response)


def _preview_headers(response: HttpResponse) -> HttpResponse:
    response["Content-Security-Policy"] = PREVIEW_CSP
    response["X-Content-Type-Options"] = "nosniff"
    response["Referrer-Policy"] = "no-referrer"
    response["Cache-Control"] = "no-store"
    response["Cross-Origin-Resource-Policy"] = "same-origin"
    response["X-Frame-Options"] = "SAMEORIGIN"
    return response


def _preview_session(session_uuid, token: str) -> RunnerSession | None:
    session = (
        RunnerSession.objects.select_related("owner", "owner__role")
        .filter(uuid=session_uuid, status=RunnerSession.Status.RUNNING)
        .exclude(preview_token="")
        .first()
    )
    if session is None:
        # Gleiche Arbeit wie beim Vergleich: keine Auskunft, ob die Sitzung existiert.
        hmac.compare_digest(token.encode(), b"x" * len(token.encode()))
        return None
    if not hmac.compare_digest(session.preview_token.encode(), token.encode()):
        return None
    return session


@require_GET
def preview(request, session_uuid, token, path=""):
    """Datei der Vorschau über den Rückkanal (Zugriff nur über das Token im Pfad)."""
    session = _preview_session(session_uuid, token or "")
    if session is None or session.runner_id is None:
        return _preview_error(404, "Keine Vorschau.")
    owner_ok = RunnerToken.objects.filter(
        owner_id=session.owner_id, runner__pk=session.runner_id, revoked_at__isnull=True
    ).exists()
    if not owner_ok or not can(session.owner, Action.USE_RUNNER):
        return _preview_error(404, "Keine Vorschau.")
    clean = clean_preview_path(path)
    if clean is None:
        return _preview_error(404, "Nicht gefunden.")
    request_id = uuid.uuid4().hex
    frame = {
        "type": "preview_request",
        "session": str(session.uuid),
        "request": request_id,
        "method": request.method if request.method in ("GET", "HEAD") else "GET",
        "path": clean,
    }
    chunks: list[bytes] = []
    status = 502
    headers: dict = {}
    total = 0
    deadline = time.monotonic() + PREVIEW_TIMEOUT
    try:
        with delivery.subscribe("request", request_id) as sub:
            delivery.send(session.runner_id, frame, session=session)
            while True:
                answer = sub.get(max(0.0, deadline - time.monotonic()))
                if answer.get("type") == "error":
                    return _preview_error(502, answer.get("message") or "Fehler im Runner.")
                if answer.get("type") != "preview_response":
                    continue
                if answer.get("seq") == 0:
                    status = int(answer.get("status") or 502)
                    headers = answer.get("headers") or {}
                data = answer.get("data")
                if data:
                    try:
                        piece = base64.b64decode(data, validate=True)
                    except (binascii.Error, ValueError):
                        return _preview_error(502, "Ungültige Antwort des Runners.")
                    total += len(piece)
                    if total > PREVIEW_MAX_BYTES:
                        return _preview_error(502, "Datei zu groß für die Vorschau.")
                    chunks.append(piece)
                if answer.get("last"):
                    break
    except delivery.RunnerOffline:
        return _preview_error(503, MSG_OFFLINE)
    except delivery.RunnerTimeout:
        return _preview_error(504, "Der Runner hat nicht rechtzeitig geantwortet.")
    except delivery.RunnerError as exc:
        return _preview_error(502, exc.message)
    content_type = _preview_type(clean, headers)
    response = HttpResponse(b"".join(chunks), status=status, content_type=content_type)
    for name, value in (headers or {}).items():
        if str(name).lower() in PREVIEW_PASS_HEADERS - {"content-type"}:
            response[str(name)] = str(value)
    return _preview_headers(response)


def _preview_type(path: str, headers: dict) -> str:
    ext = posixpath.splitext(path)[1].lower()
    if ext in PREVIEW_TYPES:
        return PREVIEW_TYPES[ext]
    for name, value in (headers or {}).items():
        if str(name).lower() == "content-type" and str(value).startswith(
            ("text/plain", "image/", "font/", "audio/", "video/")
        ):
            return str(value)
    return "application/octet-stream"
