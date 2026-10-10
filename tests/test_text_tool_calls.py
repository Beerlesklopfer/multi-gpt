"""Werkzeugaufruf als Text und OCR-Modelle (Fehler „generate_image(…) als Antwort“).

- ``text_calls.detect``: nur eingebaute Werkzeuge, nur wenn die Antwort im
  Wesentlichen aus dem Aufruf besteht; nie MCP.
- Hinweis unter der Antwort (``chat/_tool_text.html``): nur bei fertigen
  Antworten, Argument escapt, Knopf erst durch tool_text.js.
- Hinweise auf Werkzeuge im System-Prompt nur, wenn sie angeboten werden
  (Request-Body an den Anbieter).
- Hauptart OCR: Erkennung, LM-Studio-Meldung, Chat-Auswahl, RAG-Auswahl, Datenmigration.
"""

import importlib
import json
from pathlib import Path

import pytest
import respx
from django.apps import apps
from django.urls import reverse

from multigpt.accounts.models import Role, User
from multigpt.chat import capabilities, images, status, text_calls
from multigpt.chat.models import AIModel, Conversation, Message, Provider, RagSettings
from multigpt.rag import settings_form

PASSWORD = "Geheim-Test-1234"
OPENAI = "https://api.openai.com/v1"
LMS = "http://lmstudio.test:1234"
STATIC = Path(__file__).parents[1] / "multigpt/chat/static/chat"
PROMPT = "Bundesadler mit Flagge schwarz rot gold"


# --- Erkennung -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "tool", "argument"),
    [
        (f'generate_image("{PROMPT}")', "generate_image", PROMPT),
        (f'  generate_image( prompt = "{PROMPT}" );\n', "generate_image", PROMPT),
        (f"generate_image('{PROMPT}')", "generate_image", PROMPT),
        (f'```python\ngenerate_image("{PROMPT}")\n```', "generate_image", PROMPT),
        ('web_search("Wetter Berlin")', "web_search", "Wetter Berlin"),
        ('web_search(query="Wetter \\"heute\\"")', "web_search", 'Wetter "heute"'),
        (
            json.dumps({"name": "generate_image", "arguments": {"prompt": PROMPT}}),
            "generate_image",
            PROMPT,
        ),
        (
            '<tool_call>\n{"name": "web_search", "arguments": {"query": "Wetter"}}\n</tool_call>',
            "web_search",
            "Wetter",
        ),
        (
            "```json\n"
            + json.dumps(
                {"function": {"name": "generate_image", "arguments": '{"prompt": "Ball"}'}}
            )
            + "\n```",
            "generate_image",
            "Ball",
        ),
        ('[{"name": "web_search", "parameters": {"query": "x"}}]', "web_search", "x"),
    ],
)
def test_detects_text_call(content, tool, argument):
    assert text_calls.detect(content) == {"tool": tool, "argument": argument}


@pytest.mark.parametrize(
    "content",
    [
        "",
        "Hier ist dein Bild.",
        # Mitten im Text: bleibt Text.
        f'Ich rufe jetzt generate_image("{PROMPT}") auf.',
        f'generate_image("{PROMPT}")\n\nUnd noch ein Satz.',
        # MCP-Werkzeuge und andere Namen nie.
        'delete_all("alles")',
        'filesystem__write_file("x")',
        json.dumps({"name": "run_python", "arguments": {"code": "print(1)"}}),
        json.dumps({"name": "mcp__delete", "arguments": {"prompt": "x"}}),
        # Kein String-Literal bzw. falsches Argument.
        "generate_image(prompt)",
        'generate_image(f"{x}")',
        'generate_image(query="x")',
        'generate_image("")',
        'generate_image("a", "b")',
        json.dumps({"name": "generate_image", "arguments": {"query": "x"}}),
        '{"name": "generate_image", "arguments": "kaputt"}',
        "```\n```",
        "generate_image(" + '"' + "x" * 5000 + '")',
    ],
)
def test_ignores_other_content(content):
    assert text_calls.detect(content) is None


def test_only_builtin_tools():
    assert set(text_calls.TOOLS) == {images.TOOL_NAME, "web_search"}


# --- Hinweis unter der Antwort ------------------------------------------------------------


@pytest.fixture
def adult(db, client):
    user = User.objects.create_user("jo", password=PASSWORD, role=Role.objects.get(key="adult"))
    client.force_login(user)
    return user


@pytest.fixture
def lm_model(db):
    provider = Provider.objects.create(name="LM", kind=Provider.Kind.OPENAI_COMPAT)
    return AIModel.objects.create(provider=provider, model_id="m", display_name="M")


def render(client, user, model, content, status="complete"):
    conversation = Conversation.objects.create(user=user, title="T")
    question = Message.objects.create(conversation=conversation, role="user", content=content)
    reply = Message.objects.create(
        conversation=conversation,
        role="assistant",
        content=content,
        parent=question,
        model=model,
        status=status,
    )
    Conversation.objects.filter(pk=conversation.pk).update(current_leaf=reply)
    return client.get(reverse("chat:conversation", args=[conversation.pk])).content.decode()


def test_hint_under_answer(client, adult, lm_model):
    page = render(client, adult, lm_model, f'generate_image("{PROMPT}")')
    assert page.count('class="tool-text-hint"') == 1  # nicht unter der Nutzernachricht
    assert 'data-tool-text="generate_image"' in page
    assert f'data-tool-text-argument="{PROMPT}"' in page
    assert "Das Modell hat den Werkzeugaufruf als Text geschrieben" in page
    # Knopf erst durch tool_text.js (nur, wenn Modus „Bild“ nutzbar ist).
    assert "data-tool-text-run hidden" in page
    assert "chat/tool_text.js" in page


def test_hint_argument_is_escaped(client, adult, lm_model):
    page = render(client, adult, lm_model, 'web_search("<img src=x onerror=alert(1)>")')
    assert "<img src=x" not in page
    assert 'data-tool-text-argument="&lt;img src=x onerror=alert(1)&gt;"' in page


@pytest.mark.parametrize("status", ["error", "aborted", "streaming"])
def test_no_hint_for_unfinished_answers(client, adult, lm_model, status):
    page = render(client, adult, lm_model, f'generate_image("{PROMPT}")', status=status)
    assert "tool-text-hint" not in page


def test_no_hint_for_normal_answers(client, adult, lm_model):
    assert "tool-text-hint" not in render(client, adult, lm_model, "Hier ist dein Bild.")


def test_script_never_sends_and_knows_no_mcp():
    js = (STATIC / "tool_text.js").read_text()
    assert "innerHTML" not in js
    assert "requestSubmit" not in js and ".submit(" not in js and "postJson" not in js
    assert "mcp" not in js.lower().replace("nie mcp", "")
    # Modus „Bild“ einschalten können, ohne zu senden.
    assert "activate: () => setActive(true)" in (STATIC / "image_mode.js").read_text()


# --- Werkzeughinweise nur mit angebotenen Werkzeugen (Request-Body) -----------------------


def sse_body(text):
    chunks = [
        {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


@pytest.mark.parametrize("tools", [False, True])
def test_image_tool_mentioned_only_when_offered(client, adult, settings, tmp_path, tools):
    settings.MEDIA_ROOT = tmp_path
    provider = Provider.objects.create(
        name="OpenAI", kind=Provider.Kind.OPENAI_COMPAT, api_key="sk-test-1234"
    )
    chat = AIModel.objects.create(
        provider=provider, model_id="gpt-5.5", display_name="GPT", supports_tools=tools
    )
    AIModel.objects.create(
        provider=provider,
        model_id="gpt-image-1",
        display_name="Bild",
        capability=AIModel.Capability.IMAGE,
    )
    assert images.pick_image_model(adult)[0] is not None  # Bildmodell nutzbar
    conversation = Conversation.objects.create(user=adult)
    with respx.mock() as router:
        route = router.post(f"{OPENAI}/chat/completions").respond(
            200,
            text=sse_body(f'generate_image("{PROMPT}")'),
            headers={"Content-Type": "text/event-stream"},
        )
        response = client.post(
            reverse("chat:api_messages", args=[conversation.pk]),
            json.dumps({"content": f"Zeichne den {PROMPT}", "model": chat.pk}),
            content_type="application/json",
        )
        b"".join(response.streaming_content)
    raw = route.calls[0].request.content.decode()
    body = json.loads(raw)
    if tools:
        assert images.TOOL_NAME in [t["function"]["name"] for t in body["tools"]]
        assert images.HINT in body["messages"][0]["content"]
    else:
        assert "tools" not in body
        assert images.TOOL_NAME not in raw
        assert "Werkzeug generate_image" not in raw
    # Die Antwort ist nur Text; nichts wurde ausgeführt, kein Bild entstand.
    reply = Message.objects.get(conversation=conversation, role="assistant")
    assert reply.attachments.count() == 0
    assert text_calls.detect(reply.content)["tool"] == images.TOOL_NAME


# --- Hauptart OCR ---------------------------------------------------------------------------


def test_lmstudio_tool_use_does_not_make_olmocr_a_chat_model():
    item = {"id": "allenai/olmocr-2-7b", "type": "vlm", "capabilities": ["tool_use"]}
    detected = capabilities.from_lmstudio(item)
    assert (detected.capability, detected.tools, detected.vision) == ("ocr", False, True)
    # Andere Vision-Modelle bleiben Chat mit gemeldeten Werkzeugen.
    other = capabilities.from_lmstudio(
        {"id": "qwen2.5-vl-7b", "type": "vlm", "capabilities": ["tool_use"]}
    )
    assert (other.capability, other.tools) == ("chat", True)


def test_reported_olmocr_is_not_offered_in_chat(client, adult):
    lmstudio = Provider.objects.create(
        name="LM Studio",
        kind=Provider.Kind.OPENAI_COMPAT,
        base_url=f"{LMS}/v1",
        is_local=True,
        check_status=True,
    )
    items = [
        {"id": "allenai/olmocr-2-7b", "type": "vlm", "capabilities": ["tool_use"]},
        {"id": "qwen3-8b", "type": "llm", "capabilities": ["tool_use"]},
    ]
    with respx.mock() as router:
        router.get(f"{LMS}/v1/models").respond(
            200, json={"object": "list", "data": [{"id": i["id"]} for i in items]}
        )
        router.get(f"{LMS}/api/v0/models").respond(200, json={"object": "list", "data": items})
        status.force_check(lmstudio)
        data = client.get(reverse("chat:api_models")).json()
    ocr = AIModel.objects.get(model_id="allenai/olmocr-2-7b")
    assert (ocr.capability, ocr.supports_tools) == (AIModel.Capability.OCR, False)
    assert [m["display_name"] for m in data] == ["qwen3-8b"]


def test_ocr_models_in_rag_ocr_choice_not_for_figures(db):
    provider = Provider.objects.create(
        name="LM Studio", kind=Provider.Kind.OPENAI_COMPAT, is_local=True
    )
    ocr = AIModel.objects.create(
        provider=provider,
        model_id="allenai/olmocr-2-7b",
        display_name="olmOCR",
        capability=AIModel.Capability.OCR,
    )
    vision = AIModel.objects.create(
        provider=provider, model_id="qwen2.5-vl-7b", display_name="VL", supports_vision=True
    )
    values = json.dumps(settings_form.model_choices(settings_form.OCR))
    assert f"m{ocr.pk}" in values and f"m{vision.pk}" in values
    assert settings_form.first_recommended(settings_form.model_choices(settings_form.OCR)) == (
        f"m{ocr.pk}"
    )
    figures = json.dumps(settings_form.model_choices(settings_form.FIGURE))
    assert f"m{ocr.pk}" not in figures and f"m{vision.pk}" in figures
    # Bereits gewähltes OCR-Modell bleibt gültig (auch als Abbildungsmodell in der Liste).
    assert settings_form.resolve_choice(settings_form.OCR, f"m{ocr.pk}") == ocr
    assert f"m{ocr.pk}" in json.dumps(settings_form.model_choices(settings_form.FIGURE, ocr))
    cfg = RagSettings.load()
    cfg.ocr_backend = RagSettings.OcrBackend.OLMOCR
    cfg.ocr_model = ocr
    cfg.clean()


def test_new_olmocr_from_rag_choice_is_ocr(db):
    provider = Provider.objects.create(
        name="LM Studio",
        kind=Provider.Kind.OPENAI_COMPAT,
        is_local=True,
        reported_models=["allenai/olmocr-2-7b", "qwen2.5-vl-7b"],
    )
    olmocr = settings_form.resolve_choice(settings_form.OCR, f"r{provider.pk}:allenai/olmocr-2-7b")
    assert olmocr.capability == AIModel.Capability.OCR
    other = settings_form.resolve_choice(settings_form.OCR, f"r{provider.pk}:qwen2.5-vl-7b")
    assert other.capability == AIModel.Capability.CHAT


def test_data_migration_moves_only_chat_olmocr(db):
    migration = importlib.import_module("multigpt.chat.migrations.0030_aimodel_capability_ocr")
    provider = Provider.objects.create(name="LM", kind=Provider.Kind.OPENAI_COMPAT)

    def make(model_id, capability):
        return AIModel.objects.create(
            provider=provider, model_id=model_id, display_name=model_id, capability=capability
        )

    chat_ocr = make("allenai/olmOCR-2-7b", "chat")
    image_ocr = make("olmocr-bild", "image")  # bewusst andere Hauptart: bleibt
    plain = make("qwen3-8b", "chat")
    for _ in range(2):  # idempotent
        migration.olmocr_to_ocr(apps, None)
    caps = dict(AIModel.objects.values_list("pk", "capability"))
    assert caps == {chat_ocr.pk: "ocr", image_ocr.pk: "image", plain.pk: "chat"}
