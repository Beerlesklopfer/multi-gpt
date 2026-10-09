"""Abgelaufene API-Keys: eigene Meldung in Chat, Verbindungsprüfung und Statusanzeige.

Kein Anbieter hat dafür einen eigenen HTTP-Status; erkannt wird über Code und
Text der Fehlerantwort (Gemini: 400 ``API_KEY_INVALID`` + „API key expired“).
HTTP wird mit respx gemockt.
"""

import httpx
import pytest
import respx

from multigpt.chat.models import Provider
from multigpt.chat.providers.anthropic import AnthropicAdapter
from multigpt.chat.providers.base import (
    MSG_KEY_EXPIRED,
    ChatMessage,
    Error,
    ProviderHTTPError,
    http_error_message,
    is_key_expired,
)
from multigpt.chat.providers.google import GoogleAdapter
from multigpt.chat.providers.openai_compat import OpenAICompatAdapter

KEY = "sk-GEHEIM-abgelaufen-1234567890"
MESSAGES = [ChatMessage("user", "Hallo")]

GOOGLE_EXPIRED = {
    "error": {
        "code": 400,
        "message": "API key expired. Please renew the API key.",
        "status": "INVALID_ARGUMENT",
        "details": [
            {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID"}
        ],
    }
}
GOOGLE_INVALID = {
    "error": {
        "code": 400,
        "message": f"API key not valid: {KEY}",
        "status": "INVALID_ARGUMENT",
        "details": [{"reason": "API_KEY_INVALID"}],
    }
}
ANTHROPIC_EXPIRED = {
    "type": "error",
    "error": {"type": "authentication_error", "message": "OAuth token has expired."},
}
OPENAI_EXPIRED = {
    "error": {
        "message": "Your API key has expired.",
        "type": "invalid_request_error",
        "code": "api_key_expired",
    }
}


def json_body(data) -> bytes:
    return httpx.Response(200, json=data).content


@pytest.fixture
def mock():
    with respx.mock(assert_all_called=False) as router:
        yield router


def adapter(cls, kind, base):
    return cls(Provider(name="P", kind=kind, base_url=base, api_key=KEY))


# --- Erkennung ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (400, GOOGLE_EXPIRED, True),
        (401, ANTHROPIC_EXPIRED, True),
        (401, OPENAI_EXPIRED, True),
        (403, {"error": {"code": "API_KEY_EXPIRED"}}, True),
        (401, {"error": "token expired"}, True),
        (400, GOOGLE_INVALID, False),
        (401, {"error": {"code": "invalid_api_key"}}, False),
        # „expired“ ohne Key/Token, oder bei anderen Statuscodes, zählt nicht.
        (400, {"error": {"message": "Cached content has expired."}}, False),
        (503, {"error": {"message": "Deadline expired before operation could complete"}}, False),
        (500, {"error": {"message": "API key expired"}}, False),
    ],
)
def test_is_key_expired(status, body, expected):
    assert is_key_expired(status, json_body(body)) is expected


def test_is_key_expired_ignores_garbage():
    assert is_key_expired(401, b"<html>expired key</html>") is False
    assert is_key_expired(401, b"") is False
    assert is_key_expired(401, b"[1, 2]") is False


def test_http_error_message_expired():
    assert http_error_message(401, json_body(OPENAI_EXPIRED)) == (MSG_KEY_EXPIRED, False)
    message, _ = http_error_message(401)
    assert "API-Key prüfen" in message


# --- Chat-Stream -------------------------------------------------------------


@pytest.mark.parametrize(
    ("cls", "kind", "base", "url", "status", "body"),
    [
        (
            GoogleAdapter,
            Provider.Kind.GOOGLE,
            "http://gemini.test/v1beta",
            "http://gemini.test/v1beta/models/m:streamGenerateContent",
            400,
            GOOGLE_EXPIRED,
        ),
        (
            AnthropicAdapter,
            Provider.Kind.ANTHROPIC,
            "http://anthropic.test/v1",
            "http://anthropic.test/v1/messages",
            401,
            ANTHROPIC_EXPIRED,
        ),
        (
            OpenAICompatAdapter,
            Provider.Kind.OPENAI_COMPAT,
            "http://openai.test/v1",
            "http://openai.test/v1/chat/completions",
            401,
            OPENAI_EXPIRED,
        ),
    ],
)
def test_stream_expired(mock, cls, kind, base, url, status, body):
    mock.post(url__startswith=url).mock(return_value=httpx.Response(status, json=body))
    events = list(adapter(cls, kind, base).stream("m", MESSAGES))
    assert events == [Error(MSG_KEY_EXPIRED, retryable=False)]


def test_stream_google_invalid_key_stays_generic(mock):
    base = "http://gemini.test/v1beta"
    mock.post(url__startswith=f"{base}/models/").mock(
        return_value=httpx.Response(400, json=GOOGLE_INVALID)
    )
    (error,) = adapter(GoogleAdapter, Provider.Kind.GOOGLE, base).stream("m", MESSAGES)
    assert "API-Key prüfen" in error.message
    assert KEY not in error.message


# --- Verbindungsprüfung --------------------------------------------------------


@pytest.mark.parametrize(
    ("cls", "kind", "base", "status", "body"),
    [
        (GoogleAdapter, Provider.Kind.GOOGLE, "http://gemini.test/v1beta", 400, GOOGLE_EXPIRED),
        (
            AnthropicAdapter,
            Provider.Kind.ANTHROPIC,
            "http://anthropic.test/v1",
            401,
            ANTHROPIC_EXPIRED,
        ),
        (
            OpenAICompatAdapter,
            Provider.Kind.OPENAI_COMPAT,
            "http://openai.test/v1",
            401,
            OPENAI_EXPIRED,
        ),
    ],
)
def test_check_expired(mock, cls, kind, base, status, body):
    mock.get(url__startswith=f"{base}/models").mock(return_value=httpx.Response(status, json=body))
    result = adapter(cls, kind, base).check()
    assert result.online is False
    # Gemini meldet Key-Fehler als 400; der Adapter führt sie wie 401.
    assert result.short_error == "API-Key abgelaufen (HTTP 401)"
    assert "renew" not in result.error and "expired" not in result.error


def test_list_models_expired_raises_flagged_error(mock):
    base = "http://openai.test/v1"
    mock.get(f"{base}/models").mock(return_value=httpx.Response(401, json=OPENAI_EXPIRED))
    with pytest.raises(ProviderHTTPError) as info:
        adapter(OpenAICompatAdapter, Provider.Kind.OPENAI_COMPAT, base)._fetch_models(2)
    assert info.value.expired is True
    assert str(info.value) == MSG_KEY_EXPIRED


# --- Bildanfrage (OCR) und Embeddings ----------------------------------------


def test_describe_image_expired(mock):
    base = "http://openai.test/v1"
    mock.post(f"{base}/chat/completions").mock(
        return_value=httpx.Response(401, json=OPENAI_EXPIRED)
    )
    with pytest.raises(ProviderHTTPError) as info:
        adapter(OpenAICompatAdapter, Provider.Kind.OPENAI_COMPAT, base).describe_image(
            "m", b"\x89PNG", "Text?"
        )
    assert str(info.value) == MSG_KEY_EXPIRED
    assert info.value.expired is True


def test_embed_expired(mock):
    base = "http://openai.test/v1"
    mock.post(f"{base}/embeddings").mock(return_value=httpx.Response(401, json=OPENAI_EXPIRED))
    with pytest.raises(ProviderHTTPError) as info:
        adapter(OpenAICompatAdapter, Provider.Kind.OPENAI_COMPAT, base).embed("m", ["a"])
    assert str(info.value) == MSG_KEY_EXPIRED
