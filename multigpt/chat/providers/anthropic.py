"""Anthropic-Adapter für die Messages-API (Plan Abschnitt 7, M4-01).

Grundlage (gelesen 2026-10-09):

- ``POST /v1/messages`` mit Headern ``x-api-key`` und ``anthropic-version:
  2023-06-01``; Pflichtfelder ``model``, ``max_tokens``, ``messages``. Der
  System-Prompt ist ein eigener Parameter ``system``, Nachrichten haben nur
  die Rollen ``user``/``assistant``. Das Modell ist auf abwechselnde Rollen
  trainiert; ein Verlauf, der mit ``assistant`` endet, gilt als Prefill (bei
  neueren Modellen HTTP 400).
- Streaming (``stream: true``): Events ``message_start`` (``message.usage``
  mit ``input_tokens``), je Inhaltsblock ``content_block_start``,
  ``content_block_delta`` (``text_delta``, ``input_json_delta``,
  ``thinking_delta``, ``signature_delta``), ``content_block_stop``, dann
  ``message_delta`` (``delta.stop_reason``; ``usage`` ist *kumulativ*) und
  ``message_stop``. Dazu ``ping``-Events und ``error``-Events mitten im Stream
  (z. B. ``overloaded_error``). Unbekannte Event-Typen sind zu ignorieren.
- Fehler: JSON ``{"type": "error", "error": {"type", "message"}}``; Status u. a.
  401 ``authentication_error``, 402 ``billing_error``, 429
  ``rate_limit_error``, 500 ``api_error``, 504 ``timeout_error``, 529
  ``overloaded_error``.
- ``GET /v1/models``: ``{"data": [{"id", ...}], "has_more", "last_id"}``,
  Blättern mit ``after_id``, ``limit`` bis 1000.

Werkzeuge (M4a, gelesen 2026-10-09):

- Definition ``{name, description, input_schema}``; ``tool_choice``
  ``{"type": "auto"|"any"|"none"}`` oder ``{"type": "tool", "name"}``
  (``any``/``tool`` nicht bei allen Modellen, z. B. nicht mit manuellem
  Thinking).
- Antwort: ``tool_use``-Blöcke ``{id, name, input}``, ``stop_reason``
  ``tool_use``. Im Stream beginnt der Block mit ``content_block_start``
  (``input: {}``), ``input_json_delta.partial_json`` liefert JSON-Stücke,
  zusammengesetzt und geparst wird bei ``content_block_stop``.
- Rückweg: Die ``tool_result``-Blöcke (``tool_use_id``, ``content``,
  ``is_error``) stehen in der direkt folgenden User-Nachricht und dort *vor*
  jedem Text; parallele Ergebnisse in derselben Nachricht.
- Thinking: ``thinking``-Blöcke (``thinking_delta``, Signatur per
  ``signature_delta`` kurz vor ``content_block_stop``) und
  ``redacted_thinking``-Blöcke (``data``) müssen innerhalb einer
  Werkzeugrunde vollständig und unverändert zurückgegeben werden; die Doku
  empfiehlt, jede Assistant-Antwort genau so zurückzusenden, wie sie kam.
  Deshalb liefert ``Done.provider_state`` dann die vollständigen
  Inhaltsblöcke (``{"content": [...]}``), die beim Rückweg wörtlich
  eingesetzt werden.

Sicherheit (Plan 9): Der API-Key steht nur im Header ``x-api-key``; Fehlertexte
des Anbieters werden nicht weitergereicht, geloggt wird nur der Fehlertyp.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import httpx

from .base import (
    FINISH_TOOL_CALLS,
    MSG_INTERRUPTED,
    MSG_INVALID_MODEL_LIST,
    MSG_STREAM_ERROR,
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
    Turn,
    Usage,
    exception_to_error,
    group_turns,
    http_error_message,
    normalize_tools,
    parse_tool_arguments,
    provider_error_code,
    sse_data,
    tool_schema,
)
from .openai_compat import LIST_TIMEOUT, STREAM_TIMEOUT

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
API_VERSION = "2023-06-01"
# Pflichtfeld bei Anthropic; über ``params["max_tokens"]`` überschreibbar.
# Großzügig, weil neuere Modelle auch das Nachdenken davon bezahlen.
DEFAULT_MAX_TOKENS = 16000
MODELS_PAGE_SIZE = 1000
MAX_MODEL_PAGES = 20

_RESERVED_PARAMS = {"model", "messages", "stream", "system", "tools", "tool_choice"}
# Blöcke, die ``provider_state`` nötig machen (unverändert zurückzugeben).
_THINKING_BLOCKS = ("thinking", "redacted_thinking")

MSG_OVERLOADED = "Der Anbieter ist derzeit überlastet. Bitte später erneut versuchen."
# Fehlertypen im Stream, bei denen ein erneuter Versuch sinnvoll ist.
_RETRYABLE_STREAM_ERRORS = {"overloaded_error", "api_error", "rate_limit_error", "timeout_error"}


def _http_error(status: int) -> tuple[str, bool]:
    if status == 529:
        return MSG_OVERLOADED, True
    return http_error_message(status)


def _error_type(body: bytes) -> str:
    """Nur ``error.type`` aus einem Fehler-JSON (fürs Log, nie die Meldung)."""
    try:
        err = json.loads(body).get("error") or {}
    except (ValueError, AttributeError):
        return "-"
    return str(err.get("type") or "-") if isinstance(err, dict) else "-"


class AnthropicAdapter(ProviderAdapter):
    @property
    def base_url(self) -> str:
        return (self.provider.base_url or DEFAULT_BASE_URL).rstrip("/")

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "anthropic-version": API_VERSION}
        api_key = (self.provider.api_key or "").strip()
        if api_key:
            headers["x-api-key"] = api_key
        return headers

    def _log(self, what: str, detail: str) -> None:
        # Nur Anbieter-ID und technische Kurzinfo, keine Keys oder Inhalte.
        logger.warning("Anbieter %s: %s (%s)", self.provider.pk, what, detail)

    # --- Modellliste ---------------------------------------------------------

    def _fetch_models(self, timeout: httpx.Timeout | float) -> list[str]:
        ids: list[str] = []
        query: dict[str, str | int] = {"limit": MODELS_PAGE_SIZE}
        with httpx.Client(timeout=timeout) as client:
            for _ in range(MAX_MODEL_PAGES):
                response = client.get(
                    f"{self.base_url}/models", params=query, headers=self._headers()
                )
                if response.status_code != 200:
                    message, _ = _http_error(response.status_code)
                    raise ProviderHTTPError(
                        message, response.status_code, code=provider_error_code(response.content)
                    )
                try:
                    page = response.json()
                    for item in page.get("data") or []:
                        if isinstance(item, dict) and item.get("id"):
                            ids.append(str(item["id"]))
                    last_id = page.get("last_id")
                    has_more = bool(page.get("has_more"))
                except (ValueError, AttributeError, TypeError) as exc:
                    raise ProviderError(MSG_INVALID_MODEL_LIST) from exc
                if not has_more or not last_id:
                    break
                query["after_id"] = str(last_id)
        return ids

    def list_models(self, timeout: float | None = None) -> list[str]:
        try:
            return self._fetch_models(LIST_TIMEOUT if timeout is None else timeout)
        except ProviderError:
            raise
        except Exception as exc:
            error = exception_to_error(exc, started=False)
            if timeout is None:
                # Kurzprüfungen (Statusabfrage) loggen nicht.
                self._log("Modellliste", type(exc).__name__)
            raise ProviderError(error.message) from None

    # --- Streaming -----------------------------------------------------------

    def _build_body(
        self,
        model_id: str,
        messages: list[ChatMessage],
        system: str | None,
        tools: list[ToolSpec],
        params: dict,
    ) -> dict:
        turns, system_parts = group_turns(messages)
        if system and system.strip():
            system_parts.insert(0, system.strip())
        body = {k: v for k, v in params.items() if k not in _RESERVED_PARAMS and v is not None}
        body.setdefault("max_tokens", DEFAULT_MAX_TOKENS)
        body.update(
            model=model_id,
            messages=[_turn_payload(t) for t in turns],
            stream=True,
        )
        if system_parts:
            body["system"] = "\n\n".join(system_parts)
        if tools:
            body["tools"] = [_tool_payload(t) for t in tools]
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

        ``close()`` am Generator schließt über die ``with``-Blöcke die Verbindung.
        """
        started = False
        try:
            specs = normalize_tools(tools)
            body = self._build_body(model_id, messages, system, specs, params)
            with (
                httpx.Client(timeout=STREAM_TIMEOUT) as client,
                client.stream(
                    "POST",
                    f"{self.base_url}/messages",
                    headers={**self._headers(), "Accept": "text/event-stream"},
                    json=body,
                ) as response,
            ):
                if response.status_code != 200:
                    try:
                        detail = _error_type(response.read())
                    except Exception:
                        detail = "-"
                    self._log(f"HTTP {response.status_code}", detail)
                    message, retryable = _http_error(response.status_code)
                    yield Error(message, retryable=retryable)
                    return
                parser = _EventParser(emit_tool_calls=bool(specs))
                for line in response.iter_lines():
                    for event in parser.feed(line):
                        if isinstance(event, Delta):
                            started = True
                        elif isinstance(event, Error):
                            self._log("Fehler im Stream", parser.error_type)
                        yield event
                    if parser.finished:
                        return
                yield from parser.end_of_stream()
        except Exception as exc:
            self._log("Stream", type(exc).__name__)
            yield exception_to_error(exc, started=started)


def _tool_payload(tool: ToolSpec) -> dict:
    payload = {"name": tool.name, "input_schema": tool_schema(tool)}
    if tool.description:
        payload["description"] = tool.description
    return payload


def _tool_choice(choice) -> dict | None:
    if choice is None or isinstance(choice, dict):
        return choice
    choice = str(choice)
    if choice in ("auto", "none"):
        return {"type": choice}
    if choice == "required":
        return {"type": "any"}
    return {"type": "tool", "name": choice}


def _assistant_blocks(message: ChatMessage) -> list[dict]:
    state = message.provider_state or {}
    if isinstance(state.get("content"), list):
        # Wörtlich wie empfangen (Thinking-Signaturen, Reihenfolge).
        return list(state["content"])
    blocks: list[dict] = []
    if (message.content or "").strip():
        blocks.append({"type": "text", "text": message.content})
    for call in message.tool_calls:
        blocks.append(
            {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
        )
    return blocks


def _turn_payload(turn: Turn) -> dict:
    """Ein Zug als Messages-API-Nachricht; ohne Werkzeuge mit Text-Inhalt wie bisher."""
    plain = all(
        m.role != "tool" and not m.tool_calls and not m.provider_state for m in turn.messages
    )
    if plain:
        return {"role": turn.role, "content": turn.text}
    blocks: list[dict] = []
    if turn.role == "assistant":
        for message in turn.messages:
            blocks += _assistant_blocks(message)
        return {"role": "assistant", "content": blocks}
    # User-Zug: tool_result-Blöcke zuerst, Text danach (Pflicht laut Doku).
    for result in turn.tool_results:
        block: dict = {"type": "tool_result", "tool_use_id": result.tool_call_id or ""}
        if result.content:
            block["content"] = result.content
        if result.is_error:
            block["is_error"] = True
        blocks.append(block)
    if turn.text:
        blocks.append({"type": "text", "text": turn.text})
    return {"role": "user", "content": blocks}


class _EventParser:
    """Zerlegt die SSE-Zeilen der Messages-API in Events.

    Inhaltsblöcke werden je ``index`` verfolgt: ``text`` liefert ``Delta``,
    ``thinking``/``redacted_thinking`` werden samt Signatur gesammelt (nicht
    angezeigt), ``tool_use`` sammelt ``input_json_delta``. Am Ende folgen
    (nur mit ``emit_tool_calls``) die Werkzeugaufrufe, ``Usage`` und
    ``Done`` – mit ``provider_state``, falls Thinking-Blöcke dabei waren.
    ``finished`` wird True, sobald ``Done`` oder ``Error`` ausgegeben wurde.
    """

    def __init__(self, emit_tool_calls: bool = False):
        self.emit_tool_calls = emit_tool_calls
        self.finished = False
        self.stop_reason: str | None = None
        self.error_type = "-"
        self.usage: dict[str, int] = {}
        # index -> Block im API-Format, ``tool_use`` mit ``_json`` (Rohtext).
        self.blocks: dict[int, dict] = {}

    def feed(self, line: str) -> Iterator[Event]:
        data = sse_data(line)
        if data is None:
            return
        try:
            event = json.loads(data)
        except ValueError:
            logger.warning("Ungültige Zeile im Anbieter-Stream übersprungen")
            return
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message") or {}
            if isinstance(message, dict):
                self._update_usage(message.get("usage"))
        elif kind == "content_block_start":
            block = event.get("content_block") or {}
            if isinstance(block, dict):
                self.blocks[event.get("index", 0)] = self._new_block(block)
                # Text kann schon im Start-Block stehen (laut Doku meist leer).
                if block.get("type") == "text" and block.get("text"):
                    yield Delta(str(block["text"]))
        elif kind == "content_block_delta":
            yield from self._delta(event.get("index", 0), event.get("delta") or {})
        elif kind == "content_block_stop":
            pass  # Argumente werden am Ende geparst (Fehler dann als letztes Event).
        elif kind == "message_delta":
            delta = event.get("delta") or {}
            if isinstance(delta, dict) and delta.get("stop_reason"):
                self.stop_reason = str(delta["stop_reason"])
            self._update_usage(event.get("usage"))
        elif kind == "message_stop":
            yield from self._finish()
        elif kind == "error":
            err = event.get("error") or {}
            self.error_type = str(err.get("type") or "-") if isinstance(err, dict) else "-"
            self.finished = True
            if self.error_type == "overloaded_error":
                yield Error(MSG_OVERLOADED, retryable=True)
            else:
                yield Error(MSG_STREAM_ERROR, retryable=self.error_type in _RETRYABLE_STREAM_ERRORS)
        # ping und unbekannte Event-Typen: ignorieren (Doku: Versionierung).

    @staticmethod
    def _new_block(block: dict) -> dict:
        kind = block.get("type")
        if kind == "text":
            return {"type": "text", "text": str(block.get("text") or "")}
        if kind == "thinking":
            return {
                "type": "thinking",
                "thinking": str(block.get("thinking") or ""),
                "signature": str(block.get("signature") or ""),
            }
        if kind == "tool_use":
            return {
                "type": "tool_use",
                "id": str(block.get("id") or ""),
                "name": str(block.get("name") or ""),
                "_json": "",
            }
        # redacted_thinking (``data``) und Unbekanntes: unverändert übernehmen.
        return dict(block)

    def _delta(self, index: int, delta: dict) -> Iterator[Event]:
        if not isinstance(delta, dict):
            return
        kind = delta.get("type")
        block = self.blocks.get(index)
        if kind == "text_delta":
            text = delta.get("text")
            if text:
                if block is not None and block.get("type") == "text":
                    block["text"] += str(text)
                yield Delta(str(text))
        elif block is None:
            return
        elif kind == "input_json_delta" and block["type"] == "tool_use":
            block["_json"] += str(delta.get("partial_json") or "")
        elif kind == "thinking_delta" and block["type"] == "thinking":
            block["thinking"] += str(delta.get("thinking") or "")
        elif kind == "signature_delta" and block["type"] == "thinking":
            block["signature"] += str(delta.get("signature") or "")
        # citations_delta u. a.: nicht gebraucht.

    def _content(self) -> tuple[list[dict], list[ToolCallEvent]] | Error:
        """Inhaltsblöcke in Reihenfolge und Werkzeugaufrufe; ungültiges JSON -> Error."""
        content: list[dict] = []
        calls: list[ToolCallEvent] = []
        for index in sorted(self.blocks):
            block = self.blocks[index]
            kind = block.get("type")
            if kind == "tool_use":
                arguments = parse_tool_arguments(block["_json"], block["name"])
                if isinstance(arguments, Error):
                    return arguments
                calls.append(ToolCallEvent(id=block["id"], name=block["name"], arguments=arguments))
                content.append(
                    {
                        "type": "tool_use",
                        "id": block["id"],
                        "name": block["name"],
                        "input": arguments,
                    }
                )
            elif kind == "text":
                if block["text"]:
                    content.append(block)  # leere Textblöcke lehnt die API ab
            elif kind in _THINKING_BLOCKS:
                content.append(block)
        return content, calls

    def _update_usage(self, usage) -> None:
        if not isinstance(usage, dict):
            return
        for key, value in usage.items():
            if isinstance(value, int) and not isinstance(value, bool):
                self.usage[key] = value

    def _finish(self) -> Iterator[Event]:
        result = self._content()
        if isinstance(result, Error):
            self.finished = True
            self.error_type = "tool_arguments"
            yield result
            return
        content, calls = result
        if not self.emit_tool_calls:
            calls = []
        yield from calls
        if self.usage:
            # Eingabe einschließlich Cache-Schreib- und -Lesetokens.
            tokens_in = (
                self.usage.get("input_tokens", 0)
                + self.usage.get("cache_creation_input_tokens", 0)
                + self.usage.get("cache_read_input_tokens", 0)
            )
            yield Usage(tokens_in=tokens_in, tokens_out=self.usage.get("output_tokens", 0))
        self.finished = True
        state = None
        if any(b.get("type") in _THINKING_BLOCKS for b in content):
            state = {"content": content}
        reason = FINISH_TOOL_CALLS if calls else self.stop_reason
        yield Done(reason, provider_state=state)

    def end_of_stream(self) -> Iterator[Event]:
        """Stream endete ohne ``message_stop``."""
        if self.finished:
            return
        if self.stop_reason:
            # stop_reason kam schon mit message_delta: Antwort ist vollständig.
            yield from self._finish()
        else:
            self.finished = True
            yield Error(MSG_INTERRUPTED, retryable=True)
