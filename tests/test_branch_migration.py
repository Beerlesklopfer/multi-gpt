"""Datenmigration 0010: bestehende Chats -> Gesprächsbaum (Versionen)."""

import importlib

import pytest
from django.apps import apps

from multigpt.accounts.models import Role, User
from multigpt.chat import services
from multigpt.chat.models import Conversation, Message

pytestmark = pytest.mark.django_db

migration = importlib.import_module("multigpt.chat.migrations.0010_message_branches_data")


@pytest.fixture
def conv():
    user = User.objects.create_user("alt", role=Role.objects.get(key="adult"))
    return Conversation.objects.create(user=user)


def legacy(conv, role, content, status="complete"):
    """Nachricht wie vor den Versionen: ohne parent, current_leaf bleibt leer."""
    return Message.objects.create(conversation=conv, role=role, content=content, status=status)


def test_chain_and_superseded_become_siblings(conv):
    q1 = legacy(conv, "user", "Frage 1")
    old1 = legacy(conv, "assistant", "alt 1", status="superseded")
    old2 = legacy(conv, "assistant", "alt 2", status="superseded")
    a1 = legacy(conv, "assistant", "neu", status="aborted")
    sys = legacy(conv, "system", "sys")
    q2 = legacy(conv, "user", "Frage 2")
    a2 = legacy(conv, "assistant", "x", status="error")
    other = Conversation.objects.create(user=conv.user)
    lone = legacy(other, "user", "nur Frage")
    empty = Conversation.objects.create(user=conv.user)

    migration.link_messages(apps, None)

    def get(m):
        return Message.objects.get(pk=m.pk)

    assert get(q1).parent_id is None
    assert get(old1).parent_id == q1.pk and get(old2).parent_id == q1.pk
    assert get(a1).parent_id == q1.pk
    assert get(q2).parent_id == a1.pk
    assert get(a2).parent_id == q2.pk
    assert get(sys).parent_id is None
    # Ersetzte Antworten: complete, Inhalt bleibt; andere Status unverändert.
    assert (get(old1).status, get(old1).content) == ("complete", "alt 1")
    assert get(a1).status == "aborted" and get(a2).status == "error"
    conv.refresh_from_db()
    other.refresh_from_db()
    empty.refresh_from_db()
    assert conv.current_leaf_id == a2.pk
    assert other.current_leaf_id == lone.pk
    assert empty.current_leaf_id is None
    # Angezeigter Pfad wie vorher, alte Antworten als Versionen.
    path = services.visible_messages(conv)
    assert [m.content for m in path] == ["Frage 1", "neu", "Frage 2", "x"]
    assert path[1].sibling_ids == [old1.pk, old2.pk, a1.pk]
    assert (path[1].sibling_index, path[1].sibling_count) == (2, 3)
    assert [m.content for m in services.build_history(conv)] == ["Frage 1", "neu", "Frage 2"]
    assert not Message.objects.filter(status="superseded").exists()


def test_all_superseded_falls_back_to_last_message(conv):
    q = legacy(conv, "user", "Frage")
    only = legacy(conv, "assistant", "alt", status="superseded")
    migration.link_messages(apps, None)
    conv.refresh_from_db()
    # Erste Nachricht sichtbar -> sie ist das Ende; die alte Antwort hängt daran.
    assert conv.current_leaf_id == q.pk
    assert Message.objects.get(pk=only.pk).parent_id == q.pk


def test_idempotent(conv):
    legacy(conv, "user", "Frage")
    legacy(conv, "assistant", "alt", status="superseded")
    legacy(conv, "assistant", "neu")
    migration.link_messages(apps, None)
    first = list(Message.objects.order_by("pk").values_list("parent_id", "status"))
    migration.link_messages(apps, None)
    assert list(Message.objects.order_by("pk").values_list("parent_id", "status")) == first


def test_reverse_hides_other_branches(conv):
    q = legacy(conv, "user", "Frage")
    old = legacy(conv, "assistant", "alt", status="superseded")
    new = legacy(conv, "assistant", "neu")
    migration.link_messages(apps, None)
    migration.unlink_messages(apps, None)
    statuses = dict(Message.objects.values_list("pk", "status"))
    assert statuses == {q.pk: "complete", old.pk: "superseded", new.pk: "complete"}
