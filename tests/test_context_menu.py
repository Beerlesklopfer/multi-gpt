"""Kontextmenü im Chat (M13-01) und Mitscrollen beim Streamen: Einbindung und Einstellung."""

import importlib

import pytest
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.urls import reverse

from multigpt.accounts.models import Role, User
from multigpt.chat.models import AIModel, Conversation, Message, Provider, Share

pytestmark = pytest.mark.django_db

SCRIPT = "chat/context_menu.js"


@pytest.fixture
def owner(client):
    user = User.objects.create_user(
        "jo", password="Geheim-Test-1234", role=Role.objects.get(key="adult")
    )
    client.force_login(user)
    return user


@pytest.fixture
def conversation(owner):
    provider = Provider.objects.create(name="LM", kind="openai_compat")
    model = AIModel.objects.create(provider=provider, model_id="m", display_name="M")
    conversation = Conversation.objects.create(user=owner, title="T")
    question = Message.objects.create(conversation=conversation, role="user", content="Frage")
    answer = Message.objects.create(
        conversation=conversation,
        role="assistant",
        content="Antwort",
        parent=question,
        model=model,
        status="complete",
    )
    Conversation.objects.filter(pk=conversation.pk).update(current_leaf=answer)
    return conversation


def chat_page(client, conversation):
    return client.get(reverse("chat:conversation", args=[conversation.pk])).content.decode()


def test_context_menu_and_scroll_button_included(client, conversation):
    html = chat_page(client, conversation)
    assert SCRIPT in html
    assert 'data-context-menu="true"' in html
    # Mitscrollen vor chat.js, Knopf „↓ Neueste“ mit Beschriftung für Vorleser.
    assert html.index("chat/scroll_follow.js") < html.index("chat/chat.js")
    assert 'id="scroll-latest"' in html
    assert 'aria-label="Zu den neuesten Nachrichten springen"' in html


def test_context_menu_on_start_page(client, owner):
    html = client.get(reverse("chat:index")).content.decode()
    assert SCRIPT in html and 'id="scroll-latest"' in html


def test_context_menu_default_on(owner):
    assert owner.context_menu is True


def test_context_menu_can_be_switched_off(client, conversation):
    page = client.get(reverse("settings")).content.decode()
    assert 'name="context_menu"' in page and "Eigenes Kontextmenü im Chat" in page
    response = client.post(
        reverse("settings"),
        {"citation_style": "din", "citation_locator": "on", "mark_unverified_links": "on"},
    )
    assert response.status_code == 302
    conversation.user.refresh_from_db()
    assert conversation.user.context_menu is False
    html = chat_page(client, conversation)
    assert SCRIPT not in html
    assert 'data-context-menu="false"' in html
    # Mitscrollen ist keine Einstellung: bleibt immer.
    assert "chat/scroll_follow.js" in html
    client.post(
        reverse("settings"),
        {"citation_style": "din", "citation_locator": "on", "context_menu": "on"},
    )
    conversation.user.refresh_from_db()
    assert conversation.user.context_menu is True
    assert SCRIPT in chat_page(client, conversation)


def test_read_only_chat_has_menu_but_no_input(client, conversation):
    """Geteilter Chat nur zum Lesen: Menü geladen (Kopieren), kein Eingabefeld."""
    reader = User.objects.create_user("leser", role=Role.objects.get(key="adult"))
    Share.objects.create(conversation=conversation, user=reader)
    client.force_login(reader)
    html = chat_page(client, conversation)
    assert SCRIPT in html
    assert 'id="message-input"' not in html


def test_migration_adds_field_with_default_on():
    migration = importlib.import_module("multigpt.accounts.migrations.0011_context_menu")
    (operation,) = migration.Migration.operations
    assert operation.name == "context_menu"
    assert operation.field.default is True
    loader = MigrationLoader(connection)
    leaves = loader.graph.leaf_nodes("accounts")
    assert leaves == [("accounts", "0011_context_menu")]
    assert ("accounts", "0010_role_api_access") in migration.Migration.dependencies
