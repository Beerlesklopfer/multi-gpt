"""Basisschnittstelle und Registry der Anbieter-Adapter (M3-01)."""

import dataclasses

import pytest

from multigpt.chat.models import Provider
from multigpt.chat.providers import (
    ChatMessage,
    Delta,
    Done,
    Error,
    ProviderAdapter,
    ProviderError,
    ToolCallEvent,
    Usage,
    get_adapter,
)
from multigpt.chat.providers.openai_compat import OpenAICompatAdapter


def test_events_are_frozen():
    for event in (
        Delta("a"),
        ToolCallEvent("1", "x", {}),
        Usage(1, 2),
        Error("x"),
        Done(),
    ):
        with pytest.raises(dataclasses.FrozenInstanceError):
            event.foo = 1  # noqa: B010


def test_event_defaults():
    assert Error("x").retryable is False
    assert Done().finish_reason is None
    assert ChatMessage("user", "Hallo").content == "Hallo"


def test_unsupported_capabilities_raise():
    adapter = ProviderAdapter(Provider(name="x", kind="openai_compat"))
    with pytest.raises(NotImplementedError):
        adapter.embed("m", ["t"])
    with pytest.raises(NotImplementedError):
        adapter.transcribe("m", b"")
    with pytest.raises(NotImplementedError):
        adapter.speak("m", "t")
    with pytest.raises(NotImplementedError):
        adapter.generate_image("m", "p")
    with pytest.raises(NotImplementedError):
        adapter.edit_image("m", b"", "p")


def test_base_is_online_never_raises():
    # list_models ist in der Basis nicht umgesetzt -> offline, keine Ausnahme.
    assert ProviderAdapter(Provider(name="x", kind="openai_compat")).is_online() is False


def test_registry_openai_compat():
    adapter = get_adapter(Provider(name="x", kind=Provider.Kind.OPENAI_COMPAT))
    assert isinstance(adapter, OpenAICompatAdapter)


@pytest.mark.parametrize("kind", ["anthropic", "google", "unbekannt"])
def test_registry_unknown_kind(kind):
    with pytest.raises(ProviderError):
        get_adapter(Provider(name="x", kind=kind))


def test_repr_hides_key():
    adapter = get_adapter(Provider(name="x", kind="openai_compat", api_key="sk-geheim"))
    assert "sk-geheim" not in repr(adapter)
