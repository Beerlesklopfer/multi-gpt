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
    ChatMessage,
    Delta,
    Done,
    Error,
    Event,
    ProviderAdapter,
    ProviderError,
    ToolCallEvent,
    Usage,
)

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.openai.com/v1"
APP_TITLE = "MultiGPT"

# Vertrag: connect 10 s, read 300 s (lange Denkpausen mancher Modelle).
STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)
LIST_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)

# Diese Felder setzt der Adapter selbst; ``params`` darf sie nicht überschreiben.
_RESERVED_PARAMS = {"model", "messages", "stream", "stream_options", "tools"}

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


def _exception_to_error(exc: Exception, *, started: bool) -> Error:
    """Netz- und sonstige Ausnahmen in ein ``Error``-Event übersetzen."""
    if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout):
        return Error(MSG_UNREACHABLE, retryable=True)
    if isinstance(exc, httpx.TimeoutException):
        return Error(MSG_TIMEOUT, retryable=True)
    if isinstance(exc, httpx.TransportError):
        # RemoteProtocolError, ReadError, ... – meist ein abgerissener Stream.
        return Error(MSG_INTERRUPTED if started else MSG_UNREACHABLE, retryable=True)
    return Error(MSG_UNEXPECTED, retryable=False)


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
            raise ProviderError(message)
        try:
            data = response.json().get("data") or []
            return [str(item["id"]) for item in data if isinstance(item, dict) and "id" in item]
        except (ValueError, AttributeError, TypeError) as exc:
            raise ProviderError("Der Anbieter hat eine unerwartete Modellliste geliefert.") from exc

    def list_models(self) -> list[str]:
        try:
            return self._fetch_models(LIST_TIMEOUT)
        except ProviderError:
            raise
        except Exception as exc:
            error = _exception_to_error(exc, started=False)
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
        tools: list[dict] | None,
        params: dict,
    ) -> dict:
        payload_messages = []
        if system:
            payload_messages.append({"role": "system", "content": system})
        payload_messages += [{"role": m.role, "content": m.content} for m in messages]
        body = {k: v for k, v in params.items() if k not in _RESERVED_PARAMS and v is not None}
        body.update(
            model=model_id,
            messages=payload_messages,
            stream=True,
            stream_options={"include_usage": True},
        )
        if tools:
            # M3: unverändert durchreichen; die Übersetzung folgt in M4a.
            body["tools"] = tools
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
        body = self._build_body(model_id, messages, system, tools, params)
        started = False
        try:
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
                parser = _ChunkParser(emit_tool_calls=bool(tools))
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
            yield _exception_to_error(exc, started=started)


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
        # Gerüst für M4a: Werkzeugaufrufe kommen in Stücken, je ``index``.
        self.tool_calls: dict[int, dict] = {}

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

    def _collect_tool_call(self, part: dict) -> None:
        slot = self.tool_calls.setdefault(
            part.get("index", 0), {"id": "", "name": "", "arguments": ""}
        )
        if part.get("id"):
            slot["id"] = part["id"]
        function = part.get("function") or {}
        if function.get("name"):
            slot["name"] += function["name"]
        if function.get("arguments"):
            slot["arguments"] += function["arguments"]

    def _finish(self) -> Iterator[Event]:
        if self.emit_tool_calls:
            for index in sorted(self.tool_calls):
                call = self.tool_calls[index]
                try:
                    arguments = json.loads(call["arguments"] or "{}")
                except ValueError:
                    arguments = {}
                if not isinstance(arguments, dict):
                    arguments = {}
                yield ToolCallEvent(id=call["id"], name=call["name"], arguments=arguments)
        if self.usage:
            yield self.usage
        self.finished = True
        yield Done(self.finish_reason)

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
