"""Auswertbares Fehlerdetail: Anbieter (Error.detail), Speicherung an der Message,
Anzeige nur für den Besitzer (Seite, Fragment, API, SSE)."""

# Fixtures werden aus test_api_stream importiert und als Parameter wieder benutzt.
# ruff: noqa: F811

import json
import logging

import httpx
import pytest
import respx
from django.urls import reverse

from multigpt.chat import services
from multigpt.chat.models import Conversation, Message, Provider, Share
from multigpt.chat.providers.base import (
    ChatMessage,
    Delta,
    Error,
    clean_excerpt,
    http_error_detail,
    redact,
)
from multigpt.chat.providers.openai_compat import OpenAICompatAdapter
from tests.test_api_stream import (  # noqa: F401
    OK_EVENTS,
    ai_model,
    fake,
    make_user,
    parse_sse,
    provider,
)

pytestmark = pytest.mark.django_db

BASE = "http://eliezer.test:1234/v1"
KEY = "sk-test-GEHEIM-1234567890"


def run_adapter(api_key=KEY):
    adapter = OpenAICompatAdapter(
        Provider(name="T", kind="openai_compat", base_url=BASE, api_key=api_key)
    )
    return list(adapter.stream("m", [ChatMessage("user", "GEHEIMER PROMPT")]))


# --- Anbieter ---------------------------------------------------------------------


def test_5xx_with_eliezer_type_is_in_detail():
    body = {"error": {"type": "backend_error", "message": "runtime\ncrashed\n  badly"}}
    with respx.mock() as mock:
        mock.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(500, json=body))
        (error,) = run_adapter()
    assert isinstance(error, Error)
    assert error.detail == {
        "status": 500,
        "type": "backend_error",
        "excerpt": "runtime crashed badly",
        "retryable": True,
    }


def test_429_detail_is_retryable():
    body = {"error": {"type": "rate_limit_error", "code": "rate_limit", "message": "slow down"}}
    with respx.mock() as mock:
        mock.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(429, json=body))
        (error,) = run_adapter()
    assert (error.detail["status"], error.detail["type"]) == (429, "rate_limit_error")
    assert error.detail["retryable"] is True


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (httpx.ReadTimeout("t"), "timeout"),
        (httpx.ConnectError("c"), "connect"),
        (httpx.ConnectTimeout("c"), "connect"),
    ],
)
def test_exceptions_get_a_kind(exc, kind):
    with respx.mock() as mock:
        mock.post(f"{BASE}/chat/completions").mock(side_effect=exc)
        (error,) = run_adapter()
    assert error.detail["type"] == kind
    assert error.detail["status"] is None
    assert error.detail["retryable"] is True


def test_keys_and_tokens_are_redacted_and_prompt_never_included():
    body = {
        "error": {
            "type": "invalid_request_error",
            "message": f"Incorrect API key provided: {KEY}. Authorization: Bearer abc.def-123 "
            "key=AIzaSyA1234567890abcdefghij token 0123456789abcdef0123456789abcdef01",
        }
    }
    with respx.mock() as mock:
        mock.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(401, json=body))
        (error,) = run_adapter()
    text = json.dumps(error.detail)
    for secret in (KEY, "abc.def-123", "AIzaSy", "0123456789abcdef0123456789abcdef01"):
        assert secret not in text
    assert "GEHEIMER PROMPT" not in text
    assert "***" in error.detail["excerpt"]


def test_known_key_is_redacted_even_if_unusual():
    assert "Zugang-4711x" not in redact("Fehler bei Zugang-4711x!", ["Zugang-4711x"])
    assert "token limit exceeded" in redact("token limit exceeded")


def test_excerpt_is_shortened():
    detail = http_error_detail(500, json.dumps({"error": {"message": "wort " * 200}}).encode())
    assert len(detail["excerpt"]) <= 301 and detail["excerpt"].endswith("…")
    assert len(clean_excerpt("x " * 500)) <= 301


# --- Speicherung --------------------------------------------------------------------


@pytest.fixture
def owner():
    return make_user("adult", "besitzer")


@pytest.fixture
def chat(owner, ai_model):
    return Conversation.objects.create(user=owner, default_model=ai_model)


def failing_events():
    return [
        Delta("Teil"),
        Error(
            "Der Anbieter hat einen Serverfehler gemeldet.",
            retryable=True,
            detail={"status": 500, "type": "backend_error", "excerpt": "kaputt", "retryable": True},
        ),
    ]


def post_message(client, chat, ai_model, user):
    client.force_login(user)
    return parse_sse(
        client.post(
            reverse("chat:api_messages", args=[chat.pk]),
            json.dumps({"content": "Frage", "model": ai_model.pk}),
            content_type="application/json",
        )
    )


def start_failing(client, chat, ai_model, fake, user):
    fake(failing_events())
    return post_message(client, chat, ai_model, user)


def test_detail_is_stored_streamed_and_logged(client, chat, ai_model, fake, owner, caplog):
    with caplog.at_level(logging.ERROR, logger="multigpt.chat.services"):
        events = start_failing(client, chat, ai_model, fake, owner)
    message = Message.objects.get(pk=events[0][1]["assistant_message_id"])
    detail = message.error_detail
    assert (detail["status"], detail["type"], detail["excerpt"]) == (500, "backend_error", "kaputt")
    assert detail["provider"] == "Cloud" and detail["model"] == "gpt-test"
    assert detail["attempt"] == 1 and detail["retries"] == 0
    assert detail["tools"] == 0 and detail["web_search"] is False
    assert "at" in detail and detail["prompt_tokens_est"] > 0
    error_event = next(data for name, data in events if name == "error")
    assert error_event["detail"] == detail
    log = next(r.getMessage() for r in caplog.records if "Antwortfehler" in r.getMessage())
    assert "status=500" in log and "type=backend_error" in log and "model=gpt-test" in log
    assert "Frage" not in log and "kaputt" not in log


def test_internal_error_gets_a_kind_without_provider_text(client, chat, ai_model, fake, owner):
    fake([Delta("x")], raise_exc=RuntimeError("geheim sk-123"))
    events = post_message(client, chat, ai_model, owner)
    message = Message.objects.get(pk=events[0][1]["assistant_message_id"])
    assert message.error_detail["type"] == "internal"
    assert "sk-123" not in json.dumps(message.error_detail)


def test_success_leaves_detail_empty(client, chat, ai_model, fake, owner):
    fake(OK_EVENTS)
    events = post_message(client, chat, ai_model, owner)
    assert Message.objects.get(pk=events[0][1]["assistant_message_id"]).error_detail == {}


# --- Sichtbarkeit -------------------------------------------------------------------


@pytest.fixture
def failed(chat, owner, ai_model):
    services.append_message(chat, role="user", content="Frage", author=owner)
    return services.append_message(
        chat,
        role="assistant",
        content="",
        model=ai_model,
        author=owner,
        status=Message.Status.ERROR,
        error="Der Anbieter hat einen Serverfehler gemeldet.",
        error_detail={
            "status": 500,
            "type": "backend_error",
            "excerpt": "Speicher voll",
            "provider": "Cloud",
            "model": "gpt-test",
            "prompt_tokens_est": 1200,
            "context_length": 4096,
            "attempt": 2,
            "retries": 1,
            "at": "2026-10-31T12:00:00+00:00",
        },
    )


def page(client, chat):
    return client.get(reverse("chat:conversation", args=[chat.pk])).content.decode()


def fragment(client, chat):
    return client.get(reverse("chat:conversation_messages", args=[chat.pk])).content.decode()


def test_owner_sees_details_in_page_and_fragment(client, chat, owner, failed):
    client.force_login(owner)
    for html in (page(client, chat), fragment(client, chat)):
        assert "Technische Details" in html and "Details kopieren" in html
        assert "backend_error" in html and "Speicher voll" in html
        assert "ca. 1.200 von 4.096 Tokens Kontext" in html
        assert "2 (1 Wiederholung)" in html


def test_recipient_does_not_see_details(client, chat, failed):
    ben = make_user("adult", "empfaenger")
    Share.objects.create(conversation=chat, user=ben, can_write=True)
    client.force_login(ben)
    for html in (page(client, chat), fragment(client, chat)):
        assert "Fehler: Der Anbieter hat einen Serverfehler" in html
        assert "Technische Details" not in html and "Speicher voll" not in html
    data = client.get(reverse("chat:api_messages", args=[chat.pk])).json()
    assert all(m["error_detail"] == {} for m in data)
    assert "Speicher voll" not in json.dumps(data)


def test_admin_supervision_does_not_see_details(client):
    teen = make_user("teen", "jugend")
    teen.allow_supervision = True
    teen.save()
    chat = Conversation.objects.create(user=teen)
    services.append_message(
        chat,
        role="assistant",
        content="",
        author=teen,
        status=Message.Status.ERROR,
        error="Fehlertext",
        error_detail={"status": 500, "type": "backend_error", "excerpt": "Speicher voll"},
    )
    client.force_login(make_user("admin", "verwalter_x"))
    html = page(client, chat)
    assert "Fehlertext" in html and "Technische Details" not in html
    assert "Speicher voll" not in html
    data = client.get(reverse("chat:api_messages", args=[chat.pk])).json()
    assert all(m["error_detail"] == {} for m in data)


def test_owner_api_includes_detail(client, chat, owner, failed):
    client.force_login(owner)
    data = client.get(reverse("chat:api_messages", args=[chat.pk])).json()
    assert data[-1]["error_detail"]["type"] == "backend_error"


def test_sse_hides_detail_from_non_owner_writer(client, chat, ai_model, fake):
    ben = make_user("adult", "mitschreiber")
    Share.objects.create(conversation=chat, user=ben, can_write=True)
    events = start_failing(client, chat, ai_model, fake, ben)
    error_event = next(data for name, data in events if name == "error")
    assert "detail" not in error_event
    message = Message.objects.get(pk=events[0][1]["assistant_message_id"])
    assert message.error_detail["type"] == "backend_error"  # gespeichert, nur Besitzer sieht es


# --- Migration ----------------------------------------------------------------------


def test_field_defaults_to_empty_dict(owner):
    field = Message._meta.get_field("error_detail")
    assert field.default is dict and field.blank
    msg = Message.objects.create(
        conversation=Conversation.objects.create(user=owner), role="user", content="x"
    )
    msg.refresh_from_db()
    assert msg.error_detail == {}
