"""Zentrale Werkzeugablage: Privatheit, Dauerhaftigkeit und automatische Übernahme."""

import pytest
from django.core.files.base import ContentFile
from django.urls import reverse

from multigpt.chat import services, tooling
from multigpt.chat.mcp.results import ToolFile, ToolResult
from multigpt.chat.models import Attachment, Conversation, ToolCall
from multigpt.scratchpad.models import ScratchFile
from multigpt.scratchpad.services import store_attachment
from tests.test_tool_loop import make_user

pytestmark = pytest.mark.django_db
SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50">'
    b'<rect width="100" height="50"/></svg>'
)


@pytest.fixture
def source(client, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    owner = make_user("adult", "scratch-owner")
    client.force_login(owner)
    conversation = Conversation.objects.create(user=owner, title="Werkzeug")
    message = services.append_message(conversation, role="assistant", author=owner)
    call = ToolCall.objects.create(message=message, tool="fits_image", status="ok")
    return owner, conversation, message, call


def test_tool_files_automatically_enter_scratchpad(client, source):
    owner, _, message, call = source
    result = ToolResult(text="Bild", images=[ToolFile(SVG, "image/svg+xml", "passung.svg")])
    ids = tooling._store_files(message, call, result)
    item = ScratchFile.objects.get()
    assert item.owner == owner and item.source_attachment_id == ids[0]
    assert item.mime_type == "image/svg+xml" and (item.width, item.height) == (100, 50)
    assert "keine 15-Minuten-Frist" in tooling.scratchpad_note(ids)
    assert item.url in tooling.scratchpad_note(ids)
    page = client.get(reverse("scratchpad:index"))
    assert page.status_code == 200 and item.url.encode() in page.content
    response = client.get(item.url)
    assert response.status_code == 200 and response["Content-Type"] == "image/svg+xml"
    assert client.get(item.url + "?download=1")["Content-Disposition"].startswith("attachment")
    other = make_user("admin", "scratch-other")
    client.force_login(other)
    assert client.get(item.url).status_code == 404
    assert b"passung.svg" not in client.get(reverse("scratchpad:index")).content


def test_scratchpad_survives_chat_and_attachment_deletion(source):
    _, conversation, message, call = source
    attachment = Attachment(
        message=message, tool_call=call, kind="image", mime_type="image/svg+xml"
    )
    attachment.file.save("source.svg", ContentFile(SVG), save=True)
    item = store_attachment(attachment)
    assert store_attachment(attachment).pk == item.pk
    original_path = attachment.file.name
    conversation.delete()
    item.refresh_from_db()
    assert item.source_attachment_id is None
    assert item.file.name != original_path
    with item.file.open("rb") as handle:
        assert b"<svg" in handle.read()


def test_shared_chat_artifact_belongs_to_answer_author(source):
    owner, _, message, call = source
    author = make_user("adult", "scratch-author")
    message.author = author
    message.save()
    attachment = Attachment(
        message=message, tool_call=call, kind="image", mime_type="image/svg+xml"
    )
    attachment.file.save("source.svg", ContentFile(SVG), save=True)
    assert store_attachment(attachment).owner == author
    assert not ScratchFile.objects.filter(owner=owner).exists()


def test_delete_requires_confirmation_and_removes_only_scratchpad_copy(
    client, source, django_capture_on_commit_callbacks
):
    _, _, message, call = source
    ids = tooling._store_files(
        message, call, ToolResult(text="SVG", images=[ToolFile(SVG, "image/svg+xml")])
    )
    item = ScratchFile.objects.get()
    storage, name = item.file.storage, item.file.name
    url = reverse("scratchpad:delete", args=[item.pk])
    assert url.encode() in client.get(reverse("scratchpad:index")).content
    assert client.get(url).status_code == 200
    assert client.post(url, {}).status_code == 400
    assert ScratchFile.objects.filter(pk=item.pk).exists() and storage.exists(name)
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(url, {"confirm": "yes"})
    assert response.status_code == 302
    assert not ScratchFile.objects.filter(pk=item.pk).exists()
    assert not storage.exists(name)
    assert Attachment.objects.filter(pk=ids[0]).exists()
    assert client.get(item.url).status_code == 404


def test_delete_is_private_and_csrf_protected(client, source):
    from django.test import Client

    owner, _, message, call = source
    tooling._store_files(
        message, call, ToolResult(images=[ToolFile(SVG, "image/svg+xml")], text="")
    )
    item = ScratchFile.objects.get()
    url = reverse("scratchpad:delete", args=[item.pk])
    protected = Client(enforce_csrf_checks=True)
    protected.force_login(owner)
    assert protected.post(url, {"confirm": "yes"}).status_code == 403
    client.force_login(make_user("admin", "delete-other"))
    assert client.get(url).status_code == 404
    assert client.post(url, {"confirm": "yes"}).status_code == 404
    assert ScratchFile.objects.filter(pk=item.pk).exists()
