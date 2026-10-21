"""Bilderzeugung (M9-01): Adapter, Anhang, Auswahl des Bildmodells, Werkzeug,
Modus „Bild“, Hinweis bei Bildwunsch, Teilen-Rechte und Kosten.

Keine echten Anbieteraufrufe: HTTP über respx mit Beispiel-Payloads aus der
Doku (OpenAI Images API, Gemini generateContent), sonst Fake-Adapter.
"""

import base64
import io
import json
import re
from decimal import Decimal

import httpx
import pytest
import respx
from django.urls import reverse
from PIL import Image, PngImagePlugin

from multigpt.accounts import permissions
from multigpt.accounts import usage as accounts_usage
from multigpt.accounts.models import Role, User
from multigpt.billing.models import UsageEntry
from multigpt.chat import api_images, images, services, tooling
from multigpt.chat.models import (
    AIModel,
    Attachment,
    ChatSettings,
    Conversation,
    Message,
    Provider,
    Share,
    ToolCall,
)
from multigpt.chat.providers import registry
from multigpt.chat.providers.base import (
    MSG_CONTENT_BLOCKED,
    MSG_KEY_EXPIRED,
    MSG_NO_IMAGE,
    ContentBlocked,
    Delta,
    Done,
    GeneratedImage,
    ImageResult,
    ProviderAdapter,
    ProviderError,
    ToolCallEvent,
    Usage,
)
from multigpt.chat.providers.google import GoogleAdapter
from multigpt.chat.providers.openai_compat import OpenAICompatAdapter
from tests.billing_helpers import set_price

PASSWORD = "Geheim-Test-1234"
KEY = "sk-test-GEHEIM-1234567890"
OPENAI = "https://api.openai.com/v1"
GEMINI = "https://generativelanguage.googleapis.com/v1beta"


# --- Testbilder -----------------------------------------------------------------------


def png_with_metadata(size=(64, 48), color=(200, 30, 30, 255)) -> bytes:
    """PNG mit Textblock (wie ihn Generatoren mit Prompt/Software füllen)."""
    info = PngImagePlugin.PngInfo()
    info.add_text("parameters", "geheimer Prompt")
    info.add_text("Software", "Generator 1.0")
    out = io.BytesIO()
    Image.new("RGBA", size, color).save(out, "PNG", pnginfo=info)
    return out.getvalue()


def jpeg_with_exif(size=(64, 48)) -> bytes:
    exif = Image.Exif()
    exif[0x010F] = "Kamera"  # Make
    exif[0x0131] = "Generator"  # Software
    out = io.BytesIO()
    Image.new("RGB", size, (0, 90, 200)).save(out, "JPEG", exif=exif.tobytes())
    return out.getvalue()


PNG = png_with_metadata()
B64 = base64.b64encode(PNG).decode()

# Antwort laut OpenAI-Doku (images/create, gekürzt auf die Felder von gpt-image-1).
OPENAI_PAYLOAD = {
    "created": 1713833628,
    "background": "opaque",
    "data": [{"b64_json": B64}],
    "output_format": "png",
    "quality": "high",
    "size": "1024x1536",
    "usage": {
        "total_tokens": 100,
        "input_tokens": 50,
        "output_tokens": 50,
        "input_tokens_details": {"text_tokens": 10, "image_tokens": 40},
    },
}
# Fehler des Inhaltsfilters (Form wie von OpenAI gemeldet: 400, moderation_blocked).
OPENAI_BLOCKED = {
    "error": {
        "message": "Your request was rejected by the safety system. request ID req_123. "
        "safety_violations=[sexual].",
        "type": "image_generation_user_error",
        "param": None,
        "code": "moderation_blocked",
    }
}
# Gemini generateContent mit Bildausgabe (candidates[].content.parts[].inlineData).
GEMINI_PAYLOAD = {
    "candidates": [
        {
            "content": {
                "role": "model",
                "parts": [
                    {"text": "Hier ist das Bild."},
                    {"inlineData": {"mimeType": "image/png", "data": "AAAA"}, "thought": True},
                    {
                        "inlineData": {
                            "mimeType": "image/jpeg",
                            "data": base64.b64encode(jpeg_with_exif()).decode(),
                        }
                    },
                ],
            },
            "finishReason": "STOP",
            "index": 0,
        }
    ],
    "usageMetadata": {
        "promptTokenCount": 12,
        "candidatesTokenCount": 1290,
        "totalTokenCount": 1302,
        "candidatesTokensDetails": [{"modality": "IMAGE", "tokenCount": 1290}],
    },
}


def openai_adapter(api_key=KEY):
    return OpenAICompatAdapter(Provider(name="OpenAI", kind="openai_compat", api_key=api_key))


def gemini_adapter():
    return GoogleAdapter(Provider(name="Gemini", kind="google", api_key=KEY))


# --- Adapter: OpenAI ------------------------------------------------------------------


def test_openai_generate_image_success():
    with respx.mock(assert_all_called=True) as router:
        route = router.post(f"{OPENAI}/images/generations").respond(200, json=OPENAI_PAYLOAD)
        result = openai_adapter().generate_image(
            "gpt-image-1",
            "Deutschlandflagge",
            size="1024x1536",
            quality="high",
            background="transparent",
            n=1,
        )
    request = route.calls.last.request
    body = json.loads(request.content)
    assert body == {
        "model": "gpt-image-1",
        "prompt": "Deutschlandflagge",
        "n": 1,
        "size": "1024x1536",
        "quality": "high",
        "background": "transparent",
        "output_format": "png",
    }
    assert "response_format" not in body  # nur DALL·E
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    assert isinstance(result, ImageResult)
    assert result.images[0].data == PNG and result.images[0].mime_type == "image/png"
    assert (result.usage.tokens_in, result.usage.tokens_out) == (50, 50)


def test_openai_edit_image_sends_multipart_with_the_source():
    with respx.mock(assert_all_called=True) as router:
        route = router.post(f"{OPENAI}/images/edits").respond(200, json=OPENAI_PAYLOAD)
        result = openai_adapter().edit_image(
            "gpt-image-1",
            [("vorlage.png", "image/png", PNG)],
            "Mach den Kreis blau",
            size="1024x1024",
            quality="high",
            n=1,
        )
    request = route.calls.last.request
    assert request.headers["content-type"].startswith("multipart/form-data")
    body = request.content
    assert b'name="image"; filename="vorlage.png"' in body and PNG in body
    for field, value in (("model", "gpt-image-1"), ("prompt", "Mach den Kreis blau"), ("n", "1")):
        assert f'name="{field}"'.encode() in body and value.encode() in body
    assert b"response_format" not in body
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    assert result.images[0].data == PNG


def test_openai_edit_image_with_several_sources_uses_the_array_field():
    with respx.mock(assert_all_called=True) as router:
        route = router.post(f"{OPENAI}/images/edits").respond(200, json=OPENAI_PAYLOAD)
        openai_adapter().edit_image(
            "gpt-image-1", [("a.png", "image/png", PNG), ("b.png", "image/png", PNG)], "Mischen"
        )
    assert route.calls.last.request.content.count(b'name="image[]"') == 2


def test_openai_edit_image_needs_a_source():
    with pytest.raises(ProviderError):
        openai_adapter().edit_image("gpt-image-1", [], "Blau")


def test_openai_dalle3_uses_b64_and_own_sizes():
    with respx.mock() as router:
        route = router.post(f"{OPENAI}/images/generations").respond(
            200, json={"created": 1, "data": [{"b64_json": B64, "revised_prompt": "Flagge"}]}
        )
        result = openai_adapter().generate_image(
            "dall-e-3", "Flagge", size="1536x1024", quality="high"
        )
    body = json.loads(route.calls.last.request.content)
    assert body["response_format"] == "b64_json"
    assert body["size"] == "1792x1024" and body["quality"] == "hd"
    assert "output_format" not in body and "background" not in body
    assert result.images[0].revised_prompt == "Flagge" and result.usage is None


def test_openai_url_only_is_not_fetched():
    with respx.mock() as router:
        router.post(f"{OPENAI}/images/generations").respond(
            200, json={"data": [{"url": "http://intern.example/bild.png"}]}
        )
        with pytest.raises(ProviderError) as info:
            openai_adapter().generate_image("dall-e-2", "x")
    assert str(info.value) == MSG_NO_IMAGE  # keine fremde URL nachgeladen


def test_openai_content_filter_has_own_message(caplog):
    with respx.mock() as router:
        router.post(f"{OPENAI}/images/generations").respond(400, json=OPENAI_BLOCKED)
        with pytest.raises(ContentBlocked) as info:
            openai_adapter().generate_image("gpt-image-1", "etwas Verbotenes")
    assert str(info.value) == MSG_CONTENT_BLOCKED
    assert info.value.retryable is False
    assert "safety system" not in str(info.value)
    assert "etwas Verbotenes" not in caplog.text and KEY not in caplog.text


def test_openai_expired_key():
    body = {"error": {"message": f"API key expired: {KEY[:8]}…", "code": "invalid_api_key"}}
    with respx.mock() as router:
        router.post(f"{OPENAI}/images/generations").respond(401, json=body)
        with pytest.raises(ProviderError) as info:
            openai_adapter().generate_image("gpt-image-1", "x")
    assert str(info.value) == MSG_KEY_EXPIRED and info.value.expired
    assert KEY[:8] not in str(info.value)


def test_openai_invalid_key_without_raw_text():
    body = {"error": {"message": f"Incorrect API key provided: {KEY}", "code": "invalid_api_key"}}
    with respx.mock() as router:
        router.post(f"{OPENAI}/images/generations").respond(401, json=body)
        with pytest.raises(ProviderError) as info:
            openai_adapter().generate_image("gpt-image-1", "x")
    assert "API-Key prüfen" in str(info.value) and KEY not in str(info.value)
    assert not info.value.retryable


def test_openai_rate_limit_is_retryable():
    with respx.mock() as router:
        router.post(f"{OPENAI}/images/generations").respond(
            429, json={"error": {"message": "Rate limit", "code": "rate_limit_exceeded"}}
        )
        with pytest.raises(ProviderError) as info:
            openai_adapter().generate_image("gpt-image-1", "x")
    assert info.value.retryable and "zu viele Anfragen" in str(info.value)


def test_openai_network_error():
    with respx.mock() as router:
        router.post(f"{OPENAI}/images/generations").mock(side_effect=httpx.ConnectError("x"))
        with pytest.raises(ProviderError) as info:
            openai_adapter().generate_image("gpt-image-1", "x")
    assert info.value.unreachable


# --- Adapter: Google ------------------------------------------------------------------


def test_gemini_generate_image_success():
    url = f"{GEMINI}/models/gemini-2.5-flash-image:generateContent"
    with respx.mock() as router:
        route = router.post(url).respond(200, json=GEMINI_PAYLOAD)
        result = gemini_adapter().generate_image(
            "gemini-2.5-flash-image", "Flagge", size="1536x1024", quality="high"
        )
    request = route.calls.last.request
    assert request.headers["x-goog-api-key"] == KEY and "key=" not in str(request.url)
    body = json.loads(request.content)
    assert body["contents"] == [{"role": "user", "parts": [{"text": "Flagge"}]}]
    assert body["generationConfig"] == {
        "responseModalities": ["TEXT", "IMAGE"],
        "imageConfig": {"aspectRatio": "3:2"},
    }
    # Gedanken-Bild übersprungen, das eigentliche Bild übernommen.
    assert len(result.images) == 1 and result.images[0].mime_type == "image/jpeg"
    assert (result.usage.tokens_in, result.usage.tokens_out) == (12, 1290)


@pytest.mark.parametrize(
    "payload",
    [
        {"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}},
        {"candidates": [{"finishReason": "IMAGE_SAFETY", "content": {"parts": []}}]},
        {"candidates": [{"finishReason": "IMAGE_PROHIBITED_CONTENT"}]},
    ],
)
def test_gemini_blocked(payload):
    with respx.mock() as router:
        router.post(f"{GEMINI}/models/gemini-3-pro-image:generateContent").respond(
            200, json=payload
        )
        with pytest.raises(ContentBlocked):
            gemini_adapter().generate_image("gemini-3-pro-image", "x")


def test_gemini_without_image():
    payload = {"candidates": [{"finishReason": "NO_IMAGE", "content": {"parts": [{"text": "Nö"}]}}]}
    with respx.mock() as router:
        router.post(f"{GEMINI}/models/m:generateContent").respond(200, json=payload)
        with pytest.raises(ProviderError) as info:
            gemini_adapter().generate_image("m", "x")
    assert str(info.value) == MSG_NO_IMAGE


def test_gemini_expired_key():
    body = {
        "error": {
            "code": 400,
            "message": "API key expired. Please renew the API key.",
            "status": "INVALID_ARGUMENT",
            "details": [{"reason": "API_KEY_INVALID"}],
        }
    }
    with respx.mock() as router:
        router.post(f"{GEMINI}/models/m:generateContent").respond(400, json=body)
        with pytest.raises(ProviderError) as info:
            gemini_adapter().generate_image("m", "x")
    assert str(info.value) == MSG_KEY_EXPIRED


def test_gemini_rate_limit():
    body = {"error": {"code": 429, "message": "Quota", "status": "RESOURCE_EXHAUSTED"}}
    with respx.mock() as router:
        router.post(f"{GEMINI}/models/m:generateContent").respond(429, json=body)
        with pytest.raises(ProviderError) as info:
            gemini_adapter().generate_image("m", "x")
    assert info.value.retryable


# --- Daten und Fakes ------------------------------------------------------------------


class FakeImageAdapter(ProviderAdapter):
    """Bildanbieter: liefert ``image`` (bzw. wirft ``error``) und merkt sich die Aufrufe."""

    def __init__(self, provider, log, image=PNG, mime="image/png", error=None, usage=None):
        super().__init__(provider)
        self.log, self.image, self.mime, self.error, self.usage = log, image, mime, error, usage

    def generate_image(self, model_id, prompt, **params):
        self.log.append({"model": model_id, "prompt": prompt, **params})
        if self.error is not None:
            raise self.error
        return ImageResult([GeneratedImage(self.image, self.mime)], self.usage)

    def edit_image(self, model_id, images, prompt, **params):
        sizes = [(name, mime, len(raw)) for name, mime, raw in images]
        self.log.append(
            {"edit": True, "model": model_id, "prompt": prompt, "images": sizes, **params}
        )
        if self.error is not None:
            raise self.error
        return ImageResult([GeneratedImage(self.image, self.mime, "Ein blauer Kreis")], self.usage)


class ScriptedChat(ProviderAdapter):
    def __init__(self, provider, script, calls):
        super().__init__(provider)
        self.script, self.calls = script, calls

    def stream(self, model_id, messages, system=None, tools=None, **params):
        index = len(self.calls)
        self.calls.append({"messages": list(messages), "tools": tools, "system": system})
        return iter(list(self.script[min(index, len(self.script) - 1)]))

    def generate_image(self, model_id, prompt, **params):  # pragma: no cover - darf nie
        raise AssertionError("Das Chatmodell darf keine Bilder erzeugen.")


@pytest.fixture(autouse=True)
def media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"


@pytest.fixture
def chat_provider(db):
    return Provider.objects.create(name="Chat", kind=Provider.Kind.ANTHROPIC)


@pytest.fixture
def image_provider(db):
    return Provider.objects.create(name="OpenAI", kind=Provider.Kind.OPENAI_COMPAT, api_key=KEY)


@pytest.fixture
def chat_model(chat_provider):
    return AIModel.objects.create(
        provider=chat_provider, model_id="claude-test", display_name="Claude", supports_tools=True
    )


@pytest.fixture
def image_model(image_provider):
    return AIModel.objects.create(
        provider=image_provider,
        model_id="gpt-image-1",
        display_name="GPT Image",
        capability=AIModel.Capability.IMAGE,
        sort_order=5,
    )


def make_user(role_key, username):
    return User.objects.create_user(
        username, password=PASSWORD, role=Role.objects.get(key=role_key)
    )


@pytest.fixture
def adult(db, client):
    user = make_user("adult", "erwachsen")
    client.force_login(user)
    return user


@pytest.fixture
def conversation(adult, chat_model):
    return Conversation.objects.create(user=adult, default_model=chat_model)


@pytest.fixture
def fakes(monkeypatch):
    """fakes(chat_script, **bild) -> (chat_calls, image_calls)."""

    def install(script=(), **image_kwargs):
        chat_calls, image_calls = [], []

        def get_adapter(provider):
            if provider.kind == Provider.Kind.ANTHROPIC:
                return ScriptedChat(provider, list(script), chat_calls)
            return FakeImageAdapter(provider, image_calls, **image_kwargs)

        monkeypatch.setattr(registry, "get_adapter", get_adapter)
        return chat_calls, image_calls

    return install


def post(client, conversation, **data):
    return client.post(
        reverse("chat:api_messages", args=[conversation.pk]),
        json.dumps(data),
        content_type="application/json",
    )


def events_of(response):
    assert response.status_code == 200, response.content
    raw = b"".join(response.streaming_content).decode()
    events = []
    for block in raw.split("\n\n"):
        if block.strip():
            lines = dict(line.split(": ", 1) for line in block.split("\n"))
            events.append((lines["event"], json.loads(lines["data"])))
    return events


def stored_bytes(field) -> bytes:
    with field.open("rb") as handle:
        return handle.read()


# --- Anhang ohne Metadaten ------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    "data,mime", [(PNG, "image/png"), (jpeg_with_exif(), "image/jpeg")], ids=["png", "jpeg"]
)
def test_generated_image_is_reencoded_without_metadata(
    adult, conversation, image_model, fakes, data, mime
):
    fakes(image=data, mime=mime)
    msg = services.append_message(conversation, role="assistant", author=adult)
    attachment, _ = images.generate(adult, msg, "Flagge mit Adler", ai_model=image_model)
    attachment.refresh_from_db()
    assert attachment.message == msg and attachment.owner is None
    assert attachment.generated_by_model == image_model and attachment.tool_call is None
    assert attachment.kind == Attachment.Kind.IMAGE and attachment.thumbnail
    assert (attachment.width, attachment.height) == (64, 48)
    assert attachment.original_name.startswith("bild-flagge-mit-adler")
    stored = stored_bytes(attachment.file)
    assert b"geheimer Prompt" not in stored and b"Generator" not in stored
    image = Image.open(io.BytesIO(stored))
    assert not image.info.get("parameters") and not image.getexif()
    # Geschützte Auslieferung: Besitzer des Chats darf, Fremde nicht.
    assert attachment.url.startswith("/")


@pytest.mark.django_db
def test_oversized_image_is_rejected(adult, conversation, image_model, fakes, settings):
    settings.ATTACHMENT_MAX_IMAGE_MB = 0
    fakes()
    msg = services.append_message(conversation, role="assistant", author=adult)
    with pytest.raises(images.ImageError):
        images.generate(adult, msg, "x", ai_model=image_model)
    assert not Attachment.objects.exists()


@pytest.mark.django_db
def test_content_filter_message_reaches_user(adult, conversation, image_model, fakes):
    fakes(error=ContentBlocked())
    msg = services.append_message(conversation, role="assistant", author=adult)
    with pytest.raises(images.ImageError) as info:
        images.generate(adult, msg, "x", ai_model=image_model)
    assert info.value.message == MSG_CONTENT_BLOCKED


# --- Auswahl des Bildmodells ----------------------------------------------------------


@pytest.mark.django_db
def test_pick_default_model_first(adult, image_model, image_provider):
    other = AIModel.objects.create(
        provider=image_provider,
        model_id="gpt-image-2",
        display_name="Anderes",
        capability=AIModel.Capability.IMAGE,
        sort_order=9,
    )
    assert images.pick_image_model(adult) == (image_model, "")
    settings = ChatSettings.load()
    settings.default_image_model = other
    settings.save()
    assert images.pick_image_model(adult) == (other, "")


@pytest.mark.django_db
def test_pick_respects_role(image_model, image_provider):
    teen = make_user("teen", "teen")  # Rolle ohne „Bilder erzeugen“
    assert images.pick_image_model(teen) == (None, images.MSG_NOT_ALLOWED)
    role = Role.objects.create(key="eng", name="Eng", can_images=True)
    limited = User.objects.create_user("eng", password=PASSWORD, role=role)
    assert images.pick_image_model(limited) == (None, images.MSG_NO_IMAGE_MODEL)
    role.allowed_models.add(image_model)
    assert images.pick_image_model(limited) == (image_model, "")


@pytest.mark.django_db
def test_pick_skips_default_that_role_forbids(image_model, image_provider):
    allowed = AIModel.objects.create(
        provider=image_provider,
        model_id="gpt-image-1-mini",
        display_name="Mini",
        capability=AIModel.Capability.IMAGE,
        sort_order=7,
    )
    role = Role.objects.create(key="eng", name="Eng", can_images=True)
    role.allowed_models.add(allowed)
    user = User.objects.create_user("eng", password=PASSWORD, role=role)
    ChatSettings.objects.update_or_create(pk=1, defaults={"default_image_model": image_model})
    assert images.pick_image_model(user) == (allowed, "")


@pytest.mark.django_db
def test_pick_skips_budget_blocked(adult, image_model, image_provider, monkeypatch):
    free = AIModel.objects.create(
        provider=Provider.objects.create(name="Gemini", kind=Provider.Kind.GOOGLE),
        model_id="gemini-2.5-flash-image",
        display_name="Gemini Bild",
        capability=AIModel.Capability.IMAGE,
        sort_order=99,
    )
    blocked = {image_model.pk}
    monkeypatch.setattr(permissions, "budget_allows", lambda u, m: m.pk not in blocked)
    monkeypatch.setattr(accounts_usage, "blocked_reason", lambda u, m: "Budget erschöpft.")
    assert images.pick_image_model(adult) == (free, "")
    blocked.add(free.pk)
    assert images.pick_image_model(adult) == (None, "Budget erschöpft.")
    assert images.reason_status("Budget erschöpft.") == 403


@pytest.mark.django_db
def test_pick_skips_offline_and_unsupported(adult, image_model, image_provider):
    Provider.objects.filter(pk=image_provider.pk).update(check_status=True, online=False)
    anthropic = Provider.objects.create(name="Anthropic", kind=Provider.Kind.ANTHROPIC)
    AIModel.objects.create(  # Anbieterart ohne generate_image
        provider=anthropic, model_id="x", display_name="X", capability="image"
    )
    model, reason = images.pick_image_model(adult)
    assert model is None and "offline" in reason
    assert images.reason_status(reason) == 503


@pytest.mark.django_db
def test_pick_without_any_model(adult):
    assert images.pick_image_model(adult) == (None, images.MSG_NO_IMAGE_MODEL)
    assert images.reason_status(images.MSG_NO_IMAGE_MODEL) == 409


# --- Werkzeug ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_tool_offered_only_with_image_model(adult, chat_model, image_provider):
    assert "generate_image" not in tooling.builtin_bindings(adult, chat_model)
    image = AIModel.objects.create(
        provider=image_provider, model_id="gpt-image-1", display_name="B", capability="image"
    )
    assert "generate_image" in tooling.builtin_bindings(adult, chat_model)
    # Ohne Werkzeuge beim Chatmodell bzw. ohne Recht der Rolle: nicht angeboten.
    chat_model.supports_tools = False
    assert "generate_image" not in tooling.builtin_bindings(adult, chat_model)
    chat_model.supports_tools = True
    assert "generate_image" not in tooling.builtin_bindings(make_user("teen", "t"), chat_model)
    image.active = False
    image.save()
    assert "generate_image" not in tooling.builtin_bindings(adult, chat_model)


def tool_round(*calls):
    return [*calls, Usage(10, 2), Done("tool_calls")]


def answer(text="Hier ist dein Bild."):
    return [Delta(text), Usage(20, 5), Done("stop")]


def image_call(**args):
    return ToolCallEvent(id="c1", name="generate_image", arguments=args)


@pytest.mark.django_db
def test_tool_loop_generates_image(client, adult, conversation, chat_model, image_model, fakes):
    chat_calls, image_calls = fakes(
        [
            tool_round(image_call(prompt="Deutschlandflagge mit Bundesadler", size="landscape")),
            answer(),
        ]
    )
    events = events_of(
        post(client, conversation, content="Zeichne mir die Deutschlandflagge mit Bundesadler")
    )
    names = [n for n, _ in events]
    assert names == ["start", "tool_call", "tool_result", "delta", "usage", "done"]
    assert events[1][1]["server"] == images.TOOL_LABEL
    assert events[2][1]["status"] == "ok"
    # Bildanbieter bekam nur die Beschreibung, das Chatmodell nie das Bild.
    assert image_calls == [
        {
            "model": "gpt-image-1",
            "prompt": "Deutschlandflagge mit Bundesadler",
            "size": "1536x1024",
            "quality": "auto",
            "background": None,
            "n": 1,
        }
    ]
    assert images.HINT in chat_calls[0]["system"]
    assert "generate_image" in [t.name for t in chat_calls[0]["tools"]]
    tool_result = chat_calls[1]["messages"][-1]
    assert tool_result.role == "tool" and "Anhang #" in tool_result.content
    assert B64[:40] not in json.dumps([m.content for m in chat_calls[1]["messages"]])
    reply = Message.objects.get(role="assistant")
    attachment = Attachment.objects.get()
    assert attachment.message == reply and attachment.generated_by_model == image_model
    assert f"Anhang #{attachment.pk}" in tool_result.content
    # Nach dem Neuladen erscheint das Bild in der Antwort (Anhangliste mit Vorschau).
    page = client.get(reverse("chat:conversation", args=[conversation.pk])).content.decode()
    assert f'data-attachment-id="{attachment.pk}"' in page
    data = client.get(reverse("chat:api_messages", args=[conversation.pk])).json()
    messages = data["messages"] if isinstance(data, dict) else data
    shown = [m for m in messages if m["id"] == reply.pk][0]["attachments"]
    assert [a["id"] for a in shown] == [attachment.pk]


@pytest.mark.django_db
def test_tool_error_goes_to_model(client, conversation, chat_model, image_model, fakes):
    chat_calls, _ = fakes(
        [tool_round(image_call(prompt="x")), answer("Leider nicht.")], error=ContentBlocked()
    )
    events = events_of(post(client, conversation, content="Male ein Bild"))
    assert [d["status"] for n, d in events if n == "tool_result"] == ["error"]
    assert chat_calls[1]["messages"][-1].content == MSG_CONTENT_BLOCKED
    assert not Attachment.objects.exists()


@pytest.mark.django_db
def test_tool_limit_per_answer(client, conversation, chat_model, image_model, fakes, monkeypatch):
    monkeypatch.setattr(images, "MAX_IMAGES_PER_ANSWER", 1)
    calls = [
        ToolCallEvent(id=f"c{i}", name="generate_image", arguments={"prompt": "x"})
        for i in range(2)
    ]
    fakes([tool_round(*calls), answer()])
    events = events_of(post(client, conversation, content="Zwei Bilder"))
    assert [d["status"] for n, d in events if n == "tool_result"] == ["ok", "error"]
    assert Attachment.objects.count() == 1


@pytest.mark.django_db
def test_tool_with_confirmation(client, conversation, chat_model, image_model, fakes):
    ChatSettings.objects.update_or_create(pk=1, defaults={"image_tool_confirm": True})
    _, image_calls = fakes([tool_round(image_call(prompt="Katze")), answer()])
    events = events_of(post(client, conversation, content="Male eine Katze"))
    assert events[-1] == ("done", {"status": "awaiting_confirmation"})
    assert image_calls == [] and not Attachment.objects.exists()
    tool_call = ToolCall.objects.get()
    assert tool_call.status == ToolCall.Status.AWAITING_CONFIRMATION
    response = client.post(
        reverse("chat:api_tool_confirm", args=[conversation.pk]),
        json.dumps({"decisions": {str(tool_call.pk): "approve"}}),
        content_type="application/json",
    )
    events = events_of(response)
    assert events[-1] == ("done", {"status": "complete"})
    assert len(image_calls) == 1 and Attachment.objects.count() == 1


@pytest.mark.django_db
def test_tool_confirmation_rejected(client, conversation, chat_model, image_model, fakes):
    ChatSettings.objects.update_or_create(pk=1, defaults={"image_tool_confirm": True})
    _, image_calls = fakes([tool_round(image_call(prompt="Katze")), answer()])
    events_of(post(client, conversation, content="Male eine Katze"))
    tool_call = ToolCall.objects.get()
    response = client.post(
        reverse("chat:api_tool_confirm", args=[conversation.pk]),
        json.dumps({"decisions": {str(tool_call.pk): "reject"}}),
        content_type="application/json",
    )
    events_of(response)
    tool_call.refresh_from_db()
    assert tool_call.status == ToolCall.Status.REJECTED and image_calls == []


# --- Modus „Bild“ -----------------------------------------------------------------------


@pytest.mark.django_db
def test_image_mode_via_api(client, adult, conversation, chat_model, image_model, fakes):
    chat_calls, image_calls = fakes()
    events = events_of(
        post(
            client,
            conversation,
            content="Deutschlandflagge",
            model=chat_model.pk,
            image={"format": "portrait", "quality": "high"},
        )
    )
    assert [n for n, _ in events] == ["start", "status", "usage", "done"]
    assert events[-1] == ("done", {"status": "complete"})
    assert chat_calls == []  # ohne Chatmodell
    assert image_calls[0]["size"] == "1024x1536" and image_calls[0]["quality"] == "high"
    reply = Message.objects.get(role="assistant")
    assert reply.model == image_model and reply.author == adult
    assert reply.tool_state == {
        "image": {
            "format": "portrait",
            "quality": "high",
            "transparent": False,
            "prompt": "Deutschlandflagge",
        }
    }
    assert reply.content.startswith("Bild erzeugt mit GPT Image")
    question = Message.objects.get(role="user")
    assert question.content == "Deutschlandflagge" and reply.parent == question
    attachment = Attachment.objects.get()
    assert attachment.message == reply
    conversation.refresh_from_db()
    assert conversation.default_model == chat_model  # Standardmodell unverändert
    assert conversation.title == "Deutschlandflagge"


@pytest.mark.django_db
def test_image_mode_error_is_stored(client, conversation, image_model, fakes):
    fakes(error=ContentBlocked())
    events = events_of(post(client, conversation, content="x", image={}))
    assert ("error", {"message": MSG_CONTENT_BLOCKED}) in events
    reply = Message.objects.get(role="assistant")
    assert reply.status == Message.Status.ERROR and reply.error == MSG_CONTENT_BLOCKED


@pytest.mark.django_db
@pytest.mark.parametrize(
    "data,status",
    [
        ({"image": {"format": "rund"}}, 400),
        ({"image": "ja"}, 400),
        ({"image": {}, "regenerate": True}, 400),
        ({"image": {}, "attachments": "x"}, 400),
        ({"image": {}, "attachments": [True]}, 400),
        ({"image": {}, "attachments": [1, 2, 3, 4, 5]}, 400),
        ({"image": {}, "content": "  "}, 400),
    ],
)
def test_image_mode_rejects_invalid(client, conversation, image_model, fakes, data, status):
    fakes()
    data.setdefault("content", "Flagge")
    response = post(client, conversation, **data)
    assert response.status_code == status
    assert not Message.objects.exists()


@pytest.mark.django_db
def test_image_mode_without_model_or_right(client, conversation, fakes):
    fakes()
    response = post(client, conversation, content="Flagge", image={})
    assert response.status_code == 409
    assert response.json()["error"] == images.MSG_NO_IMAGE_MODEL
    teen = make_user("teen", "teen")
    own = Conversation.objects.create(user=teen)
    client.force_login(teen)
    response = post(client, own, content="Flagge", image={})
    assert response.status_code in (403, 409)


# --- Hinweis bei Bildwunsch ohne Werkzeuge ------------------------------------------------


def matches(text):
    code = re.search(images.CODE_PATTERN, text, re.I)
    return not code and any(re.search(p, text, re.I) for p in images.REQUEST_PATTERNS)


@pytest.mark.parametrize(
    "text",
    [
        "Zeichne mir die Deutschlandflagge mit Bundesadler",
        "Male ein Bild von einer Katze",
        "Erstelle ein Bild eines Hundes am Strand",
        "Erzeuge mir bitte ein realistisches Foto vom Meer",
        "draw me a cat",
        "Generate an image of a sunset",
    ],
)
def test_detects_image_requests(text):
    assert matches(text)


@pytest.mark.parametrize(
    "text",
    [
        "Mal sehen, ob das klappt",
        "Schreib mal ein Gedicht",
        "Male lions are bigger",
        "Draw a conclusion from the data",
        "Zeichne mir die Flagge als SVG",
        "Erstelle eine Tabelle mit Bildungsausgaben",
        "Wie zeichne ich eine Kurve?",
    ],
)
def test_ignores_other_texts(text):
    assert not matches(text)


def test_patterns_are_javascript_compatible():
    # Keine Python-Eigenheiten, die RegExp nicht kennt.
    for pattern in [*images.REQUEST_PATTERNS, images.CODE_PATTERN]:
        assert "(?<" not in pattern and "(?P" not in pattern and "\\A" not in pattern


@pytest.mark.django_db
def test_template_offers_image_mode(client, adult, conversation, image_model):
    page = client.get(reverse("chat:conversation", args=[conversation.pk])).content.decode()
    assert 'id="image-mode-toggle"' in page and "chat/image_mode.js" in page
    assert 'id="image-request-patterns"' in page
    data = re.search(
        r'<script id="image-request-patterns" type="application/json">(.*?)</script>', page
    )
    patterns = json.loads(data.group(1))
    assert patterns["request"] == images.REQUEST_PATTERNS
    assert 'value="portrait"' in page and "GPT Image" in page


@pytest.mark.django_db
def test_template_without_image_model(client, adult, conversation):
    page = client.get(reverse("chat:conversation", args=[conversation.pk])).content.decode()
    assert 'id="image-mode-toggle"' not in page and "image-request-patterns" not in page


# --- Geteilte Chats ---------------------------------------------------------------------


@pytest.mark.django_db
def test_shared_chat_needs_write(client, adult, conversation, chat_model, image_model, fakes):
    _, image_calls = fakes()
    ben = make_user("adult", "ben")
    share = Share.objects.create(conversation=conversation, user=ben, can_write=False)
    client.force_login(ben)
    response = post(client, conversation, content="Flagge", image={})
    assert response.status_code == 403 and image_calls == []
    share.can_write = True
    share.save()
    events = events_of(post(client, conversation, content="Flagge", image={}))
    assert events[-1] == ("done", {"status": "complete"})
    reply = Message.objects.get(role="assistant")
    assert reply.author == ben
    entry = UsageEntry.objects.get(kind=UsageEntry.Kind.ATTACHMENT)
    assert entry.user == ben  # gebucht auf den Absender, nicht den Besitzer


# --- Kosten -------------------------------------------------------------------------------


@pytest.mark.django_db
def test_cost_is_booked_per_image(client, adult, conversation, image_model, fakes):
    set_price(image_model, unit_prices={"image:high": "0.25", "image": "0.10"})
    fakes(usage=Usage(50, 4160))
    events = events_of(
        post(client, conversation, content="Flagge", image={"format": "square", "quality": "high"})
    )
    entry = UsageEntry.objects.get(kind=UsageEntry.Kind.ATTACHMENT)
    attachment = Attachment.objects.get()
    assert entry.attachment == attachment and entry.user == adult
    assert entry.units == {"image:high:1024x1024": 1}
    assert entry.amount == Decimal("0.25")  # nur Stückpreis (keine Tokenpreise gesetzt)
    assert attachment.cost == Decimal("0.25")
    assert events[-2] == ("usage", {"tokens_in": 0, "tokens_out": 0, "cost": "0.250000"})
    # Die Antwort selbst bucht nichts (keine Doppelzählung).
    assert Message.objects.get(role="assistant").cost is None
    assert not UsageEntry.objects.filter(kind=UsageEntry.Kind.ANSWER).exists()


@pytest.mark.django_db
def test_budget_blocks_image_mode(client, adult, conversation, image_model, fakes, monkeypatch):
    _, image_calls = fakes()
    monkeypatch.setattr(permissions, "budget_allows", lambda u, m: False)
    monkeypatch.setattr(accounts_usage, "blocked_reason", lambda u, m: "Budget erschöpft.")
    response = post(client, conversation, content="Flagge", image={})
    assert response.status_code == 403 and response.json()["error"] == "Budget erschöpft."
    assert image_calls == []


def test_answer_text():
    model = AIModel(display_name="GPT Image")
    assert api_images.answer_text(model, "landscape", "low") == (
        "Bild erzeugt mit GPT Image (quer, Qualität niedrig)."
    )


@pytest.fixture
def local_image_model(db):
    provider = Provider.objects.create(name="Eliezer", kind=Provider.Kind.LMSTUDIO)
    return AIModel.objects.create(
        provider=provider,
        model_id="qwen-image-2.1",
        display_name="Qwen Image",
        capability=AIModel.Capability.IMAGE,
        sort_order=9,
    )


@pytest.mark.django_db
def test_image_mode_uses_the_model_chosen_in_the_chat(
    client, conversation, image_model, local_image_model, fakes
):
    fakes()
    events = events_of(
        post(client, conversation, content="Ein Kreis", image={"model": local_image_model.pk})
    )
    assert events[-1] == ("done", {"status": "complete"})
    reply = Message.objects.get(role="assistant")
    assert reply.model == local_image_model
    assert reply.content.startswith("Bild erzeugt mit Qwen Image")


@pytest.mark.django_db
def test_image_mode_without_choice_keeps_the_default(
    client, conversation, image_model, local_image_model, fakes
):
    fakes()
    events_of(post(client, conversation, content="Ein Kreis", image={}))
    assert Message.objects.get(role="assistant").model == image_model


@pytest.mark.django_db
@pytest.mark.parametrize("bad", ["qwen", True, 999999])
def test_image_mode_rejects_unknown_choice(client, conversation, image_model, fakes, bad):
    fakes()
    response = post(client, conversation, content="x", image={"model": bad})
    assert response.status_code in (400, 409)
    assert not Message.objects.filter(role="assistant").exists()


@pytest.mark.django_db
def test_chat_page_offers_all_usable_image_models(
    client, adult, conversation, image_model, local_image_model
):
    from multigpt.chat import images

    names = [m.display_name for m in images.selectable_models(adult)]
    assert names == ["GPT Image", "Qwen Image"]
    page = client.get(reverse("chat:conversation", args=[conversation.pk])).content.decode()
    assert 'id="image-mode-model"' in page
    assert "Qwen Image" in page


REFUSALS = [
    "I can’t generate or display that image for you directly, but I can help",
    "Ich bin leider nicht in der Lage, Bilder zu erstellen, aber ich kann Ihnen",
    "I cannot create images.",
    "Ich kann leider keine Bilder erzeugen.",
    "Das Bild kann ich nicht erzeugen.",
]
NOT_REFUSALS = [
    "Hier ist der Code für die Funktion.",
    "I cannot find the file you mentioned.",
    "Ich kann die Datei nicht öffnen.",
    "Das Bild zeigt einen Hund.",
]


def refused(text):
    head = text[: images.REFUSAL_HEAD]
    return bool(
        re.search(images.REFUSAL_PATTERN, head, re.I)
        and re.search(images.REFUSAL_IMAGE_WORD, head, re.I)
    )


@pytest.mark.parametrize("text", REFUSALS)
def test_refusal_of_image_request_is_recognized(text):
    assert refused(text)


@pytest.mark.parametrize("text", NOT_REFUSALS)
def test_other_answers_are_not_refusals(text):
    assert not refused(text)


@pytest.mark.django_db
def test_refusal_patterns_reach_the_page(client, adult, conversation, image_model):
    page = client.get(reverse("chat:conversation", args=[conversation.pk])).content.decode()
    assert "refusalImage" in page and "refusalHead" in page


@pytest.fixture
def editor_model(image_provider):
    return AIModel.objects.create(
        provider=image_provider,
        model_id="gpt-image-1.5",
        display_name="GPT Image Edit",
        capability=AIModel.Capability.IMAGE,
        can_edit_images=True,
        sort_order=7,
    )


def make_draft(owner, kind="image", data=PNG, name="vorlage.png"):
    from django.core.files.base import ContentFile

    draft = Attachment(
        owner=owner,
        kind=kind,
        mime_type="image/png" if kind == "image" else "application/pdf",
        size=len(data),
        original_name=name,
    )
    draft.file.save(name, ContentFile(data), save=False)
    draft.save()
    return draft


@pytest.mark.django_db
def test_image_mode_edits_uploaded_image(
    client, adult, conversation, image_model, editor_model, fakes
):
    _, image_calls = fakes()
    draft = make_draft(adult)
    events = events_of(
        post(
            client,
            conversation,
            content="Mach den Kreis blau",
            attachments=[draft.pk],
            image={"model": editor_model.pk},
        )
    )
    assert events[-1] == ("done", {"status": "complete"})
    call = image_calls[0]
    assert call["edit"] is True and call["model"] == "gpt-image-1.5"
    assert call["images"] == [("vorlage.png", "image/png", len(PNG))]
    reply = Message.objects.get(role="assistant")
    assert reply.model == editor_model
    assert reply.content.startswith("Bild bearbeitet mit GPT Image Edit")
    assert "Ein blauer Kreis" in reply.content
    assert reply.tool_state["image"]["edit"] is True
    question = Message.objects.get(role="user")
    draft.refresh_from_db()
    assert draft.message == question  # die Vorlage hängt an der Frage
    result = Attachment.objects.get(message=reply)
    assert result.source_image == draft and result.generated_by_model == editor_model


@pytest.mark.django_db
def test_edit_needs_a_model_that_can_edit(client, adult, conversation, image_model, fakes):
    fakes()
    draft = make_draft(adult)
    response = post(client, conversation, content="Blau", attachments=[draft.pk], image={})
    assert response.status_code == 409
    assert images.MSG_NO_EDIT_MODEL in response.json()["error"]
    assert not Message.objects.filter(role="assistant").exists()


@pytest.mark.django_db
def test_edit_rejects_files_that_are_not_images(client, adult, conversation, editor_model, fakes):
    fakes()
    draft = make_draft(adult, kind="file", data=b"%PDF-1.4", name="x.pdf")
    response = post(client, conversation, content="Blau", attachments=[draft.pk], image={})
    assert response.status_code == 400
    assert not Message.objects.filter(role="assistant").exists()


@pytest.mark.django_db
def test_edit_cannot_use_foreign_drafts(client, adult, conversation, editor_model, fakes):
    fakes()
    other = make_user("adult", "fremd")
    draft = make_draft(other)
    response = post(client, conversation, content="Blau", attachments=[draft.pk], image={})
    assert response.status_code == 400
    draft.refresh_from_db()
    assert draft.message is None


@pytest.mark.django_db
def test_chat_page_marks_models_that_can_edit(
    client, adult, conversation, image_model, editor_model
):
    page = client.get(reverse("chat:conversation", args=[conversation.pk])).content.decode()
    assert f'value="{editor_model.pk}" data-name="GPT Image Edit" data-edit="true"' in page
    assert f'value="{image_model.pk}" data-name="GPT Image" data-edit="false"' in page


def first_image(client, conversation, prompt="Ein roter Kreis", **image):
    events = events_of(post(client, conversation, content=prompt, image=image))
    assert events[-1] == ("done", {"status": "complete"})


@pytest.mark.django_db
def test_continue_edits_the_last_image_with_a_model_that_can_edit(
    client, conversation, image_model, editor_model, fakes
):
    _, image_calls = fakes()
    first_image(client, conversation, model=image_model.pk)
    events = events_of(
        post(
            client,
            conversation,
            content="mehr Rot",
            image={"model": editor_model.pk, "continue": True},
        )
    )
    assert events[-1] == ("done", {"status": "complete"})
    call = image_calls[1]
    assert call["edit"] is True and call["prompt"] == "mehr Rot"
    assert call["images"][0][2] > 0  # das erste Bild ging als Vorlage mit
    first, second = Message.objects.filter(role="assistant").order_by("pk")
    result = Attachment.objects.get(message=second)
    assert result.source_image == Attachment.objects.get(message=first)
    assert second.content.startswith("Bild bearbeitet mit GPT Image Edit")
    assert second.tool_state["image"]["continued"] is True
    # Die Frage trägt keine zusätzlichen Anhänge: die Vorlage bleibt am ersten Bild.
    assert Attachment.objects.filter(message__role="user").count() == 0


@pytest.mark.django_db
def test_continue_chains_the_description_for_a_model_that_only_generates(
    client, conversation, image_model, fakes
):
    _, image_calls = fakes()
    first_image(client, conversation, prompt="Ein roter Kreis")
    events_of(post(client, conversation, content="mehr Schatten", image={"continue": True}))
    assert image_calls[1]["prompt"] == "Ein roter Kreis. Änderung: mehr Schatten"
    assert "edit" not in image_calls[1]
    reply = Message.objects.filter(role="assistant").order_by("pk").last()
    assert "Weitergearbeitet an der Beschreibung des vorigen Bildes." in reply.content
    # Auch die dritte Runde baut auf der gesendeten Beschreibung auf.
    events_of(post(client, conversation, content="und Nacht", image={"continue": True}))
    assert image_calls[2]["prompt"] == (
        "Ein roter Kreis. Änderung: mehr Schatten. Änderung: und Nacht"
    )


@pytest.mark.django_db
def test_continue_without_earlier_image_is_a_plain_request(
    client, conversation, image_model, fakes
):
    _, image_calls = fakes()
    events_of(post(client, conversation, content="Eine Katze", image={"continue": True}))
    assert image_calls[0]["prompt"] == "Eine Katze"
    reply = Message.objects.get(role="assistant")
    assert "continued" not in reply.tool_state["image"]


@pytest.mark.django_db
def test_without_continue_every_request_stands_alone(client, conversation, image_model, fakes):
    _, image_calls = fakes()
    first_image(client, conversation, prompt="Ein roter Kreis")
    events_of(post(client, conversation, content="mehr Schatten", image={"continue": False}))
    assert image_calls[1]["prompt"] == "mehr Schatten"


@pytest.mark.django_db
def test_continue_must_be_a_boolean(client, conversation, image_model, fakes):
    fakes()
    response = post(client, conversation, content="x", image={"continue": "ja"})
    assert response.status_code == 400


def test_chain_prompt_trims_the_old_description_when_too_long():
    long_prompt = "x" * (images.MAX_PROMPT + 500)
    chained = images.chain_prompt(long_prompt, "heller")
    assert len(chained) == images.MAX_PROMPT and chained.endswith(". Änderung: heller")
    assert images.chain_prompt("", "heller") == "heller"


@pytest.mark.django_db
def test_regenerate_of_an_image_reply_runs_the_image_model_again(
    client, conversation, chat_model, image_model, fakes
):
    chat_calls, image_calls = fakes()
    first_image(client, conversation, prompt="Ein roter Kreis", format="portrait", quality="high")
    reply = Message.objects.get(role="assistant")
    events = events_of(
        post(client, conversation, regenerate=True, message_id=reply.pk, model=chat_model.pk)
    )
    assert events[-1] == ("done", {"status": "complete"})
    assert chat_calls == []  # kein Chatmodell, das nur Text liefert
    assert len(image_calls) == 2 and image_calls[1]["prompt"] == "Ein roter Kreis"
    assert image_calls[1]["size"] == "1024x1536" and image_calls[1]["quality"] == "high"
    new = Message.objects.filter(role="assistant").exclude(pk=reply.pk).get()
    assert new.parent_id == reply.parent_id and new.model == image_model  # neue Version
    assert new.content.startswith("Bild erzeugt mit GPT Image")
    assert Attachment.objects.filter(message=new).exists()
    conversation.refresh_from_db()
    assert conversation.current_leaf_id == new.pk


@pytest.mark.django_db
def test_regenerate_of_an_edit_reply_edits_again(
    client, adult, conversation, chat_model, editor_model, fakes
):
    _, image_calls = fakes()
    draft = make_draft(adult)
    events_of(
        post(
            client,
            conversation,
            content="Mach den Kreis blau",
            attachments=[draft.pk],
            image={"model": editor_model.pk},
        )
    )
    reply = Message.objects.get(role="assistant")
    events_of(post(client, conversation, regenerate=True, message_id=reply.pk, model=chat_model.pk))
    assert [c.get("edit") for c in image_calls] == [True, True]
    assert image_calls[1]["images"] == image_calls[0]["images"]


@pytest.mark.django_db
def test_editing_the_question_of_an_image_reply_creates_a_new_image(
    client, conversation, chat_model, image_model, fakes
):
    chat_calls, image_calls = fakes()
    first_image(client, conversation, prompt="Ein roter Kreis")
    question = Message.objects.get(role="user")
    events = events_of(
        post(
            client,
            conversation,
            content="Ein grünes Quadrat",
            edit_of=question.pk,
            model=chat_model.pk,
        )
    )
    assert events[-1] == ("done", {"status": "complete"})
    assert chat_calls == [] and image_calls[1]["prompt"] == "Ein grünes Quadrat"
    questions = Message.objects.filter(role="user").order_by("pk")
    assert questions.count() == 2 and questions[1].parent_id == question.parent_id
    reply = Message.objects.filter(role="assistant").order_by("pk").last()
    assert reply.parent_id == questions[1].pk and reply.model == image_model


@pytest.mark.django_db
def test_regenerate_of_a_chat_reply_is_unchanged(
    client, conversation, chat_model, image_model, fakes
):
    chat_calls, image_calls = fakes(script=[[Delta("Hallo"), Usage(1, 1), Done("stop")]])
    events_of(post(client, conversation, content="Hallo", model=chat_model.pk))
    reply = Message.objects.get(role="assistant")
    events_of(post(client, conversation, regenerate=True, message_id=reply.pk, model=chat_model.pk))
    assert len(chat_calls) == 2 and image_calls == []


class EditMissingAdapter(FakeImageAdapter):
    """Anbieter ohne Pfad zum Bearbeiten: ``/images/edits`` antwortet 404."""

    def edit_image(self, model_id, images, prompt, **params):
        from multigpt.chat.providers.base import ProviderHTTPError

        self.log.append({"edit": True, "model": model_id, "prompt": prompt})
        raise ProviderHTTPError("nicht gefunden", 404)


@pytest.fixture
def no_edit_provider(monkeypatch):
    calls = []

    def get_adapter(provider):
        return EditMissingAdapter(provider, calls)

    monkeypatch.setattr(registry, "get_adapter", get_adapter)
    return calls


@pytest.mark.django_db
def test_continue_falls_back_to_the_description_when_the_provider_cannot_edit(
    client, conversation, editor_model, no_edit_provider
):
    first_image(client, conversation, prompt="Ein roter Kreis", model=editor_model.pk)
    events = events_of(
        post(
            client,
            conversation,
            content="mehr Schatten",
            image={"model": editor_model.pk, "continue": True},
        )
    )
    assert events[-1] == ("done", {"status": "complete"})
    generated = [c for c in no_edit_provider if "edit" not in c]
    assert generated[-1]["prompt"] == "Ein roter Kreis. Änderung: mehr Schatten"
    reply = Message.objects.filter(role="assistant").order_by("pk").last()
    assert reply.status == Message.Status.COMPLETE
    assert "Weitergearbeitet an der Beschreibung" in reply.content
    assert reply.tool_state["image"]["edit"] is False
    assert reply.tool_state["image"]["prompt"] == "Ein roter Kreis. Änderung: mehr Schatten"


@pytest.mark.django_db
def test_uploaded_source_is_not_replaced_when_the_provider_cannot_edit(
    client, adult, conversation, editor_model, no_edit_provider
):
    draft = make_draft(adult)
    events = events_of(
        post(
            client,
            conversation,
            content="Blau",
            attachments=[draft.pk],
            image={"model": editor_model.pk},
        )
    )
    assert any(name == "error" and "Bilder nicht bearbeiten" in d["message"] for name, d in events)
    assert [c for c in no_edit_provider if "edit" not in c] == []  # kein stilles Neuerzeugen


@pytest.mark.django_db
def test_long_message_starts_a_new_image_even_with_continue(
    client, conversation, image_model, fakes
):
    _, image_calls = fakes()
    first_image(client, conversation, prompt="Ein roter Kreis")
    long_text = "Ein Hirsch im Nebel. " * 15  # > 200 Zeichen: eine eigene Beschreibung
    assert len(long_text) > images.CONTINUE_MAX_CHARS
    events_of(post(client, conversation, content=long_text, image={"continue": True}))
    assert image_calls[1]["prompt"] == long_text.strip()


def test_parse_size_accepts_the_usual_spellings_and_limits():
    assert images.parse_size("1920x1080") == (1920, 1080)
    assert images.parse_size(" 3840 × 2160 ") == (3840, 2160)
    for bad in ("", "1920", "axb", "100x100", "5000x1000", "1920x1080x3", None):
        with pytest.raises(images.ImageError):
            images.parse_size(bad)


def test_generation_size_keeps_the_aspect_ratio_within_the_pixel_limit():
    w, h = images.generation_size(3840, 2160)
    assert w * h <= images.GENERATION_MAX_PIXELS
    assert w % images.GENERATION_STEP == 0 and h % images.GENERATION_STEP == 0
    assert abs(w / h - 3840 / 2160) < 0.03
    assert images.generation_size(1024, 1024) == (1024, 1024)  # klein genug: unverändert


def test_fit_to_size_crops_to_the_ratio_and_scales_to_the_exact_size():
    import io as _io

    from PIL import Image

    source = Image.new("RGB", (400, 300), (200, 30, 30))
    raw = _io.BytesIO()
    source.save(raw, "PNG")
    out = Image.open(_io.BytesIO(images.fit_to_size(raw.getvalue(), 640, 360)))
    assert out.size == (640, 360)
    same = images.fit_to_size(raw.getvalue(), 400, 300)
    assert same == raw.getvalue()  # schon so groß: unverändert


@pytest.mark.django_db
def test_custom_resolution_gets_exact_size_from_a_model_that_renders_smaller(
    client, conversation, local_image_model, fakes
):
    _, image_calls = fakes()
    events = events_of(
        post(
            client,
            conversation,
            content="Berge bei Sonnenaufgang",
            image={"model": local_image_model.pk, "size": "1920x1080"},
        )
    )
    assert events[-1] == ("done", {"status": "complete"})
    w, h = images.generation_size(1920, 1080)
    assert image_calls[0]["size"] == f"{w}x{h}"  # so rechnet das Modell
    reply = Message.objects.get(role="assistant")
    attachment = Attachment.objects.get(message=reply)
    assert (attachment.width, attachment.height) == (1920, 1080)  # so kommt es an
    assert reply.tool_state["image"]["size"] == "1920x1080"
    assert "1920×1080" in reply.content


@pytest.mark.django_db
def test_custom_resolution_larger_than_the_old_edge_limit_is_kept(
    client, conversation, local_image_model, fakes
):
    fakes()
    events_of(
        post(
            client,
            conversation,
            content="Wallpaper",
            image={"model": local_image_model.pk, "size": "3840x2160"},
        )
    )
    attachment = Attachment.objects.get(message__role="assistant")
    assert (attachment.width, attachment.height) == (3840, 2160)  # nicht auf 2048 verkleinert


@pytest.mark.django_db
def test_custom_resolution_is_refused_for_models_with_fixed_formats(
    client, conversation, image_model, fakes
):
    fakes()
    response = post(client, conversation, content="x", image={"size": "1920x1080"})
    assert response.status_code == 400
    assert images.MSG_SIZE_UNSUPPORTED in response.json()["error"]
    assert not Message.objects.filter(role="assistant").exists()


@pytest.mark.django_db
@pytest.mark.parametrize("size", ["abc", "100x100", "9000x9000"])
def test_custom_resolution_is_validated(client, conversation, local_image_model, fakes, size):
    fakes()
    response = post(
        client, conversation, content="x", image={"model": local_image_model.pk, "size": size}
    )
    assert response.status_code == 400
    assert images.MSG_BAD_SIZE in response.json()["error"]


@pytest.mark.django_db
def test_regenerate_keeps_the_custom_resolution(
    client, conversation, chat_model, local_image_model, fakes
):
    _, image_calls = fakes()
    events_of(
        post(
            client,
            conversation,
            content="Wallpaper",
            image={"model": local_image_model.pk, "size": "1920x1080"},
        )
    )
    reply = Message.objects.get(role="assistant")
    events_of(post(client, conversation, regenerate=True, message_id=reply.pk, model=chat_model.pk))
    assert image_calls[1]["size"] == image_calls[0]["size"]
    newest = Message.objects.filter(role="assistant").order_by("pk").last()
    attachment = Attachment.objects.get(message=newest)
    assert (attachment.width, attachment.height) == (1920, 1080)


@pytest.mark.django_db
def test_chat_page_offers_resolution_only_for_models_that_take_any_size(
    client, adult, conversation, image_model, local_image_model
):
    page = client.get(reverse("chat:conversation", args=[conversation.pk])).content.decode()
    assert 'id="image-mode-resolution"' in page and "Full HD 1920×1080" in page
    assert (
        f'value="{local_image_model.pk}" data-name="Qwen Image" data-edit="false" data-size="true"'
        in page
    )
    assert (
        f'value="{image_model.pk}" data-name="GPT Image" data-edit="false" data-size="false"'
        in page
    )


def generated_attachment(client, conversation, image_model, fakes):
    fakes()
    first_image(client, conversation, prompt="Ein roter Kreis")
    return Attachment.objects.get(message__role="assistant")


@pytest.mark.django_db
def test_copy_to_draft_makes_an_own_independent_draft(
    client, adult, conversation, image_model, fakes
):
    source = generated_attachment(client, conversation, image_model, fakes)
    response = client.post(reverse("chat:api_attachment_copy", args=[source.pk]))
    assert response.status_code == 201
    data = response.json()
    draft = Attachment.objects.get(pk=data["id"])
    assert draft.owner == adult and draft.message is None and draft.conversation == conversation
    assert draft.kind == Attachment.Kind.IMAGE and draft.file.name != source.file.name
    assert draft.file.read() == source.file.read()  # echte Kopie derselben Bytes
    source.file.delete(save=False)  # das Original verschwindet: der Entwurf bleibt heil
    draft.refresh_from_db()
    assert draft.file.read()


@pytest.mark.django_db
def test_copied_draft_can_be_sent_as_the_source_of_an_edit(
    client, adult, conversation, image_model, editor_model, fakes
):
    _, image_calls = fakes()
    first_image(client, conversation, prompt="Ein roter Kreis", model=image_model.pk)
    source = Attachment.objects.get(message__role="assistant")
    draft_id = client.post(reverse("chat:api_attachment_copy", args=[source.pk])).json()["id"]
    events = events_of(
        post(
            client,
            conversation,
            content="mehr Rot",
            attachments=[draft_id],
            image={"model": editor_model.pk},
        )
    )
    assert events[-1] == ("done", {"status": "complete"})
    assert image_calls[-1]["edit"] is True and image_calls[-1]["images"][0][2] > 0


@pytest.mark.django_db
def test_copy_to_draft_is_hidden_from_strangers(client, conversation, image_model, fakes):
    source = generated_attachment(client, conversation, image_model, fakes)
    client.force_login(make_user("adult", "fremd"))
    response = client.post(reverse("chat:api_attachment_copy", args=[source.pk]))
    assert response.status_code == 404
    assert Attachment.objects.filter(owner__username="fremd").count() == 0


@pytest.mark.django_db
def test_copy_to_draft_needs_login_and_a_chat_image(
    client, adult, conversation, image_model, fakes
):
    source = generated_attachment(client, conversation, image_model, fakes)
    draft = make_draft(adult)  # Entwurf ohne Nachricht: kein Bild „im Chat“
    assert client.post(reverse("chat:api_attachment_copy", args=[draft.pk])).status_code == 400
    client.logout()
    assert client.post(reverse("chat:api_attachment_copy", args=[source.pk])).status_code in (
        401,
        403,
    )


@pytest.mark.django_db
def test_chat_page_shows_continue_button_on_generated_images(
    client, adult, conversation, image_model, fakes
):
    generated_attachment(client, conversation, image_model, fakes)
    page = client.get(reverse("chat:conversation", args=[conversation.pk])).content.decode()
    assert "data-attachment-continue" in page and "Weiter bearbeiten" in page


@pytest.mark.django_db
def test_quality_is_only_offered_and_named_for_providers_that_use_it(
    client, adult, conversation, image_model, local_image_model, fakes
):
    page = client.get(reverse("chat:conversation", args=[conversation.pk])).content.decode()
    assert 'id="image-mode-quality-field"' in page
    assert 'data-name="GPT Image" data-edit="false" data-size="false" data-quality="true"' in page
    assert 'data-name="Qwen Image" data-edit="false" data-size="true" data-quality="false"' in page
    fakes()
    events_of(post(client, conversation, content="Kreis", image={"model": local_image_model.pk}))
    local_reply = Message.objects.get(role="assistant", model=local_image_model)
    assert "Qualität" not in local_reply.content
    events_of(
        post(
            client,
            conversation,
            content="Kreis",
            image={"model": image_model.pk, "quality": "high"},
        )
    )
    cloud_reply = Message.objects.get(role="assistant", model=image_model)
    assert "Qualität hoch" in cloud_reply.content
