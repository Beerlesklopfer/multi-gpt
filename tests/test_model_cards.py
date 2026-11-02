"""Kurzvorstellung lokaler Modelle (chat/model_cards.py): Modellkarte, Übersetzung, Auslieferung."""

import httpx
import pytest
import respx
from django.core.management import call_command

from multigpt.chat import api, model_cards
from multigpt.chat.models import AIModel, Provider
from multigpt.chat.providers import registry
from multigpt.chat.providers.base import Delta, Done, Error

CARD = """---
license: apache-2.0
---

<p align="center">
  <img alt="logo" src="https://example.org/logo.svg">
</p>

# Gemma

- [Guides](https://example.org)

[Hugging Face](https://a.example) | [GitHub](https://b.example) | [Blog](https://c.example) | Docs

Gemma is a family of **open** models built for [local use](https://example.org), with vision.

Second paragraph that must not be used, although it is long enough for the filter.
"""
URL = "https://huggingface.co/google/gemma/raw/main/README.md"


class FakeAdapter:
    events = [Delta("Gemma ist eine "), Delta("Modellfamilie."), Done()]

    def __init__(self):
        self.calls = []

    def stream(self, model_id, messages, system=None, **params):
        self.calls.append((model_id, messages[0].content, system))
        return iter(self.events)


@pytest.fixture
def adapter(monkeypatch):
    fake = FakeAdapter()
    monkeypatch.setattr(registry, "get_adapter", lambda provider: fake)
    return fake


@pytest.fixture
def local_model(db):
    provider = Provider.objects.create(name="Eliezer", kind="lmstudio", is_local=True)
    return AIModel.objects.create(
        provider=provider, model_id="google/gemma", display_name="Gemma", active=True
    )


def test_intro_text_takes_the_first_prose_paragraph_without_markup():
    assert model_cards.intro_text(CARD) == (
        "Gemma is a family of open models built for local use, with vision."
    )


def test_intro_text_is_empty_without_prose():
    assert model_cards.intro_text("---\nlicense: mit\n---\n\n# Titel\n\n- Punkt\n") == ""


@respx.mock
def test_refresh_stores_the_translation_and_sends_only_the_paragraph(local_model, adapter):
    respx.get(URL).mock(return_value=httpx.Response(200, text=CARD))
    assert model_cards.refresh(local_model) == "Gemma ist eine Modellfamilie."
    local_model.refresh_from_db()
    assert local_model.intro == "Gemma ist eine Modellfamilie."
    model_id, content, system = adapter.calls[0]
    assert model_id == "google/gemma"
    assert content.startswith("Gemma is a family") and "Second paragraph" not in content
    assert "Deutsche" in system


@respx.mock
def test_refresh_keeps_the_text_when_the_card_is_missing(local_model, adapter):
    local_model.intro = "von Hand"
    local_model.save()
    respx.get(URL).mock(return_value=httpx.Response(404))
    with pytest.raises(model_cards.CardError, match="Keine Modellkarte"):
        model_cards.refresh(local_model)
    local_model.refresh_from_db()
    assert local_model.intro == "von Hand" and adapter.calls == []


@respx.mock
def test_refresh_keeps_the_text_when_the_translation_fails(local_model, adapter, monkeypatch):
    monkeypatch.setattr(FakeAdapter, "events", [Error("Modell nicht geladen.")])
    respx.get(URL).mock(return_value=httpx.Response(200, text=CARD))
    with pytest.raises(model_cards.CardError, match="nicht geladen"):
        model_cards.refresh(local_model)
    local_model.refresh_from_db()
    assert local_model.intro == ""


def test_refresh_rejects_external_models_and_ids_without_repo(db, local_model, adapter):
    local_model.model_id = "mistral-7b-instruct-v0.3-q4-k-m"
    with pytest.raises(model_cards.CardError, match="Organisation/Name"):
        model_cards.refresh(local_model)
    local_model.provider.is_local = False
    with pytest.raises(model_cards.CardError, match="lokale"):
        model_cards.refresh(local_model)


@respx.mock
def test_command_keeps_existing_texts_unless_forced(local_model, adapter):
    respx.get(URL).mock(return_value=httpx.Response(200, text=CARD))
    local_model.intro = "von Hand"
    local_model.save()
    call_command("model_intros")
    local_model.refresh_from_db()
    assert local_model.intro == "von Hand"
    call_command("model_intros", "--force")
    local_model.refresh_from_db()
    assert local_model.intro == "Gemma ist eine Modellfamilie."


def test_models_api_delivers_the_intro(local_model):
    local_model.intro = "Kurz vorgestellt."
    assert api._serialize_model(local_model)["intro"] == "Kurz vorgestellt."
