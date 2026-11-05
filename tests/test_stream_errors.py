"""Fehler mitten im Stream (OpenAI-kompatibel): Ursache von LM Studio anzeigen."""

import json

import httpx
import respx

from multigpt.chat.models import Provider
from multigpt.chat.providers.base import ChatMessage, Error
from multigpt.chat.providers.openai_compat import (
    MSG_CONTEXT_TOO_SMALL,
    MSG_CONTEXT_TOO_SMALL_CLOUD,
    MSG_STREAM_ERROR,
    OpenAICompatAdapter,
)

BASE = "http://lmstudio.test:1234/v1"
MESSAGES = [ChatMessage("user", "Hallo")]


def sse(*events):
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
    return httpx.Response(200, text=body, headers={"Content-Type": "text/event-stream"})


def run(api_key, error_message):
    adapter = OpenAICompatAdapter(
        Provider(name="LM Studio", kind="openai_compat", base_url=BASE, api_key=api_key)
    )
    with respx.mock() as mock:
        mock.post(f"{BASE}/chat/completions").mock(
            return_value=sse({"error": {"message": error_message, "type": "x"}})
        )
        return list(adapter.stream("m", MESSAGES))


def test_local_context_error_explained():
    (error,) = run("", "Trying to keep the first 5000 tokens when context length is 4096")
    assert isinstance(error, Error)
    assert error.message.startswith(MSG_CONTEXT_TOO_SMALL)
    assert "context length is 4096" in error.message
    assert error.retryable is False


def test_local_other_error_shows_text():
    (error,) = run("", "Error rendering prompt with jinja template")
    assert (
        error.message
        == f"{MSG_STREAM_ERROR} LM Studio meldet: Error rendering prompt with jinja template"
    )
    assert error.retryable is True


def test_provider_with_key_stays_generic():
    (error,) = run("sk-geheim", "boom sk-geheim")
    assert error.message == MSG_STREAM_ERROR


def test_cloud_context_error_recognized_without_its_text():
    # Kontextüberlauf wird am Text erkannt, auch mit Key; gezeigt wird der Text nicht.
    (error,) = run("sk-geheim", "context length exceeded sk-geheim")
    assert error.message == MSG_CONTEXT_TOO_SMALL_CLOUD
    assert "sk-geheim" not in error.message
    assert error.retryable is False


ELIEZER_TEXT = "The request is invalid or does not fit the model's context window"


def _eliezer():
    return OpenAICompatAdapter(
        Provider(
            name="Eliezer",
            kind="openai_compat",
            base_url=BASE,
            api_key="eliezer-geheim-123",
            is_local=True,
        )
    )


def test_eliezer_context_error_http_400_explained():
    """Eliezer (lokal, mit Key): HTTP 400 invalid_request mit Kontext-Text (2026-11-05)."""
    body = {"error": {"message": ELIEZER_TEXT, "type": "invalid_request", "code": None}}
    with respx.mock() as mock:
        mock.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(400, json=body))
        (error,) = list(_eliezer().stream("m", MESSAGES))
    assert error.message.startswith(MSG_CONTEXT_TOO_SMALL)
    assert f"Eliezer meldet: {ELIEZER_TEXT}" in error.message
    assert "LM Studio" not in error.message
    assert error.retryable is False
    assert error.detail["status"] == 400


def test_eliezer_stream_error_shows_redacted_text():
    adapter = _eliezer()
    with respx.mock() as mock:
        mock.post(f"{BASE}/chat/completions").mock(
            return_value=sse({"error": {"message": "boom eliezer-geheim-123", "type": "x"}})
        )
        (error,) = list(adapter.stream("m", MESSAGES))
    assert error.message.startswith(f"{MSG_STREAM_ERROR} Eliezer meldet: boom")
    assert "eliezer-geheim-123" not in error.message


def _sent_tool_schema(api_key):
    from multigpt.chat.providers.base import ToolSpec

    spec = ToolSpec(
        "set_light",
        "Licht schalten",
        {"type": "object", "properties": {"entity": {"type": "string", "pattern": "light\\..+"}}},
    )
    adapter = OpenAICompatAdapter(
        Provider(name="P", kind="openai_compat", base_url=BASE, api_key=api_key)
    )
    with respx.mock() as mock:
        route = mock.post(f"{BASE}/chat/completions").mock(return_value=sse())
        list(adapter.stream("m", MESSAGES, tools=[spec]))
    body = json.loads(route.calls[0].request.content)
    return body["tools"][0]["function"]["parameters"]["properties"]["entity"].get("pattern")


def test_local_provider_gets_no_patterns():
    # llama.cpp (LM Studio) versteht nur einen Teil der Regex-Syntax: Muster weglassen.
    assert _sent_tool_schema("") is None


def test_cloud_provider_keeps_patterns():
    assert _sent_tool_schema("sk-x") == "light\\..+"


def test_grammar_safe_schema_drops_huge_limits():
    from multigpt.chat.providers.openai_compat import anchored_patterns

    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "code": {"type": "string", "maxLength": 300000, "minLength": 1},
            "tags": {"type": "array", "maxItems": 5000, "minItems": 1},
            "pattern": {"type": "string", "maxLength": 50},
        },
    }
    safe = anchored_patterns(schema)
    assert "$schema" not in safe
    assert safe["properties"]["code"] == {"type": "string", "minLength": 1}
    assert safe["properties"]["tags"] == {"type": "array", "minItems": 1}
    # Ein Parameter namens „pattern“ bleibt (Wert ist ein Schema, kein Muster).
    assert safe["properties"]["pattern"] == {"type": "string", "maxLength": 50}
