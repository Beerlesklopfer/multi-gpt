"""Fähigkeiten von Modellen: Erkennung (Heuristik, LM Studio), Anlage neuer
Modelle, Admin-Matrix und -Aktion, Befehl ``guess_capabilities``,
System-Hinweis zur Websuche und MCP-Freigabe je Modell.

HTTP (LM Studio, OpenAI) über respx; MCP über den Testserver im Prozess
(Fixtures aus ``test_tool_loop``).
"""

import json
import re

import httpx
import pytest
import respx
from django.contrib.messages import get_messages
from django.core.management import call_command
from django.urls import reverse
from mcp import Client

from multigpt.accounts.models import Role, User
from multigpt.chat import capabilities, detect, status, tooling, websearch
from multigpt.chat.mcp import bridge
from multigpt.chat.mcp import client as mcp_client
from multigpt.chat.mcp import service as mcp_service
from multigpt.chat.models import (
    AIModel,
    Conversation,
    McpServer,
    Message,
    Provider,
    SearchSettings,
    ToolCall,
)
from multigpt.chat.providers import registry
from multigpt.rag import settings_form
from tests import mcp_test_server
from tests.test_tool_loop import (
    ALL_TOOLS,
    ScriptedAdapter,
    answer,
    call,
    confirm,
    events_of,
    names,
    send,
    tool_round,
)

pytestmark = pytest.mark.django_db

PASSWORD = "Geheim-Test-1234"
LMS = "http://lmstudio.test:1234"
OPENAI = "https://api.openai.com/v1"


@pytest.fixture
def mock():
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def lmstudio():
    return Provider.objects.create(
        name="LM Studio",
        kind=Provider.Kind.OPENAI_COMPAT,
        base_url=f"{LMS}/v1",
        is_local=True,
        check_status=True,
    )


@pytest.fixture
def openai():
    return Provider.objects.create(
        name="OpenAI", kind=Provider.Kind.OPENAI_COMPAT, api_key="sk-test-1234"
    )


@pytest.fixture
def admin_client(client, django_user_model):
    user = django_user_model.objects.create_superuser(
        username="jo", password=PASSWORD, email="jo@example.invalid"
    )
    client.force_login(user)
    return client


def make_user(role_key, username):
    role = Role.objects.get(key=role_key)
    return User.objects.create_user(username, password=PASSWORD, role=role)


def models_response(*ids):
    return httpx.Response(200, json={"object": "list", "data": [{"id": i} for i in ids]})


def v0_response(*items):
    return httpx.Response(200, json={"object": "list", "data": list(items)})


def texts(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


# --- Heuristik ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        # OpenAI
        ("gpt-5.5", True),
        ("gpt-5.1-chat-latest", True),
        ("gpt-4o-mini", True),
        ("gpt-4.1-nano", True),
        ("o3", True),
        ("o4-mini", True),
        ("o3-deep-research", False),
        ("o1-mini", False),
        ("chatgpt-4o-latest", False),
        ("gpt-4o-search-preview", False),
        ("gpt-4o-realtime-preview", False),
        ("gpt-4o-transcribe", False),
        ("gpt-image-1", False),
        ("text-embedding-3-small", False),
        # Anthropic
        ("claude-3-haiku-20240307", True),
        ("claude-3-5-sonnet-latest", True),
        ("claude-sonnet-4-5", True),
        ("claude-opus-4-1", True),
        ("claude-opus-5", True),
        ("claude-2.1", False),
        # Google
        ("gemini-1.5-pro", True),
        ("gemini-2.5-flash", True),
        ("models/gemini-3-pro-preview", True),
        ("gemini-2.5-flash-image", False),
        ("gemini-2.5-flash-preview-tts", False),
        ("gemini-embedding-001", False),
        ("gemma-3-27b-it", False),
        # Lokal
        ("openai/gpt-oss-20b", True),
        ("qwen/qwen3-coder-30b", True),
        ("qwen2.5-7b-instruct", True),
        ("qwen2.5-coder-14b-instruct", True),
        ("meta-llama-3.1-8b-instruct", True),
        ("llama-3.3-70b-instruct", True),
        ("mistral-7b-instruct-v0.3", True),
        ("mistralai/mistral-small-3.2", True),
        ("mistral-nemo-instruct-2407", True),
        ("ministral-8b-instruct", True),
        ("command-r-plus", True),
        ("hermes-3-llama-3.1-8b", True),
        ("ibm/granite-3.3-8b", True),
        ("functionary-small-v3.2", True),
        ("deepseek-chat", True),
        ("deepseek-v3", True),
        ("deepseek-r1-distill-qwen-7b", False),
        ("mistral-7b-instruct-v0.2", False),
        ("google/gemma-3-12b", False),
        ("allenai/olmocr-7b-0725", False),
        ("qwen2.5-vl-7b-instruct", False),
        ("llava-v1.6-mistral-7b", False),
        ("llama-3.2-11b-vision-instruct", False),
        ("text-embedding-nomic-embed-text-v1.5", False),
        ("phi-4", False),
        ("", False),
    ],
)
def test_guess_tools(model_id, expected):
    assert capabilities.guess_tools(model_id) is expected


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("gpt-5.5", "chat"),
        ("gpt-image-1", "image"),
        ("dall-e-3", "image"),
        ("imagen-4.0-generate-001", "image"),
        ("whisper-1", "stt"),
        ("gpt-4o-transcribe", "stt"),
        ("tts-1-hd", "tts"),
        ("gpt-4o-mini-tts", "tts"),
        ("chatgpt-image-latest", "image"),
        ("gemini-2.5-flash-image", "image"),
        ("models/gemini-3-pro-image-preview", "image"),
        ("lyria-002", "music"),
        ("text-embedding-3-large", "embedding"),
        ("allenai/olmocr-7b-0725", "ocr"),
        ("allenai/olmOCR-2-7B-1025-FP8", "ocr"),
        ("deepseek-ocr", "ocr"),
    ],
)
def test_guess_capability(model_id, expected):
    assert capabilities.guess_capability(model_id) == expected


def test_guess_olmocr_is_ocr_without_tools():
    detected = capabilities.guess("allenai/olmocr-7b-0725")
    assert (detected.capability, detected.tools, detected.vision) == ("ocr", False, True)


def test_from_lmstudio():
    parse = capabilities.from_lmstudio
    assert parse({"id": "x", "type": "llm", "capabilities": ["tool_use"]}).tools is True
    assert parse({"id": "qwen3-8b", "type": "llm", "capabilities": []}).tools is False
    # Ältere LM-Studio-Versionen ohne capabilities: Werkzeuge nach Heuristik.
    assert parse({"id": "qwen3-8b", "type": "llm"}).tools is True
    vlm = parse({"id": "y", "type": "vlm", "capabilities": ["tool_use"]})
    assert (vlm.capability, vlm.tools, vlm.vision) == ("chat", True, True)
    emb = parse({"id": "z", "type": "embeddings"})
    assert (emb.capability, emb.tools, emb.vision) == ("embedding", False, False)
    assert parse({"id": "w"}) is None and parse("kaputt") is None


# --- LM Studio meldet Fähigkeiten -----------------------------------------------------

V0_ITEMS = [
    {"id": "mystery-model", "type": "llm", "capabilities": ["tool_use"]},
    {"id": "qwen3-8b", "type": "llm", "capabilities": []},
    {"id": "picture-model", "type": "vlm", "capabilities": []},
    {"id": "vectors", "type": "embeddings"},
]
V0_IDS = [item["id"] for item in V0_ITEMS]


def test_new_local_models_use_lmstudio_capabilities(lmstudio, mock):
    mock.get(f"{LMS}/v1/models").mock(return_value=models_response(*V0_IDS))
    v0 = mock.get(f"{LMS}/api/v0/models").mock(return_value=v0_response(*V0_ITEMS))
    status.force_check(lmstudio)
    models = {m.model_id: m for m in lmstudio.ai_models.all()}
    assert models["mystery-model"].supports_tools is True  # Heuristik hätte nein gesagt
    assert models["qwen3-8b"].supports_tools is False  # Heuristik hätte ja gesagt
    assert models["picture-model"].supports_vision is True
    assert models["vectors"].capability == AIModel.Capability.EMBEDDING
    assert all(m.mcp_access == AIModel.McpAccess.NONE for m in models.values())
    assert v0.call_count == 1
    # Keine neuen Modelle: kein weiterer Abruf von /api/v0/models.
    status.force_check(lmstudio)
    assert v0.call_count == 1


@pytest.mark.parametrize(
    "v0", [httpx.Response(404, text="nope"), httpx.ConnectError("x"), httpx.Response(200, text="?")]
)
def test_lmstudio_capabilities_failure_falls_back(lmstudio, mock, v0):
    mock.get(f"{LMS}/v1/models").mock(return_value=models_response("mystery-model", "qwen3-8b"))
    route = mock.get(f"{LMS}/api/v0/models")
    if isinstance(v0, Exception):
        route.mock(side_effect=v0)
    else:
        route.mock(return_value=v0)
    result = status.force_check(lmstudio)
    assert result.online
    models = {m.model_id: m for m in lmstudio.ai_models.all()}
    assert models["mystery-model"].supports_tools is False
    assert models["qwen3-8b"].supports_tools is True


def test_existing_models_are_not_overwritten(lmstudio, mock):
    AIModel.objects.create(
        provider=lmstudio, model_id="mystery-model", display_name="Eigen", supports_tools=False
    )
    mock.get(f"{LMS}/v1/models").mock(return_value=models_response("mystery-model", "qwen3-8b"))
    mock.get(f"{LMS}/api/v0/models").mock(return_value=v0_response(*V0_ITEMS))
    status.force_check(lmstudio)
    kept = lmstudio.ai_models.get(model_id="mystery-model")
    assert (kept.display_name, kept.supports_tools) == ("Eigen", False)


def test_cloud_provider_does_not_ask_lmstudio_api(openai, mock):
    v0 = mock.get("https://api.openai.com/api/v0/models")
    assert detect.detect(openai, ["gpt-5.5"])["gpt-5.5"].source == "heuristik"
    assert v0.call_count == 0


# --- sync_models und Admin-Übernahme --------------------------------------------------


def test_sync_models_sets_capabilities(openai, mock):
    mock.get(f"{OPENAI}/models").mock(
        return_value=models_response("gpt-5.5", "gpt-image-1", "whisper-1", "tts-1", "o3")
    )
    call_command("sync_models", stdout=open("/dev/null", "w"))
    models = {m.model_id: m for m in openai.ai_models.all()}
    assert models["gpt-5.5"].supports_tools is True
    assert models["gpt-5.5"].supports_vision is True
    assert models["o3"].supports_tools is True
    assert models["gpt-image-1"].capability == "image"
    assert models["gpt-image-1"].supports_tools is False
    assert models["whisper-1"].capability == "stt"
    assert models["tts-1"].capability == "tts"
    # Bekannter Cloud-Anbieter: MCP „alle“.
    assert {m.mcp_access for m in models.values()} == {"all"}


def test_default_mcp_access_by_provider():
    def make(name, **kw):
        return Provider.objects.create(name=name, **kw)

    assert detect.default_mcp_access(make("A", kind="anthropic")) == "all"
    assert detect.default_mcp_access(make("G", kind="google")) == "all"
    assert detect.default_mcp_access(make("O", kind="openai_compat")) == "all"
    router = make("R", kind="openai_compat", base_url="https://openrouter.ai/api/v1")
    assert detect.default_mcp_access(router) == "none"
    local = make("L", kind="openai_compat", base_url=f"{LMS}/v1", is_local=True)
    assert detect.default_mcp_access(local) == "none"


def test_admin_adopt_sets_capabilities(admin_client, lmstudio, mock):
    mock.get(f"{LMS}/v1/models").mock(return_value=models_response("mystery-model", "qwen3-8b"))
    mock.get(f"{LMS}/api/v0/models").mock(return_value=v0_response(*V0_ITEMS))
    lmstudio.check_status = False  # sonst legt die Prüfung die Modelle schon an
    lmstudio.is_local = False
    lmstudio.save()
    url = reverse("admin:chat_provider_select_models", args=[lmstudio.pk])
    admin_client.get(url)
    Provider.objects.filter(pk=lmstudio.pk).update(is_local=True)
    response = admin_client.post(
        url,
        {
            "take": ["mystery-model", "qwen3-8b"],
            "cap:mystery-model": "chat",
            "cap:qwen3-8b": "embedding",
            "active": ["mystery-model"],
        },
    )
    assert response.status_code == 302
    models = {m.model_id: m for m in lmstudio.ai_models.all()}
    assert models["mystery-model"].supports_tools is True  # Meldung von LM Studio
    assert models["mystery-model"].mcp_access == "none"
    # Vom Verwalter als Embedding übernommen: ohne Werkzeuge.
    assert models["qwen3-8b"].supports_tools is False


# --- Admin: Matrix, Aktion, Befehl ----------------------------------------------------


def test_admin_matrix_inline_saves_checkboxes(
    admin_client, openai, django_capture_on_commit_callbacks
):
    model = AIModel.objects.create(provider=openai, model_id="gpt-x", display_name="X")
    server = McpServer.objects.create(name="S", transport="stdio", command="x")
    url = reverse("admin:chat_provider_change", args=[openai.pk])
    html = admin_client.get(url).content.decode()
    for label in ("Werkzeuge", "Bilder verstehen", "Bilder bearbeiten", "MCP"):
        assert label in html
    data = {
        "name": "OpenAI",
        "kind": "openai_compat",
        "base_url": "",
        "api_key": "",
        "ai_models-TOTAL_FORMS": "2",
        "ai_models-INITIAL_FORMS": "1",
        "ai_models-0-id": str(model.pk),
        "ai_models-0-provider": str(openai.pk),
        "ai_models-0-model_id": "gpt-x",
        "ai_models-0-display_name": "X",
        "ai_models-0-capability": "chat",
        "ai_models-0-supports_tools": "on",
        "ai_models-0-can_edit_images": "on",
        "ai_models-0-mcp_access": "selected",
        "ai_models-0-active": "on",
        "ai_models-0-sort_order": "0",
        "ai_models-1-provider": str(openai.pk),
        "ai_models-1-model_id": "lyria-002",
        "ai_models-1-display_name": "Lyria",
        "ai_models-1-capability": "music",
        "ai_models-1-mcp_access": "none",
        "ai_models-1-sort_order": "0",
    }
    with django_capture_on_commit_callbacks(execute=False):
        response = admin_client.post(url, data)
    assert response.status_code == 302, response.content.decode()[:2000]
    model.refresh_from_db()
    assert (model.supports_tools, model.supports_vision, model.can_edit_images) == (
        True,
        False,
        True,
    )
    assert model.mcp_access == "selected"
    assert AIModel.objects.get(model_id="lyria-002").capability == "music"
    # Ausgewählte Server im Detailformular.
    detail = reverse("admin:chat_aimodel_change", args=[model.pk])
    assert 'name="mcp_servers"' in admin_client.get(detail).content.decode()
    server_page = reverse("admin:chat_mcpserver_change", args=[server.pk])
    assert "Kein Modell." in admin_client.get(server_page).content.decode()
    model.mcp_servers.add(server)
    assert "X</a> (OpenAI, ausgewählte)" in admin_client.get(server_page).content.decode()


def test_admin_matrix_changelist_list_editable(admin_client, openai):
    model = AIModel.objects.create(provider=openai, model_id="gpt-x", display_name="X")
    url = reverse("admin:chat_aimodel_changelist")
    response = admin_client.post(
        url,
        {
            "form-TOTAL_FORMS": "1",
            "form-INITIAL_FORMS": "1",
            "form-0-id": str(model.pk),
            "form-0-capability": "chat",
            "form-0-supports_tools": "on",
            "form-0-supports_vision": "on",
            "form-0-mcp_access": "all",
            "form-0-active": "on",
            "form-0-sort_order": "3",
            "_save": "Sichern",
        },
    )
    assert response.status_code == 302
    model.refresh_from_db()
    assert (model.supports_tools, model.supports_vision, model.can_edit_images) == (
        True,
        True,
        False,
    )
    assert (model.mcp_access, model.sort_order) == ("all", 3)


def test_admin_add_model_mcp_default_by_provider(admin_client, lmstudio, openai):
    url = reverse("admin:chat_aimodel_add")
    # Leere Verwaltungsformulare der Inlines (z. B. Preise) aus der Seite übernehmen.
    html = admin_client.get(url).content.decode()
    management = {name: "0" for name in re.findall(r'name="([^"]+-(?:TOTAL|INITIAL)_FORMS)"', html)}
    base = {
        "capability": "chat",
        "mcp_access": "none",
        "sort_order": "0",
        "active": "on",
        **management,
    }
    for provider, model_id in ((openai, "gpt-y"), (lmstudio, "local-y")):
        data = {**base, "provider": provider.pk, "model_id": model_id, "display_name": model_id}
        assert admin_client.post(url, data).status_code == 302
    assert AIModel.objects.get(model_id="gpt-y").mcp_access == "all"
    assert AIModel.objects.get(model_id="local-y").mcp_access == "none"


def _action(admin_client, models, **extra):
    return admin_client.post(
        reverse("admin:chat_aimodel_changelist"),
        {
            "action": "detect_capabilities_action",
            "_selected_action": [m.pk for m in models],
            **extra,
        },
    )


def test_admin_action_preview_then_apply(admin_client, openai):
    gpt = AIModel.objects.create(provider=openai, model_id="gpt-5.5", display_name="GPT")
    image = AIModel.objects.create(provider=openai, model_id="gpt-image-1", display_name="Img")
    same = AIModel.objects.create(provider=openai, model_id="phi-4", display_name="Phi")
    response = _action(admin_client, [gpt, image, same])
    assert response.status_code == 200
    html = response.content.decode()
    assert "Werkzeuge: nein → ja" in html and "Bilder verstehen: nein → ja" in html
    assert "Fähigkeit: Chat → Bilderzeugung" in html
    gpt.refresh_from_db()
    assert gpt.supports_tools is False  # Vorschau speichert nichts
    response = _action(admin_client, [gpt, image, same], apply="1")
    assert response.status_code == 302
    gpt.refresh_from_db()
    image.refresh_from_db()
    assert (gpt.supports_tools, gpt.supports_vision) == (True, True)
    assert image.capability == "image"
    assert "2 Modelle geändert, 1 unverändert." in texts(response)
    assert any("„GPT“: Werkzeuge: nein → ja" in t for t in texts(response))


def test_admin_action_nothing_to_change(admin_client, openai):
    same = AIModel.objects.create(provider=openai, model_id="phi-4", display_name="Phi")
    response = _action(admin_client, [same])
    assert response.status_code == 302
    assert any("Keine Änderungen" in t for t in texts(response))


def test_command_preview_and_apply(openai, lmstudio, mock, capsys):
    gpt = AIModel.objects.create(provider=openai, model_id="gpt-5.5", display_name="GPT")
    local = AIModel.objects.create(provider=lmstudio, model_id="mystery-model", display_name="M")
    mock.get(f"{LMS}/api/v0/models").mock(return_value=v0_response(*V0_ITEMS))
    call_command("guess_capabilities")
    out = capsys.readouterr().out
    assert "OpenAI / gpt-5.5 (Modell-ID): Werkzeuge: nein → ja" in out
    assert "LM Studio / mystery-model (LM Studio): Werkzeuge: nein → ja" in out
    assert "2 würden geändert" in out
    gpt.refresh_from_db()
    assert gpt.supports_tools is False
    call_command("guess_capabilities", "--apply")
    gpt.refresh_from_db()
    local.refresh_from_db()
    assert gpt.supports_tools is True and local.supports_tools is True
    assert local.mcp_access == "none"  # MCP-Freigabe bleibt unverändert
    call_command("guess_capabilities", "--provider", "OpenAI")
    assert "0 würden geändert" in capsys.readouterr().out


# --- Auswahlen nach Hauptart ----------------------------------------------------------


def test_only_chat_models_in_chat_selection(client, openai):
    for model_id, cap in [("gpt-5.5", "chat"), ("lyria-002", "music"), ("gpt-image-1", "image")]:
        AIModel.objects.create(
            provider=openai, model_id=model_id, display_name=model_id, capability=cap
        )
    client.force_login(make_user("adult", "erwachsen"))
    data = client.get(reverse("chat:api_models")).json()
    assert [m["display_name"] for m in data] == ["gpt-5.5"]
    assert data[0]["mcp_access"] == "none" and data[0]["mcp_server_ids"] == []


def test_music_not_offered_for_ocr_or_embedding():
    assert not settings_form._fits(settings_form.OCR, AIModel.Capability.MUSIC)
    assert not settings_form._fits(settings_form.EMBEDDING, AIModel.Capability.MUSIC)
    assert settings_form._fits(settings_form.OCR, AIModel.Capability.CHAT)


# --- System-Hinweis Websuche (Request-Body an den Anbieter) ---------------------------


def sse_body(text="Antwort."):
    chunks = [
        {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 1}},
    ]
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


@pytest.fixture
def search_on():
    return SearchSettings.objects.create(
        enabled=True, searxng_url="http://searx.intern:8888", max_results=3
    )


@pytest.mark.parametrize("tools", [False, True])
def test_system_hint_in_request_body(client, openai, mock, search_on, tools):
    model = AIModel.objects.create(
        provider=openai, model_id="gpt-5.5", display_name="GPT", supports_tools=tools
    )
    user = make_user("adult", "erwachsen")
    client.force_login(user)
    conversation = Conversation.objects.create(user=user)
    route = mock.post(f"{OPENAI}/chat/completions").mock(
        return_value=httpx.Response(
            200, text=sse_body(), headers={"Content-Type": "text/event-stream"}
        )
    )
    events = events_of(send(client, conversation, model=model.pk, web_search=False))
    assert names(events)[-1] == "done"
    body = json.loads(route.calls[0].request.content)
    system = body["messages"][0]
    assert system["role"] == "system"
    if tools:
        assert websearch.HINT_TOOL in system["content"]
        assert websearch.HINT_SWITCH not in system["content"]
        assert "web_search" in [t["function"]["name"] for t in body["tools"]]
    else:
        assert websearch.HINT_SWITCH in system["content"]
        assert "tools" not in body


def test_system_hint_rules(search_on):
    adult = make_user("adult", "erwachsen")
    assert websearch.system_hint(adult, {}, True) == websearch.HINT_TOOL
    assert websearch.system_hint(adult, {}, False) == websearch.HINT_SWITCH
    assert websearch.system_hint(adult, {"web_search": True}, False) == ""
    search_on.enabled = False
    search_on.save()
    assert websearch.system_hint(adult, {}, False) == ""


def test_web_search_tool_offered_with_supports_tools(openai, search_on):
    adult = make_user("adult", "erwachsen")
    model = AIModel.objects.create(provider=openai, model_id="m", display_name="M")
    assert "web_search" not in tooling.builtin_bindings(adult, model)
    model.supports_tools = True
    assert "web_search" in tooling.builtin_bindings(adult, model)


# --- MCP-Freigabe je Modell -----------------------------------------------------------


@pytest.fixture
def scripted(monkeypatch):
    """Fake-Anbieter mit vorgegebenen Runden (wie in ``test_tool_loop``)."""

    def install(script):
        calls = []
        monkeypatch.setattr(
            registry, "get_adapter", lambda provider: ScriptedAdapter(provider, script, calls)
        )
        return calls

    return install


@pytest.fixture
def mcp_inproc(monkeypatch, settings, tmp_path):
    """MCP-Testserver im Prozess; zeichnet tatsächliche Ausführungen auf."""
    settings.MEDIA_ROOT = tmp_path / "media"
    bridge.shutdown()
    monkeypatch.setattr(
        mcp_client, "_make_client", lambda config: Client(mcp_test_server.server, cache=None)
    )
    log = []
    original = mcp_service.call_tool

    def spy(server, name, arguments=None, timeout=None):
        log.append((name, arguments))
        return original(server, name, arguments, timeout)

    monkeypatch.setattr(mcp_service, "call_tool", spy)
    yield log
    bridge.shutdown()


@pytest.fixture
def tool_model():
    provider = Provider.objects.create(name="Cloud", kind=Provider.Kind.ANTHROPIC)
    return AIModel.objects.create(
        provider=provider, model_id="claude-test", display_name="Claude", supports_tools=True
    )


@pytest.fixture
def server():
    return McpServer.objects.create(
        name="Test",
        transport=McpServer.Transport.STDIO,
        command="unbenutzt",
        known_tools=ALL_TOOLS,
        tools_requiring_confirmation=["echo"],
        timeout_seconds=5,
    )


@pytest.fixture
def other_server():
    return McpServer.objects.create(
        name="Zweiter", transport=McpServer.Transport.STDIO, command="x", known_tools=ALL_TOOLS
    )


@pytest.fixture
def adult(client):
    user = make_user("adult", "erwachsen")
    client.force_login(user)
    return user


def test_new_model_default_none(tool_model):
    assert AIModel().mcp_access == AIModel.McpAccess.NONE
    assert tool_model.mcp_access == "none"


@pytest.mark.parametrize(
    ("access", "selected", "expected"),
    [("none", False, []), ("all", False, ["Test", "Zweiter"]), ("selected", True, ["Zweiter"])],
)
def test_offered_servers_by_mcp_access(
    client,
    adult,
    tool_model,
    server,
    other_server,
    scripted,
    mcp_inproc,
    access,
    selected,
    expected,
):
    tool_model.mcp_access = access
    tool_model.save()
    if selected:
        tool_model.mcp_servers.add(other_server)
    assert [s.name for s in tooling.available_servers(adult, tool_model)] == expected
    conversation = Conversation.objects.create(user=adult)
    calls = scripted([answer()])
    events_of(send(client, conversation, model=tool_model.pk))
    offered = {t.name.split("__")[0] for t in calls[0]["tools"] or [] if "__" in t.name}
    assert offered == set(expected)
    # API: Freigabe je Modell für die Oberfläche.
    data = {m["id"]: m for m in client.get(reverse("chat:api_models")).json()}
    ids = data[tool_model.pk]["mcp_server_ids"]
    if access == "all":
        assert ids is None
    else:
        assert ids == ([other_server.pk] if selected else [])


def test_intersection_with_role(tool_model, server, other_server):
    tool_model.mcp_access = "selected"
    tool_model.save()
    tool_model.mcp_servers.add(server, other_server)
    teen = make_user("teen", "jugend")
    role = Role.objects.get(key="teen")
    role.allowed_models.add(tool_model)
    role.allowed_mcp_servers.add(other_server)
    assert [s.name for s in tooling.available_servers(teen, tool_model)] == ["Zweiter"]
    tool_model.mcp_servers.remove(other_server)
    assert tooling.available_servers(teen, tool_model) == []


def test_disallowed_call_is_rejected(
    client, adult, tool_model, server, scripted, mcp_inproc, caplog
):
    executed = mcp_inproc
    # Gespeicherte Runde mit einem Server, den das Modell nicht nutzen darf
    # (z. B. Zuordnung aus einer Runde vor dem Entzug): Ablehnung mit Meldung.
    tool_model.mcp_access = "all"
    tool_model.save()
    conversation = Conversation.objects.create(user=adult)

    def revoke_then_round(index):
        AIModel.objects.filter(pk=tool_model.pk).update(mcp_access="none")
        return tool_round(call("Test__add", {"a": 1, "b": 2}))

    calls = scripted([revoke_then_round, answer()])
    events = events_of(send(client, conversation, model=tool_model.pk))
    assert executed == []
    tool_call = ToolCall.objects.get()
    assert tool_call.status == ToolCall.Status.ERROR
    assert tool_call.result["text"] == tooling.MSG_MODEL_NOT_ALLOWED
    assert calls[1]["messages"][-1].content == tooling.MSG_MODEL_NOT_ALLOWED
    assert "tool_result" in names(events)
    assert f"Modell {tool_model.pk} darf Server {server.pk} nicht nutzen" in caplog.text


def test_revoked_between_confirmation_and_approval(
    client, adult, tool_model, server, scripted, mcp_inproc
):
    executed = mcp_inproc
    tool_model.mcp_access = "selected"
    tool_model.save()
    tool_model.mcp_servers.add(server)
    conversation = Conversation.objects.create(user=adult)
    scripted([tool_round(call("Test__echo", {"text": "hi"})), answer("Erledigt.")])
    events = events_of(send(client, conversation, model=tool_model.pk))
    assert "confirmation_required" in names(events)
    tool_call = ToolCall.objects.get()
    # Verwalter entzieht die Freigabe, bevor der Nutzer bestätigt.
    tool_model.mcp_servers.remove(server)
    events = events_of(confirm(client, conversation, {str(tool_call.pk): "approve"}))
    assert executed == []
    tool_call.refresh_from_db()
    assert tool_call.status == ToolCall.Status.ERROR
    assert tool_call.result["text"] == tooling.MSG_MODEL_NOT_ALLOWED
    assert Message.objects.get(role="assistant").status == Message.Status.COMPLETE


def test_builtin_tools_independent_of_mcp_access(adult, tool_model, search_on):
    assert tool_model.mcp_access == "none"
    assert "web_search" in tooling.builtin_bindings(adult, tool_model)
