"""MCP-Werkzeuge list_chats und read_chat (Recht chat.read)."""

import pytest

from multigpt.accounts.models import Role, User, UserGroup
from multigpt.chat.models import AIModel, Conversation, Message, Provider, Share, ToolCall
from multigpt.node import tool_chats
from multigpt.node.tools import CallContext, ToolFailure

pytestmark = pytest.mark.django_db

PASSWORD = "Geheim-Test-1234"


def make_user(username, role_key="adult", **fields):
    return User.objects.create_user(
        username, password=PASSWORD, role=Role.objects.get(key=role_key), **fields
    )


def ctx(user):
    return CallContext(user=user, key=None, scopes=frozenset({"chat.read"}))


@pytest.fixture
def setup():
    anna = make_user("anna", display_name="Anna")
    ben = make_user("ben", display_name="Ben")
    provider = Provider.objects.create(name="Test", kind=Provider.Kind.OPENAI_COMPAT)
    model = AIModel.objects.create(provider=provider, model_id="m", display_name="Modell M")
    conv = Conversation.objects.create(user=anna, title="Thread mit Fehler")
    q = Message.objects.create(conversation=conv, role="user", content="Schalte das Licht aus")
    a = Message.objects.create(
        conversation=conv,
        parent=q,
        role="assistant",
        model=model,
        content="Erledigt?",
        status=Message.Status.ERROR,
        error="Der Anbieter hat die Antwort abgebrochen.",
    )
    ToolCall.objects.create(
        message=a,
        tool="HassTurnOff",
        arguments={"name": "Schlafzimmer"},
        result={"text": "Gerät nicht gefunden", "is_error": True},
        status="error",
    )
    conv.current_leaf = a
    conv.save()
    other = Conversation.objects.create(user=ben, title="Bens Geheimnis")
    return {"anna": anna, "ben": ben, "conv": conv, "other": other}


def test_read_chat_shows_messages_tools_and_errors(setup):
    text = tool_chats.read_chat(ctx(setup["anna"]), {"chat_id": setup["conv"].pk})
    assert "Thread mit Fehler" in text and "Schalte das Licht aus" in text
    assert "HassTurnOff" in text and "Schlafzimmer" in text and "Gerät nicht gefunden" in text
    assert "Der Anbieter hat die Antwort abgebrochen." in text
    assert "(Seite 1 von 1)" in text


def test_foreign_chat_is_hidden_even_for_admins(setup):
    admin = make_user("jo", role_key="admin", is_staff=True)
    for user in (setup["ben"], admin):
        with pytest.raises(ToolFailure, match="nicht gefunden"):
            tool_chats.read_chat(ctx(user), {"chat_id": setup["conv"].pk})


def test_shared_chat_readable_and_listed(setup):
    group = UserGroup.objects.create(name="Lesetest")
    setup["ben"].groups.add(group)
    Share.objects.create(conversation=setup["conv"], group=group)
    text = tool_chats.read_chat(ctx(setup["ben"]), {"chat_id": setup["conv"].pk})
    assert "Schalte das Licht aus" in text
    data = tool_chats.list_chats(ctx(setup["ben"]), {}).data
    titles = {c["title"]: c for c in data["chats"]}
    assert titles["Thread mit Fehler"]["shared_by"] == "Anna"
    assert "Bens Geheimnis" in titles


def test_list_chats_query_and_paging(setup):
    for i in range(3):
        Conversation.objects.create(user=setup["anna"], title=f"Notiz {i}")
    args = {"query": "Notiz", "page_size": 2}
    data = tool_chats.list_chats(ctx(setup["anna"]), args).data
    assert len(data["chats"]) == 2 and data["more"]
    data = tool_chats.list_chats(ctx(setup["anna"]), {**args, "page": 2}).data
    assert len(data["chats"]) == 1 and not data["more"]


def test_long_chat_is_paged(setup, monkeypatch):
    monkeypatch.setattr(tool_chats, "PAGE_CHARS", 80)
    first = tool_chats.read_chat(ctx(setup["anna"]), {"chat_id": setup["conv"].pk})
    assert "weiter mit page=2" in first
    with pytest.raises(ToolFailure, match="Seite"):
        tool_chats.read_chat(ctx(setup["anna"]), {"chat_id": setup["conv"].pk, "page": 99})


def test_migration_adds_chat_read_to_roles_with_chat_ask():
    import importlib

    from django.apps import apps

    migration = importlib.import_module("multigpt.node.migrations.0005_chat_read_scope")
    role = Role.objects.get(key="adult")
    role.api_scopes = ["chat.ask", "docs.read"]
    role.save()
    migration.add_chat_read(apps, None)
    role.refresh_from_db()
    assert role.api_scopes == ["chat.ask", "chat.read", "docs.read"]
