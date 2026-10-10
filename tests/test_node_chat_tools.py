"""Chat-Werkzeuge der Indexierung (M15): index_status ohne Rückfrage,
start_reindex, start_scan und cancel_run nur nach Bestätigung im Chat."""

import json

import pytest

from multigpt.chat import services, tooling
from multigpt.chat.models import (
    AIModel,
    Collection,
    Conversation,
    Document,
    IndexRun,
    Message,
    Provider,
    ToolCall,
)
from multigpt.chat.providers import registry
from multigpt.chat.providers.base import Delta, Done, ProviderAdapter, ToolCallEvent
from multigpt.node import chat_tools
from tests.node_support import make_user

pytestmark = pytest.mark.django_db


class ScriptedAdapter(ProviderAdapter):
    """Erste Runde: vorgegebene Werkzeugaufrufe, danach Text."""

    def __init__(self, provider, script):
        super().__init__(provider)
        self.script = script
        self.offered = []

    def stream(self, model_id, messages, system=None, tools=None, **params):
        self.offered.append({t.name for t in tools or []})
        step = self.script.pop(0) if self.script else [Delta("Fertig."), Done()]
        return iter(step)


@pytest.fixture
def model():
    provider = Provider.objects.create(name="Cloud", kind=Provider.Kind.OPENAI_COMPAT)
    return AIModel.objects.create(
        provider=provider, model_id="m", display_name="M", supports_tools=True
    )


def install(monkeypatch, script):
    holder = {}

    def get_adapter(provider):
        holder.setdefault("adapter", ScriptedAdapter(provider, script))
        return holder["adapter"]

    monkeypatch.setattr(registry, "get_adapter", get_adapter)
    return holder


def ask(user, model, text="Bitte"):
    conversation = Conversation.objects.create(user=user)
    turn = services.prepare_turn(user, conversation, model, content=text, mcp_servers=[])
    return conversation, list(services.run_turn(turn))


def test_tools_offered_by_rights(model):
    anna = make_user("anna")
    names = set(tooling.builtin_bindings(anna, model))
    assert chat_tools.INDEX_STATUS not in names  # nichts zu beobachten
    Collection.objects.create(owner=anna, name="Haus")
    names = set(tooling.builtin_bindings(anna, model))
    assert {chat_tools.INDEX_STATUS, chat_tools.START_REINDEX, chat_tools.CANCEL_RUN} <= names
    assert chat_tools.START_SCAN not in names  # nur Verwalter mit Quellen
    for name in (chat_tools.START_REINDEX, chat_tools.START_SCAN, chat_tools.CANCEL_RUN):
        assert tooling.builtin_needs_confirmation(name)
    assert not tooling.builtin_needs_confirmation(chat_tools.INDEX_STATUS)


def test_index_status_runs_without_confirmation(monkeypatch, model):
    anna = make_user("anna")
    collection = Collection.objects.create(owner=anna, name="Haus")
    run = IndexRun.objects.create(kind=IndexRun.Kind.UPLOAD, collection=collection)
    install(
        monkeypatch,
        [[ToolCallEvent("c1", chat_tools.INDEX_STATUS, {}), Done("tool_calls")]],
    )
    _, events = ask(anna, model)
    names = [name for name, _ in events]
    assert "confirmation_required" not in names
    call = ToolCall.objects.get(tool=chat_tools.INDEX_STATUS)
    assert call.status == ToolCall.Status.OK
    assert json.loads(call.result_text)["runs"][0]["id"] == run.pk


def test_start_reindex_waits_for_confirmation(monkeypatch, model):
    anna = make_user("anna")
    collection = Collection.objects.create(owner=anna, name="Haus")
    Document.objects.create(collection=collection, title="A")
    install(
        monkeypatch,
        [
            [
                ToolCallEvent("c1", chat_tools.START_REINDEX, {"collection": "Haus"}),
                Done("tool_calls"),
            ]
        ],
    )
    conversation, events = ask(anna, model)
    assert events[-1] == ("done", {"status": Message.Status.AWAITING_CONFIRMATION})
    assert not IndexRun.objects.exists()  # ohne Bestätigung nichts gestartet
    call = ToolCall.objects.get(tool=chat_tools.START_REINDEX)
    message = services.pending_message(conversation)
    turn = services.prepare_resume(anna, conversation, message, {str(call.pk): "approve"})
    list(services.run_turn(turn))
    run = IndexRun.objects.get()
    assert run.kind == IndexRun.Kind.REINDEX_COLLECTION and run.started_by == anna
    call.refresh_from_db()
    assert call.status == ToolCall.Status.OK and f"Lauf #{run.pk}" in call.result_text


def test_cancel_run_rejected_runs_nothing(monkeypatch, model):
    anna = make_user("anna")
    collection = Collection.objects.create(owner=anna, name="Haus")
    run = IndexRun.objects.create(kind=IndexRun.Kind.UPLOAD, collection=collection)
    install(
        monkeypatch,
        [[ToolCallEvent("c1", chat_tools.CANCEL_RUN, {"run_id": run.pk}), Done("tool_calls")]],
    )
    conversation, _ = ask(anna, model)
    call = ToolCall.objects.get(tool=chat_tools.CANCEL_RUN)
    message = services.pending_message(conversation)
    turn = services.prepare_resume(anna, conversation, message, {str(call.pk): "reject"})
    list(services.run_turn(turn))
    run.refresh_from_db()
    assert run.status == IndexRun.Status.RUNNING
    call.refresh_from_db()
    assert call.status == ToolCall.Status.REJECTED


def test_chat_tools_respect_foreign_runs():
    anna = make_user("anna")
    bernd = make_user("bernd")
    other = Collection.objects.create(owner=bernd, name="B")
    run = IndexRun.objects.create(kind=IndexRun.Kind.UPLOAD, collection=other)
    builtin = tooling.get_builtin(chat_tools.CANCEL_RUN)
    result = builtin.run(anna, {"run_id": run.pk}, None)
    assert result.is_error and "nicht gefunden" in result.text
    result = tooling.get_builtin(chat_tools.START_SCAN).run(anna, {"source": 1}, None)
    assert result.is_error and "Verwalter" in result.text
