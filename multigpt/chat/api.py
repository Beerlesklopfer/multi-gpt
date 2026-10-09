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

from multigpt.accounts import usage
from multigpt.accounts.permissions import Action, can, model_permitted

from . import services, status, tooling, websearch
from . import sources as source_refs
from .models import AIModel, Conversation, Message, ToolCall
from .rag import chat as rag_chat
from .rag import search as rag_search

MAX_COLLECTIONS = 50


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
    err = _model_denied(user, ai_model)
    if err:
        return None, err
    return ai_model, None


def _model_denied(user, ai_model: AIModel) -> JsonResponse | None:
    """403, wenn ``user`` das Modell nicht nutzen darf – mit eigenem Text, wenn
    nur das ausgeschöpfte Monatsbudget sperrt (M6-03)."""
    if can(user, Action.USE_MODEL, ai_model):
        return None
    if model_permitted(user, ai_model):
        return _error(usage.BUDGET_EXHAUSTED_MESSAGE, 403)
    return _error("Dieses Modell steht dir nicht zur Verfügung.", 403)


def _serialize_tool_call(tool_call: ToolCall) -> dict:
    return {
        "id": tool_call.pk,
        "server": tooling.server_label(tool_call),
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
        "parent_id": message.parent_id,
        "sibling_ids": message.sibling_ids,
        "sibling_index": message.sibling_index,
        "sibling_count": message.sibling_count,
        "tool_calls": [_serialize_tool_call(tc) for tc in message.tool_calls.all()],
        "sources": [
            source_refs.serialize(src, n) for n, src in enumerate(message.sources.all(), start=1)
        ],
        "notices": message.notices,
        "web_search_notice": message.web_search_notice,
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
        # Monatsbudget ausgeschöpft und Modell kostenpflichtig (M6-03): ausgrauen.
        "blocked_by_budget": getattr(m, "blocked_by_budget", False),
    }


@require_GET
@api_login_required
def models_list(request):
    return JsonResponse(
        [_serialize_model(m) for m in services.chat_models_for(request.user)],
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
        return _path_response(conversation)
    return _stream(request, conversation)


def _path_response(conversation: Conversation) -> JsonResponse:
    path = services.visible_messages(conversation)
    return JsonResponse([_serialize_message(m) for m in path], safe=False)


def _optional_pk(data: dict, key: str) -> tuple[int | None, bool]:
    """(Wert, gültig): fehlend/None -> (None, True); sonst nur echte Ganzzahlen."""
    value = data.get(key)
    if value is None:
        return None, True
    if isinstance(value, bool) or not isinstance(value, int):
        return None, False
    return value, True


@require_POST
@api_login_required
def branch(request, pk: int):
    """Version umschalten: ``{"message_id": <pk>}`` -> current_leaf = neuestes
    Blatt unter dieser Nachricht; Antwort wie GET messages. Ändert den
    gemeinsamen Zustand des Chats, braucht also Schreibrecht."""
    conversation = _get_conversation(request, pk)
    if conversation is None:
        return _error("Chat nicht gefunden.", 404)
    if not can(request.user, Action.WRITE, conversation):
        return _error("Du darfst in diesem Chat nicht schreiben.", 403)
    data = _json_body(request)
    if data is None:
        return _error("Ungültige Anfrage.", 400)
    message_id, valid = _optional_pk(data, "message_id")
    # adopt_model (Vergleichsmodus): Modell der gewählten Antwort wird Standardmodell.
    adopt_model = data.get("adopt_model", False)
    if not valid or message_id is None or not isinstance(adopt_model, bool):
        return _error("Ungültige Anfrage.", 400)
    try:
        services.switch_branch(conversation, message_id, adopt_model=adopt_model)
    except services.TurnError as exc:
        return _error(exc.message, exc.status)
    return _path_response(conversation)


def _parse_collections(user, raw) -> tuple[list[int], JsonResponse | None]:
    """``collections`` (M7): Sammlungen für die Dokumentsuche dieser Antwort.

    Fehlt/leer -> keine Dokumentsuche. Kein int-Array -> 400; eine nicht
    lesbare oder unbekannte Sammlung -> 404 (Existenz fremder Sammlungen bleibt
    verborgen). Die Suche filtert den Zugriff zusätzlich in SQL.
    """
    if raw is None:
        return [], None
    if not isinstance(raw, list) or not all(
        isinstance(x, int) and not isinstance(x, bool) for x in raw
    ):
        return [], _error("Ungültige Auswahl der Sammlungen.", 400)
    ids = list(dict.fromkeys(raw))
    if not ids:
        return [], None
    if len(ids) > MAX_COLLECTIONS:
        return [], _error("Zu viele Sammlungen gewählt.", 400)
    if rag_search.readable_collections(user).filter(pk__in=ids).count() != len(ids):
        return [], _error("Sammlung nicht gefunden.", 404)
    if not rag_chat.search_ready():
        return [], _error("Die Dokumentsuche ist derzeit nicht eingerichtet.", 409)
    return ids, None


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
    edit_of, valid_edit = _optional_pk(data, "edit_of")
    regenerate_of, valid_regen = _optional_pk(data, "message_id") if regenerate else (None, True)
    if not (valid_edit and valid_regen) or (regenerate and edit_of is not None):
        return _error("Ungültige Anfrage.", 400)
    mcp_servers = data.get("mcp_servers")
    if mcp_servers is not None and not (
        isinstance(mcp_servers, list)
        and all(isinstance(x, int) and not isinstance(x, bool) for x in mcp_servers)
    ):
        return _error("Ungültige Auswahl der MCP-Server.", 400)
    web_search = data.get("web_search") if data.get("web_search") is not None else False
    if not isinstance(web_search, bool):
        return _error("Ungültige Anfrage.", 400)
    if web_search:
        if not can(user, Action.WEB_SEARCH):
            return _error("Die Websuche ist für dieses Konto nicht freigegeben.", 403)
        if not websearch.get_settings().is_ready:
            return _error("Die Websuche ist derzeit nicht eingerichtet.", 409)
    collections, err = _parse_collections(user, data.get("collections"))
    if err:
        return err
    # Vergleichsmodus (M6): Spalten ohne MCP-Werkzeuge; weitere Spalten
    # (regenerate) lassen den angezeigten Zweig stehen, bis der Nutzer wählt.
    compare = data.get("compare") if data.get("compare") is not None else False
    if not isinstance(compare, bool):
        return _error("Ungültige Anfrage.", 400)
    try:
        turn = services.prepare_turn(
            user,
            conversation,
            ai_model,
            content=content,
            regenerate=regenerate,
            mcp_servers=mcp_servers,
            edit_of=edit_of,
            regenerate_of=regenerate_of,
            options={"web_search": web_search, "collections": collections},
            compare=compare,
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
    err = _model_denied(user, ai_model)
    if err:
        return err
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
