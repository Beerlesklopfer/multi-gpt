"""Antwort löschen: Frage, alle Versionen der Antwort und der Verlauf danach
(DELETE /api/conversations/<pk>/messages/<id>/, ``services.delete_exchange``)."""

import os
from decimal import Decimal

import pytest
from django.core.files.base import ContentFile
from django.urls import reverse

from multigpt.accounts.models import Role, User, UserGroup
from multigpt.billing.models import UsageEntry
from multigpt.chat.models import (
    AIModel,
    Attachment,
    Conversation,
    ConversationView,
    Message,
    Provider,
    Share,
)
from tests.billing_helpers import book_stored

pytestmark = pytest.mark.django_db

PASSWORD = "Geheim-Test-1234"


def make_user(username, role_key="adult"):
    return User.objects.create_user(
        username, password=PASSWORD, role=Role.objects.get(key=role_key)
    )


@pytest.fixture
def ai_model():
    provider = Provider.objects.create(name="Testanbieter", kind=Provider.Kind.OPENAI_COMPAT)
    return AIModel.objects.create(provider=provider, model_id="m-1", display_name="Modell Eins")


@pytest.fixture
def anna(ai_model):
    return make_user("anna")


def msg(conv, parent, role, content, **fields):
    return Message.objects.create(
        conversation=conv, parent=parent, role=role, content=content, **fields
    )


@pytest.fixture
def chat(anna, ai_model):
    """q1 -> a1 -> q2 -> (a2, a2b) -> q3 -> a3; angezeigt bis a3."""
    conv = Conversation.objects.create(user=anna, title="Verlauf")
    q1 = msg(conv, None, Message.Role.USER, "Frage 1")
    a1 = msg(conv, q1, Message.Role.ASSISTANT, "Antwort 1", model=ai_model)
    q2 = msg(conv, a1, Message.Role.USER, "Frage 2")
    a2 = msg(conv, q2, Message.Role.ASSISTANT, "Antwort 2", model=ai_model)
    a2b = msg(conv, q2, Message.Role.ASSISTANT, "Antwort 2b", model=ai_model)
    q3 = msg(conv, a2b, Message.Role.USER, "Frage 3")
    a3 = msg(conv, q3, Message.Role.ASSISTANT, "Antwort 3", model=ai_model)
    conv.current_leaf = a3
    conv.save()
    return {"conv": conv, "q1": q1, "a1": a1, "q2": q2, "a2": a2, "a2b": a2b, "q3": q3, "a3": a3}


def url(chat_, message):
    return reverse("chat:api_message_delete", args=[chat_["conv"].pk, message.pk])


def remaining(chat_):
    return set(chat_["conv"].messages.values_list("content", flat=True))


def test_delete_answer_removes_question_and_everything_after(client, anna, chat):
    chat["a2b"].cost = Decimal("0.10")
    chat["a2b"].save()
    book_stored(chat["a2b"])
    client.force_login(anna)
    response = client.delete(url(chat, chat["a2b"]))
    assert response.status_code == 200
    assert remaining(chat) == {"Frage 1", "Antwort 1"}
    assert [m["content"] for m in response.json()] == ["Frage 1", "Antwort 1"]
    chat["conv"].refresh_from_db()
    assert chat["conv"].current_leaf_id == chat["a1"].pk
    # Kosten bleiben gebucht, nur ohne Bezug zur Nachricht.
    entry = UsageEntry.objects.get()
    assert entry.message_id is None and entry.amount_eur == Decimal("0.10")


def test_other_question_versions_stay_and_are_shown(client, anna, chat, ai_model):
    other = msg(chat["conv"], chat["a1"], Message.Role.USER, "Frage 2 anders")
    other_answer = msg(chat["conv"], other, Message.Role.ASSISTANT, "Antwort anders")
    client.force_login(anna)
    assert client.delete(url(chat, chat["a3"])).status_code == 200
    assert client.delete(url(chat, chat["q2"])).status_code == 200  # Frage direkt
    assert remaining(chat) == {"Frage 1", "Antwort 1", "Frage 2 anders", "Antwort anders"}
    chat["conv"].refresh_from_db()
    assert chat["conv"].current_leaf_id == other_answer.pk


def test_first_exchange_empties_chat(client, anna, chat):
    client.force_login(anna)
    response = client.delete(url(chat, chat["a1"]))
    assert response.status_code == 200 and response.json() == []
    assert not chat["conv"].messages.exists()


def test_attachment_files_removed(
    client, anna, chat, settings, tmp_path, django_capture_on_commit_callbacks
):
    settings.MEDIA_ROOT = tmp_path
    att = Attachment.objects.create(
        message=chat["q3"], owner=anna, kind=Attachment.Kind.FILE, size=1
    )
    att.file.save("a.txt", ContentFile(b"x"))
    path = att.file.path
    client.force_login(anna)
    with django_capture_on_commit_callbacks(execute=True):
        assert client.delete(url(chat, chat["a3"])).status_code == 200
    assert not Attachment.objects.exists()
    assert not os.path.exists(path)


def test_permissions(client, chat):
    ben = make_user("ben")
    client.force_login(ben)
    assert client.delete(url(chat, chat["a3"])).status_code == 404  # fremder Chat
    group = UserGroup.objects.create(name="Löschtest")
    ben.groups.add(group)
    share = Share.objects.create(conversation=chat["conv"], group=group, can_write=True)
    assert client.delete(url(chat, chat["a3"])).status_code == 403  # ohne D
    share.can_delete = True
    share.save()
    ConversationView.objects.create(conversation=chat["conv"], user=ben, current_leaf=chat["a3"])
    assert client.delete(url(chat, chat["a3"])).status_code == 200
    view = ConversationView.objects.get(user=ben)
    assert view.current_leaf_id == chat["a2b"].pk  # eigene Ansicht wandert mit


def test_bad_requests(client, anna, chat):
    client.force_login(anna)
    other = Conversation.objects.create(user=anna)
    foreign = msg(other, None, Message.Role.USER, "anderer Chat")
    assert client.delete(url(chat, foreign)).status_code == 400
    assert client.post(url(chat, chat["a3"])).status_code == 405
    assert remaining(chat) >= {"Frage 3"}


def test_button_only_with_delete_right(client, anna, chat):
    client.force_login(anna)
    html = client.get(reverse("chat:conversation", args=[chat["conv"].pk])).content.decode()
    assert "data-delete-message" in html and "data-api-message-delete-template" in html
    ben = make_user("ben")
    group = UserGroup.objects.create(name="Leser")
    ben.groups.add(group)
    Share.objects.create(conversation=chat["conv"], group=group, can_write=True)
    client.force_login(ben)
    html = client.get(reverse("chat:conversation", args=[chat["conv"].pk])).content.decode()
    assert "data-delete-message" not in html
