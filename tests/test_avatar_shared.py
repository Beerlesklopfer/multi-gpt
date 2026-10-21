"""Profilbild an Nachrichten in geteilten Chats und „Profilbilder entfernen“ im Admin (LOP C34)."""

import pytest
from django.urls import reverse
from django.utils import timezone

from multigpt.accounts.models import Avatar, Role, User
from multigpt.chat.models import Conversation, Message, Share

pytestmark = pytest.mark.django_db

PASSWORD = "Geheim-Test-1234"


def make_user(username, role_key="adult", **fields):
    return User.objects.create_user(
        username, password=PASSWORD, role=Role.objects.get(key=role_key), **fields
    )


def give_avatar(user):
    Avatar.objects.update_or_create(user=user, defaults={"data": b"RIFFxxxxWEBP"})
    user.avatar_updated = timezone.now()
    user.save(update_fields=["avatar_updated"])


@pytest.fixture
def shared_chat():
    anna = make_user("anna")
    ben = make_user("ben")
    give_avatar(ben)
    conv = Conversation.objects.create(user=anna, title="Gemeinsam")
    Share.objects.create(conversation=conv, user=ben, can_write=True)
    question = Message.objects.create(
        conversation=conv, role=Message.Role.USER, content="Frage von Ben", author=ben
    )
    conv.current_leaf = question
    conv.save()
    return {"anna": anna, "ben": ben, "conv": conv}


def test_message_of_another_person_shows_the_profile_picture(client, shared_chat):
    client.force_login(shared_chat["anna"])
    page = client.get(reverse("chat:conversation", args=[shared_chat["conv"].pk])).content.decode()
    ben = shared_chat["ben"]
    assert 'class="chat-message-avatar"' in page
    assert reverse("avatar_image", args=[ben.pk]) in page


def test_person_without_picture_gets_initials(client, shared_chat):
    shared_chat["ben"].avatar_updated = None
    shared_chat["ben"].save(update_fields=["avatar_updated"])
    client.force_login(shared_chat["anna"])
    page = client.get(reverse("chat:conversation", args=[shared_chat["conv"].pk])).content.decode()
    assert 'class="chat-message-avatar"' in page and "avatar-initials" in page


def test_unshared_chat_shows_no_avatar_at_messages(client):
    anna = make_user("anna")
    give_avatar(anna)
    conv = Conversation.objects.create(user=anna, title="Allein")
    question = Message.objects.create(
        conversation=conv, role=Message.Role.USER, content="Frage", author=anna
    )
    conv.current_leaf = question
    conv.save()
    client.force_login(anna)
    page = client.get(reverse("chat:conversation", args=[conv.pk])).content.decode()
    assert "chat-message-avatar" not in page


def test_admin_can_remove_foreign_profile_pictures(client):
    admin = make_user("verwalter", "admin", is_staff=True, is_superuser=True)
    ben = make_user("ben")
    give_avatar(ben)
    client.force_login(admin)
    response = client.post(
        reverse("admin:accounts_user_changelist"),
        {"action": "remove_avatars", "_selected_action": [ben.pk]},
        follow=True,
    )
    assert response.status_code == 200
    ben.refresh_from_db()
    assert ben.avatar_updated is None and not Avatar.objects.filter(user=ben).exists()
    assert "1 Profilbild(er) entfernt." in response.content.decode()
