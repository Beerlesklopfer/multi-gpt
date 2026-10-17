"""Werkzeugbilder direkt an der Antwort, SVGs sicher auch bei alten Metadaten."""

import pytest
from django.core.files.base import ContentFile
from django.urls import reverse

from multigpt.chat import services, tooling
from multigpt.chat.mcp.results import ToolFile
from multigpt.chat.models import Attachment, Conversation, ToolCall
from tests.test_tool_loop import make_user

pytestmark = pytest.mark.django_db

SVG = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 150">
<script>alert(1)</script><foreignObject>unsafe</foreignObject>
<image href="https://invalid.example/x.png"/>
<rect width="400" height="150" fill="blue"/></svg>"""


@pytest.fixture
def message(client, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    user = make_user("adult", "svg-owner")
    client.force_login(user)
    conversation = Conversation.objects.create(user=user, title="SVG")
    return services.append_message(conversation, role="assistant", content="Die Zeichnung.")


def test_mcp_svg_saved_with_metadata_and_displayed_in_message(client, message):
    call = ToolCall.objects.create(message=message, tool="fits_image", status="ok")
    attachment = tooling._attachment(
        message, call, ToolFile(SVG, "image/svg+xml", "passung.svg"), Attachment.Kind.IMAGE
    )
    assert attachment.mime_type == "image/svg+xml" and attachment.size > 0
    assert (attachment.width, attachment.height) == (400, 150)
    attachment.file.open("rb")
    stored = attachment.file.read()
    attachment.file.close()
    assert b"<script" not in stored and b"foreignObject" not in stored
    assert b"https://invalid.example" not in stored
    payload = client.get(reverse("chat:api_messages", args=[message.conversation_id])).json()
    answer = next(item for item in payload if item["id"] == message.pk)
    assert [item["id"] for item in answer["attachments"]] == [attachment.pk]
    html = client.get(reverse("chat:conversation", args=[message.conversation_id])).content.decode()
    assert 'class="message-attachments"' in html
    assert f'src="{attachment.url}"' in html
    response = client.get(attachment.url)
    assert response.status_code == 200 and response["Content-Type"] == "image/svg+xml"
    assert response["Content-Disposition"].startswith("inline")


def test_legacy_svg_without_mime_is_cleaned_when_served(client, message):
    attachment = Attachment(message=message, kind=Attachment.Kind.IMAGE)
    attachment.file.save("legacy.svg", ContentFile(SVG), save=True)
    response = client.get(attachment.url)
    assert response.status_code == 200
    assert response["Content-Type"] == "image/svg+xml"
    assert response["Content-Disposition"].startswith("inline")
    assert b"<script" not in response.content and b"foreignObject" not in response.content
    assert b"https://invalid.example" not in response.content
    assert "sandbox" in response["Content-Security-Policy"]
    client.force_login(make_user("adult", "other"))
    assert client.get(attachment.url).status_code == 404
