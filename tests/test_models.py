"""Datenmodell der App chat (M2-01) und Admin-Masken (M2-03)."""

import re

import pytest
from cryptography.fernet import Fernet
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.urls import reverse

from multigpt.accounts.models import UserGroup
from multigpt.chat.models import (
    AIModel,
    Attachment,
    Collection,
    Conversation,
    Document,
    Job,
    McpServer,
    Message,
    Provider,
    Share,
)

SECRET = "sk-live-geheim-9876543210-wxyz"


@pytest.fixture(autouse=True)
def _env(settings, tmp_path):
    settings.FIELD_ENCRYPTION_KEY = Fernet.generate_key().decode()
    settings.MEDIA_ROOT = tmp_path / "media"


@pytest.fixture
def provider(db):
    return Provider.objects.create(name="OpenAI", kind="openai_compat", api_key=SECRET)


@pytest.fixture
def ai_model(provider):
    return AIModel.objects.create(provider=provider, model_id="gpt-4o", display_name="GPT-4o")


@pytest.fixture
def conversation(user, ai_model):
    return Conversation.objects.create(user=user, title="Urlaubsplanung", default_model=ai_model)


@pytest.fixture
def admin_client(client, django_user_model, password):
    admin_user = django_user_model.objects.create_superuser(
        username="jo", password=password, email="jo@example.invalid"
    )
    client.force_login(admin_user)
    return client


# --- Modelle -----------------------------------------------------------------


@pytest.mark.django_db
def test_message_defaults_and_order(conversation, ai_model):
    first = Message.objects.create(conversation=conversation, role="user", content="Hallo")
    second = Message.objects.create(
        conversation=conversation, role="assistant", content="Hi", model=ai_model
    )
    assert first.status == Message.Status.COMPLETE
    assert list(conversation.messages.all()) == [first, second]


@pytest.mark.django_db
def test_aimodel_unique_per_provider(ai_model):
    with pytest.raises(IntegrityError):
        AIModel.objects.create(
            provider=ai_model.provider, model_id="gpt-4o", display_name="doppelt"
        )


@pytest.mark.django_db
def test_deleting_model_keeps_history(conversation, ai_model):
    msg = Message.objects.create(conversation=conversation, role="assistant", model=ai_model)
    ai_model.delete()
    msg.refresh_from_db()
    assert msg.model is None


@pytest.mark.django_db
def test_mcpserver_requires_command_or_url():
    with pytest.raises(IntegrityError), transaction.atomic():
        McpServer.objects.create(name="kaputt", transport="http")
    McpServer.objects.create(name="ok", transport="http", url="http://nas.local:9000/mcp")


@pytest.mark.django_db
def test_job_defaults():
    job = Job.objects.create(kind=Job.Kind.INDEX_DOCUMENT, payload={"document": 1})
    assert job.status == Job.Status.PENDING
    assert job.attempts == 0


# --- Freigaben ---------------------------------------------------------------


@pytest.mark.django_db
def test_share_needs_exactly_one_target(user, conversation):
    group = UserGroup.objects.create(name="Eltern")
    collection = Collection.objects.create(owner=user, name="Rezepte")

    Share.objects.create(conversation=conversation, group=group)
    Share.objects.create(collection=collection, group=group, can_write=True)

    with pytest.raises(IntegrityError), transaction.atomic():
        Share.objects.create(group=group)
    with pytest.raises(IntegrityError), transaction.atomic():
        Share.objects.create(conversation=conversation, collection=collection, group=group)


@pytest.mark.django_db
def test_share_unique_per_group(conversation):
    group = UserGroup.objects.create(name="Eltern")
    Share.objects.create(conversation=conversation, group=group)
    with pytest.raises(IntegrityError), transaction.atomic():
        Share.objects.create(conversation=conversation, group=group, can_write=True)


# --- Uploads -----------------------------------------------------------------


@pytest.mark.django_db
def test_attachment_filename_not_taken_over(user, conversation):
    msg = Message.objects.create(conversation=conversation, role="assistant")
    att = Attachment.objects.create(message=msg, kind="image")
    att.file.save("../../Mein Urlaubsfoto.PNG", ContentFile(b"x"))

    assert re.fullmatch(rf"attachments/{user.pk}/[0-9a-f]{{32}}\.png", att.file.name)
    assert "Urlaub" not in att.file.name


@pytest.mark.django_db
def test_document_filename_not_taken_over(user):
    collection = Collection.objects.create(owner=user, name="Handbücher")
    doc = Document(collection=collection, title="Waschmaschine")
    doc.file.save("anleitung;rm -rf.pdf", ContentFile(b"%PDF"), save=True)

    assert re.fullmatch(rf"documents/{user.pk}/[0-9a-f]{{32}}\.pdf", doc.file.name)
    # Endungen mit ungewöhnlichen Zeichen fallen weg.
    other = Document(collection=collection, title="x")
    other.file.save("datei.ph p", ContentFile(b"x"), save=False)
    assert re.fullmatch(rf"documents/{user.pk}/[0-9a-f]{{32}}", other.file.name)


# --- Admin -------------------------------------------------------------------


@pytest.mark.django_db
def test_admin_shows_only_last_four_chars(admin_client, provider):
    for url in (
        reverse("admin:chat_provider_changelist"),
        reverse("admin:chat_provider_change", args=[provider.pk]),
    ):
        html = admin_client.get(url).content.decode()
        assert "••••wxyz" in html
        assert SECRET not in html
        assert SECRET[:-4] not in html


@pytest.mark.django_db
def test_admin_empty_key_keeps_value_and_new_key_replaces(admin_client, provider):
    url = reverse("admin:chat_provider_change", args=[provider.pk])
    data = {
        "name": "OpenAI",
        "kind": "openai_compat",
        "base_url": "",
        "api_key": "",
        "active": "on",
        "ai_models-TOTAL_FORMS": "0",
        "ai_models-INITIAL_FORMS": "0",
    }
    response = admin_client.post(url, data)
    assert response.status_code == 302, response.content.decode()[:2000]
    provider.refresh_from_db()
    assert provider.api_key == SECRET

    response = admin_client.post(url, {**data, "api_key": "neuer-key-1234"})
    assert response.status_code == 302
    provider.refresh_from_db()
    assert provider.api_key == "neuer-key-1234"

    response = admin_client.post(url, {**data, "clear_api_key": "on"})
    assert response.status_code == 302
    provider.refresh_from_db()
    assert provider.api_key == ""


@pytest.mark.django_db
def test_admin_mcp_credentials_masked(admin_client):
    server = McpServer.objects.create(
        name="Kalender", transport="http", url="http://nas.local/mcp", credentials=SECRET
    )
    html = admin_client.get(reverse("admin:chat_mcpserver_change", args=[server.pk])).content
    html = html.decode()
    assert "••••wxyz" in html
    assert SECRET not in html


@pytest.mark.django_db
def test_admin_does_not_show_chat_contents(admin_client, conversation):
    Message.objects.create(conversation=conversation, role="user", content="Streng geheim")
    msg = Message.objects.get()
    pages = [
        reverse("admin:chat_conversation_changelist"),
        reverse("admin:chat_conversation_change", args=[conversation.pk]),
        reverse("admin:chat_message_changelist"),
        reverse("admin:chat_message_change", args=[msg.pk]),
    ]
    for url in pages:
        response = admin_client.get(url)
        assert response.status_code == 200, url
        html = response.content.decode()
        assert "Streng geheim" not in html
        assert "Urlaubsplanung" not in html


@pytest.mark.django_db
@pytest.mark.parametrize(
    "name",
    [
        "provider",
        "aimodel",
        "mcpserver",
        "conversation",
        "message",
        "preset",
        "attachment",
        "collection",
        "share",
        "toolcall",
        "document",
        "job",
        "sourceref",
    ],
)
def test_admin_changelists_render(admin_client, name):
    response = admin_client.get(reverse(f"admin:chat_{name}_changelist"))
    assert response.status_code == 200
