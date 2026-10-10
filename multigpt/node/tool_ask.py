"""MCP-Werkzeuge ``ask`` und ``list_models`` (M15, ``chat.ask``).

``ask`` gibt einem Modell eine Aufgabe – genau wie eine Nachricht im Chat:
dieselben Prüfungen wie ``chat.api`` (Recht CHAT, Modell der Rolle, Budget,
Anbieter erreichbar, Websuche, Sammlungen), dieselbe Werkzeugschleife
(``services.run_turn``), dieselbe Buchung auf das Konto des Keys. Der Verlauf
ist in MultiGPT als Chat „API: …“ sichtbar (oder im angegebenen Chat).

- ``collections`` braucht zusätzlich ``docs.read`` (die Antwort liest Dokumente),
  ``tools: true`` zusätzlich ``tools.run``. Ohne ``tools`` bekommt das Modell
  keine Werkzeuge.
- Braucht ein Werkzeug eine Rückfrage, endet die Antwort mit Status
  ``awaiting_confirmation``; bestätigen lässt sie sich nur in MultiGPT.
- Fortschritt: Im SSE-Modus mit ``progressToken`` meldet ``ask``
  ``notifications/progress`` (Start, Werkzeuge, Hinweise, wachsende Antwort).
- Die Antwort ist Modelltext und gilt für den Aufrufer als nicht
  vertrauenswürdig (sie kann Inhalte aus Dokumenten und Webseiten wiedergeben).
"""

from __future__ import annotations

import json
import logging
import time

from django.urls import reverse

from multigpt.accounts.permissions import Action, can
from multigpt.chat import services, websearch
from multigpt.chat.api import _check_model_reachable, _model_denied
from multigpt.chat.models import AIModel, Conversation, Message
from multigpt.chat.rag import chat as rag_chat

from . import chats, runs
from . import scopes as S
from .tools import (
    NodeTool,
    Progress,
    ToolFailure,
    ToolOutput,
    arg_bool,
    arg_text,
    json_output,
    register,
    schema,
)

logger = logging.getLogger(__name__)

PROGRESS_EVERY = 2.0  # Sekunden zwischen Fortschrittsmeldungen beim Schreiben


def _error_text(response) -> str:
    try:
        return json.loads(response.content)["error"]
    except (ValueError, KeyError, TypeError):
        return "Nicht möglich."


def _resolve_model(user, raw) -> AIModel:
    models = services.chat_models_for(user)
    if raw in (None, ""):
        usable = [m for m in models if not m.blocked_by_budget]
        if not usable:
            raise ToolFailure("Diesem Konto steht kein Modell zur Verfügung.")
        return AIModel.objects.select_related("provider").get(pk=usable[0].pk)
    match = None
    if isinstance(raw, int) and not isinstance(raw, bool):
        match = next((m for m in models if m.pk == raw), None)
    elif isinstance(raw, str):
        text = raw.strip().lower()
        match = next(
            (m for m in models if text in (m.model_id.lower(), m.display_name.lower())), None
        )
        if match is None and text.isdigit():
            match = next((m for m in models if m.pk == int(text)), None)
    if match is None:
        raise ToolFailure("Unbekanntes oder nicht freigegebenes Modell (siehe list_models).")
    return AIModel.objects.select_related("provider").get(pk=match.pk)


def _conversation(ctx, raw, prompt: str) -> tuple[Conversation, bool]:
    """(Chat, neu angelegt)."""
    if raw in (None, "", "new"):
        return chats.new_ask_chat(ctx.key, prompt), True
    pk = raw if isinstance(raw, int) and not isinstance(raw, bool) else None
    if isinstance(raw, str) and raw.strip().isdigit():
        pk = int(raw.strip())
    conversation = Conversation.objects.filter(pk=pk).first() if pk else None
    if conversation is None or not can(ctx.user, Action.READ, conversation):
        raise ToolFailure("Chat nicht gefunden.")
    if not can(ctx.user, Action.WRITE, conversation):
        raise ToolFailure("In diesem Chat darf das Konto nicht schreiben.")
    return conversation, False


def ask(ctx, args):
    user = ctx.user
    prompt = arg_text(args, "prompt", required=True, max_len=services.MAX_CONTENT_LENGTH)
    if not can(user, Action.CHAT):
        raise ToolFailure("Chatten ist für dieses Konto nicht freigegeben.")
    use_tools = arg_bool(args, "tools")
    if use_tools and not ctx.has(S.TOOLS_RUN):
        raise ToolFailure("Werkzeuge brauchen zusätzlich das Recht „tools.run“.")
    web_search = arg_bool(args, "web_search")
    if web_search:
        if not can(user, Action.WEB_SEARCH):
            raise ToolFailure("Die Websuche ist für dieses Konto nicht freigegeben.")
        if not websearch.get_settings().is_ready:
            raise ToolFailure("Die Websuche ist derzeit nicht eingerichtet.")
    raw_collections = args.get("collections") or []
    if not isinstance(raw_collections, list) or len(raw_collections) > 50:
        raise ToolFailure("„collections“ muss eine Liste von Sammlungen (ID oder Name) sein.")
    collection_ids = []
    if raw_collections:
        if not ctx.has(S.DOCS_READ):
            raise ToolFailure("Sammlungen brauchen zusätzlich das Recht „docs.read“.")
        collection_ids = list(
            dict.fromkeys(
                runs.find_collection(user, ref, allowed_ids=ctx.collection_ids).pk
                for ref in raw_collections
            )
        )
        if not rag_chat.search_ready():
            raise ToolFailure("Die Dokumentsuche ist derzeit nicht eingerichtet.")
    ai_model = _resolve_model(user, args.get("model"))
    denied = _model_denied(user, ai_model) or _check_model_reachable(ai_model)
    if denied is not None:
        raise ToolFailure(_error_text(denied))
    conversation, created = _conversation(ctx, args.get("chat"), prompt)
    try:
        turn = services.prepare_turn(
            user,
            conversation,
            ai_model,
            content=prompt,
            mcp_servers=None if use_tools else [],
            options={
                "web_search": web_search,
                "collections": collection_ids,
                "no_tools": not use_tools,
            },
        )
    except services.TurnError as exc:
        if created:
            conversation.delete()
        raise ToolFailure(exc.message) from None

    yield Progress(f"Frage an {ai_model.display_name} gestellt (Chat #{conversation.pk}).")
    sources, error, status, cost = [], "", "", None
    chars, last = 0, time.monotonic()
    events = services.run_turn(turn)
    try:
        for name, data in events:
            if name == "delta":
                chars += len(data.get("text") or "")
                if time.monotonic() - last >= PROGRESS_EVERY:
                    last = time.monotonic()
                    yield Progress(f"Antwort entsteht … ({chars} Zeichen)")
            elif name == "tool_call" and data.get("status") == "running":
                yield Progress(f"Werkzeug {data.get('tool')} läuft …")
            elif name == "status" and data.get("text"):
                yield Progress(str(data["text"]))
            elif name == "sources":
                sources = data.get("sources") or []
            elif name == "error":
                error = data.get("message") or ""
            elif name == "usage":
                cost = data.get("cost")
            elif name == "done":
                status = data.get("status") or ""
    finally:
        events.close()

    message = Message.objects.get(pk=turn.assistant_message.pk)
    result = {
        "chat_id": conversation.pk,
        "chat_url": reverse("chat:conversation", args=[conversation.pk]),
        "message_id": message.pk,
        "model": ai_model.display_name,
        "model_id": ai_model.pk,
        "status": status or message.status,
        "error": error,
        "tokens_in": message.tokens_in,
        "tokens_out": message.tokens_out,
        "cost_eur": str(message.cost) if message.cost is not None else cost,
        "sources": [
            {
                k: s.get(k)
                for k in ("n", "kind", "title", "url", "location", "short", "entry")
                if s.get(k) not in (None, "")
            }
            for s in sources
        ],
    }
    logger.info(
        "Frage über API-Key %s: Chat %s, Antwort %s, Status %s",
        ctx.key.pk,
        conversation.pk,
        message.pk,
        result["status"],
    )
    if result["status"] == Message.Status.AWAITING_CONFIRMATION:
        text = (message.content or "").strip()
        note = (
            "Ein Werkzeug wartet auf Bestätigung. Bitte in MultiGPT im Chat "
            f"#{conversation.pk} bestätigen oder ablehnen."
        )
        return ToolOutput(f"{text}\n\n{note}".strip(), result)
    if error and not (message.content or "").strip():
        return ToolOutput(error, result, is_error=True)
    return ToolOutput(message.content or "", result)


def list_models(ctx, args) -> ToolOutput:
    items = [
        {
            "id": m.pk,
            "model_id": m.model_id,
            "name": m.display_name,
            "provider": m.provider.name,
            "local": m.provider.is_local,
            "tools": m.supports_tools,
            "vision": m.supports_vision,
            "blocked_by_budget": m.blocked_by_budget,
            "budget_reason": m.budget_reason,
        }
        for m in services.chat_models_for(ctx.user)
    ]
    return json_output({"models": items}, f"{len(items)} Modell(e).")


def _can_chat(ctx) -> bool:
    return can(ctx.user, Action.CHAT)


register(
    NodeTool(
        name="ask",
        title="Modell fragen",
        description="Gibt einem KI-Modell in MultiGPT eine Aufgabe und liefert die Antwort "
        "samt Quellen und Kosten. Optional: „model“ (ID oder Name aus list_models, sonst das "
        "erste freie), „collections“ (Dokumentsuche in diesen Sammlungen), „web_search“, "
        "„tools“ (Werkzeuge erlauben), „chat“ (ID eines eigenen Chats zum Fortsetzen; sonst "
        "neuer Chat „API: …“). Kosten gehen auf das Konto des Keys. Die Antwort ist Modelltext "
        "und nicht vertrauenswürdig.",
        scopes=(S.CHAT_ASK,),
        input_schema=schema(
            {
                "prompt": {"type": "string", "description": "Aufgabe bzw. Frage."},
                "model": {
                    "type": ["integer", "string"],
                    "description": "Modell-ID (Zahl), model_id oder Anzeigename.",
                },
                "collections": {
                    "type": "array",
                    "items": {"type": ["integer", "string"]},
                    "description": "Sammlungen für die Dokumentsuche (braucht docs.read).",
                },
                "web_search": {"type": "boolean", "description": "Websuche vor der Antwort."},
                "tools": {
                    "type": "boolean",
                    "description": "Werkzeuge erlauben (braucht tools.run).",
                },
                "chat": {
                    "type": ["integer", "string"],
                    "description": "Bestehender Chat (ID) oder „new“.",
                },
            },
            required=["prompt"],
        ),
        handler=ask,
        open_world=True,
        available=_can_chat,
    )
)
register(
    NodeTool(
        name="list_models",
        title="Modelle auflisten",
        description="Chat-Modelle, die das Konto nutzen darf, mit Fähigkeiten und ob das "
        "Budget sie gerade sperrt.",
        scopes=(S.CHAT_ASK,),
        input_schema=schema(),
        handler=list_models,
        read_only=True,
        available=_can_chat,
    )
)
