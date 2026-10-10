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

Embeddings (M7, gelesen 2026-10-09, developers.openai.com/api/docs/api-reference/embeddings
und …/guides/embeddings): ``POST /embeddings`` mit ``model``, ``input`` (Liste
von Strings, keine leeren; je Eingabe höchstens 8192 Tokens, je Anfrage
höchstens 2048 Eingaben und 300 000 Tokens), optional ``dimensions`` (nur
text-embedding-3 und neuer, kürzt den Vektor und behält die Normierung) und
``encoding_format`` ``float``. Antwort ``data[]`` mit ``embedding`` und
``index``, dazu ``usage.prompt_tokens``.

Bilderzeugung (M9-01, gelesen 2026-10-10,
developers.openai.com/api/docs/api-reference/images/create):
``POST /images/generations`` mit ``model``, ``prompt``, ``n`` (1–10), ``size``
(``1024x1024``, ``1536x1024``, ``1024x1536``, ``auto``; gpt-image-2 und
neuer auch freie ``WxH``), ``quality`` (``low``/``medium``/``high``/``auto``),
``background`` (``transparent`` nur mit ``output_format`` png/webp),
``output_format`` (png/jpeg/webp), ``moderation`` (auto/low). GPT-Image-Modelle
liefern immer ``data[].b64_json``; ``response_format`` (url/b64_json) gilt nur
für DALL·E. Antwort zusätzlich ``usage`` mit ``input_tokens``,
``output_tokens``, ``input_tokens_details`` (text/image). Ablehnung durch den
Inhaltsfilter: HTTP 400 mit ``code`` ``moderation_blocked`` (Typ
``image_generation_user_error``; DALL·E: ``content_policy_violation``) –
eigene Meldung, nie der Text des Anbieters.

Sicherheit (Plan 9): Der API-Key steht nur im Authorization-Header. Er
erscheint weder in Logs noch in Fehlermeldungen; Fehlertexte der Anbieter
werden nicht weitergereicht (OpenAI nennt bei 401 Teile des Keys).
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Iterator
from datetime import UTC, date, datetime
from urllib.parse import urlsplit

import httpx

from ..capabilities import Detected, from_lmstudio
from .base import (
    FINISH_TOOL_CALLS,
    MSG_IMAGE_TOO_LARGE,
    MSG_INTERRUPTED,
    MSG_INVALID_MODEL_LIST,
    MSG_NO_IMAGE,
    MSG_STREAM_ERROR,
    MSG_TIMEOUT,
    MSG_UNEXPECTED,
    MSG_UNREACHABLE,
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
    Usage,
    exception_to_error,
    http_error_message,
    is_key_expired,
    is_unreachable,
    new_tool_call_id,
    normalize_tools,
    orientation,
    pair_tool_messages,
    parse_tool_arguments,
    provider_error_code,
    rejected_parameter,
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

EMBED_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=60.0, pool=10.0)
# Teilanfragen für Embeddings: deutlich unter den OpenAI-Grenzen (2048 Eingaben,
# 300 000 Tokens). Zeichen als grobe Obergrenze für Tokens (≈ 3–4 Zeichen je Token).
EMBED_BATCH_SIZE = 128
EMBED_BATCH_CHARS = 400_000
MSG_INVALID_EMBEDDINGS = "Der Anbieter hat unerwartete Embeddings geliefert."
MSG_EMPTY_EMBED_INPUT = "Leere Texte lassen sich nicht einbetten."

# Bildanfragen (OCR): ein 7B-Vision-Modell braucht für eine volle Seite leicht
# eine Minute oder mehr, beim ersten Aufruf lädt LM Studio das Modell noch.
VISION_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0)
MSG_EMPTY_IMAGE = "Es wurde kein Bild übergeben."
MSG_INVALID_VISION_ANSWER = (
    "Der Anbieter hat eine unerwartete Antwort auf die Bildanfrage geliefert."
)
MSG_INVALID_IMAGE_ANSWER = MSG_INVALID_VISION_ANSWER
MSG_VISION_TRUNCATED = "Die Antwort des Modells wurde wegen der Längenbegrenzung abgeschnitten."

# Bilderzeugung (M9-01): GPT Image braucht für große Bilder in hoher Qualität
# leicht ein bis zwei Minuten. Antwort mit base64 (1536x1024 PNG ≈ 3–4 MB).
IMAGE_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)
IMAGE_MAX_RESPONSE_BYTES = 80 * 1024 * 1024
IMAGE_QUALITIES = ("low", "medium", "high", "auto")
_DALLE3_SIZES = {"square": "1024x1024", "landscape": "1792x1024", "portrait": "1024x1792"}
MSG_EMPTY_IMAGE_PROMPT = "Bitte beschreiben, welches Bild erzeugt werden soll."

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


def _provider_text(body: bytes) -> str:
    """Fehlertext aus der Antwort, gekürzt und ohne Steuerzeichen.

    Nur für Anbieter ohne API-Key (z. B. LM Studio) verwenden – bei Cloud-Anbietern
    kann der Text Teile des Keys enthalten.
    """
    try:
        err = json.loads(body).get("error")
    except (ValueError, AttributeError, TypeError):
        return ""
    if isinstance(err, dict):
        err = err.get("message")
    if not isinstance(err, str):
        return ""
    text = " ".join(err.split())
    return text[:200] + ("…" if len(text) > 200 else "")


def _model_not_found(model_id: str) -> str:
    return (
        f"Das Modell „{model_id}“ ist beim Anbieter nicht (mehr) verfügbar – es wurde "
        "abgekündigt oder für diesen Zugang nicht freigeschaltet. Bitte ein anderes "
        "Modell wählen."
    )


def _is_shut_down(item: dict, today: date) -> bool:
    """OpenAI führt abgekündigte Modelle mit ``shutdown_date`` weiter in ``/models``."""
    value = item.get("shutdown_date")
    if not value:
        return False
    try:
        if isinstance(value, int | float):
            shutdown = datetime.fromtimestamp(value, tz=UTC).date()
        else:
            shutdown = date.fromisoformat(str(value)[:10])
    except (ValueError, OverflowError, OSError):
        return False
    return shutdown <= today


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

    def _stream_error(self, parser: _ChunkParser) -> Error:
        """Fehler mitten im Stream: bei Anbietern ohne Key (LM Studio) mit dessen Text."""
        self._log("Fehler im Stream", parser.error_code)
        text = parser.error_text
        if (self.provider.api_key or "").strip() or not text:
            return Error(MSG_STREAM_ERROR, retryable=True)
        if _is_context_error(text):
            return Error(f"{MSG_CONTEXT_TOO_SMALL} LM Studio meldet: {text}", retryable=False)
        return Error(f"{MSG_STREAM_ERROR} LM Studio meldet: {text}", retryable=True)

    # --- Modellliste ---------------------------------------------------------

    def _fetch_models(self, timeout: httpx.Timeout | float) -> list[str]:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(f"{self.base_url}/models", headers=self._headers())
        if response.status_code != 200:
            message, _ = http_error_message(response.status_code, response.content)
            raise ProviderHTTPError(
                message,
                response.status_code,
                code=provider_error_code(response.content),
                expired=is_key_expired(response.status_code, response.content),
            )
        try:
            data = response.json().get("data") or []
            today = datetime.now(UTC).date()
            return [
                str(item["id"])
                for item in data
                if isinstance(item, dict) and "id" in item and not _is_shut_down(item, today)
            ]
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

    def model_capabilities(self, timeout: float = 2) -> dict[str, Detected]:
        """Fähigkeiten, die LM Studio selbst meldet; ``{}``, wenn das nicht geht.

        LM Studio (ab 0.3.16) liefert neben ``/v1/models`` unter ``/api/v0/models``
        je Modell ``type`` (llm, vlm, embeddings) und ``capabilities``
        (z. B. ``["tool_use"]``). Die Adresse liegt an der Server-Wurzel, also
        Basis-URL ohne ``/v1``. Andere Server (Ollama, vLLM) antworten mit 404.
        """
        root = self.base_url.removesuffix("/v1")
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.get(f"{root}/api/v0/models", headers=self._headers())
            if response.status_code != 200:
                return {}
            data = response.json().get("data") or []
        except Exception:
            return {}
        result = {}
        for item in data if isinstance(data, list) else []:
            detected = from_lmstudio(item)
            if detected is not None and item.get("id"):
                result[str(item["id"])] = detected
        return result

    # --- Embeddings (M7) ------------------------------------------------------

    def embed(
        self, model_id: str, texts: list[str], dimensions: int | None = None
    ) -> list[list[float]]:
        """Ein Vektor je Text (gleiche Reihenfolge), in Teilanfragen.

        Fehler als ``ProviderError`` mit deutschem Text und ``retryable``; der
        Fehlertext des Anbieters wird nie weitergereicht (kann Key-Teile enthalten).
        """
        texts = list(texts)
        if not texts:
            return []
        if any(not isinstance(t, str) or not t.strip() for t in texts):
            raise ProviderError(MSG_EMPTY_EMBED_INPUT)
        vectors: list[list[float]] = []
        try:
            with httpx.Client(timeout=EMBED_TIMEOUT) as client:
                for batch in _embed_batches(texts):
                    body = {"model": model_id, "input": batch, "encoding_format": "float"}
                    if dimensions:
                        body["dimensions"] = dimensions
                    response = client.post(
                        f"{self.base_url}/embeddings", headers=self._headers(), json=body
                    )
                    if response.status_code != 200:
                        detail = _error_code(response.content)
                        self._log(f"Embeddings HTTP {response.status_code}", detail)
                        message, retryable = http_error_message(
                            response.status_code, response.content
                        )
                        if response.status_code == 404 and detail.endswith("/model_not_found"):
                            message = _model_not_found(model_id)
                        raise ProviderHTTPError(
                            message,
                            response.status_code,
                            retryable=retryable,
                            expired=is_key_expired(response.status_code, response.content),
                        )
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        raise ProviderError(MSG_INVALID_EMBEDDINGS) from exc
                    vectors += _parse_embeddings(payload, len(batch), dimensions)
        except ProviderError:
            raise
        except Exception as exc:
            self._log("Embeddings", type(exc).__name__)
            error = exception_to_error(exc, started=False)
            raise ProviderError(
                error.message, retryable=error.retryable, unreachable=is_unreachable(exc)
            ) from None
        return vectors

    # --- Bild-Eingabe (OCR, M7-09) ----------------------------------------------

    def describe_image(
        self,
        model_id: str,
        image: bytes,
        prompt: str,
        *,
        mime_type: str = "image/png",
        **params,
    ) -> str:
        """Nicht streamende Chat-Anfrage mit Text und Bild (data-URI).

        Reihenfolge wie in der olmOCR-Pipeline: erst Text, dann Bild. Bricht das
        Modell wegen ``max_tokens`` ab (``finish_reason`` ``length``), gilt die
        Antwort als unvollständig (``ProviderError`` mit ``truncated``).
        """
        if not image:
            raise ProviderError(MSG_EMPTY_IMAGE)
        data_uri = f"data:{mime_type};base64," + base64.b64encode(image).decode("ascii")
        body = {k: v for k, v in params.items() if k not in _RESERVED_PARAMS and v is not None}
        body.update(
            model=model_id,
            stream=False,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            ],
        )
        try:
            with httpx.Client(timeout=VISION_TIMEOUT) as client:
                response = client.post(
                    f"{self.base_url}/chat/completions", headers=self._headers(), json=body
                )
        except Exception as exc:
            self._log("Bildanfrage", type(exc).__name__)
            error = exception_to_error(exc, started=False)
            raise ProviderError(
                error.message, retryable=error.retryable, unreachable=is_unreachable(exc)
            ) from None
        if response.status_code != 200:
            detail = _error_code(response.content)
            self._log(f"Bildanfrage HTTP {response.status_code}", detail)
            message, retryable = http_error_message(response.status_code, response.content)
            expired = is_key_expired(response.status_code, response.content)
            if response.status_code == 404 and detail.endswith("/model_not_found"):
                message = _model_not_found(model_id)
            elif not expired and not (self.provider.api_key or "").strip():
                # Lokaler Anbieter ohne Key: dessen Text nennt die eigentliche Ursache
                # (z. B. LM Studio „Failed to load model …“). Ladefehler sind vorübergehend.
                text = _provider_text(response.content)
                if text:
                    message = f"{message} LM Studio meldet: {text}"
                    retryable = retryable or "load" in text.lower()
            raise ProviderHTTPError(
                message, response.status_code, retryable=retryable, expired=expired
            )
        try:
            choice = response.json()["choices"][0]
            content = choice["message"]["content"]
            finish = choice.get("finish_reason")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ProviderError(MSG_INVALID_VISION_ANSWER) from exc
        if isinstance(content, list):  # Teile-Liste statt Text
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if not isinstance(content, str):
            raise ProviderError(MSG_INVALID_VISION_ANSWER)
        if finish == "length":
            error = ProviderError(MSG_VISION_TRUNCATED, retryable=True)
            error.truncated = True
            raise error
        return content

    # --- Bilderzeugung (M9-01) ------------------------------------------------

    def generate_image(self, model_id: str, prompt: str, **params) -> ImageResult:
        """``POST /images/generations``; Bilder als ``b64_json`` (nie eine URL
        nachladen: kein Abruf fremder Adressen, siehe Moduldoku)."""
        if not (prompt or "").strip():
            raise ProviderError(MSG_EMPTY_IMAGE_PROMPT)
        body = _image_body(model_id, prompt, params)
        try:
            with httpx.Client(timeout=IMAGE_TIMEOUT) as client:
                response = client.post(
                    f"{self.base_url}/images/generations", headers=self._headers(), json=body
                )
        except Exception as exc:
            self._log("Bilderzeugung", type(exc).__name__)
            error = exception_to_error(exc, started=False)
            raise ProviderError(
                error.message, retryable=error.retryable, unreachable=is_unreachable(exc)
            ) from None
        if response.status_code != 200:
            detail = _error_code(response.content)
            self._log(f"Bilderzeugung HTTP {response.status_code}", detail)
            if response.status_code == 400 and _is_moderation(detail):
                raise ContentBlocked()
            message, retryable = http_error_message(response.status_code, response.content)
            if response.status_code == 404 and detail.endswith("/model_not_found"):
                message = _model_not_found(model_id)
            raise ProviderHTTPError(
                message,
                response.status_code,
                retryable=retryable,
                expired=is_key_expired(response.status_code, response.content),
            )
        if len(response.content) > IMAGE_MAX_RESPONSE_BYTES:
            raise ProviderError(MSG_IMAGE_TOO_LARGE)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(MSG_INVALID_IMAGE_ANSWER) from exc
        return _parse_images(payload)

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
                        "parameters": (
                            anchored_patterns(tool_schema(t))
                            if not (self.provider.api_key or "").strip()
                            else tool_schema(t)
                        ),
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
                        error_body = response.read()
                    except Exception:
                        error_body = b""
                    detail = _error_code(error_body)
                    self._log(f"HTTP {response.status_code}", detail)
                    message, retryable = http_error_message(response.status_code, error_body)
                    if response.status_code == 404 and detail.endswith("/model_not_found"):
                        message = _model_not_found(model_id)
                    rejected = rejected_parameter(response.status_code, error_body)
                    yield Error(message, retryable=retryable, rejected_param=rejected)
                    return
                parser = _ChunkParser(emit_tool_calls=bool(specs))
                for line in response.iter_lines():
                    for event in parser.feed(line):
                        if isinstance(event, Delta):
                            started = True
                        elif isinstance(event, Error) and event.message == MSG_STREAM_ERROR:
                            event = self._stream_error(parser)
                        yield event
                    if parser.finished:
                        return
                yield from parser.end_of_stream()
        except Exception as exc:
            self._log("Stream", type(exc).__name__)
            yield exception_to_error(exc, started=started)


MSG_CONTEXT_TOO_SMALL = (
    "Der Verlauf passt nicht in den Kontext des Modells (inklusive Werkzeugbeschreibungen). "
    "In LM Studio das Modell mit größerer Kontextlänge laden (z. B. 16k oder 32k) oder "
    "den Chat kürzen bzw. Werkzeuge abschalten."
)
_CONTEXT_WORDS = (
    "context length",
    "context window",
    "context size",
    "n_ctx",
    "too many tokens",
    "exceeds the context",
    "context overflow",
    "maximum context",
)


def _anchor(pattern: str) -> str:
    """JSON-Schema-``pattern`` gilt unverankert (Suche). llama.cpp (LM Studio) wandelt
    Schemas in eine Grammatik und verlangt ``^…$`` – gleichbedeutend verankern."""
    start = pattern.startswith("^")
    end = pattern.endswith("$") and not pattern.endswith("\\$")
    if start and end:
        return pattern
    if start:
        return f"{pattern}.*$"
    if end:
        return f"^.*{pattern}"
    return f"^.*(?:{pattern}).*$"


def anchored_patterns(schema):
    """Kopie des Schemas mit verankerten ``pattern``-Angaben (für lokale Anbieter)."""
    if isinstance(schema, dict):
        return {
            key: _anchor(value)
            if key == "pattern" and isinstance(value, str)
            else anchored_patterns(value)
            for key, value in schema.items()
        }
    if isinstance(schema, list):
        return [anchored_patterns(item) for item in schema]
    return schema


def _is_context_error(text: str) -> bool:
    lower = text.lower()
    return any(word in lower for word in _CONTEXT_WORDS)


def _is_moderation(detail: str) -> bool:
    """Inhaltsfilter: ``moderation_blocked`` (GPT Image) bzw.
    ``content_policy_violation`` (DALL·E) in ``type/code``."""
    return any(code in detail for code in ("moderation_blocked", "content_policy_violation"))


def _image_body(model_id: str, prompt: str, params: dict) -> dict:
    """Anfrage je Modellfamilie: GPT Image kennt ``quality`` low/medium/high/auto,
    ``background`` und ``output_format``, liefert immer base64; DALL·E braucht
    ``response_format=b64_json`` und hat eigene Größen und Qualitätsstufen."""
    size = str(params.get("size") or "1024x1024")
    quality = str(params.get("quality") or "auto")
    body = {"model": model_id, "prompt": prompt, "n": int(params.get("n") or 1)}
    lowered = model_id.lower()
    if lowered.startswith("dall-e"):
        body["response_format"] = "b64_json"
        if lowered.startswith("dall-e-3"):
            body["size"] = _DALLE3_SIZES[orientation(size)]
            body["quality"] = "hd" if quality == "high" else "standard"
            body["n"] = 1  # DALL·E 3 erzeugt nur ein Bild je Anfrage
        else:
            body["size"] = "1024x1024"
        return body
    body["size"] = size
    if quality in IMAGE_QUALITIES:
        body["quality"] = quality
    background = params.get("background")
    if background in ("transparent", "opaque"):
        body["background"] = background
    # PNG: verlustfrei, mit Transparenz; MultiGPT kodiert ohnehin neu.
    body["output_format"] = "png"
    return body


def _parse_images(payload) -> ImageResult:
    """``data[].b64_json`` (+ ``revised_prompt``), ``usage.input_tokens``/``output_tokens``."""
    try:
        items = payload.get("data")
    except AttributeError as exc:
        raise ProviderError(MSG_INVALID_IMAGE_ANSWER) from exc
    if not isinstance(items, list):
        raise ProviderError(MSG_INVALID_IMAGE_ANSWER)
    fmt = str(payload.get("output_format") or "png").lower()
    mime_type = {"jpeg": "image/jpeg", "jpg": "image/jpeg", "webp": "image/webp"}.get(
        fmt, "image/png"
    )
    images = []
    for item in items:
        data = item.get("b64_json") if isinstance(item, dict) else None
        if not isinstance(data, str) or not data:
            continue  # nur URL: wird bewusst nicht nachgeladen
        try:
            raw = base64.b64decode(data, validate=True)
        except (ValueError, TypeError) as exc:
            raise ProviderError(MSG_INVALID_IMAGE_ANSWER) from exc
        revised = item.get("revised_prompt")
        images.append(GeneratedImage(raw, mime_type, revised if isinstance(revised, str) else ""))
    if not images:
        raise ProviderError(MSG_NO_IMAGE)
    usage = None
    raw_usage = payload.get("usage")
    if isinstance(raw_usage, dict):
        usage = Usage(
            tokens_in=_count(raw_usage, "input_tokens"),
            tokens_out=_count(raw_usage, "output_tokens"),
        )
    return ImageResult(images, usage)


def _embed_batches(texts: list[str]) -> Iterator[list[str]]:
    batch: list[str] = []
    size = 0
    for text in texts:
        if batch and (len(batch) >= EMBED_BATCH_SIZE or size + len(text) > EMBED_BATCH_CHARS):
            yield batch
            batch, size = [], 0
        batch.append(text)
        size += len(text)
    if batch:
        yield batch


def _parse_embeddings(payload, count: int, dimensions: int | None) -> list[list[float]]:
    """``data[]`` nach ``index`` sortiert prüfen; jede Abweichung -> ProviderError."""
    try:
        data = payload["data"]
        if not isinstance(data, list) or len(data) != count:
            raise ValueError
        vectors: list[list[float] | None] = [None] * count
        for item in data:
            index = item.get("index")
            vector = item["embedding"]
            if not isinstance(index, int) or not 0 <= index < count or vectors[index] is not None:
                raise ValueError
            if not isinstance(vector, list) or not vector:
                raise ValueError
            if dimensions is not None and len(vector) != dimensions:
                raise ValueError
            vectors[index] = [float(x) for x in vector]
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ProviderError(MSG_INVALID_EMBEDDINGS) from exc
    return vectors  # type: ignore[return-value]


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
    if message.role == "user" and message.images:
        # Inhaltsteile: Bilder (data-URI, detail Standard „auto“), danach der Text.
        parts: list[dict] = [
            {"type": "image_url", "image_url": {"url": image.data_uri()}}
            for image in message.images
        ]
        if (message.content or "").strip():
            parts.append({"type": "text", "text": message.content})
        return {"role": "user", "content": parts}
    return {"role": message.role, "content": message.content}


def _tool_choice(choice) -> str | dict | None:
    if choice is None or isinstance(choice, dict):
        return choice
    choice = str(choice)
    if choice in ("auto", "none", "required"):
        return choice
    return {"type": "function", "function": {"name": choice}}


def _count(data, key: str) -> int:
    value = data.get(key) if isinstance(data, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def usage_from(usage: dict) -> Usage:
    """``usage`` der Chat Completions -> ``Usage``.

    ``prompt_tokens`` enthält die gecachten Tokens
    (``prompt_tokens_details.cached_tokens``), ``completion_tokens`` die
    Reasoning-Tokens (``completion_tokens_details.reasoning_tokens``). Neuere
    Modelle (ab GPT-5.6) melden zusätzlich Cache-Schreiben
    (``prompt_tokens_details.cache_write_tokens``, Preis 1,25 × Eingabe). LM
    Studio und andere kompatible Server melden oft nur die beiden Summen; dann
    bleiben die Teilmengen 0.
    """
    tokens_in = _count(usage, "prompt_tokens")
    tokens_out = _count(usage, "completion_tokens")
    details = usage.get("prompt_tokens_details")
    cached = min(_count(details, "cached_tokens"), tokens_in)
    return Usage(
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cached_read=cached,
        cache_write=min(_count(details, "cache_write_tokens"), tokens_in - cached),
        reasoning=min(
            _count(usage.get("completion_tokens_details"), "reasoning_tokens"), tokens_out
        ),
    )


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
        # Fehler mitten im Stream: Kurzcode fürs Log, Text nur für Anbieter ohne Key.
        self.error_code = "-"
        self.error_text = ""

    def _remember_error(self, chunk: dict) -> None:
        raw = json.dumps(chunk).encode()
        self.error_code = _error_code(raw)
        self.error_text = _provider_text(raw)

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
            self._remember_error(chunk)
            yield Error(MSG_STREAM_ERROR, retryable=True)
            return
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            self.usage = usage_from(usage)
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
