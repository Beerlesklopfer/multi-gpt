"""Management-Kommando sync_models (M4-02)."""

import io
import logging

import httpx
import pytest
import respx
from django.core.management import CommandError, call_command

from multigpt.chat.management.commands import sync_models
from multigpt.chat.management.commands.sync_models import guess_capability
from multigpt.chat.models import AIModel, Provider
from multigpt.chat.providers import ProviderError

pytestmark = pytest.mark.django_db

KEY = "sk-ant-sync-GEHEIM-123"


class FakeAdapter:
    responses: dict = {}
    calls: list = []

    def __init__(self, provider):
        self.provider = provider

    def list_models(self, timeout=None):
        FakeAdapter.calls.append(self.provider.name)
        result = FakeAdapter.responses[self.provider.name]
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def fake(monkeypatch):
    FakeAdapter.responses = {}
    FakeAdapter.calls = []
    monkeypatch.setattr(sync_models, "get_adapter", FakeAdapter)
    return FakeAdapter


def run(*args):
    out, err = io.StringIO(), io.StringIO()
    call_command("sync_models", *args, stdout=out, stderr=err)
    return out.getvalue(), err.getvalue()


@pytest.mark.parametrize(
    ("model_id", "capability"),
    [
        ("gpt-4o", "chat"),
        ("claude-opus-5", "chat"),
        ("gemini-2.5-flash", "chat"),
        ("text-embedding-3-small", "embedding"),
        ("gemini-embedding-001", "embedding"),
        ("mistral-embed", "embedding"),
        ("tts-1", "tts"),
        ("gpt-4o-mini-tts", "tts"),
        ("gemini-2.5-flash-preview-tts", "tts"),
        ("whisper-1", "stt"),
        ("gpt-4o-transcribe", "stt"),
        ("dall-e-3", "image"),
        ("gpt-image-1", "image"),
        ("imagen-4.0-generate-001", "image"),
        ("gemini-2.5-flash-image", "image"),
        ("lyria-002", "music"),
        # Nicht eindeutig -> chat, der Verwalter korrigiert.
        ("shorts-model", "chat"),
    ],
)
def test_guess_capability(model_id, capability):
    assert guess_capability(model_id) == capability


def test_creates_missing_inactive_and_keeps_existing(fake):
    provider = Provider.objects.create(name="OpenAI", kind="openai_compat")
    AIModel.objects.create(
        provider=provider,
        model_id="gpt-a",
        display_name="Mein GPT",
        capability="chat",
        active=True,
        sort_order=5,
    )
    fake.responses = {"OpenAI": ["gpt-a", "gpt-b", "text-embedding-3-small", "gpt-b"]}
    out, _ = run()

    models = {m.model_id: m for m in provider.ai_models.all()}
    assert set(models) == {"gpt-a", "gpt-b", "text-embedding-3-small"}
    existing = models["gpt-a"]
    assert (existing.display_name, existing.active, existing.sort_order) == ("Mein GPT", True, 5)
    assert models["gpt-b"].active is False
    assert models["gpt-b"].capability == "chat"
    assert models["gpt-b"].display_name == "gpt-b"
    assert models["text-embedding-3-small"].capability == "embedding"
    assert "OpenAI: 4 gemeldet, 2 neu (inaktiv), 1 bereits vorhanden." in out
    assert "+ gpt-b (Chat)" in out

    # Zweiter Lauf ändert nichts.
    out, _ = run()
    assert provider.ai_models.count() == 3
    assert "0 neu" in out


def test_skips_local_and_inactive(fake):
    Provider.objects.create(name="LM Studio", kind="openai_compat", is_local=True)
    Provider.objects.create(name="Alt", kind="anthropic", active=False)
    Provider.objects.create(name="Claude", kind="anthropic")
    fake.responses = {"Claude": ["claude-x"]}
    out, _ = run()
    assert fake.calls == ["Claude"]
    assert "LM Studio: übersprungen" in out
    assert AIModel.objects.filter(model_id="claude-x", active=False).exists()


def test_provider_option(fake):
    claude = Provider.objects.create(name="Claude", kind="anthropic")
    Provider.objects.create(name="Gemini", kind="google")
    fake.responses = {"Claude": ["claude-x"], "Gemini": ["gemini-x"]}
    run("--provider", "Claude")
    assert fake.calls == ["Claude"]
    run("--provider", str(claude.pk))
    assert fake.calls == ["Claude", "Claude"]
    with pytest.raises(CommandError, match="nicht gefunden"):
        run("--provider", "Unbekannt")


def test_provider_option_inactive(fake):
    Provider.objects.create(name="Alt", kind="anthropic", active=False)
    with pytest.raises(CommandError, match="nicht aktiv"):
        run("--provider", "Alt")


def test_dry_run_saves_nothing(fake):
    Provider.objects.create(name="Claude", kind="anthropic")
    fake.responses = {"Claude": ["claude-x"]}
    out, _ = run("--dry-run")
    assert "1 würden angelegt" in out
    assert not AIModel.objects.exists()


def test_error_continues_and_fails_at_end(fake):
    Provider.objects.create(name="A-kaputt", kind="anthropic")
    Provider.objects.create(name="B-gut", kind="google")
    fake.responses = {
        "A-kaputt": ProviderError("Der Anbieter hat den Zugang abgelehnt."),
        "B-gut": ["gemini-x"],
    }
    out, err = io.StringIO(), io.StringIO()
    with pytest.raises(CommandError, match="1 Anbieter"):
        call_command("sync_models", stdout=out, stderr=err)
    assert "A-kaputt: Der Anbieter hat den Zugang abgelehnt." in err.getvalue()
    assert AIModel.objects.filter(model_id="gemini-x").exists()


def test_end_to_end_with_real_adapter(caplog):
    Provider.objects.create(name="Claude", kind="anthropic", api_key=KEY)
    page = {"data": [{"id": "claude-neu", "type": "model"}], "has_more": False}
    with respx.mock() as router, caplog.at_level(logging.DEBUG):
        route = router.get("https://api.anthropic.com/v1/models").mock(
            return_value=httpx.Response(200, json=page)
        )
        out, _ = run()
    assert route.calls.last.request.headers["x-api-key"] == KEY
    assert AIModel.objects.get(model_id="claude-neu").active is False
    assert KEY not in out and KEY not in caplog.text


def test_end_to_end_error_hides_key(caplog):
    Provider.objects.create(name="Claude", kind="anthropic", api_key=KEY)
    with respx.mock() as router, caplog.at_level(logging.DEBUG):
        router.get("https://api.anthropic.com/v1/models").mock(
            return_value=httpx.Response(401, json={"error": {"message": KEY}})
        )
        out, err = io.StringIO(), io.StringIO()
        with pytest.raises(CommandError):
            call_command("sync_models", stdout=out, stderr=err)
    assert "API-Key" in err.getvalue()
    assert KEY not in err.getvalue() + out.getvalue() + caplog.text
