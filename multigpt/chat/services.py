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
- **Logs:** nur IDs und Fehlerarten, nie Inhalte oder Keys (Plan 9).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from django.db import close_old_connections, connection
from django.utils import timezone

from multigpt.accounts.permissions import Action, can

from .models import AIModel, Conversation, Message
from .providers import registry
from .providers.base import ChatMessage, Delta, Done, Error, Usage

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


def build_history(conversation: Conversation, exclude_ids=()) -> list[ChatMessage]:
    """Verlauf für das Modell (siehe Moduldoku zu complete/aborted)."""
    qs = (
        conversation.messages.filter(
            role__in=[Message.Role.USER, Message.Role.ASSISTANT],
            status__in=[Message.Status.COMPLETE, Message.Status.ABORTED],
        )
        .exclude(content="")
        .exclude(pk__in=list(exclude_ids))
        .order_by("created", "id")
    )
    return [ChatMessage(role=m.role, content=m.content) for m in qs]


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
    line = content.strip().splitlines()[0].strip() if content.strip() else ""
    if len(line) > TITLE_LENGTH:
        line = line[: TITLE_LENGTH - 1].rstrip() + "…"
    return line


# --- Vorbereitung ---------------------------------------------------------------


def prepare_turn(
    user,
    conversation: Conversation,
    ai_model: AIModel,
    content: str | None = None,
    regenerate: bool = False,
) -> Turn:
    """Legt Nutzer- und (leere) Assistant-Nachricht an. Rechte prüft der Aufrufer."""
    user_message = None
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

    assistant_message = Message.objects.create(
        conversation=conversation,
        role=Message.Role.ASSISTANT,
        model=ai_model,
        status=Message.Status.ABORTED,  # Platzhalter, siehe Moduldoku
    )

    fields = {"updated": timezone.now(), "default_model": ai_model}
    if not conversation.title and user_message is not None:
        fields["title"] = _title_from(content)
    Conversation.objects.filter(pk=conversation.pk).update(**fields)
    for key, value in fields.items():
        setattr(conversation, key, value)

    return Turn(user, conversation, ai_model, user_message, assistant_message)


# --- Stream ----------------------------------------------------------------------


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
        )
        Conversation.objects.filter(pk=turn.conversation.pk).update(updated=timezone.now())
    except Exception as exc:
        logger.error("Antwort %s konnte nicht gespeichert werden: %s", msg.pk, type(exc).__name__)


def run_turn(turn: Turn) -> Iterator[tuple[str, dict]]:
    """Generator der SSE-Events als (Name, Daten).

    Reihenfolge: start, delta*, [error], usage, done. Wird der Generator
    geschlossen (Client hat abgebrochen), wird der bisherige Text mit
    ``aborted`` gespeichert und der Adapter-Stream geschlossen.
    """
    msg = turn.assistant_message
    stream = None
    parts: list[str] = []
    tokens_in = tokens_out = 0
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
        history = build_history(turn.conversation, exclude_ids=[msg.pk])
        system = build_system_prompt(turn.user, turn.conversation)
        adapter = registry.get_adapter(turn.ai_model.provider)
        stream = adapter.stream(turn.ai_model.model_id, history, system=system)
        last_save = time.monotonic()
        for event in stream:
            if isinstance(event, Delta):
                if not event.text:
                    continue
                parts.append(event.text)
                yield "delta", {"text": event.text}
                if time.monotonic() - last_save >= SAVE_INTERVAL:
                    _save_partial(msg.pk, "".join(parts))
                    last_save = time.monotonic()
            elif isinstance(event, Usage):
                tokens_in, tokens_out = event.tokens_in, event.tokens_out
            elif isinstance(event, Error):
                status, error = Message.Status.ERROR, event.message or GENERIC_ERROR
                logger.info("Anbieterfehler bei Antwort %s (Modell %s)", msg.pk, turn.ai_model.pk)
                break
            elif isinstance(event, Done):
                break
            # ToolCallEvent: Werkzeuge kommen erst mit M4a.
    except GeneratorExit:
        _close(stream)
        _finish(turn, "".join(parts), Message.Status.ABORTED, "", tokens_in, tokens_out)
        logger.info("Antwort %s vom Client abgebrochen", msg.pk)
        raise
    except Exception as exc:
        logger.error("Stream für Antwort %s fehlgeschlagen: %s", msg.pk, type(exc).__name__)
        status, error = Message.Status.ERROR, GENERIC_ERROR

    _close(stream)
    _finish(turn, "".join(parts), status, error, tokens_in, tokens_out)
    if status == Message.Status.ERROR:
        yield "error", {"message": error}
    yield "usage", {"tokens_in": tokens_in, "tokens_out": tokens_out}
    yield "done", {"status": status}
