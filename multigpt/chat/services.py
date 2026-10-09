"""Chat-Ablauf (M3): Verlauf aufbauen, System-Prompt, Adapter aufrufen,
Antwort speichern, Kosten berechnen.

Entscheidungen:

- **Zwischenstatus:** Die Assistant-Nachricht wird vor dem Anbieteraufruf mit
  ``status=aborted`` angelegt und erst am Ende auf ``complete``/``error``
  gesetzt. Stirbt der Prozess mitten im Stream (Worker-Timeout, Neustart),
  bleibt sie korrekt als abgebrochen stehen – ohne eigenen Status "läuft"
  im Modell. Folge: Wer die Seite während einer laufenden Antwort neu lädt,
  sieht sie als "abgebrochen". Das stimmt in aller Regel: Mit dem Neuladen
  bricht der Browser den fetch ab, der Server erkennt das beim nächsten
  Delta und speichert endgültig ``aborted``. Nur ein zweiter Tab sähe eine
  tatsächlich noch laufende Antwort vorläufig als abgebrochen.
- **Text nach Abbruch:** Der Server erkennt den Abbruch erst, wenn das
  Schreiben eines Deltas scheitert. Dieses Delta (und ggf. bereits im
  Sendepuffer liegende) ist schon gespeichert, hat den Browser aber nicht
  mehr erreicht. Nach dem Neuladen kann die Antwort daher etwas länger sein
  als zuvor angezeigt – gewollt, es geht kein empfangener Text verloren.
- **Verlauf:** Nachrichten mit ``complete`` und ``aborted`` (mit Inhalt) gehen
  an das Modell. Abgebrochene Antworten hat der Nutzer gesehen; der weitere
  Chat bezieht sich darauf. Antworten mit ``error`` und leere Nachrichten
  bleiben draußen (sie enthalten keine verwertbare Antwort).
- **regenerate:** Die letzte sichtbare Assistant-Nachricht bekommt den Status
  ``superseded`` und wird für die letzte Nutzernachricht neu erzeugt. Sie
  bleibt gespeichert, damit Tokens und Kosten für Verbrauch und Budget (M6)
  zählen (sonst ließe sich das Budget durch Neu-Erzeugen umgehen), taucht aber
  weder im Verlauf noch in der Oberfläche auf (``visible_messages``).
- **Abriss mitten in der Antwort (M4-05):** Meldet der Adapter einen
  wiederholbaren Fehler (``retryable``, z. B. LM Studio beendet, Verbindung
  abgerissen), nachdem schon Text kam, wird die Antwort als ``aborted`` mit
  dem empfangenen Text und dem Fehlertext gespeichert – wie ein Abbruch, der
  Nutzer kann mit einem anderen Modell neu erzeugen. Ohne Teiltext oder bei
  nicht wiederholbaren Fehlern bleibt es ``error``. Bei Anbietern mit
  Statusprüfung wird außerdem der Status-Cache verworfen, damit die nächste
  Abfrage sofort neu prüft.
- **Logs:** nur IDs und Fehlerarten, nie Inhalte oder Keys (Plan 9).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from django.db import close_old_connections, connection, transaction
from django.db.models import Q
from django.utils import timezone

from multigpt.accounts.permissions import Action, can

from . import status as provider_status
from . import tooling
from .models import AIModel, Conversation, McpServer, Message, ToolCall
from .providers import registry
from .providers.base import ChatMessage, Delta, Done, Error, ToolCallEvent, Usage
from .titles import title_from

logger = logging.getLogger(__name__)

# Zwischenspeichern des Teiltexts höchstens alle SAVE_INTERVAL Sekunden.
SAVE_INTERVAL = 2.0
MAX_CONTENT_LENGTH = 100_000
TITLE_LENGTH = 60
GENERIC_ERROR = "Die Antwort konnte nicht erzeugt werden. Bitte später erneut versuchen."
_MILLION = Decimal(1_000_000)
_COST_STEP = Decimal("0.000001")


class TurnError(Exception):
    """Ungültige Anfrage vor dem Stream; ``message`` ist ein Text für Nutzer."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass
class Turn:
    """Eine vorbereitete Runde: gespeicherte Nachrichten und Kontext."""

    user: object
    conversation: Conversation
    ai_model: AIModel
    user_message: Message | None
    assistant_message: Message
    resume: bool = False  # Fortsetzung nach einer Rückfrage (confirm)


# --- Hilfen --------------------------------------------------------------------


def available_chat_models(user) -> list[AIModel]:
    """Aktive Chat-Modelle aktiver Anbieter, die ``user`` nutzen darf."""
    qs = AIModel.objects.filter(
        capability=AIModel.Capability.CHAT, active=True, provider__active=True
    ).select_related("provider")
    return [m for m in qs if can(user, Action.USE_MODEL, m)]


def visible_messages(conversation: Conversation):
    """Nachrichten für Oberfläche und API: Nutzer/Assistent, ohne ersetzte."""
    return (
        conversation.messages.filter(role__in=[Message.Role.USER, Message.Role.ASSISTANT])
        .exclude(status=Message.Status.SUPERSEDED)
        .select_related("model")
        .prefetch_related("tool_calls__server", "tool_calls__attachments")
        .order_by("created", "id")
    )


def build_system_prompt(user, conversation: Conversation) -> str | None:
    """Fester Prompt der Rolle zuerst, dann der System-Prompt des Chats."""
    parts = []
    role = user.role if getattr(user, "role_id", None) else None
    if role is not None and role.fixed_system_prompt.strip():
        parts.append(role.fixed_system_prompt.strip())
    if conversation.system_prompt.strip():
        parts.append(conversation.system_prompt.strip())
    return "\n\n".join(parts) or None


def build_history(
    conversation: Conversation,
    exclude_ids=(),
    *,
    provider_id: int | None = None,
    with_tools: bool = False,
) -> list[ChatMessage]:
    """Verlauf für das Modell (siehe Moduldoku zu complete/aborted).

    Antworten mit Werkzeugrunden werden mit ``with_tools`` vollständig
    (Aufrufe, Ergebnisse) übergeben, sonst nur als Text (ohne angebotene
    Werkzeuge darf kein tool_use im Verlauf stehen). ``provider_state`` geht
    nur an denselben Anbieter zurück (``provider_id``).
    """
    qs = (
        conversation.messages.filter(
            role__in=[Message.Role.USER, Message.Role.ASSISTANT],
            status__in=[Message.Status.COMPLETE, Message.Status.ABORTED],
        )
        .exclude(pk__in=list(exclude_ids))
        .select_related("model")
        .order_by("created", "id")
    )
    history: list[ChatMessage] = []
    for m in qs:
        if m.role == Message.Role.ASSISTANT:
            history += expand_assistant(m, provider_id=provider_id, with_tools=with_tools)
        elif m.content:
            history.append(ChatMessage(role=m.role, content=m.content))
    return history


def _same_provider(message: Message, provider_id: int | None) -> bool:
    return (
        provider_id is not None
        and message.model is not None
        and (message.model.provider_id == provider_id)
    )


def round_messages(rnd: dict, *, keep_state: bool = True) -> list[ChatMessage]:
    """Eine gespeicherte Werkzeugrunde als Assistant- plus Tool-Nachrichten."""
    calls = [
        ToolCallEvent(
            id=c["id"],
            name=c["name"],
            arguments=c.get("arguments") or {},
            provider_state=c.get("provider_state") if keep_state else None,
        )
        for c in rnd.get("calls", [])
    ]
    out = [
        ChatMessage(
            "assistant",
            rnd.get("text", ""),
            tool_calls=calls,
            provider_state=rnd.get("provider_state") if keep_state else None,
        )
    ]
    for res in rnd.get("results", []):
        out.append(
            ChatMessage(
                "tool",
                res.get("content", ""),
                tool_call_id=res.get("tool_call_id"),
                name=res.get("name"),
                is_error=bool(res.get("is_error")),
            )
        )
    return out


def expand_assistant(
    message: Message, *, provider_id: int | None = None, with_tools: bool = False
) -> list[ChatMessage]:
    """Assistant-Nachricht für den Verlauf, ggf. mit ihren Werkzeugrunden."""
    state = message.tool_state or {}
    rounds = state.get("rounds") or []
    keep = _same_provider(message, provider_id)
    if not rounds:
        if not message.content:
            return []
        final_state = state.get("final_provider_state") if keep else None
        return [ChatMessage("assistant", message.content, provider_state=final_state)]
    if not with_tools:
        return [ChatMessage("assistant", message.content)] if message.content.strip() else []
    out: list[ChatMessage] = []
    for rnd in rounds:
        out += round_messages(rnd, keep_state=keep)
    final = message.content[state.get("text_offset", 0) :].strip()
    final_state = state.get("final_provider_state") if keep else None
    if final or final_state:
        out.append(ChatMessage("assistant", final, provider_state=final_state))
    return out


def compute_cost(ai_model: AIModel, tokens_in: int, tokens_out: int) -> Decimal | None:
    """Kosten in Euro; Preise je 1 Mio. Tokens. Lokal 0, ohne Preise None."""
    if ai_model.provider.is_local:
        return Decimal(0)
    if ai_model.price_in is None and ai_model.price_out is None:
        return None
    price_in = ai_model.price_in or Decimal(0)
    price_out = ai_model.price_out or Decimal(0)
    cost = (Decimal(tokens_in) * price_in + Decimal(tokens_out) * price_out) / _MILLION
    return cost.quantize(_COST_STEP, rounding=ROUND_HALF_UP)


def _refresh_connection():
    """Vor dem Speichern nach langer Streamzeit: tote/abgelaufene Verbindung
    verwerfen, damit die nächste Abfrage neu verbindet (CONN_HEALTH_CHECKS).
    Nicht innerhalb einer Transaktion (z. B. in Tests)."""
    if not connection.in_atomic_block:
        close_old_connections()


def _title_from(content: str) -> str:
    # Markdown-Zeichen entfernen, Codeblöcke überspringen (M5-02, titles.py).
    return title_from(content, TITLE_LENGTH)


# --- Vorbereitung ---------------------------------------------------------------


def prepare_turn(
    user,
    conversation: Conversation,
    ai_model: AIModel,
    content: str | None = None,
    regenerate: bool = False,
    mcp_servers: list[int] | None = None,
) -> Turn:
    """Legt Nutzer- und (leere) Assistant-Nachricht an. Rechte prüft der Aufrufer.

    ``mcp_servers``: eingeschaltete MCP-Server (None: Voreinstellung = alle
    erlaubten). Nur bei Modellen mit ``supports_tools``. Offene Rückfragen
    des Chats werden vorher geschlossen (``close_pending``).
    """
    user_message = None
    close_pending(conversation)
    if regenerate:
        last = visible_messages(conversation).last()
        if last is not None and last.role == Message.Role.ASSISTANT:
            Message.objects.filter(pk=last.pk).update(status=Message.Status.SUPERSEDED)
            last = visible_messages(conversation).last()
        if last is None or last.role != Message.Role.USER:
            raise TurnError("Es gibt keine Frage, zu der eine Antwort erzeugt werden kann.")
    else:
        content = (content or "").strip()
        if not content:
            raise TurnError("Die Nachricht ist leer.")
        if len(content) > MAX_CONTENT_LENGTH:
            raise TurnError("Die Nachricht ist zu lang.")
        user_message = Message.objects.create(
            conversation=conversation, role=Message.Role.USER, content=content
        )

    servers = tooling.enabled_server_ids(user, mcp_servers) if ai_model.supports_tools else []
    assistant_message = Message.objects.create(
        conversation=conversation,
        role=Message.Role.ASSISTANT,
        model=ai_model,
        status=Message.Status.ABORTED,  # Platzhalter, siehe Moduldoku
        tool_state={"servers": servers} if servers else {},
    )

    fields = {"updated": timezone.now(), "default_model": ai_model}
    if not conversation.title and user_message is not None:
        fields["title"] = _title_from(content)
    Conversation.objects.filter(pk=conversation.pk).update(**fields)
    for key, value in fields.items():
        setattr(conversation, key, value)

    return Turn(user, conversation, ai_model, user_message, assistant_message)


# --- Rückfrage (M4a-05) --------------------------------------------------------


def _unanswered_results(state: dict, text: str) -> None:
    """Fehlende Ergebnisse der letzten Runde als Fehler ergänzen."""
    rounds = state.get("rounds") or []
    if not rounds:
        return
    rnd = rounds[-1]
    done = {r["tool_call_id"] for r in rnd.setdefault("results", [])}
    for call in rnd.get("calls", []):
        if call["id"] not in done:
            rnd["results"].append(
                {
                    "tool_call_id": call["id"],
                    "name": call["name"],
                    "content": text,
                    "is_error": True,
                }
            )


def close_pending(conversation: Conversation) -> None:
    """Offene Rückfragen schließen, bevor der Chat weitergeht (neue Nachricht,
    Neu erzeugen): wartende Aufrufe -> ``rejected``, Antwort -> ``aborted``.
    So bleiben keine verwaisten Aufrufe zurück."""
    awaiting = Q(status=Message.Status.AWAITING_CONFIRMATION) | Q(
        tool_calls__status=ToolCall.Status.AWAITING_CONFIRMATION
    )
    # Zweiter Fall: confirm angenommen, Stream aber nie gestartet (Client weg).
    pending = conversation.messages.filter(awaiting).distinct()
    for message in pending:
        state = dict(message.tool_state or {})
        _unanswered_results(state, tooling.MSG_UNANSWERED)
        for tool_call in message.tool_calls.filter(status=ToolCall.Status.AWAITING_CONFIRMATION):
            tooling.close_call(tool_call, ToolCall.Status.REJECTED, tooling.MSG_UNANSWERED)
        fields = {"tool_state": state}
        if message.status == Message.Status.AWAITING_CONFIRMATION:
            fields["status"] = Message.Status.ABORTED
        Message.objects.filter(pk=message.pk).update(**fields)


def pending_message(conversation: Conversation) -> Message | None:
    """Die Antwort, die auf eine Bestätigung wartet (höchstens eine je Chat)."""
    return (
        conversation.messages.filter(
            role=Message.Role.ASSISTANT, status=Message.Status.AWAITING_CONFIRMATION
        )
        .select_related("model__provider")
        .order_by("-created", "-id")
        .first()
    )


DECISIONS = {"approve", "reject"}


def prepare_resume(user, conversation: Conversation, message: Message, decisions) -> Turn:
    """Entscheidungen übernehmen und die Fortsetzung vorbereiten.

    ``decisions``: ``{"<ToolCall.pk>": "approve"|"reject"}`` für **genau** die
    wartenden Aufrufe dieser Antwort. Die Antwort wechselt atomar von
    ``awaiting_confirmation`` auf den Platzhalter ``aborted`` – ein zweites,
    gleichzeitiges confirm bekommt 409. Rechte prüft die Schleife vor jedem
    Aufruf erneut.
    """
    if not isinstance(decisions, dict) or not decisions:
        raise TurnError("Bitte über alle Werkzeugaufrufe entscheiden.")
    parsed: dict[int, str] = {}
    for key, value in decisions.items():
        try:
            pk = int(key)
        except (TypeError, ValueError):
            raise TurnError("Ungültige Entscheidung.") from None
        if value not in DECISIONS:
            raise TurnError("Ungültige Entscheidung.")
        parsed[pk] = value
    waiting = set(
        message.tool_calls.filter(status=ToolCall.Status.AWAITING_CONFIRMATION).values_list(
            "pk", flat=True
        )
    )
    if set(parsed) != waiting:
        raise TurnError("Bitte über alle wartenden Werkzeugaufrufe entscheiden.")
    claimed = Message.objects.filter(
        pk=message.pk, status=Message.Status.AWAITING_CONFIRMATION
    ).update(status=Message.Status.ABORTED)
    if claimed != 1:
        raise TurnError("Über diese Werkzeugaufrufe wurde bereits entschieden.", 409)
    state = dict(message.tool_state or {})
    for call in (state.get("rounds") or [{}])[-1].get("calls", []):
        if call.get("tool_call") in parsed:
            call["decision"] = parsed[call["tool_call"]]
    message.tool_state = state
    message.status = Message.Status.ABORTED
    Message.objects.filter(pk=message.pk).update(tool_state=state)
    Conversation.objects.filter(pk=conversation.pk).update(updated=timezone.now())
    return Turn(user, conversation, message.model, None, message, resume=True)


# --- Stream und Werkzeugschleife (M3, M4a-04) ---------------------------------------

# Höchstens so viele Anbieteraufrufe je Antwort; der letzte läuft mit
# ``tool_choice="none"`` (mit Werkzeugen, weil Anthropic sie verlangt, sobald
# der Verlauf tool_use enthält).
MAX_ROUNDS = 10
ROUND_SEPARATOR = "\n\n"


def _close(stream):
    if stream is not None and hasattr(stream, "close"):
        try:
            stream.close()
        except Exception as exc:  # Aufräumen darf den Abschluss nicht verhindern
            logger.warning("Adapter-Stream ließ sich nicht schließen: %s", type(exc).__name__)


def _save_partial(message_id: int, text: str):
    _refresh_connection()
    Message.objects.filter(pk=message_id).update(content=text)


def _finish(turn: Turn, text: str, status: str, error: str, tokens_in: int, tokens_out: int):
    msg = turn.assistant_message
    msg.content = text
    msg.status = status
    msg.error = error
    msg.tokens_in = tokens_in
    msg.tokens_out = tokens_out
    msg.cost = compute_cost(turn.ai_model, tokens_in, tokens_out)
    try:
        _refresh_connection()
        Message.objects.filter(pk=msg.pk).update(
            content=text,
            status=status,
            error=error,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost=msg.cost,
            tool_state=msg.tool_state or {},
        )
        Conversation.objects.filter(pk=turn.conversation.pk).update(updated=timezone.now())
    except Exception as exc:
        logger.error("Antwort %s konnte nicht gespeichert werden: %s", msg.pk, type(exc).__name__)


class _Loop:
    """Eine Antwort mit Werkzeugrunden; ``run()`` liefert die SSE-Events.

    Zustand in ``Message.tool_state`` (eine Nachricht je Antwort, siehe
    Moduldoku unten bei ``run_turn``)::

        {"servers": [id, ...],            # eingeschaltete Server
         "model_calls": n,                # Anbieteraufrufe bisher
         "rounds": [{"text", "provider_state",
                     "calls": [{"id", "name", "arguments", "provider_state",
                                "server_id", "tool", "tool_call", "decision"?}],
                     "results": [{"tool_call_id", "name", "content", "is_error"}]}],
         "text_offset": int,              # Länge von content nach der letzten Runde
         "final_provider_state": {...}}   # provider_state der letzten Antwort
    """

    def __init__(self, turn: Turn):
        self.turn = turn
        self.msg = turn.assistant_message
        self.state = dict(self.msg.tool_state or {})
        self.state.setdefault("rounds", [])
        self.msg.tool_state = self.state
        self.servers = list(self.state.get("servers") or [])
        self.content = self.msg.content if turn.resume else ""
        self.tokens_in = self.msg.tokens_in if turn.resume else 0
        self.tokens_out = self.msg.tokens_out if turn.resume else 0
        self.stream = None
        self.paused = False
        self.bindings: dict[str, tooling.Binding] = {}

    # --- Hilfen ---

    def _persist(self):
        _refresh_connection()
        Message.objects.filter(pk=self.msg.pk).update(content=self.content, tool_state=self.state)

    def _abort_open_calls(self):
        for tool_call in ToolCall.objects.filter(
            message=self.msg,
            status__in=[ToolCall.Status.RUNNING, ToolCall.Status.AWAITING_CONFIRMATION],
        ):
            tooling.close_call(tool_call, ToolCall.Status.ERROR, tooling.MSG_ABORTED)
        _unanswered_results(self.state, tooling.MSG_ABORTED)

    def _add_result(self, rnd: dict, call: dict, text: str, is_error: bool):
        rnd.setdefault("results", []).append(
            {
                "tool_call_id": call["id"],
                "name": call["name"],
                "content": text,
                "is_error": is_error,
            }
        )

    def _tool_call_for(self, call: dict, server, status: str) -> ToolCall:
        if call.get("tool_call"):
            tool_call = ToolCall.objects.filter(pk=call["tool_call"], message=self.msg).first()
            if tool_call is not None:
                return tool_call
        tool_call = ToolCall.objects.create(
            message=self.msg,
            server=server,
            tool=(call.get("tool") or call["name"])[:200],
            provider_call_id=call["id"][:200],
            arguments=call.get("arguments") or {},
            status=status,
        )
        call["tool_call"] = tool_call.pk
        return tool_call

    def _new_round(self, text: str, provider_state, calls: list[ToolCallEvent]) -> dict:
        stored = []
        for event in calls:
            binding = self.bindings.get(event.name)
            stored.append(
                {
                    "id": event.id,
                    "name": event.name,
                    "arguments": event.arguments,
                    "provider_state": event.provider_state,
                    "server_id": binding.server_id if binding else None,
                    "tool": binding.tool if binding else None,
                    "tool_call": None,
                }
            )
        rnd = {"text": text, "provider_state": provider_state, "calls": stored, "results": []}
        self.state["rounds"].append(rnd)
        self.state["text_offset"] = len(self.content)
        return rnd

    # --- Runde ausführen ---

    def _process_round(self, rnd: dict):
        """Aufrufe der Runde in Reihenfolge ausführen oder für die Rückfrage
        pausieren. Generator; Rückgabewert True = pausiert.

        Braucht ein Aufruf eine Bestätigung, wartet die **ganze Runde** (auch
        Aufrufe ohne Rückfrage), damit die Reihenfolge der Aufrufe erhalten
        bleibt – ein Lesezugriff nach einem Schreibzugriff sieht sonst den
        alten Stand.
        """
        done = {r["tool_call_id"] for r in rnd.get("results", [])}
        open_calls = [c for c in rnd["calls"] if c["id"] not in done]
        need = []
        for call in open_calls:
            server, err = tooling.resolve_server(self.turn.user, call, self.servers)
            if server is not None and call.get("decision") is None:
                if tooling.needs_confirmation(server, call["tool"]):
                    need.append((call, server))
        if need:
            yield from self._pause(need)
            return True
        for call in open_calls:
            yield from self._execute(rnd, call)
        return False

    def _pause(self, need):
        with transaction.atomic():
            tool_calls = []
            for call, server in need:
                tool_call = self._tool_call_for(call, server, ToolCall.Status.AWAITING_CONFIRMATION)
                tool_call.status = ToolCall.Status.AWAITING_CONFIRMATION
                tool_call.save(update_fields=["status"])
                tool_calls.append((tool_call, server))
            cost = compute_cost(self.turn.ai_model, self.tokens_in, self.tokens_out)
            Message.objects.filter(pk=self.msg.pk).update(
                content=self.content,
                status=Message.Status.AWAITING_CONFIRMATION,
                error="",
                tokens_in=self.tokens_in,
                tokens_out=self.tokens_out,
                cost=cost,
                tool_state=self.state,
            )
            Conversation.objects.filter(pk=self.turn.conversation.pk).update(updated=timezone.now())
        self.msg.status = Message.Status.AWAITING_CONFIRMATION
        self.paused = True
        logger.info("Antwort %s wartet auf Bestätigung (%d Aufrufe)", self.msg.pk, len(need))
        for tool_call, server in tool_calls:
            yield "tool_call", tooling.call_event(tool_call, server.name)
        yield "confirmation_required", {"tool_call_ids": [tc.pk for tc, _ in tool_calls]}
        yield "usage", {"tokens_in": self.tokens_in, "tokens_out": self.tokens_out}
        yield "done", {"status": Message.Status.AWAITING_CONFIRMATION}

    def _execute(self, rnd: dict, call: dict):
        user = self.turn.user
        # Rechte vor jedem Aufruf erneut prüfen (auch nach einer Bestätigung).
        server, err = tooling.resolve_server(user, call, self.servers)
        decision = call.get("decision")
        if decision == "reject":
            tool_call = self._tool_call_for(call, server, ToolCall.Status.REJECTED)
            tooling.close_call(tool_call, ToolCall.Status.REJECTED, tooling.MSG_REJECTED)
            self._add_result(rnd, call, tooling.MSG_REJECTED, True)
            self._persist()
            yield "tool_result", tooling.result_event(tool_call, [])
            return
        if server is not None and decision != "approve":
            if tooling.needs_confirmation(server, call["tool"]):
                err = tooling.MSG_NOT_CONFIRMED  # nie ohne Bestätigung ausführen
        tool_call = self._tool_call_for(call, server, ToolCall.Status.RUNNING)
        if not err:
            tool_call.status = ToolCall.Status.RUNNING
            tool_call.save(update_fields=["status"])
        yield "tool_call", {**tooling.call_event(tool_call), "status": ToolCall.Status.RUNNING}
        if err:
            tooling.close_call(tool_call, ToolCall.Status.ERROR, err)
            self._add_result(rnd, call, err, True)
            self._persist()
            yield "tool_result", tooling.result_event(tool_call, [])
            return
        outcome = tooling.execute(server, tool_call, self.msg)
        self._add_result(rnd, call, outcome.text, outcome.is_error)
        self._persist()
        yield "tool_result", tooling.result_event(tool_call, outcome.attachment_ids)

    def _over_limit(self, rnd: dict):
        """Aufrufe trotz ``tool_choice="none"`` in der letzten Runde: protokollieren,
        nicht ausführen."""
        for call in rnd["calls"]:
            server = None
            if call.get("server_id"):
                server = McpServer.objects.filter(pk=call["server_id"]).first()
            tool_call = self._tool_call_for(call, server, ToolCall.Status.ERROR)
            tooling.close_call(tool_call, ToolCall.Status.ERROR, tooling.MSG_ROUND_LIMIT)
            self._add_result(rnd, call, tooling.MSG_ROUND_LIMIT, True)
            yield "tool_call", {**tooling.call_event(tool_call), "status": ToolCall.Status.RUNNING}
            yield "tool_result", tooling.result_event(tool_call, [])
        self._persist()

    # --- Ablauf ---

    def run(self) -> Iterator[tuple[str, dict]]:
        turn, msg = self.turn, self.msg
        status = Message.Status.COMPLETE
        error = ""
        try:
            yield (
                "start",
                {
                    "user_message_id": turn.user_message.pk if turn.user_message else None,
                    "assistant_message_id": msg.pk,
                },
            )
            if turn.resume and self.state["rounds"]:
                paused = yield from self._process_round(self.state["rounds"][-1])
                if paused:
                    return
            if turn.ai_model.supports_tools and self.servers:
                self.bindings = tooling.collect_tools(turn.user, self.servers)
            specs = [b.spec for b in self.bindings.values()]
            provider_id = turn.ai_model.provider_id
            history = build_history(
                turn.conversation,
                exclude_ids=[msg.pk],
                provider_id=provider_id,
                with_tools=bool(specs),
            )
            if specs:
                for rnd in self.state["rounds"]:
                    history += round_messages(rnd)
            elif self.state["rounds"]:
                # Ohne Werkzeuge kein tool_use im Verlauf: bisherige Runden als Text.
                text = self.content.strip()
                if text:
                    history.append(ChatMessage("assistant", text))
            system = build_system_prompt(turn.user, turn.conversation)
            adapter = registry.get_adapter(turn.ai_model.provider)

            while True:
                calls_so_far = int(self.state.get("model_calls", 0))
                last = calls_so_far >= MAX_ROUNDS - 1
                params = {}
                if specs:
                    params = {"tools": specs, "tool_choice": "none" if last else "auto"}
                self.stream = adapter.stream(
                    turn.ai_model.model_id, history, system=system, **params
                )
                self.state["model_calls"] = calls_so_far + 1
                round_parts: list[str] = []
                calls: list[ToolCallEvent] = []
                round_in = round_out = 0
                done: Done | None = None
                last_save = time.monotonic()
                for event in self.stream:
                    if isinstance(event, Delta):
                        if not event.text:
                            continue
                        if not round_parts and self.state["rounds"] and self.content.strip():
                            if not self.content.endswith(ROUND_SEPARATOR):
                                self.content += ROUND_SEPARATOR
                                yield "delta", {"text": ROUND_SEPARATOR}
                        round_parts.append(event.text)
                        self.content += event.text
                        yield "delta", {"text": event.text}
                        if time.monotonic() - last_save >= SAVE_INTERVAL:
                            _save_partial(msg.pk, self.content)
                            last_save = time.monotonic()
                    elif isinstance(event, ToolCallEvent):
                        calls.append(event)
                    elif isinstance(event, Usage):
                        round_in, round_out = event.tokens_in, event.tokens_out
                    elif isinstance(event, Error):
                        error = event.message or GENERIC_ERROR
                        if event.retryable and self.content.strip():
                            status = Message.Status.ABORTED  # Teiltext bleibt
                        else:
                            status = Message.Status.ERROR
                        if event.retryable:
                            provider_status.invalidate(turn.ai_model.provider_id)
                        logger.info(
                            "Anbieterfehler bei Antwort %s (Modell %s)", msg.pk, turn.ai_model.pk
                        )
                        break
                    elif isinstance(event, Done):
                        done = event
                        break
                _close(self.stream)
                self.stream = None
                self.tokens_in += round_in
                self.tokens_out += round_out
                if error:
                    break
                if calls and specs:
                    rnd = self._new_round(
                        "".join(round_parts), done.provider_state if done else None, calls
                    )
                    if last:
                        yield from self._over_limit(rnd)
                        break
                    paused = yield from self._process_round(rnd)
                    if paused:
                        return
                    history += round_messages(rnd)
                    continue
                self.state["final_provider_state"] = done.provider_state if done else None
                break
        except GeneratorExit:
            _close(self.stream)
            if self.paused:
                raise  # Zustand ist gespeichert, die Antwort wartet weiter.
            self._abort_open_calls()
            _finish(turn, self.content, Message.Status.ABORTED, "", self.tokens_in, self.tokens_out)
            logger.info("Antwort %s vom Client abgebrochen", msg.pk)
            raise
        except Exception as exc:
            logger.error("Stream für Antwort %s fehlgeschlagen: %s", msg.pk, type(exc).__name__)
            status, error = Message.Status.ERROR, GENERIC_ERROR
            _close(self.stream)
            try:
                self._abort_open_calls()
            except Exception:
                logger.error("Werkzeugaufrufe von Antwort %s nicht abgeschlossen", msg.pk)

        if self.paused:
            return
        _finish(turn, self.content, status, error, self.tokens_in, self.tokens_out)
        if error:
            yield "error", {"message": error}
        yield "usage", {"tokens_in": self.tokens_in, "tokens_out": self.tokens_out}
        yield "done", {"status": status}


def run_turn(turn: Turn) -> Iterator[tuple[str, dict]]:
    """Generator der SSE-Events als (Name, Daten).

    Reihenfolge: start, (delta | tool_call | tool_result)*, [error], usage,
    done – oder bei einer Rückfrage: …, tool_call (awaiting_confirmation)*,
    confirmation_required, usage, done ``awaiting_confirmation``. Wird der
    Generator geschlossen (Client hat abgebrochen), wird der bisherige Text
    mit ``aborted`` gespeichert, laufende Werkzeugaufrufe werden als Fehler
    abgeschlossen und der Adapter-Stream geschlossen.

    **Speicherung (Entscheidung):** Eine Antwort bleibt *eine* ``Message``.
    Zwischenrunden (Text, Aufrufe, Ergebnisse, ``provider_state``) stehen als
    JSON in ``Message.tool_state``, jeder Aufruf zusätzlich als ``ToolCall``
    (Protokoll und Anzeige). Eigene Zwischen-Nachrichten (role assistant/tool)
    hätten jede Abfrage auf sichtbare Nachrichten, Neu erzeugen, Kosten und
    Export mit Sonderfällen belastet; so bleibt eine Blase = eine Nachricht,
    Kosten summieren sich an einer Stelle, und der Verlauf geht trotzdem
    wörtlich (inkl. ``provider_state``) an das Modell – auch nach einer Pause.

    Usage und Kosten werden über alle Runden der Antwort summiert.
    """
    return _Loop(turn).run()
