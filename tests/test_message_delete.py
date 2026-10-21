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


def version_url(chat_, message):
    return url(chat_, message) + "?scope=version"


def test_delete_version_keeps_the_question_and_the_other_versions(client, anna, chat):
    client.force_login(anna)
    response = client.delete(version_url(chat, chat["a2b"]))
    assert response.status_code == 200
    # Nur a2b und was an ihr hängt (Frage 3, Antwort 3) sind weg; Frage 2 und Antwort 2 bleiben.
    assert remaining(chat) == {"Frage 1", "Antwort 1", "Frage 2", "Antwort 2"}
    assert [m["content"] for m in response.json()] == [
        "Frage 1",
        "Antwort 1",
        "Frage 2",
        "Antwort 2",
    ]
    chat["conv"].refresh_from_db()
    assert chat["conv"].current_leaf_id == chat["a2"].pk  # die übrige Version wird angezeigt


def test_delete_the_only_version_leaves_the_question_unanswered(client, anna, chat):
    client.force_login(anna)
    response = client.delete(version_url(chat, chat["a1"]))
    assert response.status_code == 200
    assert remaining(chat) == {"Frage 1"}  # alles ab Antwort 1 hing an ihr
    chat["conv"].refresh_from_db()
    assert chat["conv"].current_leaf_id == chat["q1"].pk


def test_delete_version_of_a_side_branch_keeps_the_shown_path(client, anna, chat):
    client.force_login(anna)
    response = client.delete(version_url(chat, chat["a2"]))
    assert response.status_code == 200
    assert "Antwort 2" not in remaining(chat) and "Antwort 2b" in remaining(chat)
    chat["conv"].refresh_from_db()
    assert chat["conv"].current_leaf_id == chat["a3"].pk  # angezeigter Pfad unberührt


def test_delete_version_removes_only_its_files(
    client, anna, chat, settings, tmp_path, django_capture_on_commit_callbacks
):
    settings.MEDIA_ROOT = tmp_path / "media"
    keep = Attachment(message=chat["a2"], kind=Attachment.Kind.IMAGE, mime_type="image/png")
    keep.file.save("keep.png", ContentFile(b"png"), save=True)
    gone = Attachment(message=chat["a2b"], kind=Attachment.Kind.IMAGE, mime_type="image/png")
    gone.file.save("gone.png", ContentFile(b"png"), save=True)
    gone_path, keep_path = gone.file.path, keep.file.path
    client.force_login(anna)
    with django_capture_on_commit_callbacks(execute=True):
        assert client.delete(version_url(chat, chat["a2b"])).status_code == 200
    assert not Attachment.objects.filter(pk=gone.pk).exists()
    assert Attachment.objects.filter(pk=keep.pk).exists()
    assert os.path.exists(keep_path)
    assert not os.path.exists(gone_path)


def test_delete_version_needs_an_answer(client, anna, chat):
    client.force_login(anna)
    response = client.delete(version_url(chat, chat["q2"]))
    assert response.status_code == 400
    assert "Frage 2" in remaining(chat)


def test_delete_version_needs_the_delete_right(client, chat):
    stranger = make_user("fremd")
    client.force_login(stranger)
    response = client.delete(version_url(chat, chat["a2b"]))
    assert response.status_code in (403, 404)
    assert "Antwort 2b" in remaining(chat)


def test_chat_page_offers_deleting_a_single_version(client, anna, chat):
    client.force_login(anna)
    page = client.get(reverse("chat:conversation", args=[chat["conv"].pk])).content.decode()
    assert "data-delete-version" in page and "Nur diese Version löschen" in page


def test_user_message_has_a_delete_button_for_its_own_version(client, anna, chat):
    client.force_login(anna)
    page = client.get(reverse("chat:conversation", args=[chat["conv"].pk])).content.decode()
    assert "Diese Version der Frage mit Antworten und Verlauf löschen" in page


def test_deleting_a_question_version_keeps_the_other_versions(client, anna, chat, ai_model):
    other = msg(chat["conv"], chat["a1"], Message.Role.USER, "Frage 2b")
    client.force_login(anna)
    response = client.delete(url(chat, chat["q2"]))
    assert response.status_code == 200
    assert "Frage 2b" in remaining(chat)  # die andere Version der Frage bleibt
    assert not {"Frage 2", "Antwort 2", "Antwort 2b", "Frage 3", "Antwort 3"} & remaining(chat)
    assert other.pk


def test_version_counters_shrink_after_deleting_a_version(client, anna, chat):
    """Nach dem Löschen zählt der Umschalter „i/n“ nur noch die übrigen Versionen."""
    client.force_login(anna)
    page = client.get(reverse("chat:conversation", args=[chat["conv"].pk])).content.decode()
    assert "1/1" not in page  # Vorher: Frage 3 hängt an Antwort 2b, Antwort 2/2b haben 2 Versionen
    response = client.delete(version_url(chat, chat["a2b"]))
    assert response.status_code == 200
    counts = {m["content"]: m["sibling_count"] for m in response.json()}
    assert counts["Antwort 2"] == 1  # nur noch eine Version
    page = client.get(reverse("chat:conversation", args=[chat["conv"].pk])).content.decode()
    assert "1/2" not in page and "2/2" not in page


def test_question_version_counters_shrink_after_deleting_a_question(client, anna, chat):
    other = msg(chat["conv"], chat["a1"], Message.Role.USER, "Frage 2b")
    chat["conv"].current_leaf = chat["a3"]
    chat["conv"].save()
    client.force_login(anna)
    response = client.delete(url(chat, chat["q2"]))
    assert response.status_code == 200
    counts = {m["content"]: m["sibling_count"] for m in response.json()}
    assert counts["Frage 2b"] == 1
    assert other.pk


def others_url(chat_, message):
    return url(chat_, message) + "?scope=others"


def test_delete_other_versions_of_an_answer_keeps_only_this_one(client, anna, chat):
    client.force_login(anna)
    response = client.delete(others_url(chat, chat["a2b"]))
    assert response.status_code == 200
    # a2 (die andere Version) ist weg; a2b mit Frage 3 und Antwort 3 bleibt.
    assert "Antwort 2" not in remaining(chat) and {
        "Antwort 2b",
        "Frage 3",
        "Antwort 3",
    } <= remaining(chat)
    counts = {m["content"]: m["sibling_count"] for m in response.json()}
    assert counts["Antwort 2b"] == 1  # Zähler steht auf 1/1
    chat["conv"].refresh_from_db()
    assert chat["conv"].current_leaf_id == chat["a3"].pk


def test_delete_other_versions_removes_their_subtrees_and_files(
    client, anna, chat, settings, tmp_path, django_capture_on_commit_callbacks
):
    settings.MEDIA_ROOT = tmp_path / "media"
    gone = Attachment(message=chat["a2"], kind=Attachment.Kind.IMAGE, mime_type="image/png")
    gone.file.save("gone.png", ContentFile(b"png"), save=True)
    path = gone.file.path
    client.force_login(anna)
    with django_capture_on_commit_callbacks(execute=True):
        assert client.delete(others_url(chat, chat["a2b"])).status_code == 200
    assert not Attachment.objects.filter(pk=gone.pk).exists() and not os.path.exists(path)


def test_delete_other_versions_of_a_question(client, anna, chat):
    other = msg(chat["conv"], chat["a1"], Message.Role.USER, "Frage 2b")
    client.force_login(anna)
    response = client.delete(others_url(chat, chat["q2"]))
    assert response.status_code == 200
    assert "Frage 2b" not in remaining(chat) and "Frage 2" in remaining(chat)
    assert not Message.objects.filter(pk=other.pk).exists()


def test_delete_other_versions_needs_other_versions(client, anna, chat):
    client.force_login(anna)
    response = client.delete(others_url(chat, chat["q3"]))
    assert response.status_code == 400 and "Frage 3" in remaining(chat)


def test_chat_page_offers_deleting_the_other_versions(client, anna, chat):
    client.force_login(anna)
    page = client.get(reverse("chat:conversation", args=[chat["conv"].pk])).content.decode()
    assert "data-delete-others" in page and "Alle anderen Versionen löschen" in page
