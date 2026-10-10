"""Denktiefe (Reasoning) und fester Prompt der Rolle im Bereich „System-Prompt“.

Regel je Modell (capabilities), Request-Body je Adapter und Stufe (respx),
Zusammenspiel mit der Temperatur, Wiederholversuch ohne Denktiefe bei HTTP
400, Speichern mit Rechten, ``/api/models`` und Vergleich. Kein echter
Netzaufruf.
"""

import json
from decimal import Decimal

import pytest
import respx
from django.urls import reverse

from multigpt.accounts.models import Role, User
from multigpt.chat import capabilities, creativity, reasoning
from multigpt.chat.models import (
    AIModel,
    ChatSettings,
    Conversation,
    Message,
    Project,
    Provider,
    ReasoningEffort,
)
from multigpt.chat.providers.base import rejected_parameter
from tests.test_creativity import (
    STREAMS,
    make_model,
    make_user,
    patch,
    run_with,
    send,
    share,
)

pytestmark = pytest.mark.django_db

OPENAI = Provider.Kind.OPENAI_COMPAT
ANTHROPIC = Provider.Kind.ANTHROPIC
GOOGLE = Provider.Kind.GOOGLE


@pytest.fixture(autouse=True)
def _fresh_memory():
    creativity.forget_rejected()
    reasoning.forget_rejected()
    yield
    creativity.forget_rejected()
    reasoning.forget_rejected()


@pytest.fixture
def anna(client):
    user = make_user("anna")
    client.force_login(user)
    return user


# --- Regel je Modell ----------------------------------------------------------------------


ALL = ("off", "low", "medium", "high", "xhigh", "max")


@pytest.mark.parametrize(
    "model_id, provider, expected",
    [
        ("gpt-4o", OPENAI, None),
        ("gpt-4.1-mini", OPENAI, None),
        ("o1-mini", OPENAI, None),
        ("o3", OPENAI, ("low", "medium", "high")),
        ("o4-mini-2025-04-16", OPENAI, ("low", "medium", "high")),
        ("gpt-5", OPENAI, ("off", "low", "medium", "high")),
        ("gpt-5-nano", OPENAI, ("off", "low", "medium", "high")),
        ("gpt-5-chat-latest", OPENAI, None),
        ("gpt-5.1", OPENAI, ("off", "low", "medium", "high")),
        ("openai/gpt-5.2", OPENAI, ("off", "low", "medium", "high", "xhigh")),
        ("gpt-5.5", OPENAI, ("off", "low", "medium", "high", "xhigh")),
        ("gpt-5.6", OPENAI, ALL),
        ("gpt-6-luna", OPENAI, ALL),
        ("gpt-6-astra", OPENAI, ("low", "medium", "high", "xhigh", "max")),
        ("gpt-6.1-sol", OPENAI, ("low", "medium", "high", "xhigh", "max")),
        ("openai/gpt-oss-20b", OPENAI, ("low", "medium", "high")),
        ("qwen3-30b-a3b", OPENAI, None),
        ("claude-sonnet-4-5", OPENAI, None),  # nur über die Anthropic-API
        ("claude-3-5-sonnet-latest", ANTHROPIC, None),
        ("claude-3-7-sonnet-latest", ANTHROPIC, ("off", "low", "medium", "high")),
        ("claude-opus-4-20250514", ANTHROPIC, ("off", "low", "medium", "high")),
        ("claude-haiku-4-5-20251001", ANTHROPIC, ("off", "low", "medium", "high")),
        ("claude-opus-4-6", ANTHROPIC, ("off", "low", "medium", "high", "max")),
        ("claude-sonnet-4-6", ANTHROPIC, ("off", "low", "medium", "high", "max")),
        ("claude-opus-4-7", ANTHROPIC, ALL),
        ("claude-opus-5", ANTHROPIC, ALL),
        ("claude-sonnet-5-5", ANTHROPIC, ALL),
        ("claude-haiku-5-5", ANTHROPIC, ALL),
        ("claude-opus-5-5", ANTHROPIC, ("low", "medium", "high", "xhigh", "max")),
        ("claude-fable-5-1", ANTHROPIC, ("low", "medium", "high", "xhigh", "max")),
        ("claude-mythos-preview", ANTHROPIC, ("low", "medium", "high", "max")),
        ("gemini-2.0-flash", GOOGLE, None),
        ("gemma-3-27b-it", GOOGLE, None),
        ("gemini-2.5-pro", GOOGLE, ("low", "medium", "high")),
        ("models/gemini-2.5-flash-lite", GOOGLE, ("off", "low", "medium", "high")),
        ("gemini-3-pro-preview", GOOGLE, ("low", "high")),
        ("gemini-3.1-pro-preview", GOOGLE, ("low", "medium", "high")),
        ("gemini-3-flash-preview", GOOGLE, ("off", "low", "medium", "high")),
        ("gemini-3.8-flash", GOOGLE, ("low", "medium", "high")),
    ],
)
def test_reasoning_support(model_id, provider, expected):
    assert capabilities.reasoning_support(model_id, provider) == expected


def test_reasoning_support_infers_provider():
    assert capabilities.reasoning_support("claude-opus-4-7") == ALL
    assert capabilities.reasoning_support("gemini-3-pro-preview") == ("low", "high")
    assert capabilities.reasoning_support("o3") == ("low", "medium", "high")


@pytest.mark.parametrize(
    "level, supported, expected",
    [
        ("high", ("low", "medium", "high"), "high"),
        ("off", ("low", "medium", "high"), "low"),  # kein „Aus“: kleinste Stufe
        ("max", ("off", "low", "medium", "high", "xhigh"), "xhigh"),
        ("xhigh", ("low", "medium", "high", "max"), "high"),
        ("medium", ("low", "high"), "low"),  # abgerundet
        ("low", ("high",), "high"),  # notfalls aufgerundet
        ("", ("low",), None),
        ("high", None, None),
        ("unsinn", ("low",), None),
    ],
)
def test_reasoning_level(level, supported, expected):
    assert capabilities.reasoning_level(level, supported) == expected


@pytest.mark.parametrize(
    "model_id, provider, level, reasoning_level, expected",
    [
        ("gpt-4o", OPENAI, "", None, True),
        ("gpt-5.1", OPENAI, "", None, False),
        ("gpt-5.1", OPENAI, "off", "off", True),  # reasoning_effort none
        ("gpt-5.5", OPENAI, "off", "off", True),
        ("gpt-5.5", OPENAI, "low", "low", False),
        ("gpt-5", OPENAI, "off", "off", False),  # minimal, denkt trotzdem
        ("gpt-6-astra", OPENAI, "off", "off", False),  # kein none
        ("claude-sonnet-4-5", ANTHROPIC, "off", "off", True),
        ("claude-sonnet-4-5", ANTHROPIC, "low", "low", False),  # Thinking an
        ("claude-opus-4-6", ANTHROPIC, "high", "high", False),
        ("claude-3-5-sonnet-latest", ANTHROPIC, "high", "high", True),  # kein Reasoning
        ("gemini-2.5-flash", GOOGLE, "high", "high", True),
        ("gpt-oss-20b", OPENAI, "high", "high", True),
    ],
)
def test_accepts_temperature_with_reasoning(model_id, provider, level, reasoning_level, expected):
    del level  # nur zur Lesbarkeit der Tabelle
    assert (
        capabilities.accepts_temperature(model_id, provider, reasoning=reasoning_level) is expected
    )


def test_rejected_parameter_reasoning():
    openai = {
        "error": {
            "message": "Unsupported value: 'reasoning_effort' does not support 'none' with "
            "this model.",
            "type": "invalid_request_error",
            "param": "reasoning_effort",
            "code": "unsupported_value",
        }
    }
    assert rejected_parameter(400, json.dumps(openai).encode()) == "reasoning"
    anthropic = {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": '"thinking.type.enabled" is not supported for this model.',
        },
    }
    assert rejected_parameter(400, json.dumps(anthropic).encode()) == "reasoning"
    gemini = {"error": {"code": 400, "message": "thinking_level is not supported by this model."}}
    assert rejected_parameter(400, json.dumps(gemini).encode()) == "reasoning"
    lmstudio = {"error": "Unrecognized key: reasoning_effort"}
    assert rejected_parameter(422, json.dumps(lmstudio).encode()) == "reasoning"
    both = {"error": {"message": "temperature may only be set to 1 when thinking is enabled"}}
    assert rejected_parameter(400, json.dumps(both).encode()) == "temperature,reasoning"
    # Fehler zu zurückgegebenen Thinking-Blöcken sind keine Ablehnung des Parameters.
    signature = {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": "messages.1.content.0: Invalid `signature` in `thinking` block",
        },
    }
    assert rejected_parameter(400, json.dumps(signature).encode()) == ""
    modified = {
        "error": {
            "message": "`thinking` or `redacted_thinking` blocks in the latest assistant "
            "message cannot be modified"
        }
    }
    assert rejected_parameter(400, json.dumps(modified).encode()) == ""


def test_clean_values():
    assert reasoning.clean(None) == ("", None)
    assert reasoning.clean("") == ("", None)
    assert reasoning.clean("high") == ("high", None)
    for bad in ("sehr", 3, True, ["low"], "HIGH"):
        assert reasoning.clean(bad)[1] == reasoning.MSG_INVALID


# --- Request-Body je Adapter und Stufe ---------------------------------------------------


def _local_model(model_id):
    provider = Provider.objects.create(
        name="LM Studio", kind=OPENAI, base_url="http://lmstudio.test/v1", is_local=True
    )
    return AIModel.objects.create(provider=provider, model_id=model_id, display_name=model_id)


@pytest.mark.parametrize(
    "kind, model_id, level, expected, temperature",
    [
        # OpenAI: reasoning_effort; mit „Aus“ (none) ab gpt-5.1 wieder Temperatur
        (OPENAI, "gpt-5.5", "high", {"reasoning_effort": "high"}, None),
        (OPENAI, "gpt-5.5", "off", {"reasoning_effort": "none"}, 0.3),
        (OPENAI, "gpt-5.5", "max", {"reasoning_effort": "xhigh"}, None),
        (OPENAI, "gpt-5", "off", {"reasoning_effort": "minimal"}, None),
        (OPENAI, "o3", "max", {"reasoning_effort": "high"}, None),
        (OPENAI, "gpt-6-astra", "off", {"reasoning_effort": "low"}, None),
        (OPENAI, "gpt-6-luna", "max", {"reasoning_effort": "max"}, None),
        (OPENAI, "gpt-5.5", "", {}, None),  # Standard: nichts senden
        (OPENAI, "gpt-4o", "high", {}, 0.3),  # kein Reasoning-Modell
        # Anthropic: effort (adaptiv) bzw. budget_tokens; mit Thinking ohne Temperatur
        (ANTHROPIC, "claude-opus-5-5", "medium", {"output_config": {"effort": "medium"}}, None),
        (ANTHROPIC, "claude-opus-5-5", "off", {"output_config": {"effort": "low"}}, None),
        (ANTHROPIC, "claude-sonnet-5-5", "off", {"thinking": {"type": "between_tools"}}, None),
        (ANTHROPIC, "claude-haiku-5-5", "off", {"thinking": {"type": "disabled"}}, None),
        (
            ANTHROPIC,
            "claude-opus-4-6",
            "xhigh",
            {"output_config": {"effort": "high"}, "thinking": {"type": "adaptive"}},
            None,
        ),
        (
            ANTHROPIC,
            "claude-opus-4-7",
            "max",
            {
                "output_config": {"effort": "max"},
                "thinking": {"type": "adaptive"},
                "max_tokens": 64000,
            },
            None,
        ),
        (
            ANTHROPIC,
            "claude-sonnet-4-5",
            "medium",
            {"thinking": {"type": "enabled", "budget_tokens": 8000}, "max_tokens": 24000},
            None,
        ),
        (ANTHROPIC, "claude-sonnet-4-5", "off", {}, 0.3),
        # Gemini: thinkingConfig in generationConfig
        (GOOGLE, "gemini-3-flash-preview", "off", {"thinkingLevel": "MINIMAL"}, None),
        (GOOGLE, "gemini-3-pro-preview", "medium", {"thinkingLevel": "LOW"}, None),
        (GOOGLE, "gemini-2.5-flash", "off", {"thinkingBudget": 0}, 0.3),
        (GOOGLE, "gemini-2.5-pro", "high", {"thinkingBudget": 24576}, 0.3),
        (GOOGLE, "gemini-2.0-flash", "high", {}, 0.3),
    ],
)
def test_reasoning_in_request_body(client, anna, kind, model_id, level, expected, temperature):
    model = make_model(kind, model_id)
    conv = Conversation.objects.create(user=anna, default_model=model, reasoning_effort=level)
    _, bodies = run_with(client, conv, kind)
    assert len(bodies) == 1
    body = bodies[0]
    if kind == GOOGLE:
        generation = body.get("generationConfig") or {}
        assert generation.get("thinkingConfig", {}) == expected
        assert "thinkingConfig" not in body
        assert generation.get("temperature") == temperature
        return
    for key in ("reasoning_effort", "output_config", "thinking"):
        assert body.get(key) == expected.get(key), key
    if kind == ANTHROPIC:
        assert body["max_tokens"] == expected.get("max_tokens", 16000)
    assert body.get("temperature") == temperature


@pytest.mark.parametrize(
    "model_id, level, expected",
    [
        ("gpt-oss-20b", "medium", "medium"),
        ("gpt-oss-120b", "off", "low"),
        ("qwen3-8b", "high", None),
    ],
)
def test_local_models_only_gpt_oss(client, anna, model_id, level, expected):
    model = _local_model(model_id)
    conv = Conversation.objects.create(user=anna, default_model=model, reasoning_effort=level)
    _, bodies = run_with(client, conv, OPENAI)
    assert bodies[0].get("reasoning_effort") == expected
    assert bodies[0]["temperature"] == 0.3  # lokal immer mit Temperatur


def test_inherited_from_project_and_settings(client, anna):
    model = make_model(OPENAI, "gpt-5.5")
    settings = ChatSettings.load()
    assert settings.default_reasoning_effort == ""  # Standard des Anbieters
    settings.default_reasoning_effort = "low"
    settings.save()
    conv = Conversation.objects.create(user=anna, default_model=model)
    _, bodies = run_with(client, conv, OPENAI)
    assert bodies[0]["reasoning_effort"] == "low"
    project = Project.objects.create(owner=anna, name="P", reasoning_effort="high")
    Conversation.objects.filter(pk=conv.pk).update(project=project)
    _, bodies = run_with(client, conv, OPENAI)
    assert bodies[0]["reasoning_effort"] == "high"
    Conversation.objects.filter(pk=conv.pk).update(reasoning_effort="medium")
    _, bodies = run_with(client, conv, OPENAI)
    assert bodies[0]["reasoning_effort"] == "medium"


def test_foreign_project_is_ignored(anna):
    ben = make_user("ben")
    project = Project.objects.create(owner=ben, name="Fremd", reasoning_effort="high")
    conv = Conversation.objects.create(user=anna, project=project)
    assert reasoning.effective(conv) == ""


def test_temperature_and_reasoning_together(client, anna):
    """gpt-5.1: mit „Aus“ (none) Temperatur, mit einer Stufe nicht; Claude 4.5 ebenso."""
    model = make_model(OPENAI, "gpt-5.1")
    conv = Conversation.objects.create(
        user=anna, default_model=model, temperature=Decimal("0.7"), reasoning_effort="off"
    )
    _, bodies = run_with(client, conv, OPENAI)
    assert bodies[0]["reasoning_effort"] == "none" and bodies[0]["temperature"] == 0.7
    Conversation.objects.filter(pk=conv.pk).update(reasoning_effort="high")
    _, bodies = run_with(client, conv, OPENAI)
    assert bodies[0]["reasoning_effort"] == "high" and "temperature" not in bodies[0]
    claude = make_model(ANTHROPIC, "claude-sonnet-4-5", name="Anthropic")
    Conversation.objects.filter(pk=conv.pk).update(default_model=claude)
    _, bodies = run_with(client, conv, ANTHROPIC)
    assert bodies[0]["thinking"]["type"] == "enabled" and "temperature" not in bodies[0]


# --- Wiederholversuch bei HTTP 400 ----------------------------------------------------------


def test_retry_without_reasoning_on_400(client, anna, caplog):
    model = make_model(OPENAI, "gpt-5.1")
    conv = Conversation.objects.create(
        user=anna, default_model=model, temperature=Decimal("0.2"), reasoning_effort="off"
    )
    reject = respx.MockResponse(
        400,
        json={
            "error": {
                "message": "Unsupported value: 'reasoning_effort' does not support 'none'.",
                "type": "invalid_request_error",
                "param": "reasoning_effort",
                "code": "unsupported_value",
            }
        },
    )
    caplog.set_level("INFO")
    events, bodies = run_with(client, conv, OPENAI, [reject])
    assert [b.get("reasoning_effort") for b in bodies] == ["none", None]
    # Ohne Denktiefe gilt wieder die Regel ohne Temperatur (gpt-5.1 denkt sonst).
    assert [b.get("temperature") for b in bodies] == [0.2, None]
    assert "error" not in [name for name, _ in events]
    answer = Message.objects.get(role=Message.Role.ASSISTANT)
    assert answer.status == Message.Status.COMPLETE and answer.content == "Hallo"
    # Gemerkt: die nächste Antwort geht gleich ohne.
    _, bodies = run_with(client, conv, OPENAI)
    assert len(bodies) == 1 and "reasoning_effort" not in bodies[0]
    assert f"Modell {model.pk}: Denktiefe abgelehnt" in caplog.text
    assert "does not support" not in caplog.text


def test_retry_anthropic_thinking(client, anna):
    model = make_model(ANTHROPIC, "claude-sonnet-4-5")
    conv = Conversation.objects.create(user=anna, default_model=model, reasoning_effort="high")
    reject = respx.MockResponse(
        400,
        json={
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": '"thinking.type.enabled" is not supported for this model. Use '
                '"thinking.type.adaptive" and "output_config.effort" to control thinking.',
            },
        },
    )
    _, bodies = run_with(client, conv, ANTHROPIC, [reject])
    assert len(bodies) == 2
    assert "thinking" in bodies[0] and "thinking" not in bodies[1]
    assert bodies[1]["max_tokens"] == 16000
    assert bodies[1]["temperature"] == 0.3  # ohne Thinking wieder mit Temperatur


def test_retry_gemini_thinking_level(client, anna):
    model = make_model(GOOGLE, "gemini-3-flash-preview")
    conv = Conversation.objects.create(user=anna, default_model=model, reasoning_effort="off")
    reject = respx.MockResponse(
        400,
        json={
            "error": {
                "code": 400,
                "message": "Thinking level MINIMAL is not supported for this model.",
                "status": "INVALID_ARGUMENT",
            }
        },
    )
    _, bodies = run_with(client, conv, GOOGLE, [reject])
    assert len(bodies) == 2
    assert "thinkingConfig" in bodies[0]["generationConfig"]
    assert "thinkingConfig" not in bodies[1].get("generationConfig", {})


def test_reasoning_error_without_reasoning_sent_is_not_retried(client, anna):
    model = make_model(OPENAI, "gpt-4o")
    conv = Conversation.objects.create(user=anna, default_model=model, reasoning_effort="high")
    reject = respx.MockResponse(400, json={"error": {"message": "reasoning is broken"}})
    events, bodies = run_with(client, conv, OPENAI, [reject])
    assert len(bodies) == 1
    assert "error" in [name for name, _ in events]


# --- Speichern mit Rechten -----------------------------------------------------------------


def test_owner_sets_and_resets_reasoning(client, anna):
    conv = Conversation.objects.create(user=anna)
    response = patch(client, conv.pk, {"reasoning_effort": "high"})
    assert response.status_code == 200 and response.json()["reasoning_effort"] == "high"
    conv.refresh_from_db()
    assert conv.reasoning_effort == "high"
    assert patch(client, conv.pk, {"reasoning_effort": "riesig"}).status_code == 400
    response = patch(client, conv.pk, {"reasoning_effort": None})
    assert response.status_code == 200 and response.json()["reasoning_effort"] == ""
    conv.refresh_from_db()
    assert conv.reasoning_effort == ""


def test_shared_update_right_may_set_reasoning(client, anna):
    conv = Conversation.objects.create(user=anna)
    ben = make_user("ben")
    share(conv, ben, can_write=True, can_update=True)
    client.force_login(ben)
    assert patch(client, conv.pk, {"reasoning_effort": "low"}).status_code == 200
    conv.refresh_from_db()
    assert conv.reasoning_effort == "low"


@pytest.mark.parametrize("can_write", [False, True])
def test_reader_or_writer_without_update_gets_403(client, anna, can_write):
    conv = Conversation.objects.create(user=anna, reasoning_effort="max")
    carla = make_user("carla")
    share(conv, carla, can_write=can_write)
    client.force_login(carla)
    assert patch(client, conv.pk, {"reasoning_effort": "low"}).status_code == 403
    conv.refresh_from_db()
    assert conv.reasoning_effort == "max"
    make_model()
    html = client.get(reverse("chat:conversation", args=[conv.pk])).content.decode()
    assert 'id="reasoning-select"' not in html
    assert 'Denktiefe: <span class="chat-prompt-value">Maximal</span>' in html


def test_project_api_and_form(client, anna):
    project = Project.objects.create(owner=anna, name="P")
    response = client.patch(
        reverse("chat:api_project_detail", args=[project.pk]),
        data=json.dumps({"reasoning_effort": "medium"}),
        content_type="application/json",
    )
    assert response.status_code == 200 and response.json()["reasoning_effort"] == "medium"
    response = client.patch(
        reverse("chat:api_project_detail", args=[project.pk]),
        data=json.dumps({"reasoning_effort": "x"}),
        content_type="application/json",
    )
    assert response.status_code == 400
    url = reverse("chat:project_detail", args=[project.pk])
    html = client.get(url).content.decode()
    assert '<option value="medium" selected>Mittel</option>' in html
    data = {"name": "P", "description": "", "instructions": "", "color": "", "temperature": ""}
    assert client.post(url, {**data, "reasoning_effort": "off"}).status_code == 302
    project.refresh_from_db()
    assert project.reasoning_effort == "off"
    assert client.post(url, {**data, "reasoning_effort": ""}).status_code == 302
    project.refresh_from_db()
    assert project.reasoning_effort == ""


def test_chat_select_shows_inherited_default(client, anna):
    project = Project.objects.create(owner=anna, name="P", reasoning_effort="high")
    conv = Conversation.objects.create(user=anna, project=project)
    make_model()
    html = client.get(reverse("chat:conversation", args=[conv.pk])).content.decode()
    assert 'id="reasoning-select"' in html
    assert '<option value="">Standard – Vorgabe des Projekts (Hoch)</option>' in html
    for value, text in ReasoningEffort.choices:
        assert f'<option value="{value}">{text}</option>' in html
    other = Conversation.objects.create(user=anna)
    html = client.get(reverse("chat:conversation", args=[other.pk])).content.decode()
    assert '<option value="">Standard – Standard des Anbieters</option>' in html


# --- API und Vergleich ---------------------------------------------------------------------


def test_api_models_reasoning_levels(client, anna):
    first = make_model(OPENAI, "gpt-5.5")
    plain = AIModel.objects.create(provider=first.provider, model_id="gpt-4o", display_name="4o")
    claude = make_model(ANTHROPIC, "claude-opus-5-5", name="Anthropic")
    data = {m["id"]: m for m in client.get(reverse("chat:api_models")).json()}
    assert data[first.pk]["reasoning_levels"] == ["off", "low", "medium", "high", "xhigh"]
    assert data[plain.pk]["reasoning_levels"] is None
    assert data[claude.pk]["reasoning_levels"] == ["low", "medium", "high", "xhigh", "max"]


def test_compare_maps_level_per_model(client, anna):
    first = make_model(OPENAI, "gpt-5.5")
    second = AIModel.objects.create(provider=first.provider, model_id="gpt-4o", display_name="4o")
    third = AIModel.objects.create(provider=first.provider, model_id="o3", display_name="o3")
    conv = Conversation.objects.create(
        user=anna, default_model=first, reasoning_effort="off", temperature=Decimal("0.7")
    )
    pattern, stream = STREAMS[OPENAI]
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
    assert bodies["gpt-5.5"]["reasoning_effort"] == "none"
    assert bodies["gpt-5.5"]["temperature"] == 0.7  # mit none erlaubt
    assert "reasoning_effort" not in bodies["gpt-4o"]  # weggelassen
    assert bodies["gpt-4o"]["temperature"] == 0.7
    assert bodies["o3"]["reasoning_effort"] == "low"  # kein „Aus“: kleinste Stufe
    assert "temperature" not in bodies["o3"]


# --- Fester Prompt der Rolle ---------------------------------------------------------------


def _role(key, prompt):
    role = Role.objects.get(key=key)
    role.fixed_system_prompt = prompt
    role.save()
    return role


def test_role_prompt_shown_escaped_without_admin_link(client):
    role = _role("teen", "Sei <b>jugendfrei</b> & freundlich.")
    teen = User.objects.create_user("tom", password="Geheim-Test-1234", role=role)
    client.force_login(teen)
    make_model()
    conv = Conversation.objects.create(user=teen)
    html = client.get(reverse("chat:conversation", args=[conv.pk])).content.decode()
    assert f"Fester Prompt deiner Rolle ({role.name})" in html
    assert "Sei &lt;b&gt;jugendfrei&lt;/b&gt; &amp; freundlich." in html
    assert "<b>jugendfrei</b>" not in html
    assert reverse("admin:accounts_role_change", args=[role.pk]) not in html
    # Reihenfolge wie im System-Prompt: Grundregeln, Rolle, technische Hinweise.
    assert html.index("Grundregeln <span") < html.index("Fester Prompt deiner Rolle")
    assert html.index("Fester Prompt deiner Rolle") < html.index("+ technische Hinweise")


def test_role_prompt_admin_link(client):
    role = _role("admin", "VERWALTER-REGEL")
    admin = make_user("verwalter", "admin")
    client.force_login(admin)
    make_model()
    html = client.get(reverse("chat:index")).content.decode()
    assert "VERWALTER-REGEL" in html
    link = reverse("admin:accounts_role_change", args=[role.pk])
    assert f'<a href="{link}">Ändern</a>' in html


def test_role_without_prompt_shows_no_block(client, anna):
    _role("adult", "")
    make_model()
    html = client.get(reverse("chat:index")).content.decode()
    assert "Fester Prompt deiner Rolle" not in html


def test_shared_chat_recipient_sees_own_role_prompt(client):
    owner_role = Role.objects.create(
        name="Anna-Rolle", key="anna-rolle", all_models=True, fixed_system_prompt="ANNA-REGEL"
    )
    ben_role = Role.objects.create(
        name="Ben-Rolle", key="ben-rolle", all_models=True, fixed_system_prompt="BEN-REGEL"
    )
    anna = User.objects.create_user("anna", password="Geheim-Test-1234", role=owner_role)
    ben = User.objects.create_user("ben", password="Geheim-Test-1234", role=ben_role)
    make_model()
    conv = Conversation.objects.create(user=anna)
    share(conv, ben, can_write=True)
    client.force_login(ben)
    html = client.get(reverse("chat:conversation", args=[conv.pk])).content.decode()
    assert "Fester Prompt deiner Rolle (Ben-Rolle)" in html and "BEN-REGEL" in html
    assert "ANNA-REGEL" not in html
    client.force_login(anna)
    html = client.get(reverse("chat:conversation", args=[conv.pk])).content.decode()
    assert "ANNA-REGEL" in html and "BEN-REGEL" not in html
