"""SVG-Vorschau in Codeblöcken: eingebundene Datei, Reihenfolge der Skripte,
sichere Darstellung nur als <img>, Export mit rohem Code.

Bereinigen und Anzeigen selbst (svg_preview.js) prüft der Browserlauf; Node gibt
es in der Entwicklungsumgebung nicht."""

from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse

from multigpt.accounts.models import Role, User
from multigpt.chat import services
from multigpt.chat.models import AIModel, Conversation, Provider

pytestmark = pytest.mark.django_db

STATIC = Path(settings.BASE_DIR) / "multigpt" / "chat" / "static" / "chat"
ANSWER = (
    "Hier die Flagge:\n\n```svg\n"
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 5 3">'
    '<rect width="5" height="1" fill="#000"/></svg>\n```\n'
)


@pytest.fixture
def ai_model():
    provider = Provider.objects.create(name="Testanbieter", kind=Provider.Kind.OPENAI_COMPAT)
    return AIModel.objects.create(provider=provider, model_id="m-1", display_name="Modell Eins")


@pytest.fixture
def anna_client(client):
    anna = User.objects.create_user(
        "anna", password="Geheim-Test-1234", role=Role.objects.get(key="adult")
    )
    client.force_login(anna)
    return client, anna


def test_chat_page_includes_svg_preview_before_markdown(anna_client, ai_model):
    client, anna = anna_client
    conv = Conversation.objects.create(user=anna, title="Flagge")
    services.append_message(conv, role="assistant", model=ai_model, content=ANSWER)
    html = client.get(reverse("chat:conversation", args=[conv.pk])).content.decode()
    assert f'"{settings.STATIC_URL}chat/svg_preview.js"' in html
    assert (
        html.index("dompurify/purify.min.js")
        < html.index("chat/svg_preview.js")
        < html.index("chat/markdown.js")
    )


def test_svg_preview_renders_only_as_image():
    js = (STATIC / "svg_preview.js").read_text()
    # Bildmodus über data:-URL, bereinigt mit DOMPurify im SVG-Profil.
    assert "data:image/svg+xml" in js
    assert "USE_PROFILES: { svg: true, svgFilters: true }" in js
    assert "512 * 1024" in js
    # Nie als Inline-SVG ins DOM.
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "createElementNS"):
        assert forbidden not in js
    markdown = (STATIC / "markdown.js").read_text()
    assert markdown.index("decorateCodeBlocks(el, final);") < markdown.index(
        "svgPreview?.decorate(el, text, final)"
    )


def test_export_keeps_svg_as_code(anna_client, ai_model):
    client, anna = anna_client
    conv = Conversation.objects.create(user=anna, title="Flagge")
    services.append_message(conv, role="assistant", model=ai_model, content=ANSWER)
    body = client.get(reverse("chat:conversation_export", args=[conv.pk])).content.decode()
    assert ANSWER.strip() in body
