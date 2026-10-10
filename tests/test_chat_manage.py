"""M5 (Komfort): Chats verwalten, Suche/Archiv, Export, System-Prompt, Titel,
Markdown-Bibliotheken, Seitenleiste überall, Online-Anzeige (M4-04)."""

import json
import re
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse

from multigpt.accounts.models import Role, User, UserGroup
from multigpt.chat import services
from multigpt.chat.models import (
    DEFAULT_BASE_INSTRUCTIONS,
    AIModel,
    Conversation,
    Message,
    Provider,
    Share,
)
from multigpt.chat.titles import title_from
from multigpt.chat.views import export_filename

pytestmark = pytest.mark.django_db

PASSWORD = "Geheim-Test-1234"
FIXED_PROMPT = "GEHEIMER-ROLLENPROMPT: Sei immer jugendfrei."
VENDOR = Path(settings.BASE_DIR) / "multigpt" / "chat" / "static" / "chat" / "vendor"


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


@pytest.fixture
def conv(anna):
    return Conversation.objects.create(user=anna, title="Urlaub planen")


def detail_url(pk):
    return reverse("chat:api_conversation_detail", args=[pk])


def patch(client, pk, payload):
    return client.patch(detail_url(pk), data=json.dumps(payload), content_type="application/json")


def share(conv, user, can_write, can_update=False):
    group = UserGroup.objects.create(name=f"Gruppe {user.username} {can_write} {can_update}")
    user.groups.add(group)
    Share.objects.create(conversation=conv, group=group, can_write=can_write, can_update=can_update)


# --- Umbenennen, Archivieren, Löschen --------------------------------------------


def test_rename(anna_client, conv):
    before = Conversation.objects.get(pk=conv.pk).updated
    response = patch(anna_client, conv.pk, {"title": "  Neuer\n Titel  "})
    assert response.status_code == 200
    assert response.json()["title"] == "Neuer Titel"
    conv.refresh_from_db()
    assert conv.title == "Neuer Titel"
    # Umbenennen verschiebt den Chat in der Liste nicht.
    assert conv.updated == before


@pytest.mark.parametrize("title", ["", "   ", None, 5, "x" * 201])
def test_rename_invalid(anna_client, conv, title):
    response = patch(anna_client, conv.pk, {"title": title})
    assert response.status_code == 400
    assert "error" in response.json()
    conv.refresh_from_db()
    assert conv.title == "Urlaub planen"


def test_empty_or_bad_body(anna_client, conv):
    assert patch(anna_client, conv.pk, {}).status_code == 400
    response = anna_client.patch(detail_url(conv.pk), data="kein json", content_type="text/plain")
    assert response.status_code == 400
    assert patch(anna_client, conv.pk, {"archived": "ja"}).status_code == 400


def test_get_not_allowed(anna_client, conv):
    assert anna_client.get(detail_url(conv.pk)).status_code == 405


def test_archive_and_restore(anna_client, conv):
    response = patch(anna_client, conv.pk, {"archived": True})
    assert response.status_code == 200
    assert response.json()["archived"] is True
    conv.refresh_from_db()
    assert conv.archived

    html = anna_client.get(reverse("chat:index")).content.decode()
    assert "Urlaub planen" not in html
    html = anna_client.get(reverse("chat:index") + "?archived=1").content.decode()
    assert "Urlaub planen" in html
    assert "Zurück zu den Chats" in html

    # Archivierter Chat bleibt lesbar und zeigt den Hinweis.
    html = anna_client.get(reverse("chat:conversation", args=[conv.pk])).content.decode()
    assert "Archiviert" in html
    assert 'data-chat-action="restore"' in html

    assert patch(anna_client, conv.pk, {"archived": False}).status_code == 200
    conv.refresh_from_db()
    assert not conv.archived


def test_delete(anna_client, conv):
    services.append_message(conv, role="user", content="Hallo")
    response = anna_client.delete(detail_url(conv.pk))
    assert response.status_code == 200
    assert response.json() == {"deleted": True, "id": conv.pk}
    assert not Conversation.objects.filter(pk=conv.pk).exists()
    assert not Message.objects.filter(conversation_id=conv.pk).exists()
    assert anna_client.delete(detail_url(conv.pk)).status_code == 404


def test_foreign_conversation_is_404(client, conv, ben):
    client.force_login(ben)
    assert patch(client, conv.pk, {"title": "Gekapert"}).status_code == 404
    assert patch(client, conv.pk, {"archived": True}).status_code == 404
    assert client.delete(detail_url(conv.pk)).status_code == 404
    conv.refresh_from_db()
    assert conv.title == "Urlaub planen" and not conv.archived


def test_superuser_cannot_manage_foreign(client, conv):
    boss = User.objects.create_superuser("boss", password=PASSWORD)
    client.force_login(boss)
    assert client.delete(detail_url(conv.pk)).status_code == 404
    assert Conversation.objects.filter(pk=conv.pk).exists()


def test_anonymous_gets_403(client, conv):
    assert patch(client, conv.pk, {"title": "x"}).status_code == 403
    assert client.delete(detail_url(conv.pk)).status_code == 403


def test_shared_read_only_cannot_change(client, conv, ben):
    share(conv, ben, can_write=False)
    client.force_login(ben)
    for payload in ({"title": "x"}, {"system_prompt": "x"}, {"archived": True}):
        assert patch(client, conv.pk, payload).status_code == 403
    assert client.delete(detail_url(conv.pk)).status_code == 403
    conv.refresh_from_db()
    assert conv.title == "Urlaub planen" and conv.system_prompt == "" and not conv.archived
    # Seite: keine Aktionsknöpfe, Export aber erlaubt.
    html = client.get(reverse("chat:conversation", args=[conv.pk])).content.decode()
    assert "data-chat-action" not in html
    assert reverse("chat:conversation_export", args=[conv.pk]) in html
    assert 'id="system-prompt-form"' not in html


def test_shared_writer_without_update_cannot_rename(client, conv, ben):
    # RWUD (Chats teilen): Umbenennen und System-Prompt brauchen U, nicht W.
    share(conv, ben, can_write=True)
    client.force_login(ben)
    assert patch(client, conv.pk, {"title": "Gemeinsam"}).status_code == 403
    assert patch(client, conv.pk, {"system_prompt": "Kurz bitte"}).status_code == 403
    html = client.get(reverse("chat:conversation", args=[conv.pk])).content.decode()
    assert 'data-chat-action="rename"' not in html


def test_shared_updater_can_rename_but_not_archive_or_delete(client, conv, ben):
    share(conv, ben, can_write=True, can_update=True)
    client.force_login(ben)
    assert patch(client, conv.pk, {"title": "Gemeinsam"}).status_code == 200
    assert patch(client, conv.pk, {"system_prompt": "Kurz bitte"}).status_code == 200
    assert patch(client, conv.pk, {"archived": True}).status_code == 403
    assert client.delete(detail_url(conv.pk)).status_code == 403
    conv.refresh_from_db()
    assert conv.title == "Gemeinsam" and conv.system_prompt == "Kurz bitte"
    assert not conv.archived
    html = client.get(reverse("chat:conversation", args=[conv.pk])).content.decode()
    assert 'data-chat-action="rename"' in html
    assert 'data-chat-action="delete"' not in html
    assert 'data-chat-action="archive"' not in html


# --- Seitenleiste: Suche, Archiv, überall -----------------------------------------


def test_sidebar_search(anna_client, anna, ben):
    Conversation.objects.create(user=anna, title="Rezept Apfelkuchen")
    Conversation.objects.create(user=anna, title="Steuererklärung")
    Conversation.objects.create(user=anna, title="Altes Rezept", archived=True)
    Conversation.objects.create(user=ben, title="Bens Rezept")
    html = anna_client.get(reverse("chat:index") + "?q=rezept").content.decode()
    assert "Rezept Apfelkuchen" in html
    assert "Steuererklärung" not in html
    assert "Altes Rezept" not in html
    assert "Bens Rezept" not in html
    assert 'value="rezept"' in html
    assert "Suche zurücksetzen" in html

    html = anna_client.get(reverse("chat:index") + "?q=rezept&archived=1").content.decode()
    assert "Altes Rezept" in html
    assert "Rezept Apfelkuchen" not in html

    html = anna_client.get(reverse("chat:index") + "?q=gibtsnicht").content.decode()
    assert "Keine Treffer für „gibtsnicht“" in html


def test_sidebar_search_escaped(anna_client):
    html = anna_client.get(reverse("chat:index") + "?q=<script>x").content.decode()
    assert "<script>x" not in html
    assert "&lt;script&gt;x" in html


def test_sidebar_title_escaped(anna_client, anna):
    Conversation.objects.create(user=anna, title='<img src=x onerror="alert(1)">')
    html = anna_client.get(reverse("chat:index")).content.decode()
    assert "<img" not in html
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in html


def test_empty_archive_hint(anna_client):
    html = anna_client.get(reverse("chat:index") + "?archived=1").content.decode()
    assert "Das Archiv ist leer." in html


def test_sidebar_on_password_change_page(anna_client, anna):
    Conversation.objects.create(user=anna, title="Chat auf anderer Seite")
    html = anna_client.get(reverse("password_change")).content.decode()
    assert "Chat auf anderer Seite" in html
    assert 'id="chat-search-input"' in html
    assert "chat/sidebar.js" in html
    # Markdown-Bibliotheken nur in der Chatansicht.
    assert "vendor/marked" not in html


def test_login_page_has_no_sidebar(client):
    html = client.get(reverse("login")).content.decode()
    assert 'id="chat-list"' not in html
    assert "sidebar.js" not in html


# --- System-Prompt -----------------------------------------------------------------


def test_save_system_prompt(anna_client, conv):
    response = patch(anna_client, conv.pk, {"system_prompt": "  Antworte kurz.\r\n  "})
    assert response.status_code == 200
    assert response.json()["system_prompt"] == "Antworte kurz."
    conv.refresh_from_db()
    assert conv.system_prompt == "Antworte kurz."
    html = anna_client.get(reverse("chat:conversation", args=[conv.pk])).content.decode()
    assert "Antworte kurz.</textarea>" in html
    # Leeren ist erlaubt.
    assert patch(anna_client, conv.pk, {"system_prompt": ""}).status_code == 200
    conv.refresh_from_db()
    assert conv.system_prompt == ""


def test_system_prompt_too_long(anna_client, conv):
    assert patch(anna_client, conv.pk, {"system_prompt": "x" * 20001}).status_code == 400
    assert patch(anna_client, conv.pk, {"system_prompt": 1}).status_code == 400


def test_fixed_role_prompt_invisible_but_sent(client, ai_model):
    role = Role.objects.get(key="teen")
    role.fixed_system_prompt = FIXED_PROMPT
    role.save()
    teen = User.objects.create_user("tim", password=PASSWORD, role=role)
    conv = Conversation.objects.create(user=teen, title="Hausaufgaben", system_prompt="Eigener")
    services.append_message(conv, role="user", content="Frage")
    client.force_login(teen)

    page = client.get(reverse("chat:conversation", args=[conv.pk])).content.decode()
    assert FIXED_PROMPT not in page
    assert "Eigener" in page
    export = client.get(reverse("chat:conversation_export", args=[conv.pk])).content.decode()
    assert FIXED_PROMPT not in export
    assert "Eigener" in export
    # Die PATCH-Antwort verrät ihn ebenfalls nicht.
    response = patch(client, conv.pk, {"system_prompt": "Neu"})
    assert FIXED_PROMPT not in response.content.decode()
    # Beim Modell kommt er an, vor dem Chat-Prompt.
    conv.refresh_from_db()
    assert services.build_system_prompt(teen, conv) == (
        f"{DEFAULT_BASE_INSTRUCTIONS}\n\n{FIXED_PROMPT}\n\nNeu"
    )


# --- Export ------------------------------------------------------------------------


def test_export(anna_client, conv, ai_model):
    conv.system_prompt = "Sei freundlich.\nUnd kurz."
    conv.save()
    question = services.append_message(conv, role="user", content="Wohin im Mai?")
    # Ältere Antwortversion (anderer Zweig) erscheint nicht im Export.
    services.append_message(conv, role="assistant", model=ai_model, content="Alt")
    services.append_message(
        conv, parent=question, role="assistant", model=ai_model, content="**Nach Rom.**"
    )
    services.append_message(
        conv,
        role="assistant",
        model=ai_model,
        content="Teil",
        status="aborted",
        error="LM Studio ist offline.",
    )
    Message.objects.create(conversation=conv, role="system", content="SYSTEMZEILE")

    response = anna_client.get(reverse("chat:conversation_export", args=[conv.pk]))
    assert response.status_code == 200
    assert response["Content-Type"] == "text/markdown; charset=utf-8"
    assert response["Content-Disposition"] == (
        f'attachment; filename="chat-{conv.pk}-urlaub-planen.md"'
    )
    assert "no-store" in response["Cache-Control"]
    body = response.content.decode()
    assert body.startswith("# Urlaub planen\n")
    assert "> Sei freundlich.\n> Und kurz." in body
    assert "## Du" in body and "Wohin im Mai?" in body
    assert "## Modell Eins" in body and "**Nach Rom.**" in body
    assert "*Abgebrochen: LM Studio ist offline.*" in body
    assert "Alt" not in body
    assert "SYSTEMZEILE" not in body
    assert body.index("Wohin im Mai?") < body.index("Nach Rom.")


def test_export_foreign_404_and_shared_reader_ok(client, conv, ben):
    client.force_login(ben)
    url = reverse("chat:conversation_export", args=[conv.pk])
    assert client.get(url).status_code == 404
    share(conv, ben, can_write=False)
    assert client.get(url).status_code == 200


def test_export_anonymous_redirect(client, conv):
    response = client.get(reverse("chat:conversation_export", args=[conv.pk]))
    assert response.status_code == 302


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Größe & Maße", "chat-7-grosse-masse.md"),
        ('../../etc/passwd"; x', "chat-7-etc-passwd-x.md"),
        ("", "chat-7.md"),
        ("日本語", "chat-7.md"),
        ("a" * 120, "chat-7-" + "a" * 50 + ".md"),
    ],
)
def test_export_filename_safe(title, expected):
    conv = Conversation(pk=7, title=title)
    name = export_filename(conv)
    assert name == expected
    assert re.fullmatch(r"[a-z0-9.-]+", name)


# --- Automatischer Titel -----------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("## **Hallo** _Welt_", "Hallo Welt"),
        ("> - [ ] [Link](https://example.org) `code`", "Link code"),
        ("\n\n  1. Erstens <b>fett</b>\nZweitens", "Erstens fett"),
        ("```python\nprint(1)\n```\nWas macht das?", "Was macht das?"),
        ("```\nnur_code()\n```", "nur_code()"),
        ("![Katze](k.png) ansehen", "Katze ansehen"),
        ("snake_case_name bleibt", "snake_case_name bleibt"),
        ("   ", ""),
    ],
)
def test_title_from(content, expected):
    assert title_from(content) == expected


def test_title_set_from_first_message(ai_model, anna):
    conv = Conversation.objects.create(user=anna)
    services.prepare_turn(anna, conv, ai_model, content="# **Rezept** für Pfannkuchen\nmehr")
    conv.refresh_from_db()
    assert conv.title == "Rezept für Pfannkuchen"


# --- Markdown-Bibliotheken und Seiten-HTML -----------------------------------------


VENDOR_FILES = [
    "marked/marked.umd.js",
    "marked/LICENSE",
    "dompurify/purify.min.js",
    "dompurify/LICENSE",
    "highlight/highlight.min.js",
    "highlight/LICENSE",
    "highlight/styles/github.min.css",
    "highlight/styles/github-dark.min.css",
    "README.md",
]


@pytest.mark.parametrize("name", VENDOR_FILES)
def test_vendor_file_present(name):
    path = VENDOR / name
    assert path.is_file() and path.stat().st_size > 0


def test_vendor_readme_checksums_match():
    import hashlib

    readme = (VENDOR / "README.md").read_text()
    sums = re.findall(r"^([0-9a-f]{64})  \./(\S+)$", readme, flags=re.M)
    assert len(sums) >= 8
    for digest, name in sums:
        assert hashlib.sha256((VENDOR / name).read_bytes()).hexdigest() == digest, name


def test_chat_page_includes_local_vendor_scripts(anna_client, conv, ai_model):
    services.append_message(conv, role="user", content="*Frage*")
    services.append_message(conv, role="assistant", model=ai_model, content="*Ja*")
    html = anna_client.get(reverse("chat:conversation", args=[conv.pk])).content.decode()
    for name in (
        "vendor/marked/marked.umd.js",
        "vendor/dompurify/purify.min.js",
        "vendor/highlight/highlight.min.js",
        "vendor/highlight/styles/github.min.css",
        "vendor/highlight/styles/github-dark.min.css",
        "markdown.js",
    ):
        assert f'"{settings.STATIC_URL}chat/{name}"' in html
    # Reihenfolge: Bibliotheken vor markdown.js vor chat.js.
    assert html.index("purify.min.js") < html.index("markdown.js") < html.index("chat/chat.js")
    # Kein Inline-JS, keine externen Ressourcen.
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert not re.search(r"\son[a-z]+=", html)
    assert "https://" not in html and "http://" not in html
    # Nur Antworten werden als Markdown gerendert, der Rohtext steht escaped im HTML.
    assert re.search(r'chat-message-content" data-markdown="true">\*Ja\*</div>', html)
    assert re.search(r'chat-message-content">\*Frage\*</div>', html)


def test_index_without_inline_js(anna_client):
    html = anna_client.get(reverse("chat:index")).content.decode()
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert "markdown.js" in html
    assert 'id="system-prompt-input"' in html


# --- Online-Anzeige (M4-04) --------------------------------------------------------


def test_status_indicator_only_with_status_providers(anna_client):
    html = anna_client.get(reverse("chat:index")).content.decode()
    assert 'id="provider-status"' not in html
    Provider.objects.create(
        name="LM Studio",
        kind=Provider.Kind.OPENAI_COMPAT,
        is_local=True,
        check_status=True,
        base_url="http://192.0.2.1:1234/v1",
    )
    html = anna_client.get(reverse("chat:index")).content.decode()
    assert 'id="provider-status"' in html
    assert f'data-url="{reverse("chat:api_provider_status")}"' in html
    assert "LM Studio offline" in html
    assert "192.0.2.1" not in html


def test_status_indicator_hidden_for_inactive_provider(anna_client):
    Provider.objects.create(
        name="LM Studio",
        kind=Provider.Kind.OPENAI_COMPAT,
        is_local=True,
        check_status=True,
        active=False,
    )
    html = anna_client.get(reverse("chat:index")).content.decode()
    assert 'id="provider-status"' not in html
