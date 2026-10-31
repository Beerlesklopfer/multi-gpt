# ruff: noqa: F401, F811 - Fixtures aus tests/test_speech.py
"""Sprache (M10-02): Oberfläche zum Vorlesen – Auslieferung der Audiodatei, Audio-Zeile im Chat und
Seite „Einstellungen“ (Modelle, Stimme, automatisch vorlesen).

Fixtures und Fake-Adapter stammen aus ``tests/test_speech.py``.
"""

import pytest
from django.urls import reverse

from multigpt.accounts.models import Role
from multigpt.chat.models import SpeechPreference
from tests.test_speech import (
    WAV,
    _clear_cache,
    adult,
    answer,
    chat_model,
    fake,
    make_user,
    media,
    provider,
    speak,
    stt_model,
    tts_model,
)


@pytest.mark.django_db
def test_audio_is_served_inline_to_readers_only(client, adult, tts_model, answer, fake):
    fake()
    url = speak(client, answer).json()["attachment"]["url"]
    response = client.get(url)
    assert response.status_code == 200 and response["Content-Type"] == "audio/wav"
    assert response["Content-Disposition"].startswith("inline")
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["Cache-Control"] == "private, no-store"
    assert b"".join(response.streaming_content) == WAV
    client.force_login(make_user("adult", "fremd"))
    assert client.get(url).status_code == 404
    client.logout()
    assert client.get(url).status_code == 302  # Anmeldung nötig


@pytest.mark.django_db
def test_audio_shows_in_the_chat_as_player(client, adult, tts_model, answer, fake):
    fake()
    speak(client, answer)
    html = client.get(reverse("chat:conversation", args=[answer.conversation_id])).content.decode()
    assert '<audio class="attachment-audio" controls' in html and "data-speak-message" in html


# --- Stimmen ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_settings_page_saves_speech_preferences(client, adult, stt_model, tts_model, fake):
    fake(voices=["alloy"])
    url = reverse("settings")
    page = client.get(url).content.decode()
    assert 'id="sprache"' in page and "Antworten automatisch vorlesen" in page
    base = {
        "citation_style": "apa",
        "citation_locator": "on",
        "context_menu": "on",
        "speech-stt_model": stt_model.pk,
        "speech-stt_language": "EN",
        "speech-tts_model": tts_model.pk,
        "speech-voice": "marin",
        "speech-auto_read_aloud": "on",
    }
    response = client.post(url, base)
    assert response.status_code == 302, response.content.decode()[:500]
    pref = SpeechPreference.objects.get(user=adult)
    assert (pref.stt_model, pref.tts_model, pref.voice, pref.stt_language) == (
        stt_model,
        tts_model,
        "marin",
        "en",
    )
    adult.refresh_from_db()
    assert adult.auto_read_aloud is True
    base.pop("speech-auto_read_aloud")
    assert client.post(url, base).status_code == 302
    adult.refresh_from_db()
    assert adult.auto_read_aloud is False


@pytest.mark.django_db
def test_settings_page_rejects_a_bad_voice_and_foreign_models(
    client, adult, stt_model, tts_model, fake
):
    fake()
    url = reverse("settings")
    base = {"citation_style": "apa", "citation_locator": "on", "context_menu": "on"}
    bad = client.post(url, {**base, "speech-voice": "../etc/passwd"})
    assert bad.status_code == 200 and "Nur Buchstaben" in bad.content.decode()
    foreign = client.post(url, {**base, "speech-stt_model": tts_model.pk})  # falsche Art
    assert foreign.status_code == 200
    assert not SpeechPreference.objects.filter(user=adult).exists()


@pytest.mark.django_db
def test_settings_page_hides_speech_without_right_or_models(client, adult):
    assert 'id="sprache"' not in client.get(reverse("settings")).content.decode()


@pytest.mark.django_db
def test_settings_page_hides_speech_without_the_role_right(client, stt_model, tts_model):
    guest = make_user("guest", "gast")
    Role.objects.filter(pk=guest.role_id).update(all_models=True)
    client.force_login(guest)
    assert 'id="sprache"' not in client.get(reverse("settings")).content.decode()
