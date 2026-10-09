"""Seiten der Chatoberfläche (M3-05): Laden, Rechte, Chatliste, Verlauf, Escaping."""

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


def test_anonymous_redirected_to_login(client, anna):
    conv = Conversation.objects.create(user=anna, title="Privat")
    for url in (reverse("chat:index"), reverse("chat:conversation", args=[conv.pk])):
        response = client.get(url)
        assert response.status_code == 302
        assert response.url.startswith(reverse("login"))


def test_index_loads_with_script_and_api_urls(anna_client):
    response = anna_client.get(reverse("chat:index"))
    assert response.status_code == 200
    html = response.content.decode()
    assert "chat/chat.js" in html
    assert f'data-api-models="{reverse("chat:api_models")}"' in html
    assert f'data-api-conversations="{reverse("chat:api_conversations")}"' in html
    assert 'id="chat-form"' in html
    assert 'id="new-chat-button"' in html
    # Kein Inline-JS, keine externen Ressourcen.
    assert "<script>" not in html
    assert "onclick" not in html
    assert "https://" not in html and "http://" not in html


def test_conversation_page_loads(anna_client, anna):
    conv = Conversation.objects.create(user=anna, title="Urlaub planen")
    response = anna_client.get(reverse("chat:conversation", args=[conv.pk]))
    assert response.status_code == 200
    html = response.content.decode()
    assert f'data-conversation-id="{conv.pk}"' in html
    assert "<title>Urlaub planen – MultiGPT</title>" in html
    assert 'aria-current="page"' in html


def test_foreign_conversation_is_404(anna_client, ben):
    conv = Conversation.objects.create(user=ben, title="Geheim von Ben")
    response = anna_client.get(reverse("chat:conversation", args=[conv.pk]))
    assert response.status_code == 404
    assert "Geheim von Ben" not in response.content.decode()


def test_missing_conversation_is_404(anna_client):
    assert anna_client.get(reverse("chat:conversation", args=[999999])).status_code == 404


def test_superuser_cannot_read_foreign_conversation(client, anna):
    boss = User.objects.create_superuser("boss", password=PASSWORD)
    conv = Conversation.objects.create(user=anna, title="Privat")
    client.force_login(boss)
    assert client.get(reverse("chat:conversation", args=[conv.pk])).status_code == 404


def test_shared_conversation_readable_but_read_only(client, anna, ben):
    group = UserGroup.objects.create(name="Eltern")
    ben.groups.add(group)
    conv = Conversation.objects.create(user=anna, title="Geteilt")
    Message.objects.create(conversation=conv, role=Message.Role.USER, content="Hallo Familie")
    Share.objects.create(conversation=conv, group=group, can_write=False)
    client.force_login(ben)
    response = client.get(reverse("chat:conversation", args=[conv.pk]))
    assert response.status_code == 200
    html = response.content.decode()
    assert "Hallo Familie" in html
    assert "nur lesen" in html
    assert 'id="chat-form"' not in html
    # Fremder Chat erscheint nicht in Bens eigener Liste.
    assert "Geteilt</a>" not in html


def test_sidebar_lists_only_own_unarchived_newest_first(anna_client, anna, ben):
    old = Conversation.objects.create(user=anna, title="Alter Chat")
    new = Conversation.objects.create(user=anna, title="Neuer Chat von Anna")
    Conversation.objects.create(user=anna, title="Archivierter Chat", archived=True)
    Conversation.objects.create(user=ben, title="Bens Chat")
    # "updated" bestimmt die Reihenfolge.
    Conversation.objects.filter(pk=old.pk).update(updated=new.updated.replace(year=2000))

    html = anna_client.get(reverse("chat:index")).content.decode()
    assert "Alter Chat" in html
    assert "Neuer Chat von Anna" in html
    assert "Archivierter Chat" not in html
    assert "Bens Chat" not in html
    assert html.index("Neuer Chat von Anna") < html.index("Alter Chat")


def test_untitled_conversation_has_placeholder_title(anna_client, anna):
    conv = Conversation.objects.create(user=anna)
    html = anna_client.get(reverse("chat:index")).content.decode()
    assert reverse("chat:conversation", args=[conv.pk]) in html
    assert ">Neuer Chat</a>" in html


def test_history_rendered_server_side_and_escaped(anna_client, anna, ai_model):
    conv = Conversation.objects.create(user=anna, title="<b>Titel</b>")
    Message.objects.create(
        conversation=conv, role=Message.Role.USER, content="<script>alert(1)</script>"
    )
    Message.objects.create(
        conversation=conv,
        role=Message.Role.ASSISTANT,
        model=ai_model,
        content='Antwort mit <img src=x onerror="alert(2)">',
    )
    Message.objects.create(conversation=conv, role=Message.Role.SYSTEM, content="SYSTEMTEXT")
    html = anna_client.get(reverse("chat:conversation", args=[conv.pk])).content.decode()

    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)" not in html
    assert "&lt;img src=x onerror=&quot;alert(2)&quot;&gt;" in html
    assert "<img" not in html
    assert "&lt;b&gt;Titel&lt;/b&gt;" in html
    assert "<b>Titel</b>" not in html
    assert "Modell Eins" in html
    assert "SYSTEMTEXT" not in html
    assert html.index("alert(1)") < html.index("alert(2)")


def test_message_status_shown(anna_client, anna, ai_model):
    conv = Conversation.objects.create(user=anna)
    Message.objects.create(
        conversation=conv, role=Message.Role.ASSISTANT, content="Halb", status="aborted"
    )
    Message.objects.create(
        conversation=conv,
        role=Message.Role.ASSISTANT,
        content="",
        status="error",
        error="Anbieter nicht erreichbar",
    )
    html = anna_client.get(reverse("chat:conversation", args=[conv.pk])).content.decode()
    assert "Abgebrochen" in html
    assert "Fehler: Anbieter nicht erreichbar" in html


def test_empty_state_without_models(client):
    guest = make_user("gast", role_key="guest")
    client.force_login(guest)
    html = client.get(reverse("chat:index")).content.decode()
    assert "Keine Modelle freigegeben" in html
    assert "Verwalter" in html
    assert 'id="chat-form"' not in html


def test_inactive_model_counts_as_no_model(client, ai_model):
    ai_model.active = False
    ai_model.save()
    client.force_login(make_user("carla"))
    html = client.get(reverse("chat:index")).content.decode()
    assert "Keine Modelle freigegeben" in html


def test_with_model_shows_composer(anna_client):
    html = anna_client.get(reverse("chat:index")).content.decode()
    assert "Keine Modelle freigegeben" not in html
    assert 'id="message-input"' in html
    assert 'id="model-select"' in html
