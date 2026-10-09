"""Gemeinsame Schnittstelle aller Anbieter-Adapter (Plan Abschnitt 7, M3-01).

Ein Adapter liefert beim Streamen eine Folge von Events. Garantie für alle
Adapter: Das letzte Event ist genau ein ``Done`` oder ein ``Error``; Ausnahmen
werden intern zu ``Error`` und gelangen nie roh zum Aufrufer.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import httpx

if TYPE_CHECKING:
    from multigpt.chat.models import Provider


# --- Events ------------------------------------------------------------------


@dataclass(frozen=True)
class Delta:
    """Ein Stück Antworttext."""

    text: str


@dataclass(frozen=True)
class ToolCallEvent:
    """Das Modell möchte ein Werkzeug aufrufen (M4a).

    ``id`` ist immer gesetzt (fehlt sie beim Anbieter, vergibt der Adapter
    eine). ``provider_state`` ist undurchsichtig und anbieterspezifisch (z. B.
    Gemini ``thoughtSignature``); unverändert im Verlauf zurückgeben.
    """

    id: str
    name: str
    arguments: dict
    provider_state: dict | None = None


@dataclass(frozen=True)
class Usage:
    """Tokenverbrauch der Anfrage."""

    tokens_in: int
    tokens_out: int


@dataclass(frozen=True)
class Error:
    """Fehler; ``message`` ist ein deutscher Text für Nutzer, ohne Keys und Interna."""

    message: str
    retryable: bool = False


# Vereinheitlichter ``finish_reason``, wenn das Modell Werkzeuge aufrufen will.
FINISH_TOOL_CALLS = "tool_calls"


@dataclass(frozen=True)
class Done:
    """Regulärer Abschluss des Streams.

    ``finish_reason`` ist ``"tool_calls"``, wenn Werkzeugaufrufe (``ToolCallEvent``) kamen.
    ``provider_state`` ist der undurchsichtige, JSON-serialisierbare Zustand der
    Assistant-Antwort (Anthropic: Inhaltsblöcke mit thinking-Signaturen;
    Gemini: ``parts`` mit ``thoughtSignature``). Er gehört unverändert in
    ``ChatMessage.provider_state`` der Assistant-Nachricht dieser Antwort.
    """

    finish_reason: str | None = None
    provider_state: dict | None = None


Event = Delta | ToolCallEvent | Usage | Error | Done


# --- Werkzeuge und Nachrichten -----------------------------------------------

Role = Literal["user", "assistant", "system", "tool"]


@dataclass(frozen=True)
class ToolSpec:
    """Werkzeugdefinition, anbieterneutral (Plan 7, 8g).

    ``parameters`` ist ein JSON-Schema vom Typ ``object`` (wie MCP
    ``inputSchema``).
    """

    name: str
    description: str
    parameters: dict


@dataclass
class ChatMessage:
    """Eine Nachricht im Verlauf.

    - ``assistant``: ``content`` (Text, darf leer sein) und optional
      ``tool_calls`` sowie ``provider_state`` aus ``Done.provider_state``.
    - ``tool``: Ergebnis eines Werkzeugaufrufs; ``content`` ist der
      Ergebnistext, ``tool_call_id`` verweist auf ``ToolCallEvent.id``,
      ``name`` ist der Werkzeugname, ``is_error`` markiert Fehlschläge.
    """

    role: Role
    content: str = ""
    tool_calls: list[ToolCallEvent] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    is_error: bool = False
    provider_state: dict | None = None


def normalize_tools(tools: Iterable[ToolSpec | dict] | None) -> list[ToolSpec]:
    """``ToolSpec`` oder Dicts (``parameters``/``input_schema``/``inputSchema``)
    in eine Liste von ``ToolSpec`` überführen."""
    result: list[ToolSpec] = []
    for tool in tools or []:
        if isinstance(tool, ToolSpec):
            result.append(tool)
            continue
        if not isinstance(tool, dict):
            raise ProviderError("Ungültige Werkzeugdefinition.")
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            tool = tool["function"]  # OpenAI-Form
        name = str(tool.get("name") or "")
        if not name:
            raise ProviderError("Werkzeugdefinition ohne Namen.")
        schema = tool.get("parameters") or tool.get("input_schema") or tool.get("inputSchema")
        result.append(
            ToolSpec(
                name=name,
                description=str(tool.get("description") or ""),
                parameters=dict(schema) if isinstance(schema, dict) else {},
            )
        )
    return result


def tool_schema(spec: ToolSpec) -> dict:
    """JSON-Schema der Parameter; leer oder ohne ``type`` -> Objekt ohne Felder."""
    schema = dict(spec.parameters or {})
    schema.setdefault("type", "object")
    if schema["type"] == "object":
        schema.setdefault("properties", {})
    return schema


def new_tool_call_id() -> str:
    """Eigene ID für Werkzeugaufrufe ohne Anbieter-ID (Gemini, manche lokale Server)."""
    return f"call_{uuid.uuid4().hex[:24]}"


MSG_TOOL_ARGUMENTS = "Das Modell hat ungültige Argumente für das Werkzeug „{name}“ geliefert."
MSG_TOOL_NO_RESULT = "Kein Ergebnis vorhanden: Das Werkzeug wurde nicht ausgeführt."


def parse_tool_arguments(raw: str | None, name: str) -> dict | Error:
    """Gestreamte Argumente (JSON-Text) als Dict; leer -> ``{}``; sonst ``Error``."""
    if raw is None or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return Error(MSG_TOOL_ARGUMENTS.format(name=name or "?"), retryable=True)
    if not isinstance(value, dict):
        return Error(MSG_TOOL_ARGUMENTS.format(name=name or "?"), retryable=True)
    return value


def pair_tool_messages(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Werkzeugaufrufe und -ergebnisse paaren, wie es alle drei APIs verlangen.

    - Ergebnisse (Rolle ``tool``) gelten nur direkt nach der Assistant-Nachricht
      mit dem passenden Aufruf; verwaiste Ergebnisse fallen weg, doppelte auch.
    - Fehlt zu einem Aufruf das Ergebnis, wird es als Fehler ergänzt
      (``MSG_TOOL_NO_RESULT``), bevor der Verlauf weitergeht.
    - Ergebnisse stehen in der Reihenfolge der Aufrufe.
    """
    result: list[ChatMessage] = []
    i = 0
    while i < len(messages):
        message = messages[i]
        i += 1
        if message.role == "tool":
            continue  # verwaist (kein direkt vorangehender Aufruf)
        result.append(message)
        if message.role != "assistant" or not message.tool_calls:
            continue
        found: dict[str, ChatMessage] = {}
        while i < len(messages) and messages[i].role == "tool":
            tool_msg = messages[i]
            i += 1
            if tool_msg.tool_call_id and tool_msg.tool_call_id not in found:
                found[tool_msg.tool_call_id] = tool_msg
        for call in message.tool_calls:
            result.append(
                found.get(call.id)
                or ChatMessage(
                    "tool",
                    MSG_TOOL_NO_RESULT,
                    tool_call_id=call.id,
                    name=call.name,
                    is_error=True,
                )
            )
    return result


@dataclass
class Turn:
    """Ein Zug im streng abwechselnden Verlauf (Anthropic, Gemini).

    ``role`` ist ``user`` oder ``assistant``; Werkzeugergebnisse gehören zum
    ``user``-Zug und stehen dort vor dem Text.
    """

    role: Literal["user", "assistant"]
    messages: list[ChatMessage]

    @property
    def tool_results(self) -> list[ChatMessage]:
        return [m for m in self.messages if m.role == "tool"]

    @property
    def text(self) -> str:
        return "\n\n".join(
            m.content for m in self.messages if m.role != "tool" and (m.content or "").strip()
        )


def sse_data(line: str) -> str | None:
    """Inhalt einer SSE-``data:``-Zeile, sonst None (Leerzeile, Kommentar,
    ``event:``/``id:``/``retry:``)."""
    line = line.rstrip("\r")
    if not line or line.startswith(":"):
        return None
    name, _, value = line.partition(":")
    if name != "data":
        return None
    return value.removeprefix(" ")


def alternate_turns(messages: list[ChatMessage]) -> tuple[list[ChatMessage], list[str]]:
    """Verlauf für APIs mit streng abwechselnden Rollen (Anthropic, Gemini).

    - ``system``-Nachrichten werden herausgelöst und als zweiter Wert geliefert
      (die APIs haben dafür einen eigenen Parameter).
    - Leere Nachrichten fallen weg; aufeinanderfolgende Nachrichten derselben
      Rolle werden mit Leerzeile zusammengefasst (z. B. zwei Nutzernachrichten,
      wenn die Antwort dazwischen fehlerhaft war).
    - Der Verlauf beginnt mit ``user``: führende Assistant-Nachrichten fallen weg.
    - ``tool``-Nachrichten und Werkzeugaufrufe werden ausgelassen; mit
      Werkzeugen gilt ``group_turns``.
    """
    turns: list[ChatMessage] = []
    system_parts: list[str] = []
    for message in messages:
        text = message.content or ""
        if message.role == "system":
            if text.strip():
                system_parts.append(text.strip())
            continue
        if message.role not in ("user", "assistant") or not text.strip():
            continue
        if not turns and message.role != "user":
            continue
        if turns and turns[-1].role == message.role:
            turns[-1] = ChatMessage(message.role, f"{turns[-1].content}\n\n{text}")
        else:
            turns.append(ChatMessage(message.role, text))
    return turns, system_parts


def group_turns(messages: list[ChatMessage]) -> tuple[list[Turn], list[str]]:
    """Wie ``alternate_turns``, aber mit Werkzeugaufrufen und -ergebnissen.

    - ``system`` wird herausgelöst, Paarung über ``pair_tool_messages``.
    - Werkzeugergebnisse zählen zum ``user``-Zug.
    - Leere Nachrichten ohne Werkzeugaufruf fallen weg; aufeinanderfolgende
      Nachrichten derselben Rolle bilden einen Zug.
    - Der Verlauf beginnt mit ``user``: führende Assistant-Züge fallen samt
      ihren Ergebnissen weg.
    """
    turns: list[Turn] = []
    system_parts: list[str] = []
    for message in pair_tool_messages(messages):
        text = message.content or ""
        if message.role == "system":
            if text.strip():
                system_parts.append(text.strip())
            continue
        if message.role == "assistant":
            if not text.strip() and not message.tool_calls and not message.provider_state:
                continue
            role = "assistant"
        elif message.role == "user":
            if not text.strip():
                continue
            role = "user"
        elif message.role == "tool":
            # Folgt immer direkt auf einen Assistant-Zug mit Aufrufen
            # (pair_tool_messages). Fiel der als führender Zug weg, ist
            # ``turns`` noch leer und das Ergebnis fällt mit.
            if not turns:
                continue
            role = "user"
        else:
            continue
        if not turns and role != "user":
            continue
        if turns and turns[-1].role == role:
            turns[-1].messages.append(message)
        else:
            turns.append(Turn(role, [message]))
    return turns, system_parts


# --- Fehlertexte -------------------------------------------------------------

MSG_UNREACHABLE = "Der Anbieter ist nicht erreichbar."
MSG_TIMEOUT = "Der Anbieter hat nicht rechtzeitig geantwortet."
MSG_INTERRUPTED = "Die Verbindung zum Anbieter wurde während der Antwort unterbrochen."
MSG_UNEXPECTED = "Unerwarteter Fehler bei der Anfrage an den Anbieter."
MSG_STREAM_ERROR = "Der Anbieter hat die Antwort mit einem Fehler abgebrochen."


def http_error_message(status: int) -> tuple[str, bool]:
    """Deutscher Text und ``retryable`` zu einem HTTP-Status des Anbieters."""
    if status in (401, 403):
        return "Der Anbieter hat den Zugang abgelehnt. Bitte den API-Key prüfen.", False
    if status == 402:
        return "Beim Anbieter ist kein Guthaben mehr vorhanden.", False
    if status == 404:
        return "Modell oder Adresse beim Anbieter nicht gefunden.", False
    if status == 429:
        return (
            "Der Anbieter meldet zu viele Anfragen oder ein erschöpftes Kontingent. "
            "Bitte später erneut versuchen.",
            True,
        )
    if status >= 500:
        return "Der Anbieter hat einen Serverfehler gemeldet. Bitte später erneut versuchen.", True
    return f"Der Anbieter hat die Anfrage abgelehnt (HTTP {status}).", False


def exception_to_error(exc: Exception, *, started: bool) -> Error:
    """Netz- und sonstige Ausnahmen in ein ``Error``-Event übersetzen."""
    if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout):
        return Error(MSG_UNREACHABLE, retryable=True)
    if isinstance(exc, httpx.TimeoutException):
        return Error(MSG_TIMEOUT, retryable=True)
    if isinstance(exc, httpx.TransportError):
        # RemoteProtocolError, ReadError, ... – meist ein abgerissener Stream.
        return Error(MSG_INTERRUPTED if started else MSG_UNREACHABLE, retryable=True)
    if isinstance(exc, ProviderError):
        return Error(str(exc), retryable=False)
    return Error(MSG_UNEXPECTED, retryable=False)


# --- Fehler und Basisklasse --------------------------------------------------


class ProviderError(Exception):
    """Fehler bei Konfiguration oder Aufruf eines Anbieters (Text ohne Keys)."""


class ProviderAdapter:
    """Basisklasse. Nicht unterstützte Fähigkeiten werfen ``NotImplementedError``."""

    def __init__(self, provider: Provider):
        self.provider = provider

    def stream(
        self,
        model_id: str,
        messages: list[ChatMessage],
        system: str | None = None,
        tools: list[ToolSpec | dict] | None = None,
        **params,
    ) -> Iterator[Event]:
        """Antwort als Events; letztes Event ist genau ein ``Done`` oder ``Error``.

        ``tools``: angebotene Werkzeuge. ``params["tool_choice"]``: ``"auto"``,
        ``"none"``, ``"required"`` oder ein Werkzeugname (erzwingt dieses).
        """
        raise NotImplementedError

    def list_models(self, timeout: float | None = None) -> list[str]:
        """Modell-IDs des Anbieters. ``timeout`` in Sekunden (None: Standard des
        Adapters). Fehler als ``ProviderError``."""
        raise NotImplementedError

    def is_online(self, timeout: float = 2) -> bool:
        """Kurzer Abruf der Modellliste; True/False, nie eine Ausnahme."""
        try:
            self.list_models(timeout=timeout)
        except Exception:
            return False
        return True

    def embed(self, model_id: str, texts: list[str]):
        raise NotImplementedError

    def transcribe(self, model_id: str, audio):
        raise NotImplementedError

    def speak(self, model_id: str, text: str, voice: str | None = None):
        raise NotImplementedError

    def generate_image(self, model_id: str, prompt: str, **params):
        raise NotImplementedError

    def edit_image(self, model_id: str, image, prompt: str, **params):
        raise NotImplementedError

    def __repr__(self):
        # Bewusst ohne Key oder URL-Interna.
        return f"<{type(self).__name__} provider={getattr(self.provider, 'pk', None)}>"
