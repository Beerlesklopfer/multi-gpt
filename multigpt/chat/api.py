"""JSON- und SSE-Endpunkte des Chats (M3, Vertrag m3-contract).

Fehler vor dem Stream kommen immer als JSON ``{"error": "<Text>"}``.
Fremde Chats (kein READ) -> 404, damit ihre Existenz nicht sichtbar wird;
lesbar, aber ohne Schreibrecht -> 403.
"""

import json
from functools import wraps

from django.http import JsonResponse, StreamingHttpResponse
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from multigpt.accounts.permissions import Action, can

from . import services, status, tooling
from .models import AIModel, Conversation, Message, ToolCall


def _error(message: str, status: int) -> JsonResponse:
    return JsonResponse({"error": message}, status=status)


def api_login_required(view):
    """Wie login_required, aber mit JSON-403 statt Weiterleitung."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated or not request.user.is_active:
            return _error("Nicht angemeldet.", 403)
        return view(request, *args, **kwargs)

    return wrapper


def _json_body(request) -> dict | None:
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _get_conversation(request, pk: int) -> Conversation | None:
    conversation = Conversation.objects.filter(pk=pk).first()
    if conversation is None or not can(request.user, Action.READ, conversation):
        return None
    return conversation


def _get_chat_model(user, raw) -> tuple[AIModel | None, JsonResponse | None]:
    """Modell aus der Anfrage laden und prüfen: (Modell, None) oder (None, Fehler)."""
    try:
        pk = int(raw)
    except (TypeError, ValueError):
        return None, _error("Bitte ein Modell wählen.", 400)
    ai_model = AIModel.objects.select_related("provider").filter(pk=pk).first()
    if ai_model is None or ai_model.capability != AIModel.Capability.CHAT:
        return None, _error("Unbekanntes Modell.", 400)
    if not can(user, Action.USE_MODEL, ai_model):
        return None, _error("Dieses Modell steht dir nicht zur Verfügung.", 403)
    return ai_model, None


def _serialize_tool_call(tool_call: ToolCall) -> dict:
    return {
        "id": tool_call.pk,
        "server": tool_call.server.name if tool_call.server_id else "",
        "tool": tool_call.tool,
        "arguments": tool_call.arguments,
        "status": tool_call.status,
        "result": tool_call.result_text[: tooling.EVENT_RESULT_CHARS],
        "duration_ms": tool_call.duration_ms,
        "attachment_ids": [a.pk for a in tool_call.attachments.all()],
    }


def _serialize_message(message: Message) -> dict:
    return {
        "id": message.pk,
        "role": message.role,
        "content": message.content,
        "model": message.model.display_name if message.model else None,
        "model_id": message.model_id,
        "status": message.status,
        "error": message.error,
        "created": message.created.isoformat(),
        "tool_calls": [_serialize_tool_call(tc) for tc in message.tool_calls.all()],
    }


# --- Endpunkte --------------------------------------------------------------------


def _serialize_model(m: AIModel) -> dict:
    online, available = status.model_state(m)
    return {
        "id": m.pk,
        "display_name": m.display_name,
        "provider": m.provider.name,
        "provider_id": m.provider_id,
        "is_local": m.provider.is_local,
        "supports_tools": m.supports_tools,
        # Nach dem letzten gespeicherten Status (M4-03); nicht-lokale immer True.
        "online": online,
        "available": available,
    }


@require_GET
@api_login_required
def models_list(request):
    return JsonResponse(
        [_serialize_model(m) for m in services.available_chat_models(request.user)],
        safe=False,
    )


@require_GET
@api_login_required
def mcp_servers_list(request):
    """Aktive MCP-Server, die das Konto nutzen darf. Voreinstellung nach Rolle
    (Plan 8g): Alle erlaubten Server sind eingeschaltet."""
    return JsonResponse(
        [
            {"id": s.pk, "name": s.name, "default_enabled": True}
            for s in tooling.available_servers(request.user)
        ],
        safe=False,
    )


@require_GET
@api_login_required
def provider_status(request):
    """Status aller aktiven Anbieter mit Statusprüfung (Plan 8a, 15 s Cache in der DB)."""
    return JsonResponse([status.serialize(p) for p in status.refresh_all()], safe=False)


def _check_model_reachable(ai_model: AIModel) -> JsonResponse | None:
    """Lokales Modell: Anbieter online und Modell gemeldet? Sonst Fehler (M4-05)."""
    provider = ai_model.provider
    if not (provider.is_local and provider.check_status):
        return None
    status.refresh(provider)  # nur bei Status älter als 15 s ein Netzaufruf
    online, available = status.model_state(ai_model)
    if not online:
        return _error(
            f"{provider.name} ist offline. Bitte ein anderes Modell wählen "
            "oder es später erneut versuchen.",
            503,
        )
    if not available:
        return _error(
            f"Das Modell „{ai_model.display_name}“ wird von {provider.name} "
            "derzeit nicht angeboten.",
            409,
        )
    return None


@require_POST
@api_login_required
def conversation_create(request):
    if not can(request.user, Action.CHAT):
        return _error("Chatten ist für dieses Konto nicht freigegeben.", 403)
    data = _json_body(request)
    if data is None:
        return _error("Ungültige Anfrage.", 400)
    default_model = None
    if data.get("default_model") is not None:
        default_model, err = _get_chat_model(request.user, data["default_model"])
        if err:
            return err
    conversation = Conversation.objects.create(user=request.user, default_model=default_model)
    return JsonResponse(
        {
            "id": conversation.pk,
            "title": conversation.title,
            "url": reverse("chat:conversation", args=[conversation.pk]),
        },
        status=201,
    )


@require_http_methods(["GET", "POST"])
@api_login_required
def messages(request, pk: int):
    conversation = _get_conversation(request, pk)
    if conversation is None:
        return _error("Chat nicht gefunden.", 404)
    if request.method == "GET":
        qs = services.visible_messages(conversation)
        return JsonResponse([_serialize_message(m) for m in qs], safe=False)
    return _stream(request, conversation)


def _stream(request, conversation: Conversation):
    user = request.user
    if not can(user, Action.WRITE, conversation):
        return _error("Du darfst in diesem Chat nicht schreiben.", 403)
    if not can(user, Action.CHAT):
        return _error("Chatten ist für dieses Konto nicht freigegeben.", 403)
    data = _json_body(request)
    if data is None:
        return _error("Ungültige Anfrage.", 400)
    raw_model = data.get("model", conversation.default_model_id)
    ai_model, err = _get_chat_model(user, raw_model)
    if err:
        return err
    err = _check_model_reachable(ai_model)
    if err:
        return err
    regenerate = data.get("regenerate") is True
    content = data.get("content")
    if not regenerate and not isinstance(content, str):
        return _error("Die Nachricht ist leer.", 400)
    mcp_servers = data.get("mcp_servers")
    if mcp_servers is not None and not (
        isinstance(mcp_servers, list)
        and all(isinstance(x, int) and not isinstance(x, bool) for x in mcp_servers)
    ):
        return _error("Ungültige Auswahl der MCP-Server.", 400)
    try:
        turn = services.prepare_turn(
            user,
            conversation,
            ai_model,
            content=content,
            regenerate=regenerate,
            mcp_servers=mcp_servers,
        )
    except services.TurnError as exc:
        return _error(exc.message, exc.status)
    return _event_stream(services.run_turn(turn))


def _event_stream(events) -> StreamingHttpResponse:
    response = StreamingHttpResponse(_sse(events), content_type="text/event-stream; charset=utf-8")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


@require_POST
@api_login_required
def tool_confirm(request, pk: int):
    """Rückfrage beantworten und die Werkzeugschleife fortsetzen (M4a-05).

    JSON ``{"decisions": {"<ToolCall.pk>": "approve"|"reject"}}`` für genau die
    wartenden Aufrufe; Antwort als SSE wie bei ``messages``.
    """
    conversation = _get_conversation(request, pk)
    if conversation is None:
        return _error("Chat nicht gefunden.", 404)
    user = request.user
    if not can(user, Action.WRITE, conversation):
        return _error("Du darfst in diesem Chat nicht schreiben.", 403)
    if not can(user, Action.CHAT):
        return _error("Chatten ist für dieses Konto nicht freigegeben.", 403)
    data = _json_body(request)
    if data is None:
        return _error("Ungültige Anfrage.", 400)
    message = services.pending_message(conversation)
    if message is None:
        return _error("In diesem Chat wartet keine Rückfrage.", 409)
    ai_model = message.model
    if ai_model is None:
        return _error("Das Modell dieser Antwort gibt es nicht mehr.", 409)
    if not can(user, Action.USE_MODEL, ai_model):
        return _error("Dieses Modell steht dir nicht zur Verfügung.", 403)
    err = _check_model_reachable(ai_model)
    if err:
        return err
    try:
        turn = services.prepare_resume(user, conversation, message, data.get("decisions"))
    except services.TurnError as exc:
        return _error(exc.message, exc.status)
    return _event_stream(services.run_turn(turn))


def _sse(events):
    """(Name, Daten) -> SSE-Bytes. Schließen reicht der Server an ``events`` weiter."""
    try:
        for name, data in events:
            payload = json.dumps(data, ensure_ascii=False)
            yield f"event: {name}\ndata: {payload}\n\n".encode()
    finally:
        events.close()
