"""OpenAI-kompatibler Adapter (Plan Abschnitt 7, M3-02).

Deckt OpenAI, Mistral, Groq, OpenRouter und LM Studio ab; nur ``base_url``
unterscheidet sich. Grundlage (gelesen 2026-10-09):

- OpenAI Chat Completions, Streaming-Chunks: ``choices[].delta.content``,
  ``finish_reason``; mit ``stream_options.include_usage`` folgt vor
  ``data: [DONE]`` ein letzter Chunk mit ``usage`` und leerem ``choices``.
- OpenAI ``GET /models``: ``{"object": "list", "data": [{"id": ...}, ...]}``.
- OpenRouter: SSE-Kommentarzeilen (``: OPENROUTER PROCESSING``) ignorieren;
  Fehler mitten im Stream kommen als Chunk mit ``error`` und
  ``finish_reason="error"``; Usage-Chunk hat dort eine Choice mit leerem Delta.
  Optionaler Header ``X-Title`` zur App-Zuordnung.
- LM Studio: OpenAI-kompatibel unter ``/v1``; ohne Authentifizierung, sofern
  nicht eingeschaltet (dann Bearer-Token wie bei OpenAI).

Werkzeuge (M4a, gelesen 2026-10-09, developers.openai.com/api/docs/api-reference/chat):

- ``tools: [{"type": "function", "function": {name, description,
  parameters}}]``; ``tool_choice`` ``none``/``auto``/``required`` oder
  ``{"type": "function", "function": {"name": …}}``.
- Stream: ``delta.tool_calls[]`` mit ``index``, beim ersten Stück ``id``,
  ``type`` und ``function.name``, danach Stücke von ``function.arguments``
  (JSON-Text, nicht immer gültig); ``finish_reason`` ``tool_calls``.
- Verlauf: Assistant mit ``tool_calls`` (``content`` darf ``null`` sein), dann
  je Aufruf ``{"role": "tool", "tool_call_id", "content"}``.

Sicherheit (Plan 9): Der API-Key steht nur im Authorization-Header. Er
erscheint weder in Logs noch in Fehlermeldungen; Fehlertexte der Anbieter
werden nicht weitergereicht (OpenAI nennt bei 401 Teile des Keys).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from urllib.parse import urlsplit

import httpx

from .base import (
    FINISH_TOOL_CALLS,
    MSG_INTERRUPTED,
    MSG_INVALID_MODEL_LIST,
    MSG_STREAM_ERROR,
    MSG_TIMEOUT,
    MSG_UNEXPECTED,
    MSG_UNREACHABLE,
    ChatMessage,
    Delta,
    Done,
    Error,
    Event,
    ProviderAdapter,
    ProviderError,
    ProviderHTTPError,
    ToolCallEvent,
    ToolSpec,
    Usage,
    exception_to_error,
    http_error_message,
    new_tool_call_id,
    normalize_tools,
    pair_tool_messages,
    parse_tool_arguments,
    tool_schema,
)

# Frühere Namen (vor M4a hier definiert), für Importe von außen.
_exception_to_error = exception_to_error
__all__ = [
    "MSG_INTERRUPTED",
    "MSG_STREAM_ERROR",
    "MSG_TIMEOUT",
    "MSG_UNEXPECTED",
    "MSG_UNREACHABLE",
    "OpenAICompatAdapter",
    "http_error_message",
]

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.openai.com/v1"
APP_TITLE = "MultiGPT"

# Vertrag: connect 10 s, read 300 s (lange Denkpausen mancher Modelle).
STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)
LIST_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)

# Diese Felder setzt der Adapter selbst; ``params`` darf sie nicht überschreiben.
_RESERVED_PARAMS = {"model", "messages", "stream", "stream_options", "tools", "tool_choice"}


def _error_code(body: bytes) -> str:
    """Nur ``type``/``code`` aus einem Fehler-JSON (fürs Log, nie die Meldung)."""
    try:
        err = json.loads(body).get("error") or {}
    except (ValueError, AttributeError):
        return "-"
    if not isinstance(err, dict):
        return "-"
    return f"{err.get('type') or '-'}/{err.get('code') or '-'}"


class OpenAICompatAdapter(ProviderAdapter):
    @property
    def base_url(self) -> str:
        return (self.provider.base_url or DEFAULT_BASE_URL).rstrip("/")

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        api_key = (self.provider.api_key or "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        host = urlsplit(self.base_url).hostname or ""
        if host == "openrouter.ai" or host.endswith(".openrouter.ai"):
            headers["X-Title"] = APP_TITLE
        return headers

    def _log(self, what: str, detail: str) -> None:
        # Nur Anbieter-ID und technische Kurzinfo, keine Keys oder Inhalte.
        logger.warning("Anbieter %s: %s (%s)", self.provider.pk, what, detail)

    # --- Modellliste ---------------------------------------------------------

    def _fetch_models(self, timeout: httpx.Timeout | float) -> list[str]:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(f"{self.base_url}/models", headers=self._headers())
        if response.status_code != 200:
            message, _ = http_error_message(response.status_code)
            raise ProviderHTTPError(message, response.status_code)
        try:
            data = response.json().get("data") or []
            return [str(item["id"]) for item in data if isinstance(item, dict) and "id" in item]
        except (ValueError, AttributeError, TypeError) as exc:
            raise ProviderError(MSG_INVALID_MODEL_LIST) from exc

    def list_models(self, timeout: float | None = None) -> list[str]:
        try:
            return self._fetch_models(LIST_TIMEOUT if timeout is None else timeout)
        except ProviderError:
            raise
        except Exception as exc:
            error = exception_to_error(exc, started=False)
            if timeout is None:
                # Kurzprüfungen (Statusabfrage, M4-03) loggen nicht: Dort ist
                # „nicht erreichbar“ der Normalfall (LM Studio meist aus).
                self._log("Modellliste", type(exc).__name__)
            raise ProviderError(error.message) from None

    def is_online(self, timeout: float = 2) -> bool:
        try:
            self._fetch_models(timeout)
        except Exception:
            return False
        return True

    # --- Streaming -----------------------------------------------------------

    def _build_body(
        self,
        model_id: str,
        messages: list[ChatMessage],
        system: str | None,
        tools: list[ToolSpec],
        params: dict,
    ) -> dict:
        payload_messages = []
        if system:
            payload_messages.append({"role": "system", "content": system})
        payload_messages += [_message_payload(m) for m in pair_tool_messages(messages)]
        body = {k: v for k, v in params.items() if k not in _RESERVED_PARAMS and v is not None}
        body.update(
            model=model_id,
            messages=payload_messages,
            stream=True,
            stream_options={"include_usage": True},
        )
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": tool_schema(t),
                    },
                }
                for t in tools
            ]
            choice = _tool_choice(params.get("tool_choice"))
            if choice is not None:
                body["tool_choice"] = choice
        return body

    def stream(
        self,
        model_id: str,
        messages: list[ChatMessage],
        system: str | None = None,
        tools: list[dict] | None = None,
        **params,
    ) -> Iterator[Event]:
        """Antwort als Events; letztes Event ist immer ``Done`` oder ``Error``.

        Bricht der Aufrufer ab (``close()`` am Generator), schließen die
        ``with``-Blöcke Antwort und Client und damit die HTTP-Verbindung.
        """
        started = False
        try:
            specs = normalize_tools(tools)
            body = self._build_body(model_id, messages, system, specs, params)
            with (
                httpx.Client(timeout=STREAM_TIMEOUT) as client,
                client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers={**self._headers(), "Accept": "text/event-stream"},
                    json=body,
                ) as response,
            ):
                if response.status_code != 200:
                    try:
                        detail = _error_code(response.read())
                    except Exception:
                        detail = "-"
                    self._log(f"HTTP {response.status_code}", detail)
                    message, retryable = http_error_message(response.status_code)
                    yield Error(message, retryable=retryable)
                    return
                parser = _ChunkParser(emit_tool_calls=bool(specs))
                for line in response.iter_lines():
                    for event in parser.feed(line):
                        if isinstance(event, Delta):
                            started = True
                        yield event
                    if parser.finished:
                        return
                yield from parser.end_of_stream()
        except Exception as exc:
            self._log("Stream", type(exc).__name__)
            yield exception_to_error(exc, started=started)


def _message_payload(message: ChatMessage) -> dict:
    """``ChatMessage`` im Chat-Completions-Format."""
    if message.role == "assistant" and message.tool_calls:
        return {
            "role": "assistant",
            "content": message.content or None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ],
        }
    if message.role == "tool":
        # Chat Completions kennt kein ``is_error``; Fehler stehen im Text.
        content = message.content or ""
        if message.is_error:
            content = f"Fehler: {content}"
        return {"role": "tool", "tool_call_id": message.tool_call_id or "", "content": content}
    return {"role": message.role, "content": message.content}


def _tool_choice(choice) -> str | dict | None:
    if choice is None or isinstance(choice, dict):
        return choice
    choice = str(choice)
    if choice in ("auto", "none", "required"):
        return choice
    return {"type": "function", "function": {"name": choice}}


class _ChunkParser:
    """Zerlegt SSE-Zeilen eines Chat-Completions-Streams in Events.

    ``finished`` wird True, sobald ein abschließendes Event (``Done``/``Error``)
    ausgegeben wurde.
    """

    def __init__(self, emit_tool_calls: bool = False):
        self.emit_tool_calls = emit_tool_calls
        self.finished = False
        self.finish_reason: str | None = None
        self.usage: Usage | None = None
        # Werkzeugaufrufe kommen in Stücken, je ``index`` (Reihenfolge des Eintreffens).
        self.tool_calls: list[dict] = []
        self._slots: dict[int, int] = {}

    def feed(self, line: str) -> Iterator[Event]:
        line = line.rstrip("\r")
        if not line or line.startswith(":"):
            return  # Leerzeile (Event-Ende) oder Kommentar wie ": OPENROUTER PROCESSING"
        field, _, value = line.partition(":")
        if field != "data":
            return  # event:, id:, retry: werden nicht gebraucht
        value = value.removeprefix(" ")
        if value.strip() == "[DONE]":
            yield from self._finish()
            return
        try:
            chunk = json.loads(value)
        except ValueError:
            logger.warning("Ungültige Zeile im Anbieter-Stream übersprungen")
            return
        if not isinstance(chunk, dict):
            return
        if chunk.get("error"):
            self.finished = True
            yield Error(MSG_STREAM_ERROR, retryable=True)
            return
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            self.usage = Usage(
                tokens_in=int(usage.get("prompt_tokens") or 0),
                tokens_out=int(usage.get("completion_tokens") or 0),
            )
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta") or {}
            text = delta.get("content")
            if text:
                yield Delta(text)
            for part in delta.get("tool_calls") or []:
                self._collect_tool_call(part)
            reason = choice.get("finish_reason")
            if reason == "error":
                self.finished = True
                yield Error(MSG_STREAM_ERROR, retryable=True)
                return
            if reason:
                self.finish_reason = reason

    def _collect_tool_call(self, part) -> None:
        if not isinstance(part, dict):
            return
        call_id = str(part.get("id") or "")
        index = part.get("index")
        position = self._slots.get(index) if isinstance(index, int) else None
        if position is None and not isinstance(index, int) and self.tool_calls:
            # Manche kompatiblen Server lassen ``index`` weg: Fortsetzung des
            # letzten Aufrufs, außer es kommt eine neue ID.
            last = len(self.tool_calls) - 1
            if not call_id or call_id == self.tool_calls[last]["id"]:
                position = last
        known_id = self.tool_calls[position]["id"] if position is not None else ""
        if call_id and known_id not in ("", call_id):
            position = None  # gleicher Index, neue ID: eigener Aufruf
        if position is None:
            self.tool_calls.append({"id": "", "name": "", "arguments": ""})
            position = len(self.tool_calls) - 1
            if isinstance(index, int):
                self._slots[index] = position
        slot = self.tool_calls[position]
        if call_id:
            slot["id"] = call_id
        function = part.get("function") or {}
        if not isinstance(function, dict):
            return
        if function.get("name"):
            slot["name"] += str(function["name"])
        arguments = function.get("arguments")
        if isinstance(arguments, dict):
            # Einige Server liefern die Argumente schon als Objekt.
            slot["arguments"] += json.dumps(arguments, ensure_ascii=False)
        elif arguments:
            slot["arguments"] += str(arguments)

    def _finish(self) -> Iterator[Event]:
        calls: list[ToolCallEvent] = []
        if self.emit_tool_calls:
            for call in self.tool_calls:
                arguments = parse_tool_arguments(call["arguments"], call["name"])
                if isinstance(arguments, Error):
                    self.finished = True
                    yield arguments
                    return
                calls.append(
                    ToolCallEvent(
                        id=call["id"] or new_tool_call_id(),
                        name=call["name"],
                        arguments=arguments,
                    )
                )
        yield from calls
        if self.usage:
            yield self.usage
        self.finished = True
        reason = self.finish_reason
        if calls:
            reason = FINISH_TOOL_CALLS
        yield Done(reason)

    def end_of_stream(self) -> Iterator[Event]:
        """Stream endete ohne ``[DONE]``."""
        if self.finished:
            return
        if self.finish_reason:
            # Manche Server lassen [DONE] weg; mit finish_reason ist die Antwort vollständig.
            yield from self._finish()
        else:
            self.finished = True
            yield Error(MSG_INTERRUPTED, retryable=True)
