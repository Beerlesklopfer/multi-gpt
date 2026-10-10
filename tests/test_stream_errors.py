"""Fehler mitten im Stream (OpenAI-kompatibel): Ursache von LM Studio anzeigen."""

import json

import httpx
import pytest
import respx

from multigpt.chat.models import Provider
from multigpt.chat.providers.base import ChatMessage, Error
from multigpt.chat.providers.openai_compat import (
    MSG_CONTEXT_TOO_SMALL,
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


@pytest.mark.parametrize("message", ["context length exceeded", "boom sk-geheim"])
def test_provider_with_key_stays_generic(message):
    (error,) = run("sk-geheim", message)
    assert error.message == MSG_STREAM_ERROR


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
    return body["tools"][0]["function"]["parameters"]["properties"]["entity"]["pattern"]


def test_local_provider_gets_anchored_patterns():
    # llama.cpp (LM Studio) lehnt unverankerte Muster ab: „Pattern must start with '^'“.
    assert _sent_tool_schema("") == "^.*(?:light\\..+).*$"


def test_cloud_provider_keeps_patterns():
    assert _sent_tool_schema("sk-x") == "light\\..+"
