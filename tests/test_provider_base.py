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
from multigpt.chat.providers.anthropic import AnthropicAdapter
from multigpt.chat.providers.base import (
    MSG_TOOL_NO_RESULT,
    ToolSpec,
    alternate_turns,
    group_turns,
    normalize_tools,
    pair_tool_messages,
    parse_tool_arguments,
)
from multigpt.chat.providers.google import GoogleAdapter
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
    message = ChatMessage("assistant")
    assert message.content == "" and message.tool_calls == [] and message.provider_state is None
    assert ToolCallEvent("1", "x", {}).provider_state is None
    assert Done("stop").provider_state is None


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


@pytest.mark.parametrize(
    ("kind", "cls"), [("anthropic", AnthropicAdapter), ("google", GoogleAdapter)]
)
def test_registry_m4_adapters(kind, cls):
    assert isinstance(get_adapter(Provider(name="x", kind=kind)), cls)


@pytest.mark.parametrize("kind", ["unbekannt", ""])
def test_registry_unknown_kind(kind):
    with pytest.raises(ProviderError):
        get_adapter(Provider(name="x", kind=kind))


def test_repr_hides_key():
    adapter = get_adapter(Provider(name="x", kind="openai_compat", api_key="sk-geheim"))
    assert "sk-geheim" not in repr(adapter)


def test_alternate_turns_merges_and_extracts_system():
    turns, system = alternate_turns(
        [
            ChatMessage("assistant", "verwaist"),
            ChatMessage("system", "Regel"),
            ChatMessage("user", "Eins"),
            ChatMessage("user", "Zwei"),
            ChatMessage("assistant", ""),
            ChatMessage("assistant", "Antwort"),
            ChatMessage("tool", "Ergebnis"),
            ChatMessage("assistant", "Mehr"),
            ChatMessage("user", "Drei"),
        ]
    )
    assert system == ["Regel"]
    assert turns == [
        ChatMessage("user", "Eins\n\nZwei"),
        ChatMessage("assistant", "Antwort\n\nMehr"),
        ChatMessage("user", "Drei"),
    ]


# --- Werkzeuge (M4a) ---------------------------------------------------------


def test_normalize_tools_accepts_specs_and_dicts():
    spec = ToolSpec("a", "A", {"type": "object"})
    tools = normalize_tools(
        [
            spec,
            {"name": "b", "description": "B", "parameters": {"type": "object"}},
            {"name": "c", "inputSchema": {"type": "object"}},
            {"type": "function", "function": {"name": "d", "parameters": {}}},
        ]
    )
    assert tools == [
        spec,
        ToolSpec("b", "B", {"type": "object"}),
        ToolSpec("c", "", {"type": "object"}),
        ToolSpec("d", "", {}),
    ]
    assert normalize_tools(None) == []
    with pytest.raises(ProviderError):
        normalize_tools([{"description": "ohne Namen"}])


def test_parse_tool_arguments():
    assert parse_tool_arguments("", "x") == {}
    assert parse_tool_arguments('{"a": 1}', "x") == {"a": 1}
    for raw in ("{kaputt", "[1]", "3"):
        error = parse_tool_arguments(raw, "suche")
        assert isinstance(error, Error) and "„suche“" in error.message


def test_pair_tool_messages_orders_and_fills():
    calls = [ToolCallEvent("a", "x", {}), ToolCallEvent("b", "y", {})]
    paired = pair_tool_messages(
        [
            ChatMessage("tool", "verwaist", tool_call_id="z"),
            ChatMessage("user", "Frage"),
            ChatMessage("assistant", "", tool_calls=calls),
            ChatMessage("tool", "B", tool_call_id="b"),
            ChatMessage("tool", "B doppelt", tool_call_id="b"),
            ChatMessage("user", "Weiter"),
            ChatMessage("tool", "zu spät", tool_call_id="a"),
        ]
    )
    assert [(m.role, m.content, m.tool_call_id) for m in paired] == [
        ("user", "Frage", None),
        ("assistant", "", None),
        ("tool", MSG_TOOL_NO_RESULT, "a"),
        ("tool", "B", "b"),
        ("user", "Weiter", None),
    ]
    assert paired[2].is_error and paired[2].name == "x"


def test_group_turns_with_tools():
    call = ToolCallEvent("a", "x", {})
    turns, system = group_turns(
        [
            ChatMessage("assistant", "", tool_calls=[ToolCallEvent("v", "x", {})]),
            ChatMessage("tool", "verwaist mit führendem Aufruf", tool_call_id="v"),
            ChatMessage("system", "Regel"),
            ChatMessage("user", "Frage"),
            ChatMessage("assistant", "", tool_calls=[call]),
            ChatMessage("tool", "Ergebnis", tool_call_id="a"),
            ChatMessage("user", "Nachtrag"),
            ChatMessage("assistant", "Antwort"),
        ]
    )
    assert system == ["Regel"]
    assert [t.role for t in turns] == ["user", "assistant", "user", "assistant"]
    assert turns[1].messages[0].tool_calls == [call]
    assert [m.content for m in turns[2].tool_results] == ["Ergebnis"]
    assert turns[2].text == "Nachtrag"
