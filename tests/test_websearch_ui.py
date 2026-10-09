"""Oberfläche der Websuche (M8): Schalter im Eingabefeld und Quellenliste unter
Antworten (serverseitig gerendert, nicht vertrauenswürdige Titel/URLs)."""

import re

import pytest
from django.urls import reverse

from multigpt.accounts.models import Role, User
from multigpt.chat import services
from multigpt.chat.models import (
    AIModel,
    Conversation,
    Message,
    Provider,
    SearchSettings,
    SourceRef,
    ToolCall,
)
from multigpt.chat.templatetags.source_tags import (
    source_domain,
    source_href,
    source_label,
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
def anna(ai_model):
    return make_user("anna")


@pytest.fixture
def anna_client(client, anna):
    client.force_login(anna)
    return client


@pytest.fixture
def search_on():
    cfg = SearchSettings.load()
    cfg.enabled = True
    cfg.searxng_url = "http://searx.intern:8888"
    cfg.save()
    return cfg


def answer_with_sources(user, ai_model, sources):
    conv = Conversation.objects.create(user=user, title="Quellen")
    services.append_message(conv, role=Message.Role.USER, content="Frage")
    answer = services.append_message(
        conv, role=Message.Role.ASSISTANT, content="Antwort [1]", model=ai_model
    )
    for title, url in sources:
        SourceRef.objects.create(message=answer, kind=SourceRef.Kind.WEB, title=title, url=url)
    return conv, answer


def sources_html(html):
    match = re.search(r'<section class="message-sources".*?</section>', html, re.S)
    assert match, "Quellenliste fehlt"
    return match.group(0)


# --- Schalter -----------------------------------------------------------------


def test_switch_shown_when_enabled_and_allowed(anna_client, search_on):
    html = anna_client.get(reverse("chat:index")).content.decode()
    assert 'id="web-search-toggle"' in html
    assert "Websuche" in html
    # Kein Inline-JS, Zustand kommt aus chat.js.
    assert "<script>" not in html
    assert "onclick" not in html and "onchange" not in html


def test_switch_hidden_when_disabled(anna_client):
    cfg = SearchSettings.load()
    cfg.enabled = False
    cfg.searxng_url = "http://searx.intern:8888"
    cfg.save()
    html = anna_client.get(reverse("chat:index")).content.decode()
    assert 'id="web-search-toggle"' not in html


def test_switch_hidden_without_url(anna_client):
    cfg = SearchSettings.load()
    cfg.enabled = True
    cfg.searxng_url = ""
    cfg.save()
    html = anna_client.get(reverse("chat:index")).content.decode()
    assert 'id="web-search-toggle"' not in html


def test_switch_hidden_without_permission(client, ai_model, search_on):
    guest = make_user("gast", role_key="guest")
    assert not guest.role.can_web_search
    client.force_login(guest)
    html = client.get(reverse("chat:index")).content.decode()
    assert 'id="web-search-toggle"' not in html


def test_switch_on_conversation_page(anna_client, anna, search_on):
    conv = Conversation.objects.create(user=anna, title="x")
    html = anna_client.get(reverse("chat:conversation", args=[conv.pk])).content.decode()
    assert 'id="web-search-toggle"' in html


# --- Quellenliste ---------------------------------------------------------------


def test_sources_rendered_numbered_with_safe_links(anna_client, anna, ai_model):
    conv, _ = answer_with_sources(
        anna,
        ai_model,
        [
            ("Erste Seite", "https://www.example.org/a?x=1&y=2"),
            ("Zweite", "http://news.example.com/b"),
        ],
    )
    html = anna_client.get(reverse("chat:conversation", args=[conv.pk])).content.decode()
    block = sources_html(html)
    assert ">Quellen</h3>" in block
    assert block.count('<li class="message-source">') == 2
    assert 'href="https://www.example.org/a?x=1&amp;y=2"' in block
    assert 'rel="noopener noreferrer nofollow"' in block
    assert 'target="_blank"' in block
    assert '<span class="message-source-domain">example.org</span>' in block
    assert '<span class="message-source-domain">news.example.com</span>' in block
    assert "<details" not in block  # höchstens 3: nichts eingeklappt


def test_sources_titles_escaped_and_bad_urls_not_linked(anna_client, anna, ai_model):
    conv, _ = answer_with_sources(
        anna,
        ai_model,
        [
            (XSS, "javascript:alert(1)"),
            ("Daten", "data:text/html,<script>alert(1)</script>"),
            ('"><svg onload=alert(1)>', "https://ok.example/"),
        ],
    )
    html = anna_client.get(reverse("chat:conversation", args=[conv.pk])).content.decode()
    block = sources_html(html)
    assert "<script" not in block
    assert "<img" not in block and "<svg" not in block
    assert "&lt;script&gt;" in block
    assert 'href="javascript' not in block
    assert 'href="data:' not in block
    # Nur die http(s)-Quelle ist ein Link.
    assert block.count("<a ") == 1
    assert 'href="https://ok.example/"' in block


def test_more_than_three_sources_collapsible(anna_client, anna, ai_model):
    conv, _ = answer_with_sources(
        anna, ai_model, [(f"Seite {i}", f"https://s{i}.example/") for i in range(1, 6)]
    )
    html = anna_client.get(reverse("chat:conversation", args=[conv.pk])).content.decode()
    block = sources_html(html)
    head, _, tail = block.partition("<details")
    assert head.count('<li class="message-source">') == 3
    assert "Weitere Quellen (2)" in tail
    assert '<ol class="message-sources-list" start="4">' in tail
    assert tail.count('<li class="message-source">') == 2
    # Reihenfolge = Nummer
    assert block.index("Seite 1") < block.index("Seite 3") < block.index("Seite 5")


def test_no_sources_section_without_sources(anna_client, anna, ai_model):
    conv, _ = answer_with_sources(anna, ai_model, [])
    html = anna_client.get(reverse("chat:conversation", args=[conv.pk])).content.decode()
    assert "message-sources" not in html


def test_sources_in_messages_fragment(anna_client, anna, ai_model):
    conv, _ = answer_with_sources(anna, ai_model, [("Titel", "https://a.example/")])
    response = anna_client.get(reverse("chat:conversation_messages", args=[conv.pk]))
    assert response.status_code == 200
    assert 'href="https://a.example/"' in sources_html(response.content.decode())


def test_web_search_notice_shown(anna_client, anna, ai_model):
    conv, answer = answer_with_sources(anna, ai_model, [])
    answer.tool_state = {"notices": {"web_search": "Websuche fehlgeschlagen: <b>Zeit</b>"}}
    answer.save()
    html = anna_client.get(reverse("chat:conversation", args=[conv.pk])).content.decode()
    assert "chat-message-status-warning" in html
    assert "Websuche fehlgeschlagen: &lt;b&gt;Zeit&lt;/b&gt;" in html


def test_web_search_tool_call_labelled(anna_client, anna, ai_model):
    conv, answer = answer_with_sources(anna, ai_model, [])
    ToolCall.objects.create(
        message=answer,
        server=None,
        tool="web_search",
        arguments={"query": "wetter"},
        status=ToolCall.Status.OK,
    )
    html = anna_client.get(reverse("chat:conversation", args=[conv.pk])).content.decode()
    assert '<span class="tool-call-server">(Websuche)</span>' in html


# --- Filter -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "href"),
    [
        ("https://example.org/x", "https://example.org/x"),
        ("HTTP://Example.org", "HTTP://Example.org"),
        ("javascript:alert(1)", ""),
        ("JaVaScRiPt:alert(1)", ""),
        (" javascript:alert(1)", ""),
        ("data:text/html,x", ""),
        ("//evil.example/", ""),
        ("/relativ", ""),
        ("https://", ""),
        ("http://[::1", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_source_href(url, href):
    assert source_href(url) == href


def test_source_domain_and_label():
    assert source_domain("https://www.heise.de/news") == "heise.de"
    assert source_domain("javascript:alert(1)") == ""
    src = SourceRef(kind=SourceRef.Kind.WEB, title="  Viel   Platz ", url="https://a.example/")
    assert source_label(src) == "Viel Platz"
    untitled = SourceRef(kind="web", title="", url="https://www.b.example/")
    assert source_label(untitled) == "b.example"
    doc = SourceRef(kind="document", title="Handbuch", page=3)
    assert source_label(doc) == "Handbuch, S. 3"


def test_static_js_has_no_html_injection_for_sources():
    """chat.js setzt Quellentitel nur als Text (kein innerHTML)."""
    from pathlib import Path

    from django.conf import settings

    js = (Path(settings.BASE_DIR) / "multigpt/chat/static/chat/chat.js").read_text()
    assert "innerHTML" not in js
    assert 'rel = "noopener noreferrer nofollow"' in js


# --- Kopierknopf im Eingabefeld -------------------------------------------------


def test_input_copy_button_present(anna_client):
    html = anna_client.get(reverse("chat:index")).content.decode()
    match = re.search(r'<button[^>]*id="input-copy-button"[^>]*>', html)
    assert match, "Kopierknopf der Eingabe fehlt"
    tag = match.group(0)
    assert 'type="button"' in tag
    assert 'aria-label="Eingabe kopieren"' in tag
    assert "disabled" in tag  # leeres Feld: deaktiviert
    assert "onclick" not in html and "<script>" not in html
    # Der Knopf liegt im Eingabefeld (vor „Senden“).
    assert html.index('id="message-input"') < html.index('id="input-copy-button"')
    assert html.index('id="input-copy-button"') < html.index('id="send-button"')
