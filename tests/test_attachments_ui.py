"""Anhänge in der Chatoberfläche (attachui): Büroklammer, Vorschauleiste und
Ablagezone in der Eingabe, Anhänge in Nachrichten (serverseitig gerendert,
escaped), kein Inline-JS."""

import re
from html.parser import HTMLParser

import pytest
from django.conf import settings
from django.contrib.staticfiles import finders
from django.urls import reverse

from multigpt.accounts.models import Role, User, UserGroup
from multigpt.chat import attachments
from multigpt.chat.models import (
    AIModel,
    Attachment,
    Conversation,
    Message,
    Provider,
    Share,
    ToolCall,
)

pytestmark = pytest.mark.django_db

PASSWORD = "Geheim-Test-1234"
EVIL = '<img src=x onerror="alert(1)">'


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


def add_attachment(message, owner, kind, name, **fields):
    defaults = {
        "image": {
            "mime_type": "image/png",
            "size": 2048,
            "width": 640,
            "height": 480,
            "thumbnail": "attachments/test/thumb.webp",
        },
        "file": {"mime_type": "application/pdf", "size": 1536 * 1024},
    }[kind]
    defaults.update(fields)
    return Attachment.objects.create(
        message=message,
        owner=owner,
        conversation=message.conversation,
        kind=kind,
        file=f"attachments/test/{kind}.bin",
        original_name=name,
        **defaults,
    )


@pytest.fixture
def chat_with_attachments(anna, ai_model):
    conv = Conversation.objects.create(user=anna, title="Mit Anhängen")
    question = Message.objects.create(
        conversation=conv, role=Message.Role.USER, content="Was ist auf dem Bild?"
    )
    answer = Message.objects.create(
        conversation=conv,
        parent=question,
        role=Message.Role.ASSISTANT,
        content="Eine Katze.",
        model=ai_model,
    )
    conv.current_leaf = answer
    conv.save()
    image = add_attachment(question, anna, "image", f"{EVIL}.png")
    doc = add_attachment(question, anna, "file", 'Bericht "Q3" <b>.pdf')
    return {"conv": conv, "question": question, "answer": answer, "image": image, "doc": doc}


def article(html, pk):
    match = re.search(rf'<article[^>]*data-message-id="{pk}".*?</article>', html, re.S)
    assert match, f"Nachricht {pk} fehlt"
    return match.group(0)


def page(client, conv):
    response = client.get(reverse("chat:conversation", args=[conv.pk]))
    assert response.status_code == 200
    return response.content.decode()


def attribute_names(html):
    names = []

    class Collector(HTMLParser):
        def handle_starttag(self, tag, attrs):
            names.extend(name for name, _value in attrs)

    Collector().feed(html)
    return names


def assert_no_inline_js(html):
    assert "<script>" not in html
    # Keine Ereignis-Attribute (geparst: escapte Namen in Attributwerten zählen nicht).
    assert not [name for name in attribute_names(html) if name.startswith("on")]
    assert "javascript:" not in html
    assert "{#" not in html and "#}" not in html


# --- Eingabe ------------------------------------------------------------------


def test_composer_has_attach_button_input_tray_and_dropzone(anna_client):
    html = anna_client.get(reverse("chat:index")).content.decode()
    assert 'id="attach-button"' in html
    assert 'aria-label="Datei oder Bild anhängen"' in html
    match = re.search(r'<input type="file" id="attach-input"[^>]*>', html)
    assert match, "Dateiauswahl fehlt"
    assert "multiple" in match.group(0)
    assert f'accept="{attachments.ACCEPT}"' in match.group(0)
    assert 'id="attach-tray"' in html
    assert 'id="attach-drop"' in html
    assert "Dateien hier ablegen" in html
    assert 'id="attach-vision-hint"' in html
    assert 'aria-live="polite"' in html
    # API-Adressen und Grenzen für attachments.js.
    assert f'data-api-attachments="{reverse("chat:api_attachments")}"' in html
    detail = reverse("chat:api_attachment_detail", args=[0])
    assert f'data-api-attachment-template="{detail}"' in html
    assert f'data-max-image-bytes="{settings.ATTACHMENT_MAX_IMAGE_MB * 1024 * 1024}"' in html
    assert f'data-max-document-bytes="{settings.DOCUMENT_MAX_UPLOAD_MB * 1024 * 1024}"' in html
    assert f'data-max-attachments="{settings.ATTACHMENT_MAX_PER_MESSAGE}"' in html
    # attachments.js vor chat.js (stellt window.MultiGPT.attachments bereit).
    assert html.index("chat/attachments.js") < html.index("chat/chat.js")
    assert_no_inline_js(html)


def test_reader_has_no_composer_but_sees_attachments(client, chat_with_attachments, ben):
    group = UserGroup.objects.create(name="Leser")
    ben.groups.add(group)
    Share.objects.create(conversation=chat_with_attachments["conv"], group=group, can_write=False)
    client.force_login(ben)
    html = page(client, chat_with_attachments["conv"])
    assert 'id="attach-button"' not in html
    assert 'id="attach-input"' not in html
    assert "message-attachments" in html
    # Lightbox-Skript ist auch beim Lesen geladen.
    assert "chat/attachments.js" in html


# --- Nachrichten ----------------------------------------------------------------


def test_message_renders_thumbnail_and_chip_escaped(anna_client, chat_with_attachments):
    data = chat_with_attachments
    html = page(anna_client, data["conv"])
    question = article(html, data["question"].pk)
    image, doc = data["image"], data["doc"]
    # Bild: Thumbnail über die geschützte Vorschau-URL, Link aufs Original, alt = Name.
    thumb = reverse("chat:attachment_thumb", args=[image.pk])
    full = reverse("chat:attachment", args=[image.pk])
    assert f'src="{thumb}"' in question
    assert f'href="{full}" data-attachment-image' in question
    assert 'alt="&lt;img src=x onerror=&quot;alert(1)&quot;&gt;.png"' in question
    assert 'width="640" height="480"' in question
    # Datei: Chip mit Download, Name und Größe als Text.
    assert f'href="{reverse("chat:attachment", args=[doc.pk])}" download' in question
    assert "Bericht &quot;Q3&quot; &lt;b&gt;.pdf" in question
    assert doc.size_label in question
    # Nichts davon unescaped.
    assert EVIL not in html
    assert "<b>.pdf" not in html
    assert_no_inline_js(html)
    # Antworten ohne Anhänge bekommen keine Liste.
    assert "message-attachments" not in article(html, data["answer"].pk)


def test_attachment_list_after_content_and_data_for_editor(anna_client, chat_with_attachments):
    data = chat_with_attachments
    question = article(page(anna_client, data["conv"]), data["question"].pk)
    assert question.index("chat-message-content") < question.index("message-attachments")
    image = data["image"]
    assert f'data-attachment-id="{image.pk}"' in question
    assert 'data-kind="image"' in question and 'data-kind="file"' in question
    assert f'data-thumbnail-url="{reverse("chat:attachment_thumb", args=[image.pk])}"' in question
    assert f'data-size="{image.size}"' in question
    # Bearbeiten bleibt möglich (Editor übernimmt die Anhänge per JS).
    assert "data-edit-message" in question


def test_fragment_renders_attachments(anna_client, chat_with_attachments):
    data = chat_with_attachments
    url = reverse("chat:conversation_messages", args=[data["conv"].pk])
    html = anna_client.get(url).content.decode()
    assert html.count('class="message-attachment ') == 2
    assert reverse("chat:attachment_thumb", args=[data["image"].pk]) in html
    assert EVIL not in html
    assert "<script" not in html


def test_tool_attachments_not_listed_as_uploads(anna_client, anna, ai_model, chat_with_attachments):
    data = chat_with_attachments
    call = ToolCall.objects.create(
        message=data["answer"], tool="bild", arguments={}, status=ToolCall.Status.OK
    )
    add_attachment(data["answer"], anna, "image", "erzeugt.png", tool_call=call)
    html = page(anna_client, data["conv"])
    assert "message-attachments" not in article(html, data["answer"].pk)
    assert 'alt="erzeugt.png"' not in html


def test_static_script_has_no_html_injection_of_names():
    path = finders.find("chat/attachments.js")
    assert path, "attachments.js fehlt"
    source = open(path, encoding="utf-8").read()
    # innerHTML nur für feste, eigene SVG-Symbole.
    for line in source.splitlines():
        if "innerHTML" in line:
            assert re.search(r"innerHTML\s*=\s*(FILE_ICON|PAPERCLIP_ICON)\s*;", line), line
    assert "eval(" not in source
    assert "http://" not in source and "https://" not in source


# --- Eingabefeld im Chat-Design und einklappbare Eingabeoptionen ---------------------


def element_html(html, element_id):
    """HTML ab dem Element mit ``element_id`` bis zum passenden schließenden Tag."""
    start = html.index(f'id="{element_id}"')
    start = html.rindex("<", 0, start)
    tag = re.match(r"<([a-z]+)", html[start:]).group(1)
    depth, pos = 0, start
    for match in re.finditer(rf"<(/?){tag}\b", html[start:]):
        depth += -1 if match.group(1) else 1
        if depth == 0:
            pos = start + match.start()
            break
    return html[start : html.index(">", pos) + 1]


def test_send_button_is_round_icon_button_in_field(anna_client):
    html = anna_client.get(reverse("chat:index")).content.decode()
    field = html[html.index('<div class="chat-input-field">') :]
    field = field[: field.index("</form>")]
    toolbar = field[field.index('class="chat-input-toolbar"') :]
    # Büroklammer links, Senden rechts in derselben Leiste im Feld.
    assert toolbar.index('id="attach-button"') < toolbar.index('id="send-button"')
    button = re.search(r'<button type="submit"[^>]*id="send-button"[^>]*>', html).group(0)
    assert 'class="send-button"' in button
    assert 'aria-label="Senden"' in button and 'title="Senden"' in button
    assert "disabled" in button and 'data-state="send"' in button
    assert "send-icon-send" in toolbar and "send-icon-stop" in toolbar
    # Kopierknopf bleibt im Feld.
    assert 'id="input-copy-button"' in field and 'id="message-input"' in field
    assert_no_inline_js(html)


def test_input_options_can_be_collapsed(anna_client):
    html = anna_client.get(reverse("chat:index")).content.decode()
    toggle = re.search(r'<button[^>]*id="input-options-toggle"[^>]*>', html).group(0)
    assert 'aria-expanded="true"' in toggle
    assert 'aria-controls="chat-input-options"' in toggle
    assert "Eingabeoptionen ausblenden" in html
    assert 'id="input-options-summary"' in html
    options = element_html(html, "chat-input-options")
    for element_id in ("model-select", "compare-toggle", "compare-models", "tool-servers"):
        assert f'id="{element_id}"' in options, element_id
    assert 'id="collection-picker"' in options
    assert "Im Vergleich ohne Werkzeuge (MCP)." in options
    # Das Eingabefeld selbst gehört nicht dazu.
    assert 'id="message-input"' not in options
    assert 'id="send-button"' not in options
    assert "chat/input_options.js" in html


def test_scripts_keep_storage_access_safe():
    source = open(finders.find("chat/input_options.js"), encoding="utf-8").read()
    assert "multigpt.inputOptionsCollapsed" in source
    # Jeder Zugriff auf localStorage steht in einem try-Block.
    assert source.count("window.localStorage") == 3
    assert source.count("try {") == 2
    for line in source.splitlines():
        assert "innerHTML" not in line
    chat = open(finders.find("chat/chat.js"), encoding="utf-8").read()
    assert '"Antwort stoppen"' in chat
