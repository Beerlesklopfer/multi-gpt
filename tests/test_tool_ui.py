"""Anzeige der Werkzeugaufrufe im Chat (M4a-06): serverseitiges Rendern,
Escaping, Rückfrage-Knöpfe nach Neuladen, Filter."""

import datetime
import re

import pytest
from django.urls import reverse

from multigpt.accounts.models import Role, User, UserGroup
from multigpt.chat.models import (
    AIModel,
    Conversation,
    McpServer,
    Message,
    Provider,
    Share,
    ToolCall,
)
from multigpt.chat.templatetags.tool_tags import (
    tool_duration,
    tool_result_display,
    tool_result_truncated,
    tool_status_icon,
)

pytestmark = pytest.mark.django_db

PASSWORD = "Geheim-Test-1234"
XSS = '<script>alert("x")</script><img src=x onerror=alert(1)>'


def make_user(username, role_key="adult"):
    return User.objects.create_user(
        username, password=PASSWORD, role=Role.objects.get(key=role_key)
    )


@pytest.fixture
def ai_model():
    provider = Provider.objects.create(name="Testanbieter", kind=Provider.Kind.OPENAI_COMPAT)
    return AIModel.objects.create(provider=provider, model_id="m-1", display_name="Modell Eins")


@pytest.fixture
def server():
    return McpServer.objects.create(
        name="Werkzeugkiste <i>", transport=McpServer.Transport.STDIO, command="true"
    )


@pytest.fixture
def anna(ai_model):
    return make_user("anna")


@pytest.fixture
def anna_client(client, anna):
    client.force_login(anna)
    return client


def make_answer(conv, ai_model, status=Message.Status.COMPLETE, content="Fertig."):
    Message.objects.create(conversation=conv, role=Message.Role.USER, content="Frage")
    return Message.objects.create(
        conversation=conv,
        role=Message.Role.ASSISTANT,
        model=ai_model,
        content=content,
        status=status,
    )


def page(client, conv):
    response = client.get(reverse("chat:conversation", args=[conv.pk]))
    assert response.status_code == 200
    return response.content.decode()


def test_tool_calls_rendered_escaped(anna_client, anna, ai_model, server):
    conv = Conversation.objects.create(user=anna)
    msg = make_answer(conv, ai_model)
    tc = ToolCall.objects.create(
        message=msg,
        server=server,
        tool="<b>tool</b>",
        arguments={"query": XSS},
        result={"text": "Ergebnis " + XSS, "is_error": False},
        status=ToolCall.Status.OK,
        duration=datetime.timedelta(milliseconds=1234),
    )
    html = page(anna_client, conv)

    assert f'data-tool-call-id="{tc.pk}"' in html
    assert 'data-status="ok"' in html
    assert "<details" in html
    # Nichts aus Werkzeugdaten wird zu Markup.
    assert "<script>" not in html
    assert "<img" not in html
    assert "<b>tool</b>" not in html
    assert "<i>" not in html
    assert "&lt;b&gt;tool&lt;/b&gt;" in html
    assert "Werkzeugkiste &lt;i&gt;" in html
    assert "&lt;script&gt;alert(" in html
    assert "Ergebnis &lt;script&gt;" in html
    assert "1,2 s" in html
    assert "erfolgreich" in html
    # Werkzeugaufrufe stehen vor dem Antworttext, außerhalb des Markdown-Bereichs.
    assert html.index("tool-calls") < html.index('data-markdown="true"')
    # Abgeschlossene Aufrufe: keine Rückfrage.
    assert "data-tool-decision" not in html
    assert "Bestätigung nötig" not in html


def test_long_result_truncated_with_hint(anna_client, anna, ai_model, server):
    conv = Conversation.objects.create(user=anna)
    msg = make_answer(conv, ai_model)
    ToolCall.objects.create(
        message=msg,
        server=server,
        tool="lang",
        result={"text": "a" * 4500 + "ENDE", "is_error": False},
        status=ToolCall.Status.OK,
    )
    html = page(anna_client, conv)
    assert "ENDE" not in html
    assert "a" * 4000 + "…" in html
    assert re.search(r'<p class="hint tool-call-truncated">', html)


def test_deleted_server_and_running_call(anna_client, anna, ai_model):
    conv = Conversation.objects.create(user=anna)
    msg = make_answer(conv, ai_model)
    ToolCall.objects.create(message=msg, server=None, tool="weg", status=ToolCall.Status.RUNNING)
    html = page(anna_client, conv)
    assert "(Server entfernt)" in html
    # Noch kein Ergebnis: Ergebnisbereich verborgen.
    assert '<div class="tool-call-output" hidden>' in html


def test_awaiting_confirmation_buttons_after_reload(anna_client, anna, ai_model, server):
    conv = Conversation.objects.create(user=anna)
    msg = make_answer(conv, ai_model, status=Message.Status.AWAITING_CONFIRMATION, content="")
    first = ToolCall.objects.create(
        message=msg,
        server=server,
        tool="schreiben",
        arguments={"pfad": XSS},
        status=ToolCall.Status.AWAITING_CONFIRMATION,
    )
    second = ToolCall.objects.create(
        message=msg,
        server=server,
        tool="senden",
        status=ToolCall.Status.AWAITING_CONFIRMATION,
    )
    html = page(anna_client, conv)

    assert 'data-status="awaiting_confirmation"' in html
    for tc in (first, second):
        assert f'data-tool-decision="approve" data-tool-call-id="{tc.pk}"' in html
        assert f'data-tool-decision="reject" data-tool-call-id="{tc.pk}"' in html
    assert 'aria-label="Werkzeug schreiben ausführen"' in html
    assert ">Ausführen</button>" in html
    assert ">Ablehnen</button>" in html
    assert "data-tool-confirm-all" in html
    assert "Bestätigung nötig" in html
    assert "verändern oder Daten nach außen" in html
    # Wartende Aufrufe sind aufgeklappt, damit die Argumente sichtbar sind.
    assert '<details class="tool-call" open>' in html
    assert "<script>" not in html
    assert f'data-api-tool-confirm-template="{reverse("chat:api_tool_confirm", args=[0])}"' in html


def test_read_only_viewer_sees_no_confirm_buttons(client, anna, ai_model, server):
    ben = make_user("ben")
    group = UserGroup.objects.create(name="Eltern")
    ben.groups.add(group)
    conv = Conversation.objects.create(user=anna)
    Share.objects.create(conversation=conv, group=group, can_write=False)
    msg = make_answer(conv, ai_model, status=Message.Status.AWAITING_CONFIRMATION, content="")
    ToolCall.objects.create(
        message=msg, server=server, tool="x", status=ToolCall.Status.AWAITING_CONFIRMATION
    )
    client.force_login(ben)
    html = page(client, conv)
    assert "Bestätigung nötig" in html
    assert "data-tool-decision" not in html
    assert "data-tool-confirm-all" not in html


def test_composer_has_tool_switch_container_and_no_inline_js(anna_client):
    html = anna_client.get(reverse("chat:index")).content.decode()
    assert 'id="tool-servers"' in html
    assert f'data-api-mcp-servers="{reverse("chat:api_mcp_servers")}"' in html
    assert "<script>" not in html
    assert not re.search(r"<[^>]*\son[a-z]+=", html)


def test_tool_page_has_no_inline_js(anna_client, anna, ai_model, server):
    conv = Conversation.objects.create(user=anna)
    msg = make_answer(conv, ai_model, status=Message.Status.AWAITING_CONFIRMATION, content="")
    ToolCall.objects.create(
        message=msg,
        server=server,
        tool="t",
        arguments={"a": 'x" onclick="alert(1)'},
        status=ToolCall.Status.AWAITING_CONFIRMATION,
    )
    html = page(anna_client, conv)
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert not re.search(r"<[^>]*\son[a-z]+=", html)


def test_tool_filters():
    assert tool_duration(None) == ""
    assert tool_duration(0) == "0 ms"
    assert tool_duration(350) == "350 ms"
    assert tool_duration(1234) == "1,2 s"
    assert tool_duration(125_000) == "2 min 5 s"
    assert tool_status_icon("ok") == "✓"
    assert tool_status_icon("unbekannt") == "•"
    assert tool_result_display("kurz") == "kurz"
    assert tool_result_display(None) == ""
    assert tool_result_truncated("x" * 4001)
    assert not tool_result_truncated("x" * 4000)
