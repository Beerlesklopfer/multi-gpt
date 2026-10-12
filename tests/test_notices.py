"""Mitteilungen an den Besitzer geteilter Chats (A15, chat/notices.py).

Empfänger mit Recht D löscht/archiviert den Chat oder löscht eine Antwort samt
Folgeverlauf -> Mitteilung an den Besitzer; handelt der Besitzer selbst, keine.
Dazu Menü (Zahl ungelesener), Seite, „gelesen“ und Aufräumen im Worker.
"""

import json
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from multigpt.accounts.models import Role, User
from multigpt.chat import notices
from multigpt.chat.models import Conversation, Message, Notice, Share

pytestmark = pytest.mark.django_db

PASSWORD = "Geheim-Test-1234"


def make_user(username, display_name=""):
    return User.objects.create_user(
        username,
        password=PASSWORD,
        role=Role.objects.get(key="adult"),
        display_name=display_name,
    )


@pytest.fixture
def anna():
    return make_user("anna_owner", "Anna")


@pytest.fixture
def ben():
    return make_user("ben_empf", "Ben")


@pytest.fixture
def conv(anna, ben):
    conv = Conversation.objects.create(user=anna, title="Urlaubsplanung")
    q = Message.objects.create(conversation=conv, role=Message.Role.USER, content="Geheime Frage")
    a = Message.objects.create(
        conversation=conv, parent=q, role=Message.Role.ASSISTANT, content="Geheime Antwort"
    )
    conv.current_leaf = a
    conv.save()
    Share.objects.create(conversation=conv, user=ben, can_delete=True)
    conv.answer = a
    return conv


def detail(conv):
    return reverse("chat:api_conversation_detail", args=[conv.pk])


def patch(client, url, data):
    return client.patch(url, data=json.dumps(data), content_type="application/json")


def test_recipient_deletes_chat_notifies_owner_without_link(client, anna, ben, conv):
    client.force_login(ben)
    assert client.delete(detail(conv)).status_code == 200
    notice = Notice.objects.get()
    assert notice.recipient == anna
    assert notice.conversation is None
    assert notice.text == "Ben hat deinen geteilten Chat „Urlaubsplanung“ gelöscht."
    assert "Geheim" not in notice.text


def test_recipient_archives_and_unarchives(client, anna, ben, conv):
    client.force_login(ben)
    assert patch(client, detail(conv), {"archived": True}).status_code == 200
    assert patch(client, detail(conv), {"archived": True}).status_code == 200  # unverändert
    assert patch(client, detail(conv), {"archived": False}).status_code == 200
    texts = list(Notice.objects.order_by("pk").values_list("text", "conversation_id"))
    assert texts == [
        ("Ben hat deinen geteilten Chat „Urlaubsplanung“ archiviert.", conv.pk),
        ("Ben hat deinen geteilten Chat „Urlaubsplanung“ aus dem Archiv geholt.", conv.pk),
    ]


def test_recipient_deletes_exchange(client, anna, ben, conv):
    client.force_login(ben)
    url = reverse("chat:api_message_delete", args=[conv.pk, conv.answer.pk])
    assert client.delete(url).status_code == 200
    notice = Notice.objects.get()
    assert notice.recipient == anna and notice.conversation_id == conv.pk
    assert "eine Antwort samt Frage und Folgeverlauf gelöscht" in notice.text
    assert "Geheim" not in notice.text


def test_owner_actions_and_failures_create_nothing(client, anna, ben, conv):
    client.force_login(ben)
    Share.objects.filter(user=ben).update(can_delete=False)
    assert client.delete(detail(conv)).status_code == 403
    client.force_login(anna)
    assert patch(client, detail(conv), {"archived": True}).status_code == 200
    url = reverse("chat:api_message_delete", args=[conv.pk, conv.answer.pk])
    assert client.delete(url).status_code == 200
    assert client.delete(detail(conv)).status_code == 200
    assert not Notice.objects.exists()


def test_menu_badge_page_and_mark_read(client, anna, ben, conv):
    first = notices.notify_owner(conv, ben, notices.ARCHIVED)
    second = notices.notify_owner(conv, ben, notices.EXCHANGE_DELETED)
    other = Notice.objects.create(recipient=ben, text="Für Ben")
    client.force_login(anna)
    html = client.get(reverse("chat:index")).content.decode()
    assert "2 ungelesene Mitteilungen" in html
    assert reverse("chat:notices") in html and 'class="notice-badge' in html

    page = client.get(reverse("chat:notices")).content.decode()
    assert "Urlaubsplanung" in page and "Für Ben" not in page
    assert "{#" not in page

    # Fremde Mitteilung: 404, bleibt ungelesen.
    assert client.post(reverse("chat:notice_read", args=[other.pk])).status_code == 404
    assert client.get(reverse("chat:notice_read", args=[first.pk])).status_code == 405
    response = client.post(reverse("chat:notice_read", args=[first.pk]))
    assert response.status_code == 302
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.read is not None and second.read is None
    html = client.get(reverse("chat:index")).content.decode()
    assert '1 ungelesene Mitteilung"' in html

    client.post(reverse("chat:notice_read_all"))
    assert notices.unread_count(anna) == 0
    other.refresh_from_db()
    assert other.read is None
    html = client.get(reverse("chat:index")).content.decode()
    assert "ungelesene Mitteilung" not in html and 'class="notice-badge' not in html


def test_page_requires_login(client):
    response = client.get(reverse("chat:notices"))
    assert response.status_code == 302 and "login" in response["Location"]


def test_cleanup_removes_old_read_only(anna):
    now = timezone.now()
    old_read = Notice.objects.create(recipient=anna, text="alt", read=now - timedelta(days=91))
    fresh_read = Notice.objects.create(recipient=anna, text="neu", read=now - timedelta(days=5))
    unread = Notice.objects.create(recipient=anna, text="offen")
    Notice.objects.filter(pk=unread.pk).update(created=now - timedelta(days=400))
    assert notices.cleanup(now) == 1
    remaining = set(Notice.objects.values_list("pk", flat=True))
    assert remaining == {fresh_read.pk, unread.pk}
    assert old_read.pk not in remaining
