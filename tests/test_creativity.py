"""Kreativität (Temperatur) und Übersicht „System-Prompt“ im Chat.

Temperatur je Adapter bis in den Request-Body (respx), Regel für Modelle ohne
Temperatur, Wiederholversuch ohne Temperatur bei HTTP 400, Speichern mit
Rechten (Besitzer, Freigabe mit U, Leser) und Vergleich. Kein echter Netzaufruf.
"""

import json
from decimal import Decimal

import pytest
import respx
from django.urls import reverse

from multigpt.accounts.models import Role, User, UserGroup
from multigpt.chat import capabilities, creativity
from multigpt.chat.models import (
    AIModel,
    ChatSettings,
    Conversation,
    Message,
    Project,
    Provider,
    Share,
)
from multigpt.chat.providers.base import Error, rejected_parameter

pytestmark = pytest.mark.django_db

PASSWORD = "Geheim-Test-1234"

OPENAI_SSE = (
    'data: {"choices":[{"index":0,"delta":{"content":"Hallo"},"finish_reason":"stop"}]}\n\n'
    'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":1}}\n\n'
    "data: [DONE]\n\n"
)
ANTHROPIC_SSE = (
    'event: message_start\ndata: {"type":"message_start","message":{"usage":'
    '{"input_tokens":1,"output_tokens":0}}}\n\n'
    'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
    '"delta":{"type":"text_delta","text":"Hallo"}}\n\n'
    'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":'
    '"end_turn"},"usage":{"output_tokens":1}}\n\n'
    'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)
GOOGLE_SSE = (
    'data: {"candidates":[{"content":{"role":"model","parts":[{"text":"Hallo"}]},'
    '"finishReason":"STOP","index":0}],"usageMetadata":{"promptTokenCount":3,'
    '"candidatesTokenCount":1}}\r\n\r\n'
)
STREAMS = {
    Provider.Kind.OPENAI_COMPAT: (r".*/chat/completions", OPENAI_SSE),
    Provider.Kind.ANTHROPIC: (r".*/v1/messages", ANTHROPIC_SSE),
    Provider.Kind.GOOGLE: (r".*:streamGenerateContent.*", GOOGLE_SSE),
}
BASE_URLS = {
    Provider.Kind.OPENAI_COMPAT: "https://api.openai.test/v1",
    Provider.Kind.ANTHROPIC: "https://api.anthropic.test/v1",
    Provider.Kind.GOOGLE: "https://gemini.test/v1beta",
}
OPENAI_REJECT = {
    "error": {
        "message": "Unsupported value: 'temperature' does not support 0.2 with this model. "
        "Only the default (1) value is supported.",
        "type": "invalid_request_error",
        "param": "temperature",
        "code": "unsupported_value",
    }
}


@pytest.fixture(autouse=True)
def _fresh_memory():
    creativity.forget_rejected()
    yield
    creativity.forget_rejected()


def make_user(username, role_key="adult"):
    return User.objects.create_user(
        username, password=PASSWORD, role=Role.objects.get(key=role_key)
    )


def make_model(kind=Provider.Kind.OPENAI_COMPAT, model_id="gpt-4o", name="Cloud"):
    provider = Provider.objects.create(
        name=name, kind=kind, base_url=BASE_URLS[kind], api_key="sk-test"
    )
    return AIModel.objects.create(provider=provider, model_id=model_id, display_name=model_id)


@pytest.fixture
def anna(client):
    user = make_user("anna")
    client.force_login(user)
    return user


def send(client, conversation, content="Frage", **data):
    response = client.post(
        reverse("chat:api_messages", args=[conversation.pk]),
        json.dumps({"content": content, **data}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    raw = b"".join(response.streaming_content).decode()
    events = []
    for block in raw.split("\n\n"):
        if block.strip():
            lines = dict(line.split(": ", 1) for line in block.split("\n"))
            events.append((lines["event"], json.loads(lines["data"])))
    return events


def run_with(client, conversation, kind, responses=None, **data):
    """Antwort über den echten Adapter; liefert (Events, Request-Bodies)."""
    pattern, stream = STREAMS[kind]
    ok = respx.MockResponse(200, headers={"content-type": "text/event-stream"}, text=stream)
    with respx.mock(assert_all_called=False) as router:
        route = router.post(url__regex=pattern)
        route.side_effect = list(responses or []) + [ok] * 3
        events = send(client, conversation, **data)
    return events, [json.loads(call.request.content) for call in route.calls]


def temperature_in(body, kind):
    if kind == Provider.Kind.GOOGLE:
        return (body.get("generationConfig") or {}).get("temperature")
    return body.get("temperature")


# --- Regel je Modell ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "model_id, expected",
    [
        ("gpt-4o", True),
        ("gpt-4.1-mini", True),
        ("o1", False),
        ("o3-mini", False),
        ("o4-mini-2025-04-16", False),
        ("gpt-5", False),
        ("gpt-5-mini", False),
        ("gpt-5.1", False),
        ("openai/gpt-5.2", False),
        ("gpt-6-sol", False),
        ("gpt-oss-20b", True),
        ("claude-sonnet-4-5", True),
        ("claude-haiku-4-5-20251001", True),
        ("claude-opus-4-6", True),
        ("claude-opus-4-1-20250805", True),
        ("claude-opus-4-20250514", True),
        ("claude-opus-4-7", False),
        ("claude-opus-4-8", False),
        ("claude-sonnet-5", False),
        ("claude-opus-5-5", False),
        ("claude-haiku-5-5", False),
        ("anthropic.claude-sonnet-5-5", False),
        ("claude-fable-5-1", False),
        ("claude-3-5-sonnet-latest", True),
        ("gemini-2.5-flash", True),
        ("models/gemini-1.5-pro", True),
        ("gemini-3-pro-preview", False),
        ("qwen3-30b-a3b", True),
        ("llama-3.3-70b-instruct", True),
    ],
)
def test_accepts_temperature(model_id, expected):
    assert capabilities.accepts_temperature(model_id) is expected


def test_accepts_temperature_not_with_thinking():
    assert not capabilities.accepts_temperature("claude-sonnet-4-5", "anthropic", thinking=True)


def test_rejected_parameter():
    body = json.dumps(OPENAI_REJECT).encode()
    assert rejected_parameter(400, body) == "temperature"
    anthropic = {
        "type": "error",
        "error": {"type": "invalid_request_error", "message": "temperature is not supported"},
    }
    assert rejected_parameter(400, json.dumps(anthropic).encode()) == "temperature"
    assert rejected_parameter(500, body) == ""
    other = {"error": {"message": "max_tokens too large", "code": "invalid_value"}}
    assert rejected_parameter(400, json.dumps(other).encode()) == ""
    assert rejected_parameter(400, b"kein json") == ""
    # Gleichheit von Error bleibt wie bisher (Feld zählt nicht).
    assert Error("x", rejected_param="temperature") == Error("x")


def test_clean_values():
    assert creativity.clean(None) == (None, None)
    assert creativity.clean("") == (None, None)
    assert creativity.clean(0.2) == (Decimal("0.20"), None)
    assert creativity.clean("1,0") == (Decimal("1.00"), None)
    for bad in (-0.1, 2.5, "abc", True, [], float("nan")):
        assert creativity.clean(bad)[1] == creativity.MSG_INVALID


# --- Request-Body je Adapter ---------------------------------------------------------------


@pytest.mark.parametrize(
    "kind, model_id",
    [
        (Provider.Kind.OPENAI_COMPAT, "gpt-4o"),
        (Provider.Kind.ANTHROPIC, "claude-sonnet-4-5"),
        (Provider.Kind.GOOGLE, "gemini-2.5-flash"),
    ],
)
def test_temperature_in_request_body(client, anna, kind, model_id):
    model = make_model(kind, model_id)
    conv = Conversation.objects.create(user=anna, default_model=model, temperature=Decimal("0.7"))
    _, bodies = run_with(client, conv, kind)
    assert len(bodies) == 1
    assert temperature_in(bodies[0], kind) == 0.7


def test_default_temperature_from_settings(client, anna):
    model = make_model()
    conv = Conversation.objects.create(user=anna, default_model=model)
    assert ChatSettings.load().default_temperature == Decimal("0.3")
    _, bodies = run_with(client, conv, Provider.Kind.OPENAI_COMPAT)
    assert bodies[0]["temperature"] == 0.3


def test_empty_default_sends_no_temperature(client, anna):
    settings = ChatSettings.load()
    settings.default_temperature = None
    settings.save()
    model = make_model()
    conv = Conversation.objects.create(user=anna, default_model=model)
    _, bodies = run_with(client, conv, Provider.Kind.OPENAI_COMPAT)
    assert "temperature" not in bodies[0]


def test_project_temperature_between_chat_and_settings(client, anna):
    model = make_model()
    project = Project.objects.create(owner=anna, name="P", temperature=Decimal("1.0"))
    conv = Conversation.objects.create(user=anna, default_model=model, project=project)
    _, bodies = run_with(client, conv, Provider.Kind.OPENAI_COMPAT)
    assert bodies[0]["temperature"] == 1.0
    Conversation.objects.filter(pk=conv.pk).update(temperature=Decimal("0.2"))
    _, bodies = run_with(client, conv, Provider.Kind.OPENAI_COMPAT)
    assert bodies[0]["temperature"] == 0.2


@pytest.mark.parametrize(
    "kind, model_id",
    [
        (Provider.Kind.OPENAI_COMPAT, "o3-mini"),
        (Provider.Kind.OPENAI_COMPAT, "gpt-5"),
        (Provider.Kind.ANTHROPIC, "claude-opus-4-7"),
        (Provider.Kind.GOOGLE, "gemini-3-pro-preview"),
    ],
)
def test_reasoning_models_without_temperature(client, anna, kind, model_id):
    model = make_model(kind, model_id)
    conv = Conversation.objects.create(user=anna, default_model=model, temperature=Decimal("0.2"))
    _, bodies = run_with(client, conv, kind)
    assert temperature_in(bodies[0], kind) is None


# --- Wiederholversuch bei HTTP 400 ----------------------------------------------------------


def test_retry_without_temperature_on_400(client, anna, caplog):
    model = make_model(model_id="neues-modell")
    conv = Conversation.objects.create(user=anna, default_model=model, temperature=Decimal("0.2"))
    reject = respx.MockResponse(400, json=OPENAI_REJECT)
    caplog.set_level("INFO")
    events, bodies = run_with(client, conv, Provider.Kind.OPENAI_COMPAT, [reject])
    assert [b.get("temperature") for b in bodies] == [0.2, None]
    assert "error" not in [name for name, _ in events]
    answer = Message.objects.get(role=Message.Role.ASSISTANT)
    assert answer.status == Message.Status.COMPLETE and answer.content == "Hallo"
    # Gemerkt: die nächste Antwort geht gleich ohne Temperatur.
    _, bodies = run_with(client, conv, Provider.Kind.OPENAI_COMPAT)
    assert len(bodies) == 1 and "temperature" not in bodies[0]
    # Log nur mit IDs, ohne Modellnamen und ohne Fehlertext des Anbieters.
    assert "neues-modell" not in caplog.text and "Only the default" not in caplog.text
    assert f"Modell {model.pk}: Temperatur abgelehnt" in caplog.text


def test_retry_anthropic(client, anna):
    model = make_model(Provider.Kind.ANTHROPIC, "claude-unbekannt")
    conv = Conversation.objects.create(user=anna, default_model=model, temperature=Decimal("1.0"))
    reject = respx.MockResponse(
        400,
        json={
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "`temperature` is deprecated for this model.",
            },
        },
    )
    _, bodies = run_with(client, conv, Provider.Kind.ANTHROPIC, [reject])
    assert [b.get("temperature") for b in bodies] == [1.0, None]


def test_other_400_is_not_retried(client, anna):
    model = make_model()
    conv = Conversation.objects.create(user=anna, default_model=model, temperature=Decimal("0.2"))
    reject = respx.MockResponse(400, json={"error": {"message": "Bad", "code": "x"}})
    events, bodies = run_with(client, conv, Provider.Kind.OPENAI_COMPAT, [reject])
    assert len(bodies) == 1
    assert "error" in [name for name, _ in events]


# --- Speichern mit Rechten -----------------------------------------------------------------


def patch(client, pk, payload):
    return client.patch(
        reverse("chat:api_conversation_detail", args=[pk]),
        data=json.dumps(payload),
        content_type="application/json",
    )


def share(conv, user, *, can_write=False, can_update=False):
    group = UserGroup.objects.create(name=f"Gruppe {user.username}")
    user.groups.add(group)
    Share.objects.create(conversation=conv, group=group, can_write=can_write, can_update=can_update)


def test_owner_sets_and_resets_temperature(client, anna):
    conv = Conversation.objects.create(user=anna)
    response = patch(client, conv.pk, {"temperature": 0.2})
    assert response.status_code == 200 and response.json()["temperature"] == 0.2
    conv.refresh_from_db()
    assert conv.temperature == Decimal("0.2")
    assert patch(client, conv.pk, {"temperature": 3}).status_code == 400
    response = patch(client, conv.pk, {"temperature": None})
    assert response.status_code == 200 and response.json()["temperature"] is None
    conv.refresh_from_db()
    assert conv.temperature is None


def test_shared_update_right_may_set_temperature(client, anna):
    conv = Conversation.objects.create(user=anna)
    ben = make_user("ben")
    share(conv, ben, can_write=True, can_update=True)
    client.force_login(ben)
    assert patch(client, conv.pk, {"temperature": 1.0}).status_code == 200
    conv.refresh_from_db()
    assert conv.temperature == Decimal("1.0")


@pytest.mark.parametrize("can_write", [False, True])
def test_reader_or_writer_without_update_gets_403(client, anna, can_write):
    conv = Conversation.objects.create(user=anna)
    carla = make_user("carla")
    share(conv, carla, can_write=can_write)
    client.force_login(carla)
    assert patch(client, conv.pk, {"temperature": 1.0}).status_code == 403
    conv.refresh_from_db()
    assert conv.temperature is None
    html = client.get(reverse("chat:conversation", args=[conv.pk])).content.decode()
    assert 'id="creativity-select"' not in html
    assert "Kreativität: <span" in html


# --- Vergleich -----------------------------------------------------------------------------


def test_compare_uses_same_temperature(client, anna):
    first = make_model(model_id="gpt-4o")
    second = AIModel.objects.create(
        provider=first.provider, model_id="gpt-4.1", display_name="gpt-4.1"
    )
    third = AIModel.objects.create(provider=first.provider, model_id="o3", display_name="o3")
    conv = Conversation.objects.create(user=anna, default_model=first, temperature=Decimal("1.0"))
    pattern, stream = STREAMS[Provider.Kind.OPENAI_COMPAT]
    with respx.mock(assert_all_called=False) as router:
        route = router.post(url__regex=pattern).respond(
            200, headers={"content-type": "text/event-stream"}, text=stream
        )
        events = send(client, conv, model=first.pk, compare=True)
        anchor = events[0][1]["assistant_message_id"]
        for other in (second, third):
            send(
                client,
                conv,
                "",
                model=other.pk,
                compare=True,
                regenerate=True,
                message_id=anchor,
            )
    bodies = {
        json.loads(c.request.content)["model"]: json.loads(c.request.content) for c in route.calls
    }
    assert bodies["gpt-4o"]["temperature"] == 1.0
    assert bodies["gpt-4.1"]["temperature"] == 1.0
    assert "temperature" not in bodies["o3"]  # Reasoning-Modell: Standard des Anbieters


# --- Anzeige im Chat -----------------------------------------------------------------------


def test_prompt_info_shows_base_instructions_escaped(client, anna):
    settings = ChatSettings.load()
    settings.base_instructions = "Sei <b>sachlich</b> & knapp."
    settings.save()
    project = Project.objects.create(owner=anna, name="Haus", instructions="Projekt <i>Regel</i>")
    conv = Conversation.objects.create(user=anna, project=project)
    make_model()
    html = client.get(reverse("chat:conversation", args=[conv.pk])).content.decode()
    assert "System-Prompt: Grundregeln aktiv · eigener Prompt" in html
    assert 'Grundregeln <span class="hint">(vom Verwalter, gelten für alle Chats)</span>' in html
    assert "Sei &lt;b&gt;sachlich&lt;/b&gt; &amp; knapp." in html
    assert "<b>sachlich</b>" not in html
    assert "+ Projekt-Anweisungen (Haus): Projekt &lt;i&gt;Regel&lt;/i&gt;" in html
    assert "+ technische Hinweise zu Werkzeugen und Quellen" in html
    # Interne Hinweise von MultiGPT erscheinen nicht im Wortlaut.
    assert "quellmaterial" not in html.lower()
    # Nur Verwalter bekommen den Link in den Admin.
    admin_url = reverse("admin:chat_chatsettings_changelist")
    assert admin_url not in html
    assert 'id="creativity-select"' in html
    assert "Standard – Einstellung des Verwalters (0,3)" in html


def test_prompt_info_admin_link_for_admins(client):
    admin = make_user("verwalter", "admin")
    client.force_login(admin)
    make_model()
    html = client.get(reverse("chat:index")).content.decode()
    assert reverse("admin:chat_chatsettings_changelist") in html
    assert 'eigener Prompt <span id="system-prompt-state">leer</span>' in html


def test_prompt_info_role_only_mentioned(client):
    teen = make_user("tom", "teen")
    teen.role.fixed_system_prompt = "GEHEIMER-ROLLENPROMPT"
    teen.role.save()
    client.force_login(teen)
    make_model()
    conv = Conversation.objects.create(user=teen)
    html = client.get(reverse("chat:conversation", args=[conv.pk])).content.decode()
    assert "+ fester Prompt deiner Rolle" in html
    assert "GEHEIMER-ROLLENPROMPT" not in html


def test_project_form_temperature(client, anna):
    project = Project.objects.create(owner=anna, name="P")
    url = reverse("chat:project_detail", args=[project.pk])
    html = client.get(url).content.decode()
    assert "Präzise (0,2)" in html
    response = client.patch(
        reverse("chat:api_project_detail", args=[project.pk]),
        data=json.dumps({"temperature": 0.7}),
        content_type="application/json",
    )
    assert response.status_code == 200 and response.json()["temperature"] == 0.7
    project.refresh_from_db()
    assert project.temperature == Decimal("0.7")


def test_project_form_saves_temperature(client, anna):
    project = Project.objects.create(owner=anna, name="P")
    url = reverse("chat:project_detail", args=[project.pk])
    data = {"name": "P", "description": "", "instructions": "", "color": "", "temperature": "0.2"}
    assert client.post(url, data).status_code == 302
    project.refresh_from_db()
    assert project.temperature == Decimal("0.2")
    html = client.get(url).content.decode()
    assert '<option value="0.2" selected>Präzise (0,2)</option>' in html
    data["temperature"] = ""
    assert client.post(url, data).status_code == 302
    project.refresh_from_db()
    assert project.temperature is None
