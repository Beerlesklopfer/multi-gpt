"""Sprache (M10): Adapter (OpenAI und Eliezer, über respx), Spracherkennung, Vorlesen,
Grenzen (M10-03), Abrechnung, Rechte, Auslieferung der Audiodatei und Einstellungen.

Keine echten Anbieteraufrufe: HTTP über respx mit den Beispielantworten aus der OpenAI-Doku
(audio/transcriptions, audio/speech) und der Eliezer-Spezifikation (Branch feat/speech-to-text,
Specification.md: audio/voices, verbose_json), sonst ein Fake-Adapter.
"""

import json
from decimal import Decimal

import httpx
import pytest
import respx
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from multigpt.accounts.models import Role, User
from multigpt.billing import booking
from multigpt.billing.models import UsageEntry
from multigpt.chat import capabilities, speech
from multigpt.chat.lmstudio import CatalogModel
from multigpt.chat.models import (
    AIModel,
    Attachment,
    ChatSettings,
    Conversation,
    Message,
    Provider,
    SpeechPreference,
)
from multigpt.chat.providers import registry
from multigpt.chat.providers.base import (
    ProviderAdapter,
    ProviderError,
    SpeechResult,
    Transcription,
    Usage,
)
from multigpt.chat.providers.lmstudio import LMStudioAdapter
from multigpt.chat.providers.openai_compat import OpenAICompatAdapter
from tests.billing_helpers import set_price

PASSWORD = "Geheim-Test-1234"
KEY = "sk-test-GEHEIM-1234567890"
OPENAI = "https://api.openai.com/v1"
ELIEZER = "http://192.0.2.7:1234"
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 40
MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xff\xfb" * 20


def openai_adapter():
    return OpenAICompatAdapter(Provider(name="OpenAI", kind="openai_compat", api_key=KEY))


def eliezer_adapter():
    provider = Provider(name="Eliezer", kind="lmstudio", base_url=ELIEZER, pk=77)
    return LMStudioAdapter(provider)


# --- Adapter: Spracherkennung ---------------------------------------------------------


def test_openai_transcribe_gpt4o_sends_multipart_and_reads_tokens():
    # Antwort von gpt-4o-transcribe laut Doku: text + usage (Tokens).
    payload = {
        "text": "Hallo Welt",
        "usage": {"type": "tokens", "input_tokens": 14, "output_tokens": 45, "total_tokens": 59},
    }
    with respx.mock(assert_all_called=True) as router:
        route = router.post(f"{OPENAI}/audio/transcriptions").respond(200, json=payload)
        result = openai_adapter().transcribe(
            "gpt-4o-transcribe", ("aufnahme.webm", "audio/webm", b"WEBM"), language="de"
        )
    request = route.calls.last.request
    assert request.headers["content-type"].startswith("multipart/form-data")
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    body = request.content
    assert b'name="file"; filename="aufnahme.webm"' in body and b"WEBM" in body
    for field, value in (
        ("model", "gpt-4o-transcribe"),
        ("response_format", "json"),
        ("language", "de"),
    ):
        assert f'name="{field}"'.encode() in body and value.encode() in body
    assert isinstance(result, Transcription) and result.text == "Hallo Welt"
    assert (result.usage.tokens_in, result.usage.tokens_out) == (14, 45)


def test_openai_transcribe_whisper_reads_duration_and_segments():
    payload = {
        "task": "transcribe",
        "language": "german",
        "duration": 8.47,
        "text": " Guten Morgen. Wie geht es dir? ",
        "segments": [
            {"id": 0, "start": 0.0, "end": 3.2, "text": " Guten Morgen."},
            {"id": 1, "start": 3.2, "end": 8.4, "text": " Wie geht es dir?"},
        ],
    }
    with respx.mock() as router:
        route = router.post(f"{OPENAI}/audio/transcriptions").respond(200, json=payload)
        result = openai_adapter().transcribe("whisper-1", ("a.mp3", "audio/mpeg", b"MP3"))
    assert b"verbose_json" in route.calls.last.request.content
    assert b'name="language"' not in route.calls.last.request.content  # automatisch
    assert result.text == "Guten Morgen. Wie geht es dir?"
    assert result.duration == 8.47 and result.language == "german"
    assert result.segments[0] == (0.0, 3.2, " Guten Morgen.")


def test_eliezer_transcribe_uses_the_server_root_and_verbose_json():
    # Eliezer-Spezifikation: verbose_json = task, language, duration, text, segments[].
    payload = {
        "task": "transcribe",
        "language": "de",
        "duration": 62.5,
        "text": "Das ist ein Test.",
        "segments": [{"id": 0, "start": 0.0, "end": 2.0, "text": "Das ist ein Test."}],
    }
    with respx.mock(assert_all_called=True) as router:
        route = router.post(f"{ELIEZER}/v1/audio/transcriptions").respond(200, json=payload)
        result = eliezer_adapter().transcribe(
            "whisper-large-v3", ("a.webm", "audio/webm", b"X"), language="de"
        )
    assert b"verbose_json" in route.calls.last.request.content
    assert result.duration == 62.5 and result.text == "Das ist ein Test."


@pytest.mark.parametrize(
    ("status", "text"),
    [
        (413, "zu groß oder zu lang"),
        (400, "kann die Aufnahme nicht lesen"),
        (401, "API-Key prüfen"),
        (404, "nicht gefunden"),
        (500, "Serverfehler"),
    ],
)
def test_transcribe_errors_are_german_and_hide_the_provider_text(status, text, caplog):
    body = {"error": {"message": f"Incorrect API key provided: {KEY}", "type": "x"}}
    with respx.mock() as router:
        router.post(f"{OPENAI}/audio/transcriptions").respond(status, json=body)
        with pytest.raises(ProviderError) as info:
            openai_adapter().transcribe("whisper-1", ("a.webm", "audio/webm", b"X"))
    assert text in str(info.value)
    assert KEY not in str(info.value) and KEY not in caplog.text


def test_transcribe_network_error_and_empty_input():
    with respx.mock() as router:
        router.post(f"{OPENAI}/audio/transcriptions").mock(side_effect=httpx.ConnectError("x"))
        with pytest.raises(ProviderError) as info:
            openai_adapter().transcribe("whisper-1", ("a.webm", "audio/webm", b"X"))
    assert info.value.unreachable
    with pytest.raises(ProviderError):
        openai_adapter().transcribe("whisper-1", ("a.webm", "audio/webm", b""))


def test_transcribe_rejects_a_strange_answer():
    with respx.mock() as router:
        router.post(f"{OPENAI}/audio/transcriptions").respond(200, json={"foo": 1})
        with pytest.raises(ProviderError):
            openai_adapter().transcribe("whisper-1", ("a.webm", "audio/webm", b"X"))


# --- Adapter: Sprachausgabe -----------------------------------------------------------


def test_openai_speak_sends_json_and_returns_mp3():
    with respx.mock(assert_all_called=True) as router:
        route = router.post(f"{OPENAI}/audio/speech").respond(
            200, content=MP3, headers={"content-type": "audio/mpeg"}
        )
        result = openai_adapter().speak("gpt-4o-mini-tts", "Guten Tag.", "marin")
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "model": "gpt-4o-mini-tts",
        "input": "Guten Tag.",
        "response_format": "mp3",
        "voice": "marin",
    }
    assert isinstance(result, SpeechResult)
    assert result.data == MP3 and result.mime_type == "audio/mpeg" and result.extension == ".mp3"


def test_openai_speak_defaults_to_a_voice_because_openai_requires_one():
    with respx.mock() as router:
        route = router.post(f"{OPENAI}/audio/speech").respond(200, content=MP3)
        openai_adapter().speak("tts-1", "Hallo")
    assert json.loads(route.calls.last.request.content)["voice"] == "alloy"


def test_eliezer_speak_asks_for_wav_and_sends_no_voice_by_default():
    with respx.mock(assert_all_called=True) as router:
        route = router.post(f"{ELIEZER}/v1/audio/speech").respond(
            200, content=WAV, headers={"content-type": "audio/wav"}
        )
        result = eliezer_adapter().speak("chatterbox-q8", "Hallo", None)
    body = json.loads(route.calls.last.request.content)
    assert body == {"model": "chatterbox-q8", "input": "Hallo", "response_format": "wav"}
    assert result.mime_type == "audio/wav" and result.extension == ".wav"


def test_eliezer_speak_without_a_required_voice_gets_a_german_hint():
    # Eliezer: 400 invalid_request, wenn das Modell eine Stimme verlangt (Specification.md).
    body = {
        "error": {
            "message": 'The model "chatterbox-q8" needs a voice: pass "voice" '
            "(see GET /v1/audio/voices)",
            "type": "invalid_request",
        }
    }
    with respx.mock() as router:
        router.post(f"{ELIEZER}/v1/audio/speech").respond(400, json=body)
        with pytest.raises(ProviderError) as info:
            eliezer_adapter().speak("chatterbox-q8", "Hallo")
    assert "braucht eine Stimme" in str(info.value) and "needs a voice" not in str(info.value)


def test_speak_413_and_empty_text():
    with respx.mock() as router:
        router.post(f"{ELIEZER}/v1/audio/speech").respond(
            413, json={"error": {"message": "too long", "type": "payload_too_large"}}
        )
        with pytest.raises(ProviderError) as info:
            eliezer_adapter().speak("chatterbox-q8", "x" * 10)
    assert "zu lang" in str(info.value)
    with pytest.raises(ProviderError):
        openai_adapter().speak("tts-1", "   ")


def test_list_voices_eliezer_and_failures():
    with respx.mock() as router:
        router.get(f"{ELIEZER}/v1/audio/voices").respond(200, json={"voices": ["mama", "papa"]})
        assert eliezer_adapter().list_voices() == ["mama", "papa"]
    with respx.mock() as router:
        router.get(f"{ELIEZER}/v1/audio/voices").respond(404, json={"error": {}})
        assert eliezer_adapter().list_voices() == []
    with respx.mock() as router:
        router.get(f"{ELIEZER}/v1/audio/voices").mock(side_effect=httpx.ConnectError("x"))
        assert eliezer_adapter().list_voices() == []
    assert ProviderAdapter(Provider()).list_voices() == []  # Basisklasse: keine Liste


# --- Fähigkeiten aus dem Katalog -----------------------------------------------------


@pytest.mark.parametrize("kind", ["stt", "tts"])
def test_catalog_types_stt_and_tts_become_capabilities(kind):
    detected = capabilities.from_catalog(CatalogModel(key="m", type=kind))
    assert detected.capability == kind and not detected.tools and not detected.vision


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("whisper-1", "stt"),
        ("gpt-4o-transcribe", "stt"),
        ("tts-1-hd", "tts"),
        ("gpt-4o-mini-tts", "tts"),
    ],
)
def test_openai_ids_are_guessed(model_id, expected):
    assert capabilities.guess_capability(model_id) == expected


# --- Hilfsfunktionen ------------------------------------------------------------------


def test_plain_text_removes_markdown_code_links_and_citations():
    text = (
        "# Überschrift\n\nDas ist **fett** und `code`, siehe [die Seite](https://x.example/a) "
        "und https://y.example/b [1][2].\n\n```python\nprint('geheim')\n```\n\n"
        "- Punkt eins\n- Punkt zwei\n\n1. Erstens\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n"
        "Formel $$x^2$$ Ende.\n\n> Zitat\n"
    )
    plain = speech.plain_text(text)
    assert "Überschrift" in plain and "Das ist fett und code, siehe die Seite und ." in plain
    for gone in ("**", "`", "https://", "geheim", "[1]", "#", "$$", "---", "- Punkt", "> "):
        assert gone not in plain
    assert "Punkt eins" in plain and "Erstens" in plain and "Zitat" in plain


def test_plain_text_keeps_an_unclosed_code_fence_out():
    assert "nie" not in speech.plain_text("Text\n```\nnie vorlesen")


def test_shorten_cuts_at_a_sentence_end():
    text = "Erster Satz. Zweiter Satz. Dritter Satz ist deutlich länger als der Rest."
    short, cut = speech.shorten(text, 30)
    assert cut and short == "Erster Satz. Zweiter Satz."
    assert speech.shorten("kurz", 30) == ("kurz", False)


def test_clean_language():
    assert speech.clean_language("") is None and speech.clean_language("auto") is None
    assert speech.clean_language("DE") == "de" and speech.clean_language("de-DE") == "de"
    with pytest.raises(speech.SpeechError):
        speech.clean_language("deutsch!")


# --- Fake-Adapter und Fixtures ---------------------------------------------------------


class FakeSpeechAdapter(ProviderAdapter):
    def __init__(self, provider, calls, **kwargs):
        super().__init__(provider)
        self.calls = calls
        self.kwargs = kwargs

    def transcribe(self, model_id, audio, *, language=None, prompt=None):
        self.calls.append(("transcribe", model_id, audio[0], audio[1], len(audio[2]), language))
        error = self.kwargs.get("error")
        if error:
            raise error
        return Transcription(
            self.kwargs.get("text", "Das ist ein Test."),
            "de",
            self.kwargs.get("duration", 0.0),
            usage=self.kwargs.get("usage"),
        )

    def speak(self, model_id, text, voice=None):
        self.calls.append(("speak", model_id, text, voice))
        error = self.kwargs.get("error")
        if error:
            raise error
        return SpeechResult(WAV, "audio/wav", ".wav", self.kwargs.get("usage"))

    def list_voices(self, timeout=5):
        return self.kwargs.get("voices", [])


@pytest.fixture(autouse=True)
def media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"


@pytest.fixture(autouse=True)
def _clear_cache():
    from django.core.cache import cache

    cache.clear()


@pytest.fixture
def provider(db):
    return Provider.objects.create(name="OpenAI", kind=Provider.Kind.OPENAI_COMPAT, api_key=KEY)


@pytest.fixture
def chat_model(provider):
    return AIModel.objects.create(provider=provider, model_id="gpt-test", display_name="GPT")


@pytest.fixture
def stt_model(provider):
    return AIModel.objects.create(
        provider=provider,
        model_id="whisper-1",
        display_name="Whisper",
        capability=AIModel.Capability.STT,
    )


@pytest.fixture
def tts_model(provider):
    return AIModel.objects.create(
        provider=provider,
        model_id="tts-1",
        display_name="Stimme",
        capability=AIModel.Capability.TTS,
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
def fake(monkeypatch):
    """fake(**kwargs) -> Liste der Aufrufe des Fake-Adapters."""

    def install(**kwargs):
        calls = []
        monkeypatch.setattr(
            registry, "get_adapter", lambda provider: FakeSpeechAdapter(provider, calls, **kwargs)
        )
        return calls

    return install


def audio_upload(size=2048, content_type="audio/webm;codecs=opus", name="aufnahme.webm"):
    return SimpleUploadedFile(name, b"A" * size, content_type=content_type)


def transcribe(client, upload=None, **data):
    payload = {"duration": "4.2", **data}
    if upload is not None:
        payload["file"] = upload
    return client.post(reverse("chat:api_speech_transcribe"), payload)


@pytest.fixture
def answer(adult, chat_model):
    conversation = Conversation.objects.create(user=adult, default_model=chat_model)
    Message.objects.create(conversation=conversation, role="user", content="Frage", author=adult)
    return Message.objects.create(
        conversation=conversation,
        role="assistant",
        content="Das ist die **Antwort**. Siehe [1].",
        model=chat_model,
        status=Message.Status.COMPLETE,
    )


def speak(client, message, **data):
    return client.post(
        reverse("chat:api_message_speak", args=[message.pk]),
        json.dumps(data),
        content_type="application/json",
    )


# --- Spracherkennung über die API ------------------------------------------------------


@pytest.mark.django_db
def test_transcribe_returns_text_and_stores_no_audio(client, adult, stt_model, fake):
    calls = fake()
    response = transcribe(client, audio_upload())
    assert response.status_code == 200, response.content
    data = response.json()
    assert data["text"] == "Das ist ein Test." and data["model"] == "Whisper"
    # Die Standardsprache ist „de“; die Aufnahme bleibt nirgends liegen.
    assert calls == [("transcribe", "whisper-1", "aufnahme.webm", "audio/webm", 2048, "de")]
    assert not Attachment.objects.exists()


@pytest.mark.django_db
def test_transcribe_uses_the_language_of_the_account_and_the_chosen_model(
    client, adult, stt_model, provider, fake
):
    other = AIModel.objects.create(
        provider=provider, model_id="whisper-2", display_name="Whisper 2", capability="stt"
    )
    SpeechPreference.objects.create(user=adult, stt_model=other, stt_language="en")
    calls = fake()
    assert transcribe(client, audio_upload()).status_code == 200
    assert calls[0][1] == "whisper-2" and calls[0][5] == "en"
    SpeechPreference.objects.filter(user=adult).update(stt_language="")
    calls.clear()
    assert transcribe(client, audio_upload()).status_code == 200
    assert calls[0][5] is None  # automatisch


@pytest.mark.django_db
def test_transcribe_books_audio_minutes_from_the_provider_duration(client, adult, stt_model, fake):
    set_price(stt_model, unit_prices={"audio_minute": "0.006"})
    fake(duration=90.0)
    assert transcribe(client, audio_upload(), duration="85").status_code == 200
    entry = UsageEntry.objects.get()
    assert entry.kind == UsageEntry.Kind.TOOL and entry.user == adult
    assert entry.ai_model == stt_model and entry.message is None and entry.attachment is None
    assert Decimal(str(entry.units["audio_minute"])) == Decimal("1.5")
    assert entry.amount == Decimal("0.009000")


@pytest.mark.django_db
def test_transcribe_books_the_browser_duration_without_one_from_the_provider(
    client, adult, stt_model, fake
):
    fake(duration=0.0)
    assert transcribe(client, audio_upload(), duration="30").status_code == 200
    assert Decimal(str(UsageEntry.objects.get().units["audio_minute"])) == Decimal("0.5")


@pytest.mark.django_db
def test_transcribe_books_tokens_when_the_model_is_priced_by_tokens(client, adult, stt_model, fake):
    set_price(stt_model, input=6, output=10, unit_prices={"audio_minute": "0.006"})
    fake(usage=Usage(tokens_in=1000, tokens_out=100), duration=60.0)
    assert transcribe(client, audio_upload(), duration="60").status_code == 200
    entry = UsageEntry.objects.get()
    assert (entry.tokens_in, entry.tokens_out) == (1000, 100)
    assert entry.units == {}  # keine Doppelzählung
    assert entry.amount == Decimal("0.007000")


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("data", "status"),
    [
        ({"duration": "abc"}, 400),
        ({"duration": "-1"}, 400),
        ({"duration": "nan"}, 400),
        ({"language": "deutsch!"}, 400),
    ],
)
def test_transcribe_rejects_bad_fields(client, adult, stt_model, fake, data, status):
    fake()
    assert transcribe(client, audio_upload(), **data).status_code == status


@pytest.mark.django_db
def test_transcribe_needs_a_file_with_audio_type(client, adult, stt_model, fake):
    fake()
    assert transcribe(client).status_code == 400
    assert (
        transcribe(client, audio_upload(content_type="text/html", name="x.html")).status_code == 415
    )
    empty = SimpleUploadedFile("a.webm", b"", content_type="audio/webm")
    assert transcribe(client, empty).status_code == 400


@pytest.mark.django_db
def test_transcribe_without_right_or_model(client, stt_model, fake):
    fake()
    guest = make_user("guest", "gast")
    client.force_login(guest)
    response = transcribe(client, audio_upload())
    assert response.status_code == 403 and "nicht freigegeben" in response.json()["error"]
    adult = make_user("adult", "erw")
    client.force_login(adult)
    stt_model.delete()
    response = transcribe(client, audio_upload())
    assert response.status_code == 409 and "Spracherkennung" in response.json()["error"]


@pytest.mark.django_db
def test_transcribe_requires_login(client, stt_model):
    assert transcribe(client, audio_upload()).status_code == 403


@pytest.mark.django_db
def test_transcribe_respects_budget_and_offline(
    client, adult, stt_model, provider, fake, monkeypatch
):
    from multigpt.accounts import usage

    fake()
    monkeypatch.setattr(usage, "blocked_reason", lambda user, model: "Budget aufgebraucht.")
    response = transcribe(client, audio_upload())
    assert response.status_code == 403 and "Budget" in response.json()["error"]
    monkeypatch.undo()
    Provider.objects.filter(pk=provider.pk).update(check_status=True, online=False)
    response = transcribe(client, audio_upload())
    assert response.status_code == 503 and "offline" in response.json()["error"]


@pytest.mark.django_db
def test_transcribe_provider_error_has_a_german_message(client, adult, stt_model, fake):
    fake(error=ProviderError("Der Anbieter hat den Zugang abgelehnt. Bitte den API-Key prüfen."))
    response = transcribe(client, audio_upload())
    assert response.status_code == 502 and "API-Key" in response.json()["error"]
    assert not UsageEntry.objects.exists()


@pytest.mark.django_db
def test_transcribe_without_speech_in_the_recording(client, adult, stt_model, fake):
    fake(text="")
    response = transcribe(client, audio_upload())
    assert response.status_code == 422 and "keine Sprache" in response.json()["error"]


@pytest.mark.django_db
def test_transcribe_rejects_a_recording_longer_than_the_limit_reported_by_the_provider(
    client, adult, stt_model, fake
):
    fake(duration=500.0)
    assert transcribe(client, audio_upload(), duration="10").status_code == 413


# --- Vorlesen ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_speak_stores_audio_as_attachment_and_books_characters(
    client, adult, tts_model, answer, fake
):
    set_price(tts_model, unit_prices={"tts_characters": "15"})
    calls = fake()
    response = speak(client, answer)
    assert response.status_code == 200, response.content
    data = response.json()
    assert data["created"] is True and data["truncated"] is False
    attachment = Attachment.objects.get()
    assert attachment.kind == "audio" and attachment.message == answer and attachment.owner is None
    assert attachment.generated_by_model == tts_model and attachment.mime_type == "audio/wav"
    assert data["attachment"]["id"] == attachment.pk and data["attachment"]["url"] == attachment.url
    # Ohne Markdown, ohne Quellenzahl.
    assert calls == [("speak", "tts-1", "Das ist die Antwort. Siehe.", None)]
    entry = UsageEntry.objects.get(kind=UsageEntry.Kind.ATTACHMENT)
    assert entry.units == {"tts_characters": 27} and entry.user == adult
    assert entry.amount == Decimal("0.000405")  # 27 Zeichen * 15 je 1 Mio.
    assert attachment.cost is not None


@pytest.mark.django_db
def test_speak_twice_plays_the_stored_file_without_another_booking(
    client, adult, tts_model, answer, fake
):
    calls = fake()
    first = speak(client, answer).json()
    second = speak(client, answer).json()
    assert second["created"] is False and second["attachment"]["id"] == first["attachment"]["id"]
    assert len(calls) == 1 and Attachment.objects.count() == 1 and UsageEntry.objects.count() == 1
    forced = speak(client, answer, force=True).json()
    assert forced["created"] is True and Attachment.objects.count() == 2


@pytest.mark.django_db
def test_speak_uses_the_voice_of_the_account(client, adult, tts_model, provider, answer, fake):
    SpeechPreference.objects.create(user=adult, tts_model=tts_model, voice="mama")
    calls = fake()
    assert speak(client, answer).status_code == 200
    assert calls[0][3] == "mama"


@pytest.mark.django_db
def test_speak_ignores_a_voice_that_belongs_to_another_model(
    client, adult, tts_model, provider, answer, fake
):
    other = AIModel.objects.create(
        provider=provider, model_id="tts-x", display_name="Andere", capability="tts"
    )
    SpeechPreference.objects.create(user=adult, tts_model=other, voice="mama")
    row = ChatSettings.load()
    row.default_tts_model = tts_model
    row.save()
    SpeechPreference.objects.filter(user=adult).update(tts_model=other)
    other.delete()  # gewähltes Modell weg -> Standard; die Stimme gehört zum alten Modell
    calls = fake()
    assert speak(client, answer).status_code == 200
    assert calls[0][1] == "tts-1"


@pytest.mark.django_db
def test_speak_refuses_non_answers_unfinished_and_empty(client, adult, tts_model, answer, fake):
    calls = fake()
    user_message = Message.objects.get(role="user")
    assert speak(client, user_message).status_code == 400
    Message.objects.filter(pk=answer.pk).update(status=Message.Status.ERROR)
    assert speak(client, answer).status_code == 409
    Message.objects.filter(pk=answer.pk).update(
        status=Message.Status.COMPLETE, content="```\nx\n```"
    )
    assert speak(client, answer).status_code == 422
    assert calls == []


@pytest.mark.django_db
def test_speak_checks_read_access_and_right(client, adult, tts_model, answer, fake):
    fake()
    stranger = make_user("adult", "fremd")
    client.force_login(stranger)
    assert speak(client, answer).status_code == 404
    guest = make_user("guest", "gast")
    client.force_login(guest)
    assert speak(client, answer).status_code == 403
    client.logout()
    assert speak(client, answer).status_code == 403
    assert not Attachment.objects.exists()


@pytest.mark.django_db
def test_speak_error_and_missing_model(client, adult, tts_model, answer, fake):
    fake(error=ProviderError("Der Anbieter hat einen Serverfehler gemeldet.", retryable=True))
    response = speak(client, answer)
    assert response.status_code == 502 and "Serverfehler" in response.json()["error"]
    assert not Attachment.objects.exists() and not UsageEntry.objects.exists()
    tts_model.delete()
    assert speak(client, answer).status_code == 409


@pytest.mark.django_db
def test_speak_rejects_bad_json(client, adult, tts_model, answer, fake):
    fake()
    response = client.post(
        reverse("chat:api_message_speak", args=[answer.pk]), "[1]", content_type="application/json"
    )
    assert response.status_code == 400


# --- Auslieferung ----------------------------------------------------------------------


@pytest.mark.django_db
def test_voices_endpoint_openai_list_and_provider_list(client, adult, tts_model, provider, fake):
    fake(voices=[])
    url = reverse("chat:api_speech_voices")
    openai = client.get(url, {"model": tts_model.pk}).json()["voices"]
    assert "alloy" in openai and "marin" in openai
    local = Provider.objects.create(name="Eliezer", kind="lmstudio", base_url=ELIEZER)
    chatterbox = AIModel.objects.create(
        provider=local, model_id="chatterbox-q8", display_name="Chatterbox", capability="tts"
    )
    fake(voices=["mama", "papa"])
    assert client.get(url, {"model": chatterbox.pk}).json() == {"voices": ["mama", "papa"]}
    assert client.get(url, {"model": "x"}).status_code == 400
    assert client.get(url, {"model": 99999}).status_code == 404


@pytest.mark.django_db
def test_local_provider_without_voices_gets_no_openai_list(db):
    local = Provider.objects.create(name="Eliezer", kind="lmstudio", base_url=ELIEZER)
    model = AIModel.objects.create(provider=local, model_id="c", display_name="C", capability="tts")
    with respx.mock() as router:
        router.get(f"{ELIEZER}/v1/audio/voices").respond(404, json={})
        assert speech.voices(model) == []


# --- Einstellungen und Oberfläche -----------------------------------------------------


@pytest.mark.django_db
def test_chat_page_offers_microphone_and_speaker_with_limits(
    client, adult, stt_model, tts_model, chat_model
):
    ChatSettings.objects.update_or_create(
        pk=ChatSettings.SINGLETON_PK, defaults={"speech_max_seconds": 45, "speech_max_upload_mb": 3}
    )
    adult.auto_read_aloud = True
    adult.save()
    html = client.get(reverse("chat:index")).content.decode()
    assert 'id="mic-button"' in html and 'aria-label="Spracheingabe starten"' in html
    assert 'data-speech-stt="true"' in html and 'data-speech-tts="true"' in html
    assert 'data-speech-auto="true"' in html and 'data-speech-max-seconds="45"' in html
    assert f'data-speech-max-bytes="{3 * 1024 * 1024}"' in html
    assert "chat/speech.js" in html


@pytest.mark.django_db
def test_chat_page_has_no_microphone_without_models(client, adult, chat_model):
    html = client.get(reverse("chat:index")).content.decode()
    assert 'id="mic-button"' not in html and 'data-speech-stt="false"' in html


@pytest.mark.django_db
def test_chat_page_has_no_microphone_without_the_right(client, stt_model, chat_model):
    guest = make_user("guest", "gast")
    Role.objects.filter(pk=guest.role_id).update(all_models=True)
    client.force_login(guest)
    html = client.get(reverse("chat:index")).content.decode()
    assert 'id="mic-button"' not in html and 'data-speech-stt="false"' in html


def test_speech_script_is_plain_javascript_without_inline_handlers():
    from pathlib import Path

    source = (Path(speech.__file__).parent / "static/chat/speech.js").read_text(encoding="utf-8")
    assert "innerHTML" not in source and "eval(" not in source
    assert "isSecureContext" in source and "MediaRecorder" in source


# --- Admin-Grenzen (M10-03) ------------------------------------------------------------


@pytest.mark.django_db
def test_pick_prefers_account_then_default_then_first(adult, provider, stt_model):
    second = AIModel.objects.create(
        provider=provider, model_id="whisper-b", display_name="B", capability="stt", sort_order=9
    )
    assert speech.pick_model(adult, "stt")[0] == stt_model
    row = ChatSettings.load()
    row.default_stt_model = second
    row.save()
    assert speech.pick_model(adult, "stt")[0] == second
    SpeechPreference.objects.create(user=adult, stt_model=stt_model)
    assert speech.pick_model(adult, "stt")[0] == stt_model
    assert speech.availability(adult) == {"stt": True, "tts": False}


def test_book_service_has_no_message_and_uses_the_provider_account(db, provider, stt_model, adult):
    entry = booking.book_service(adult, stt_model, {"audio_minute": 2})
    assert entry.kind == UsageEntry.Kind.TOOL and entry.units == {"audio_minute": 2}
    assert entry.account == booking.account_of(provider)
