"""Bearbeiten und Versionen in der Chatoberfläche: Stift, Versionsumschalter
„‹ i/n ›“, Kopierknopf und das Verlaufsfragment GET /c/<pk>/messages/."""

import re

import pytest
from django.urls import reverse

from multigpt.accounts.models import Role, User, UserGroup
from multigpt.chat.models import AIModel, Conversation, Message, Provider, Share

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


@pytest.fixture
def ben():
    return make_user("ben")


@pytest.fixture
def anna_client(client, anna):
    client.force_login(anna)
    return client


def msg(conv, parent, role, content, **fields):
    return Message.objects.create(
        conversation=conv, parent=parent, role=role, content=content, **fields
    )


@pytest.fixture
def tree(anna, ai_model):
    """Frage in zwei Fassungen (u1, u2); u1 hat zwei Antworten (a1, a2).
    Angezeigt: u1 -> a2."""
    conv = Conversation.objects.create(user=anna, title="Versionen")
    u1 = msg(conv, None, Message.Role.USER, "Erste Fassung <b>")
    a1 = msg(conv, u1, Message.Role.ASSISTANT, "Antwort A", model=ai_model)
    a2 = msg(conv, u1, Message.Role.ASSISTANT, "Antwort B", model=ai_model)
    u2 = msg(conv, None, Message.Role.USER, "Zweite Fassung")
    a3 = msg(conv, u2, Message.Role.ASSISTANT, "Antwort C", model=ai_model)
    conv.current_leaf = a2
    conv.save()
    return {"conv": conv, "u1": u1, "a1": a1, "a2": a2, "u2": u2, "a3": a3}


def article(html, pk):
    """HTML einer Nachricht (article bis zum schließenden Tag)."""
    match = re.search(rf'<article[^>]*data-message-id="{pk}".*?</article>', html, re.S)
    assert match, f"Nachricht {pk} fehlt"
    return match.group(0)


def page(client, conv):
    response = client.get(reverse("chat:conversation", args=[conv.pk]))
    assert response.status_code == 200
    return response.content.decode()


def test_path_and_version_switcher(anna_client, tree):
    html = page(anna_client, tree["conv"])
    assert "Antwort B" in html
    assert "Antwort A" not in html and "Antwort C" not in html and "Zweite Fassung" not in html

    user = article(html, tree["u1"].pk)
    assert "Version 1 von 2" in user
    assert ">1/2<" in user
    assert 'aria-label="Vorherige Version"' in user
    assert re.search(r'data-version-target=""\s+aria-label="Vorherige Version"[^>]*disabled', user)
    assert f'data-version-target="{tree["u2"].pk}"' in user

    answer = article(html, tree["a2"].pk)
    assert "Version 2 von 2" in answer
    assert f'data-version-target="{tree["a1"].pk}"' in answer
    assert re.search(r'data-version-target=""\s+aria-label="Nächste Version"[^>]*disabled', answer)


def test_no_switcher_without_siblings(anna_client, anna, ai_model):
    conv = Conversation.objects.create(user=anna)
    u = msg(conv, None, Message.Role.USER, "Hallo")
    a = msg(conv, u, Message.Role.ASSISTANT, "Hi", model=ai_model)
    conv.current_leaf = a
    conv.save()
    html = page(anna_client, conv)
    assert "message-versions" not in html
    assert "Version 1 von 1" not in html
    # Kopieren an beiden, Stift nur an der eigenen Nachricht, Neu erzeugen an der Antwort.
    assert 'aria-label="Nachricht kopieren"' in article(html, u.pk)
    assert 'aria-label="Antwort kopieren"' in article(html, a.pk)
    assert "data-edit-message" in article(html, u.pk)
    assert "data-edit-message" not in article(html, a.pk)
    assert "data-regenerate-message" in article(html, a.pk)
    assert "data-regenerate-message" not in article(html, u.pk)


def test_reader_sees_versions_but_no_edit(client, tree, ben):
    group = UserGroup.objects.create(name="Versionen-Leser")
    ben.groups.add(group)
    Share.objects.create(conversation=tree["conv"], group=group, can_write=False)
    client.force_login(ben)
    html = page(client, tree["conv"])
    assert "Version 1 von 2" in article(html, tree["u1"].pk)
    # Geteilte Chats: Umschalten ändert nur die eigene Ansicht, Lesen genügt.
    assert "data-version-target" in html
    assert "data-edit-message" not in html
    assert "data-regenerate-message" not in html
    assert "data-copy-message" in html


def test_shared_writer_can_edit(client, tree, ben):
    group = UserGroup.objects.create(name="Versionen-Leser")
    ben.groups.add(group)
    Share.objects.create(conversation=tree["conv"], group=group, can_write=True, can_update=True)
    client.force_login(ben)
    html = page(client, tree["conv"])
    assert "data-edit-message" in article(html, tree["u1"].pk)
    assert f'data-version-target="{tree["u2"].pk}"' in html


def test_fragment_view_renders_path(anna_client, tree):
    url = reverse("chat:conversation_messages", args=[tree["conv"].pk])
    assert url == f"/c/{tree['conv'].pk}/messages/"
    response = anna_client.get(url)
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/html")
    assert "no-store" in response["Cache-Control"]
    html = response.content.decode()
    assert "<html" not in html and "<body" not in html
    assert html.count("<article") == 2
    assert html.index("Erste Fassung") < html.index("Antwort B")
    assert "Erste Fassung &lt;b&gt;" in html
    assert "Version 2 von 2" in article(html, tree["a2"].pk)
    assert "data-edit-message" in html
    assert "<script" not in html
    assert "{#" not in html

    # Nach dem Umschalten auf den anderen Zweig liefert das Fragment diesen.
    conv = tree["conv"]
    conv.current_leaf = tree["a3"]
    conv.save()
    html = anna_client.get(url).content.decode()
    assert "Zweite Fassung" in html and "Antwort C" in html
    assert "Erste Fassung" not in html
    assert "Version 2 von 2" in article(html, tree["u2"].pk)


def test_fragment_view_permissions(client, tree, ben):
    url = reverse("chat:conversation_messages", args=[tree["conv"].pk])
    assert client.get(url).status_code == 302  # nicht angemeldet
    client.force_login(ben)
    response = client.get(url)
    assert response.status_code == 404
    assert "Erste Fassung" not in response.content.decode()
    assert client.post(url).status_code == 405

    group = UserGroup.objects.create(name="Versionen-Leser")
    ben.groups.add(group)
    Share.objects.create(conversation=tree["conv"], group=group, can_write=False)
    html = client.get(url).content.decode()
    assert "Antwort B" in html
    assert "data-edit-message" not in html


def test_page_has_urls_and_no_inline_js(anna_client, tree):
    html = page(anna_client, tree["conv"])
    conv_pk = tree["conv"].pk
    fragment = reverse("chat:conversation_messages", args=[0])
    assert f'data-messages-fragment-template="{fragment}"' in html
    assert f'data-api-branch-template="{reverse("chat:api_branch", args=[0])}"' in html
    assert reverse("chat:conversation_messages", args=[conv_pk]) != fragment
    assert "<script>" not in html
    assert "{#" not in html and "#}" not in html  # keine durchgerutschten Template-Kommentare
    assert not re.search(r"\son[a-z]+=", html)
    assert "javascript:" not in html
