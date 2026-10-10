"""MCP-Endpunkt ``/mcp/`` (M15).

Ablauf je Anfrage (zustandslos, jede Prüfung frisch aus der Datenbank):

1. ``Origin`` prüfen (Schutz gegen DNS-Rebinding): nur Hosts aus ``ALLOWED_HOSTS``.
2. Nur POST (GET/DELETE: 405, kein Server→Client-Stream, keine Sitzungen).
3. Drosselung je IP nach Fehlversuchen (429), dann ``Authorization: Bearer``:
   ohne bzw. mit ungültigem, abgelaufenem oder widerrufenem Key 401 ohne
   Details. Cookies und Sitzung werden auf ``/mcp/`` nie ausgewertet, deshalb
   gilt CSRF hier nicht.
4. Drosselung je Key (429), Größengrenze (413), JSON-RPC-Dispatch.
5. Audit-Log (``ApiCall``) mit Methode, Werkzeug, Status, Dauer und Größen –
   ohne Inhalte. Logs nur mit IDs.
"""

from __future__ import annotations

import json
import logging
import math
import time

from django.conf import settings
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.http.request import split_domain_port, validate_host
from django.views.decorators.csrf import csrf_exempt

from multigpt.accounts.client_ip import client_ip

from . import audit, keys, protocol
from .models import ApiCall
from .protocol import RpcError
from .tools import CallContext, Progress

logger = logging.getLogger(__name__)

SSE_TYPE = "text/event-stream"


def max_request_bytes() -> int:
    """Upload als base64 (4/3 der Datei) plus 1 MB für den Rest der Anfrage."""
    upload = int(getattr(settings, "DOCUMENT_MAX_UPLOAD_MB", 25)) * 1024 * 1024
    return math.ceil(upload * 4 / 3) + 1024 * 1024


def _origin_ok(origin: str | None) -> bool:
    if not origin:
        return True
    host = origin.split("://", 1)[-1].split("/", 1)[0]
    domain, _port = split_domain_port(host)
    allowed = list(settings.ALLOWED_HOSTS)
    if settings.DEBUG and not allowed:
        allowed = [".localhost", "127.0.0.1", "[::1]"]
    return bool(domain) and validate_host(domain, allowed)


def _rpc_error(request_id, code: int, message: str, data=None) -> dict:
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _json(payload: dict, status: int = 200) -> JsonResponse:
    response = JsonResponse(payload, status=status, json_dumps_params={"ensure_ascii": False})
    response["Cache-Control"] = "no-store"
    return response


def _plain(status: int, **headers) -> HttpResponse:
    response = HttpResponse(status=status)
    for name, value in headers.items():
        response[name.replace("_", "-")] = value
    response["Cache-Control"] = "no-store"
    return response


class _Call:
    """Sammelt Audit-Daten einer Anfrage."""

    def __init__(self, request):
        self.started = time.monotonic()
        self.ip = client_ip(request)
        self.key = None
        self.method = ""
        self.tool = ""
        try:
            self.request_bytes = int(request.META.get("CONTENT_LENGTH") or 0)
        except ValueError:
            self.request_bytes = 0

    def record(self, status: str, http_status: int, response_bytes: int = 0) -> None:
        duration = int((time.monotonic() - self.started) * 1000)
        audit.record(
            key=self.key,
            method=self.method,
            tool=self.tool,
            status=status,
            http_status=http_status,
            duration_ms=duration,
            request_bytes=self.request_bytes,
            response_bytes=response_bytes,
            ip=self.ip,
        )
        if self.key is not None:
            logger.info(
                "MCP Key %s: %s %s -> %s (%d ms)",
                self.key.pk,
                self.method or "-",
                self.tool or "-",
                status,
                duration,
            )

    def reply(self, payload: dict, status: str, http_status: int = 200) -> JsonResponse:
        response = _json(payload, http_status)
        self.record(status, http_status, len(response.content))
        return response


@csrf_exempt
def mcp_endpoint(request):
    call = _Call(request)
    if not _origin_ok(request.headers.get("Origin")):
        return _json(_rpc_error(None, protocol.INVALID_REQUEST, "Forbidden origin"), 403)
    if request.method != "POST":
        return _plain(405, Allow="POST")

    if audit.ip_blocked(call.ip):
        call.record(ApiCall.Status.RATE_LIMITED, 429)
        return _plain(429, Retry_After=str(int(audit.failure_window().total_seconds())))
    key = keys.authenticate(keys.bearer_token(request.headers.get("Authorization")))
    if key is None:
        call.record(ApiCall.Status.UNAUTHORIZED, 401)
        return _plain(401, WWW_Authenticate='Bearer realm="MultiGPT"')
    call.key = key
    if audit.key_limited(key):
        call.record(ApiCall.Status.RATE_LIMITED, 429)
        return _plain(429, Retry_After="60")
    keys.touch(key, call.ip)
    ctx = CallContext(key=key, user=key.owner, scopes=keys.effective_scopes(key), ip=call.ip)
    if not ctx.scopes:
        # Rolle hat das Recht verloren o. Ä.: wie ein ungültiger Key, ohne Details.
        call.record(ApiCall.Status.DENIED, 403)
        return _plain(403)

    limit = max_request_bytes()
    if call.request_bytes > limit:
        call.record(ApiCall.Status.INVALID, 413)
        return _plain(413)
    raw = request.read(limit + 1)
    call.request_bytes = len(raw)
    if len(raw) > limit:
        call.record(ApiCall.Status.INVALID, 413)
        return _plain(413)
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        return call.reply(
            _rpc_error(None, protocol.PARSE_ERROR, "Parse error"), ApiCall.Status.INVALID, 400
        )
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        message = "Body must be a single JSON-RPC request or notification object"
        return call.reply(
            _rpc_error(None, protocol.INVALID_REQUEST, message), ApiCall.Status.INVALID, 400
        )
    method = body.get("method")
    call.method = method if isinstance(method, str) else ""
    if "id" not in body:
        # Benachrichtigung (z. B. notifications/initialized): annehmen, nichts tun.
        call.record(ApiCall.Status.OK, 202)
        return _plain(202)
    request_id = body.get("id")
    if not isinstance(method, str) or not isinstance(request_id, str | int):
        return call.reply(
            _rpc_error(None, protocol.INVALID_REQUEST, "Invalid request"),
            ApiCall.Status.INVALID,
            400,
        )
    headers = {name.lower(): value for name, value in request.headers.items()}
    try:
        env = protocol.envelope(body, headers)
    except RpcError as exc:
        return call.reply(
            _rpc_error(request_id, exc.code, exc.message, exc.data),
            ApiCall.Status.INVALID,
            exc.http_status,
        )
    params = body.get("params") if isinstance(body.get("params"), dict) else {}
    try:
        if method == "tools/call":
            return _tools_call(request, call, ctx, env, request_id, params)
        if method == "initialize" and not env.modern:
            result = protocol.initialize_result(params)
        elif method == "server/discover":
            result = protocol.discover_result()
        elif method == "ping":
            result = protocol.ping_result(env)
        elif method == "tools/list":
            result = protocol.list_tools_result(ctx)
        else:
            raise RpcError(protocol.METHOD_NOT_FOUND, "Method not found")
    except RpcError as exc:
        status = (
            ApiCall.Status.DENIED
            if exc.code == protocol.INVALID_PARAMS
            else (ApiCall.Status.INVALID)
        )
        return call.reply(
            _rpc_error(request_id, exc.code, exc.message, exc.data),
            status,
            exc.http_status if exc.http_status != 200 else env.status_for(exc.code),
        )
    payload = {"jsonrpc": "2.0", "id": request_id, "result": protocol.finish(result, env)}
    return call.reply(payload, ApiCall.Status.OK)


def _tools_call(request, call: _Call, ctx, env, request_id, params):
    tool, arguments = protocol.resolve_tool(ctx, params)
    call.tool = tool.name
    token = protocol.progress_token(params)
    wants_sse = SSE_TYPE in (request.headers.get("Accept") or "")
    steps = protocol.run_tool(tool, ctx, arguments)
    if token is None or not wants_sse:
        output = _drain(steps)
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": protocol.finish(protocol.call_tool_result(output), env),
        }
        status = ApiCall.Status.TOOL_ERROR if output.is_error else ApiCall.Status.OK
        return call.reply(payload, status)
    response = StreamingHttpResponse(
        _sse(call, steps, env, request_id, token), content_type=SSE_TYPE
    )
    response["Cache-Control"] = "no-cache, no-transform"
    response["X-Accel-Buffering"] = "no"
    return response


def _drain(steps):
    while True:
        try:
            next(steps)
        except StopIteration as stop:
            return stop.value


def _event(payload: dict) -> bytes:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: message\ndata: {data}\n\n".encode()


def _sse(call: _Call, steps, env, request_id, token):
    """SSE-Antwort: Fortschritt, dann das Ergebnis. Schließt der Client die
    Verbindung, wird ``steps`` geschlossen (Abbruch, z. B. der laufenden Antwort)."""
    sent = 0
    step = 0
    status = ApiCall.Status.ERROR
    try:
        while True:
            try:
                item = next(steps)
            except StopIteration as stop:
                output = stop.value
                break
            if isinstance(item, Progress):
                step += 1
                chunk = _event(protocol.progress_notification(token, step, item))
                sent += len(chunk)
                yield chunk
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": protocol.finish(protocol.call_tool_result(output), env),
        }
        chunk = _event(payload)
        sent += len(chunk)
        status = ApiCall.Status.TOOL_ERROR if output.is_error else ApiCall.Status.OK
        yield chunk
    finally:
        steps.close()
        call.record(status, 200, sent)
