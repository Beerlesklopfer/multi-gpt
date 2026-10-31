# ruff: noqa: F401, F811 - Fixtures aus tests/test_speech.py
"""Sprache (M10-03): Grenzen aus den Chat-Einstellungen (Aufnahmelänge, Dateigröße, Zeichen) und
ihr Admin-Formular.

Fixtures und Fake-Adapter stammen aus ``tests/test_speech.py``.
"""

import pytest
from django.urls import reverse

from multigpt.accounts.models import User
from multigpt.billing.models import UsageEntry
from multigpt.chat import speech
from multigpt.chat.models import ChatSettings, Message
from tests.test_speech import (
    PASSWORD,
    _clear_cache,
    adult,
    answer,
    audio_upload,
    chat_model,
    fake,
    media,
    provider,
    speak,
    stt_model,
    transcribe,
    tts_model,
)


@pytest.mark.django_db
def test_transcribe_limits_file_size_and_length(client, adult, stt_model, fake):
    calls = fake()
    row = ChatSettings.load()
    row.speech_max_upload_mb = 1
    row.speech_max_seconds = 30
    row.save()
    big = transcribe(client, audio_upload(size=1024 * 1024 + 1))
    assert big.status_code == 413 and "höchstens 1 MB" in big.json()["error"]
    long = transcribe(client, audio_upload(), duration="45")
    assert long.status_code == 413 and "höchstens 30 Sekunden" in long.json()["error"]
    assert transcribe(client, audio_upload(), duration="31").status_code == 200  # Spielraum
    assert len(calls) == 1  # abgelehnte Aufnahmen gehen nie an den Anbieter
    assert UsageEntry.objects.count() == 1


@pytest.mark.django_db
def test_limits_come_from_the_chat_settings():
    assert speech.limits() == {
        "max_seconds": 120,
        "max_upload_bytes": 25 * 1024 * 1024,
        "max_chars": 4000,
    }
    row = ChatSettings.load()
    row.speech_max_seconds, row.speech_max_upload_mb, row.speech_max_chars = 60, 5, 500
    row.save()
    assert speech.limits() == {
        "max_seconds": 60,
        "max_upload_bytes": 5 * 1024 * 1024,
        "max_chars": 500,
    }


@pytest.mark.django_db
def test_admin_shows_the_speech_fields(client, db):
    admin = User.objects.create_superuser("chef", password=PASSWORD)
    client.force_login(admin)
    html = client.get(
        reverse("admin:chat_chatsettings_change", args=[ChatSettings.SINGLETON_PK])
    ).content.decode()
    for name in (
        "speech_max_seconds",
        "speech_max_upload_mb",
        "speech_max_chars",
        "default_stt_model",
    ):
        assert f'name="{name}"' in html


@pytest.mark.django_db
def test_speak_shortens_long_answers_at_the_limit(client, adult, tts_model, answer, fake):
    row = ChatSettings.load()
    row.speech_max_chars = 100
    row.save()
    Message.objects.filter(pk=answer.pk).update(content="Ein Satz hier. " * 20)
    calls = fake()
    data = speak(client, answer).json()
    assert data["truncated"] is True
    assert len(calls[0][2]) <= 100 and calls[0][2].endswith(".")
    assert UsageEntry.objects.get().units == {"tts_characters": len(calls[0][2])}
