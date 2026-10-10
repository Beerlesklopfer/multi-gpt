"""Google-Adapter für die Gemini-API (Plan Abschnitt 7, M4-01).

Grundlage (gelesen 2026-10-09):

- ``POST /v1beta/models/{model}:streamGenerateContent?alt=sse``; der API-Key
  steht im Header ``x-goog-api-key`` (nie als ``?key=`` in der URL, sonst
  landete er in Logs). Jedes SSE-``data:`` ist eine vollständige
  ``GenerateContentResponse``; ein ``[DONE]`` gibt es nicht.
- Anfrage: ``contents[]`` mit ``role`` ``user``/``model`` und ``parts[].text``;
  System-Prompt als ``systemInstruction`` (Content, nur Text);
  ``generationConfig`` u. a. mit ``maxOutputTokens``, ``temperature``,
  ``topP``, ``topK``, ``stopSequences``.
- Antwort: ``candidates[].content.parts[]`` (``text``; ``thought: true`` bei
  Gedanken-Zusammenfassungen; ``functionCall`` mit ``name``/``args``),
  ``candidates[].finishReason`` (``STOP``, ``MAX_TOKENS``, ``SAFETY`` …),
  ``promptFeedback.blockReason`` bei blockierter Anfrage, ``usageMetadata``
  mit ``promptTokenCount`` (inkl. ``cachedContentTokenCount``),
  ``candidatesTokenCount``, ``thoughtsTokenCount``, ``toolUsePromptTokenCount``.
- Fehler: HTTP-Status mit ``{"error": {"code", "message", "status",
  "details"}}``; ein ungültiger Key kommt als 400 mit ``reason``
  ``API_KEY_INVALID``. Mitten im Stream kann ein Chunk mit ``error`` kommen.
- ``GET /v1beta/models``: ``{"models": [{"name": "models/…", …}],
  "nextPageToken"}``, ``pageSize`` bis 1000.

Werkzeuge (M4a, gelesen 2026-10-09; ai.google.dev/api/generate-content,
ai.google.dev/api/caching (ToolConfig), ai.google.dev/gemini-api/docs/
generate-content/thought-signatures):

- ``tools: [{"functionDeclarations": [{name, description,
  parametersJsonSchema}]}]`` – ``parametersJsonSchema`` nimmt volles
  JSON-Schema (MCP-Schemata); ``parameters`` nur eine OpenAPI-Teilmenge.
  ``toolConfig.functionCallingConfig.mode`` ``AUTO``/``ANY``/``NONE``
  (``VALIDATED``), ``allowedFunctionNames`` nur mit ``ANY``/``VALIDATED``.
- Antwort: ``functionCall``-Teile ``{id?, name, args}``; ``id`` ist
  optional und fehlt oft – der Adapter vergibt dann eine eigene und sendet
  sie nicht an Gemini zurück. Kein eigener ``finishReason`` für
  Werkzeugaufrufe (``STOP``); ``MALFORMED_FUNCTION_CALL`` bei ungültigem
  Aufruf, ``MISSING_THOUGHT_SIGNATURE`` bei fehlender Signatur.
- Rückweg: Modellantwort mit allen Teilen unverändert (``thoughtSignature``
  genau im Teil, in dem sie kam; bei parallelen Aufrufen nur am ersten
  ``functionCall``; ohne Signatur am ersten Aufruf eines Schritts HTTP 400),
  dann *ein* ``user``-Content mit allen ``functionResponse``-Teilen
  ``{name, response, id?}`` (``response`` ist ein Objekt, Fehler unter
  ``error``). Verschachteln (FC1, FR1, FC2, FR2) ergibt HTTP 400. Für
  rekonstruierte Aufrufe ohne Signatur nennt die Doku den Platzhalter
  ``skip_thought_signature_validator``.

Bilderzeugung (M9-01, gelesen 2026-10-10; ai.google.dev/api/generate-content,
ai.google.dev/gemini-api/docs/image-generation): ``generateContent`` mit
``generationConfig.responseModalities`` ``["TEXT", "IMAGE"]`` und
``imageConfig.aspectRatio`` (z. B. ``1:1``, ``2:3``, ``3:2``). Bilder kommen als
Teile mit ``inlineData`` (``mimeType``, base64 ``data``); Gedanken-Teile
(``thought: true``) werden übergangen. Sperren: ``promptFeedback.blockReason``
oder ``finishReason`` ``IMAGE_SAFETY``, ``IMAGE_PROHIBITED_CONTENT``,
``SAFETY`` u. a.; ``NO_IMAGE`` bzw. kein Bildteil -> „kein Bild geliefert“.
Tokens aus ``usageMetadata`` (Bildausgabe zählt in ``candidatesTokenCount``).
Bildmodelle laut Doku z. B. ``gemini-2.5-flash-image``, ``gemini-3-pro-image``,
``gemini-3.1-flash-image``; Imagen ist abgeschaltet. Der Leitfaden zeigt
inzwischen die Interactions-API (``response_format`` mit ``aspect_ratio``).

Hinweis: Google stellt daneben die neuere Interactions-API vor; dieser
Adapter nutzt bewusst die etablierte ``generateContent``-Schnittstelle.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Iterator
from urllib.parse import quote

import httpx

from .base import (
    FINISH_TOOL_CALLS,
    MSG_IMAGE_TOO_LARGE,
    MSG_INTERRUPTED,
    MSG_INVALID_MODEL_LIST,
    MSG_KEY_EXPIRED,
    MSG_NO_IMAGE,
    MSG_STREAM_ERROR,
    MSG_TOOL_ARGUMENTS,
    ChatMessage,
    ContentBlocked,
    Delta,
    Done,
    Error,
    Event,
    GeneratedImage,
    ImageResult,
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
    is_key_expired,
    is_unreachable,
    new_tool_call_id,
    normalize_tools,
    orientation,
    provider_error_code,
    rejected_parameter,
    sse_data,
    tool_schema,
)
from .openai_compat import (
    IMAGE_MAX_RESPONSE_BYTES,
    IMAGE_TIMEOUT,
    LIST_TIMEOUT,
    MSG_EMPTY_IMAGE_PROMPT,
    MSG_INVALID_IMAGE_ANSWER,
    STREAM_TIMEOUT,
)

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
MODELS_PAGE_SIZE = 1000
MAX_MODEL_PAGES = 20

# Allgemeine Parameternamen -> Feld in generationConfig.
_GENERATION_PARAMS = {
    "max_tokens": "maxOutputTokens",
    "max_output_tokens": "maxOutputTokens",
    "maxOutputTokens": "maxOutputTokens",
    "temperature": "temperature",
    "top_p": "topP",
    "topP": "topP",
    "top_k": "topK",
    "topK": "topK",
    "stop": "stopSequences",
    "stop_sequences": "stopSequences",
    "stopSequences": "stopSequences",
}
_RESERVED_PARAMS = {
    "model",
    "contents",
    "systemInstruction",
    "system_instruction",
    "tools",
    "toolConfig",
    "tool_choice",
    "stream",
}
_RETRYABLE_STATUSES = {"RESOURCE_EXHAUSTED", "UNAVAILABLE", "INTERNAL", "DEADLINE_EXCEEDED"}

MSG_BLOCKED = "Der Anbieter hat die Anfrage aus Sicherheitsgründen blockiert."
MSG_MALFORMED_CALL = "Das Modell hat einen ungültigen Werkzeugaufruf erzeugt."
MSG_MISSING_SIGNATURE = (
    "Der Anbieter hat den Verlauf abgelehnt, weil eine Denk-Signatur fehlt "
    "(Werkzeugaufruf ohne thoughtSignature)."
)
# finishReason (klein geschrieben) -> Fehler statt Done.
_FINISH_ERRORS = {
    "malformed_function_call": MSG_MALFORMED_CALL,
    "unexpected_tool_call": MSG_MALFORMED_CALL,
    "missing_thought_signature": MSG_MISSING_SIGNATURE,
}
# Bilderzeugung (M9-01): Format -> ``imageConfig.aspectRatio``; finishReason
# bzw. blockReason, die eine Sperre durch den Inhaltsfilter bedeuten.
_ASPECT_RATIOS = {"square": "1:1", "portrait": "2:3", "landscape": "3:2"}
_IMAGE_BLOCKED = {
    "SAFETY",
    "PROHIBITED_CONTENT",
    "BLOCKLIST",
    "SPII",
    "RECITATION",
    "IMAGE_SAFETY",
    "IMAGE_PROHIBITED_CONTENT",
    "IMAGE_RECITATION",
}
# Platzhalter laut Doku für Aufrufe, deren Signatur nicht mehr vorliegt.
DUMMY_THOUGHT_SIGNATURE = "skip_thought_signature_validator"


def _parse_error(body: bytes) -> tuple[str, str]:
    """``status`` und ``reason`` aus einem Fehler-JSON (nie die Meldung)."""
    try:
        err = json.loads(body).get("error") or {}
    except (ValueError, AttributeError):
        return "-", "-"
    if not isinstance(err, dict):
        return "-", "-"
    reason = "-"
    for detail in err.get("details") or []:
        if isinstance(detail, dict) and detail.get("reason"):
            reason = str(detail["reason"])
            break
    return str(err.get("status") or "-"), reason


def _http_error(status: int, reason: str, body: bytes = b"") -> tuple[str, bool]:
    if is_key_expired(status, body):
        # Abgelaufener Key: 400 mit ``API_KEY_INVALID`` und Text „API key expired“.
        return MSG_KEY_EXPIRED, False
    if status == 400 and reason == "API_KEY_INVALID":
        # Gemini meldet einen falschen Key als 400, nicht 401.
        return http_error_message(401)
    return http_error_message(status)


def _model_path(model_id: str) -> str:
    return quote(model_id.removeprefix("models/"), safe="-._~")


class GoogleAdapter(ProviderAdapter):
    @property
    def base_url(self) -> str:
        return (self.provider.base_url or DEFAULT_BASE_URL).rstrip("/")

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        api_key = (self.provider.api_key or "").strip()
        if api_key:
            headers["x-goog-api-key"] = api_key
        return headers

    def _log(self, what: str, detail: str) -> None:
        # Nur Anbieter-ID und technische Kurzinfo, keine Keys oder Inhalte.
        logger.warning("Anbieter %s: %s (%s)", self.provider.pk, what, detail)

    # --- Modellliste ---------------------------------------------------------

    def _fetch_models(self, timeout: httpx.Timeout | float) -> list[str]:
        ids: list[str] = []
        query: dict[str, str | int] = {"pageSize": MODELS_PAGE_SIZE}
        with httpx.Client(timeout=timeout) as client:
            for _ in range(MAX_MODEL_PAGES):
                response = client.get(
                    f"{self.base_url}/models", params=query, headers=self._headers()
                )
                if response.status_code != 200:
                    _, reason = _parse_error(response.content)
                    message, _ = _http_error(response.status_code, reason, response.content)
                    status = response.status_code
                    if status == 400 and reason == "API_KEY_INVALID":
                        status = 401  # falscher Key, siehe _http_error
                    raise ProviderHTTPError(
                        message,
                        status,
                        code=provider_error_code(response.content),
                        expired=is_key_expired(response.status_code, response.content),
                    )
                try:
                    page = response.json()
                    for item in page.get("models") or []:
                        if isinstance(item, dict) and item.get("name"):
                            ids.append(str(item["name"]).removeprefix("models/"))
                    token = page.get("nextPageToken")
                except (ValueError, AttributeError, TypeError) as exc:
                    raise ProviderError(MSG_INVALID_MODEL_LIST) from exc
                if not token:
                    break
                query["pageToken"] = str(token)
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

    # --- Bilderzeugung (M9-01) ------------------------------------------------

    def generate_image(self, model_id: str, prompt: str, **params) -> ImageResult:
        """Gemini-Bildmodell über ``generateContent`` mit Bildausgabe (siehe
        ``_image_body``); ein Bild je Anfrage, ``quality``/``background`` entfallen."""
        if not (prompt or "").strip():
            raise ProviderError(MSG_EMPTY_IMAGE_PROMPT)
        url = f"{self.base_url}/models/{_model_path(model_id)}:generateContent"
        try:
            with httpx.Client(timeout=IMAGE_TIMEOUT) as client:
                response = client.post(
                    url, headers=self._headers(), json=_image_body(prompt, params)
                )
        except Exception as exc:
            self._log("Bilderzeugung", type(exc).__name__)
            error = exception_to_error(exc, started=False)
            raise ProviderError(
                error.message, retryable=error.retryable, unreachable=is_unreachable(exc)
            ) from None
        if response.status_code != 200:
            status, reason = _parse_error(response.content)
            self._log(f"Bilderzeugung HTTP {response.status_code}", f"{status}/{reason}")
            message, retryable = _http_error(response.status_code, reason, response.content)
            raise ProviderHTTPError(
                message,
                response.status_code,
                retryable=retryable or status in _RETRYABLE_STATUSES,
                expired=is_key_expired(response.status_code, response.content),
            )
        if len(response.content) > IMAGE_MAX_RESPONSE_BYTES:
            raise ProviderError(MSG_IMAGE_TOO_LARGE)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(MSG_INVALID_IMAGE_ANSWER) from exc
        return _parse_image_answer(payload)

    # --- Streaming -----------------------------------------------------------

    def _build_body(
        self,
        messages: list[ChatMessage],
        system: str | None,
        tools: list[ToolSpec],
        params: dict,
    ) -> dict:
        turns, system_parts = group_turns(messages)
        if system and system.strip():
            system_parts.insert(0, system.strip())
        generation = dict(params.get("generationConfig") or params.get("generation_config") or {})
        body: dict = {}
        for key, value in params.items():
            if value is None or key in _RESERVED_PARAMS:
                continue
            if key in ("generationConfig", "generation_config"):
                continue
            if key in _GENERATION_PARAMS:
                if key in ("stop", "stop_sequences") and isinstance(value, str):
                    value = [value]
                generation[_GENERATION_PARAMS[key]] = value
            else:
                body[key] = value
        calls = {
            call.id: call
            for message in messages
            if message.role == "assistant"
            for call in message.tool_calls
        }
        body["contents"] = [_turn_payload(t, calls) for t in turns]
        if system_parts:
            body["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
        if generation:
            body["generationConfig"] = generation
        if tools:
            body["tools"] = [{"functionDeclarations": [_declaration(t) for t in tools]}]
            config = _tool_config(params.get("tool_choice"))
            if config is not None:
                body["toolConfig"] = config
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
            body = self._build_body(messages, system, specs, params)
            url = f"{self.base_url}/models/{_model_path(model_id)}:streamGenerateContent"
            with (
                httpx.Client(timeout=STREAM_TIMEOUT) as client,
                client.stream(
                    "POST",
                    url,
                    params={"alt": "sse"},
                    headers={**self._headers(), "Accept": "text/event-stream"},
                    json=body,
                ) as response,
            ):
                if response.status_code != 200:
                    try:
                        error_body = response.read()
                    except Exception:
                        error_body = b""
                    status, reason = _parse_error(error_body)
                    self._log(f"HTTP {response.status_code}", f"{status}/{reason}")
                    message, retryable = _http_error(response.status_code, reason, error_body)
                    rejected = rejected_parameter(response.status_code, error_body)
                    yield Error(message, retryable=retryable, rejected_param=rejected)
                    return
                parser = _ChunkParser(emit_tool_calls=bool(specs))
                for line in response.iter_lines():
                    for event in parser.feed(line):
                        if isinstance(event, Delta):
                            started = True
                        elif isinstance(event, Error):
                            self._log("Fehler im Stream", parser.error_status)
                        yield event
                    if parser.finished:
                        return
                yield from parser.end_of_stream()
        except Exception as exc:
            self._log("Stream", type(exc).__name__)
            yield exception_to_error(exc, started=started)


def _image_body(prompt: str, params: dict) -> dict:
    """Nur Text hinein, Text und Bild heraus; Format als Seitenverhältnis."""
    aspect = _ASPECT_RATIOS[orientation(params.get("size") or "")]
    return {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"],
            "imageConfig": {"aspectRatio": aspect},
        },
    }


def _parse_image_answer(payload) -> ImageResult:
    """Bilder aus ``candidates[0].content.parts[].inlineData``; Sperren aus
    ``promptFeedback.blockReason`` bzw. ``finishReason`` -> ``ContentBlocked``."""
    if not isinstance(payload, dict):
        raise ProviderError(MSG_INVALID_IMAGE_ANSWER)
    feedback = payload.get("promptFeedback")
    if isinstance(feedback, dict) and feedback.get("blockReason"):
        raise ContentBlocked()
    candidates = payload.get("candidates")
    candidate = candidates[0] if isinstance(candidates, list) and candidates else {}
    if not isinstance(candidate, dict):
        raise ProviderError(MSG_INVALID_IMAGE_ANSWER)
    finish = str(candidate.get("finishReason") or "").upper()
    if finish in _IMAGE_BLOCKED:
        raise ContentBlocked()
    images = []
    content = candidate.get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    for part in parts if isinstance(parts, list) else []:
        if not isinstance(part, dict) or part.get("thought"):
            continue  # Gedanken-Bilder (Entwürfe) nicht übernehmen
        inline = part.get("inlineData") or part.get("inline_data")
        if not isinstance(inline, dict):
            continue
        mime_type = str(inline.get("mimeType") or inline.get("mime_type") or "")
        data = inline.get("data")
        if not mime_type.startswith("image/") or not isinstance(data, str):
            continue
        try:
            images.append(GeneratedImage(base64.b64decode(data, validate=True), mime_type))
        except (ValueError, TypeError) as exc:
            raise ProviderError(MSG_INVALID_IMAGE_ANSWER) from exc
    if not images:
        raise ProviderError(MSG_NO_IMAGE)
    meta = payload.get("usageMetadata")
    usage = None
    if isinstance(meta, dict):

        def count(key: str) -> int:
            value = meta.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) else 0

        usage = Usage(
            tokens_in=count("promptTokenCount"),
            tokens_out=count("candidatesTokenCount") + count("thoughtsTokenCount"),
            reasoning=count("thoughtsTokenCount"),
        )
    return ImageResult(images, usage)


def _declaration(tool: ToolSpec) -> dict:
    declaration = {"name": tool.name, "parametersJsonSchema": tool_schema(tool)}
    if tool.description:
        declaration["description"] = tool.description
    return declaration


def _tool_config(choice) -> dict | None:
    if choice is None or isinstance(choice, dict):
        return choice
    choice = str(choice)
    modes = {"auto": "AUTO", "none": "NONE", "required": "ANY"}
    if choice in modes:
        return {"functionCallingConfig": {"mode": modes[choice]}}
    return {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": [choice]}}


def _native_id(call: ToolCallEvent | None) -> str | None:
    """Die von Gemini vergebene ID, None bei selbst vergebener."""
    if call is None or (call.provider_state or {}).get("id_generated"):
        return None
    return call.id or None


def _model_parts(message: ChatMessage) -> list[dict]:
    state = message.provider_state or {}
    if isinstance(state.get("parts"), list):
        # Wörtlich wie empfangen (thoughtSignature im jeweiligen Teil).
        return list(state["parts"])
    parts: list[dict] = []
    if (message.content or "").strip():
        parts.append({"text": message.content})
    signed = any((c.provider_state or {}).get("thought_signature") for c in message.tool_calls)
    for position, call in enumerate(message.tool_calls):
        function_call: dict = {"name": call.name, "args": call.arguments}
        native_id = _native_id(call)
        if native_id:
            function_call["id"] = native_id
        part: dict = {"functionCall": function_call}
        signature = (call.provider_state or {}).get("thought_signature")
        if signature:
            part["thoughtSignature"] = signature
        elif position == 0 and not signed:
            # Signatur verloren (z. B. Verlauf ohne provider_state): Platzhalter
            # laut Doku, sonst lehnen Gemini-3-Modelle mit HTTP 400 ab.
            part["thoughtSignature"] = DUMMY_THOUGHT_SIGNATURE
        parts.append(part)
    return parts


def _turn_payload(turn: Turn, calls: dict[str, ToolCallEvent]) -> dict:
    if turn.role == "assistant":
        parts: list[dict] = []
        for message in turn.messages:
            parts += _model_parts(message)
        return {"role": "model", "parts": parts}
    parts = []
    # Alle Ergebnisse eines Schritts in einem Content, vor dem Text.
    for result in turn.tool_results:
        call = calls.get(result.tool_call_id or "")
        response: dict = {
            "name": result.name or (call.name if call else ""),
            "response": {"error" if result.is_error else "result": result.content or ""},
        }
        native_id = _native_id(call)
        if native_id:
            response["id"] = native_id
        parts.append({"functionResponse": response})
    for image in turn.images:
        parts.append({"inlineData": {"mimeType": image.mime_type, "data": image.base64()}})
    if turn.text:
        parts.append({"text": turn.text})
    return {"role": "user", "parts": parts}


class _ChunkParser:
    """Zerlegt die SSE-Zeilen von ``streamGenerateContent`` in Events.

    Weil Gemini kein ``[DONE]`` sendet, endet die Antwort regulär erst mit dem
    Ende des HTTP-Streams; maßgeblich ist, ob ein ``finishReason`` kam.
    ``functionCall``-Teile werden gesammelt und (nur mit ``emit_tool_calls``)
    am Ende als ``ToolCallEvent`` ausgegeben. Alle Teile der Antwort werden
    für ``Done.provider_state`` mitgeschrieben; ausgegeben werden sie
    nur, wenn Signaturen dabei sind.
    """

    def __init__(self, emit_tool_calls: bool = False):
        self.emit_tool_calls = emit_tool_calls
        self.finished = False
        self.finish_reason: str | None = None
        self.error_status = "-"
        self.usage: Usage | None = None
        self.tool_calls: list[ToolCallEvent] = []
        self.parts: list[dict] = []
        self.invalid_call: str | None = None

    def feed(self, line: str) -> Iterator[Event]:
        data = sse_data(line)
        if data is None or data.strip() == "[DONE]":
            return
        try:
            chunk = json.loads(data)
        except ValueError:
            logger.warning("Ungültige Zeile im Anbieter-Stream übersprungen")
            return
        if not isinstance(chunk, dict):
            return
        err = chunk.get("error")
        if err:
            self.error_status = str(err.get("status") or "-") if isinstance(err, dict) else "-"
            self.finished = True
            yield Error(MSG_STREAM_ERROR, retryable=self.error_status in _RETRYABLE_STATUSES)
            return
        self._update_usage(chunk.get("usageMetadata"))
        feedback = chunk.get("promptFeedback") or {}
        if isinstance(feedback, dict) and feedback.get("blockReason"):
            self.error_status = str(feedback["blockReason"])
            self.finished = True
            yield Error(MSG_BLOCKED, retryable=False)
            return
        for candidate in chunk.get("candidates") or []:
            if not isinstance(candidate, dict) or candidate.get("index", 0) != 0:
                continue
            content = candidate.get("content") or {}
            parts = content.get("parts") if isinstance(content, dict) else None
            for part in parts or []:
                if not isinstance(part, dict):
                    continue
                self._keep_part(part)
                if part.get("thought"):
                    continue  # Gedanken-Zusammenfassungen nicht als Antworttext
                if part.get("text"):
                    yield Delta(str(part["text"]))
                call = part.get("functionCall")
                if isinstance(call, dict):
                    self._collect_call(call, part.get("thoughtSignature"))
            if candidate.get("finishReason"):
                self.finish_reason = str(candidate["finishReason"]).lower()

    def _keep_part(self, part: dict) -> None:
        """Teil für den Rückweg merken; Textstücke ohne Signatur zusammenfassen."""
        previous = self.parts[-1] if self.parts else None
        mergeable = (
            previous is not None
            and set(part) <= {"text", "thought"}
            and set(previous) <= {"text", "thought"}
            and bool(part.get("thought")) == bool(previous.get("thought"))
        )
        if mergeable:
            previous["text"] = str(previous.get("text") or "") + str(part.get("text") or "")
        else:
            self.parts.append(dict(part))

    def _collect_call(self, call: dict, signature) -> None:
        name = str(call.get("name") or "")
        args = call.get("args")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            self.invalid_call = self.invalid_call or name
            return
        state: dict = {}
        call_id = str(call.get("id") or "")
        if not call_id:
            call_id = new_tool_call_id()
            state["id_generated"] = True
        if signature:
            state["thought_signature"] = str(signature)
        self.tool_calls.append(
            ToolCallEvent(id=call_id, name=name, arguments=args, provider_state=state or None)
        )

    def _update_usage(self, meta) -> None:
        if not isinstance(meta, dict):
            return

        def count(key: str) -> int:
            value = meta.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) else 0

        # Nachdenk-Tokens werden als Ausgabe abgerechnet. promptTokenCount
        # enthält die gecachten Tokens (cachedContentTokenCount).
        tokens_in = count("promptTokenCount") + count("toolUsePromptTokenCount")
        self.usage = Usage(
            tokens_in=tokens_in,
            tokens_out=count("candidatesTokenCount") + count("thoughtsTokenCount"),
            cached_read=min(count("cachedContentTokenCount"), tokens_in),
            reasoning=count("thoughtsTokenCount"),
        )

    def end_of_stream(self) -> Iterator[Event]:
        """Ende des HTTP-Streams: mit ``finishReason`` vollständig, sonst abgerissen."""
        if self.finished:
            return
        self.finished = True
        if not self.finish_reason:
            yield Error(MSG_INTERRUPTED, retryable=True)
            return
        if self.emit_tool_calls and self.invalid_call is not None:
            self.error_status = "tool_arguments"
            yield Error(MSG_TOOL_ARGUMENTS.format(name=self.invalid_call or "?"), retryable=True)
            return
        if self.finish_reason in _FINISH_ERRORS:
            self.error_status = self.finish_reason
            yield Error(_FINISH_ERRORS[self.finish_reason], retryable=True)
            return
        calls = self.tool_calls if self.emit_tool_calls else []
        yield from calls
        if self.usage:
            yield self.usage
        state = None
        if any("thoughtSignature" in p for p in self.parts):
            state = {"parts": self.parts}
        yield Done(FINISH_TOOL_CALLS if calls else self.finish_reason, provider_state=state)
