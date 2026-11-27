"""Bilder: einstellbares Zeitlimit und Lebenszeichen im Stream (LOP C45)."""

import threading

import pytest
from django.test import override_settings

from multigpt.chat import api, api_images
from multigpt.chat.models import Provider
from multigpt.chat.providers import openai_compat, registry


def test_keepalive_while_waiting_then_result():
    release = threading.Event()
    gen = api_images._waiting(lambda: release.wait(5) and "fertig", interval=0.01)
    assert next(gen) == (None, None)  # Lebenszeichen, solange die Arbeit läuft
    release.set()
    with pytest.raises(StopIteration) as stop:
        while True:
            next(gen)
    assert stop.value.value == "fertig"


def test_waiting_reraises_errors():
    def boom():
        raise ValueError("kaputt")

    with pytest.raises(ValueError, match="kaputt"):
        list(api_images._waiting(boom, interval=0.01))


@pytest.mark.django_db
def test_waiting_thread_sees_test_data():
    """Der Thread nutzt die Verbindung der Anfrage und sieht deshalb ihre Daten."""
    Provider.objects.create(name="Sichtbar", kind="openai_compat")
    gen = api_images._waiting(lambda: Provider.objects.filter(name="Sichtbar").count())
    with pytest.raises(StopIteration) as stop:
        while True:
            next(gen)
    assert stop.value.value == 1


def test_sse_keepalive_is_a_comment():
    def events():
        yield None, None
        yield "done", {"ok": True}

    chunks = list(api._sse(events()))
    assert chunks[0] == b": keepalive\n\n"
    assert chunks[1].startswith(b"event: done\n")


@override_settings(IMAGE_TIMEOUT_SECONDS=900)
def test_adapter_takes_image_timeout_from_settings():
    adapter = registry.get_adapter(Provider(name="x", kind="openai_compat"))
    assert adapter.image_timeout_seconds == 900
    assert openai_compat.image_timeout(adapter.image_timeout_seconds).read == 900
    assert openai_compat.image_timeout(5).read == 30  # Untergrenze
