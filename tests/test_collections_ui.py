"""M7 (RAG), Oberfläche: Sammlungen verwalten, teilen, Dokumente löschen,
Download, Abschnittsansicht, Auswahl im Chat und Dokument-Quellen."""

import json
import re

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from multigpt.accounts.models import Role, User, UserGroup
from multigpt.chat import services
from multigpt.chat.models import (
    EMBEDDING_DIMENSIONS,
    AIModel,
    Chunk,
    Collection,
    Conversation,
    Document,
    Message,
    Provider,
    Share,
    SourceRef,
)

pytestmark = pytest.mark.django_db

PASSWORD = "Geheim-Test-1234"


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"


def make_user(username, role_key="adult"):
    return User.objects.create_user(
        username, password=PASSWORD, role=Role.objects.get(key=role_key)
    )


@pytest.fixture
def anna():
    return make_user("anna")


@pytest.fixture
def ben():
    return make_user("ben")


def client_for(client, user):
    client.force_login(user)
    return client


@pytest.fixture
def anna_client(client, anna):
    return client_for(client, anna)


@pytest.fixture
def coll(anna):
    return Collection.objects.create(owner=anna, name="Anleitungen")


def make_document(collection, title="Handbuch.txt", content=b"Hallo Welt", status="indexed"):
    return Document.objects.create(
        collection=collection,
        title=title,
        file=SimpleUploadedFile("egal.txt", content),
        status=status,
    )


def make_chunk(document, text="Abschnittstext", page=3, position=0):
    return Chunk.objects.create(
        document=document,
        position=position,
        text=text,
        page=page,
        embedding=[0.1] * EMBEDDING_DIMENSIONS,
    )


def share_with(collection, user, can_write=False, name=None):
    group = UserGroup.objects.create(name=name or f"Gruppe {user.username} {can_write}")
    user.groups.add(group)
    return Share.objects.create(collection=collection, group=group, can_write=can_write)


def api(client, method, url, payload=None):
    body = json.dumps(payload) if payload is not None else ""
    return getattr(client, method)(url, data=body, content_type="application/json")


def detail_api(pk):
    return reverse("chat:api_collection_detail", args=[pk])


def shares_api(pk):
    return reverse("chat:api_collection_shares", args=[pk])


# --- Seiten ---------------------------------------------------------------------------


def test_sidebar_links_to_collections(anna_client):
    html = anna_client.get(reverse("chat:index")).content.decode()
    assert f'href="{reverse("chat:collection_list")}"' in html
    assert "Sammlungen" in html


def test_list_shows_own_and_shared_not_foreign(client, anna, ben, coll):
    foreign = Collection.objects.create(owner=ben, name="Bens Geheimnisse")
    shared = Collection.objects.create(owner=ben, name="Bens Rezepte")
    share_with(shared, anna)
    html = client_for(client, anna).get(reverse("chat:collection_list")).content.decode()
    assert "Anleitungen" in html
    assert "Bens Rezepte" in html
    assert "lesend" in html
    assert foreign.name not in html


def test_pages_without_inline_js(anna_client, coll):
    doc = make_document(coll)
    chunk = make_chunk(doc)
    for url in (
        reverse("chat:collection_list"),
        reverse("chat:collection_detail", args=[coll.pk]),
        reverse("chat:document_chunk", args=[chunk.pk]),
    ):
        html = anna_client.get(url).content.decode()
        scripts = re.findall(r"<script\b[^>]*>", html)
        assert scripts, url
        assert all("src=" in tag for tag in scripts), url
        assert not re.search(r"\son[a-z]+=", html), url


def test_names_and_titles_escaped(anna_client, anna):
    coll = Collection.objects.create(owner=anna, name="<img src=x onerror=alert(1)>")
    doc = make_document(coll, title="<script>alert(2)</script>.txt", status="error")
    Document.objects.filter(pk=doc.pk).update(error_text="<b>kaputt</b>")
    html = anna_client.get(reverse("chat:collection_detail", args=[coll.pk])).content.decode()
    assert "<img src=x" not in html
    assert "<script>alert(2)" not in html
    assert "<b>kaputt</b>" not in html
    assert "&lt;b&gt;kaputt&lt;/b&gt;" in html


def test_detail_buttons_by_rights(client, anna, ben, coll):
    make_document(coll)
    html = client_for(client, anna).get(reverse("chat:collection_detail", args=[coll.pk]))
    html = html.content.decode()
    assert 'id="upload-form"' in html
    assert 'id="collection-delete"' in html
    assert 'id="collection-shares"' in html
    assert "data-document-delete" in html

    share_with(coll, ben, can_write=False)
    html = client_for(client, ben).get(reverse("chat:collection_detail", args=[coll.pk]))
    html = html.content.decode()
    assert "nur lesend" in html
    for marker in (
        'id="upload-form"',
        'id="collection-delete"',
        'id="collection-shares"',
        "data-document-delete",
        'id="collection-rename"',
    ):
        assert marker not in html


def test_detail_pending_sets_poll(anna_client, coll):
    make_document(coll, status="pending")
    html = anna_client.get(reverse("chat:collection_detail", args=[coll.pk])).content.decode()
    assert 'data-poll="true"' in html
    assert "status-pending" in html


def test_foreign_pages_404(client, anna, ben, coll):
    doc = make_document(coll)
    chunk = make_chunk(doc)
    c = client_for(client, ben)
    assert c.get(reverse("chat:collection_detail", args=[coll.pk])).status_code == 404
    assert c.get(reverse("chat:document_download", args=[doc.pk])).status_code == 404
    assert c.get(reverse("chat:document_chunk", args=[chunk.pk])).status_code == 404


def test_pages_require_login(client, coll):
    response = client.get(reverse("chat:collection_list"))
    assert response.status_code == 302
    assert reverse("login") in response["Location"]


# --- API: Sammlungen ------------------------------------------------------------------


def test_api_list_readable_only(client, anna, ben, coll):
    Collection.objects.create(owner=ben, name="Fremd")
    shared = Collection.objects.create(owner=ben, name="Geteilt")
    share_with(shared, anna, can_write=True)
    make_document(coll)
    data = client_for(client, anna).get(reverse("chat:api_collections")).json()
    by_name = {c["name"]: c for c in data}
    assert set(by_name) == {"Anleitungen", "Geteilt"}
    assert by_name["Anleitungen"]["is_owner"] is True
    assert by_name["Anleitungen"]["indexed_count"] == 1
    assert by_name["Geteilt"]["access"] == "schreibend"
    assert by_name["Geteilt"]["can_write"] is True


def test_create_collection(anna_client, anna):
    response = api(
        anna_client, "post", reverse("chat:api_collections"), {"name": "  Steuer  2026 "}
    )
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "Steuer 2026"
    assert data["url"] == reverse("chat:collection_detail", args=[data["id"]])
    assert Collection.objects.get(pk=data["id"]).owner == anna


@pytest.mark.parametrize("name", ["", "   ", 5, "x" * 201])
def test_create_collection_invalid(anna_client, name):
    response = api(anna_client, "post", reverse("chat:api_collections"), {"name": name})
    assert response.status_code == 400
    assert response.json()["error"]


def test_create_collection_duplicate(anna_client, coll):
    response = api(anna_client, "post", reverse("chat:api_collections"), {"name": "Anleitungen"})
    assert response.status_code == 400
    assert "schon" in response.json()["error"]


def test_guest_cannot_create(client):
    guest = make_user("gast", "guest")
    response = api(
        client_for(client, guest), "post", reverse("chat:api_collections"), {"name": "X"}
    )
    assert response.status_code == 403
    assert not Collection.objects.exists()


def test_rename(anna_client, coll):
    response = api(anna_client, "patch", detail_api(coll.pk), {"name": "Handbücher"})
    assert response.status_code == 200
    assert response.json()["name"] == "Handbücher"
    coll.refresh_from_db()
    assert coll.name == "Handbücher"


def test_foreign_api_404(client, anna, ben, coll):
    doc = make_document(coll)
    c = client_for(client, ben)
    assert api(c, "patch", detail_api(coll.pk), {"name": "x"}).status_code == 404
    assert api(c, "delete", detail_api(coll.pk)).status_code == 404
    assert api(c, "post", shares_api(coll.pk), {"group": 1}).status_code == 404
    assert api(c, "delete", reverse("chat:api_document_detail", args=[doc.pk])).status_code == 404
    docs_url = reverse("chat:api_collection_documents", args=[coll.pk])
    assert c.get(docs_url).status_code == 404
    assert Collection.objects.filter(pk=coll.pk).exists()
    assert Document.objects.filter(pk=doc.pk).exists()


def test_reader_cannot_change(client, anna, ben, coll):
    doc = make_document(coll)
    share_with(coll, ben, can_write=False)
    c = client_for(client, ben)
    assert c.get(reverse("chat:api_collection_documents", args=[coll.pk])).status_code == 200
    assert api(c, "patch", detail_api(coll.pk), {"name": "x"}).status_code == 403
    assert api(c, "delete", detail_api(coll.pk)).status_code == 403
    assert api(c, "delete", reverse("chat:api_document_detail", args=[doc.pk])).status_code == 403
    upload = c.post(
        reverse("chat:api_collection_documents", args=[coll.pk]),
        {"file": SimpleUploadedFile("a.txt", b"Text")},
    )
    assert upload.status_code == 403
    assert coll.documents.count() == 1


def test_writer_can_rename_and_delete_documents_not_collection(client, anna, ben, coll):
    doc = make_document(coll)
    share_with(coll, ben, can_write=True)
    c = client_for(client, ben)
    assert api(c, "patch", detail_api(coll.pk), {"name": "Neu"}).status_code == 200
    response = api(c, "delete", detail_api(coll.pk))
    assert response.status_code == 403
    assert "angelegt" in response.json()["error"]
    assert api(c, "delete", reverse("chat:api_document_detail", args=[doc.pk])).status_code == 200
    assert not Document.objects.filter(pk=doc.pk).exists()


def test_delete_collection_removes_files(anna_client, coll, django_capture_on_commit_callbacks):
    doc = make_document(coll)
    storage, name = doc.file.storage, doc.file.name
    assert storage.exists(name)
    with django_capture_on_commit_callbacks(execute=True):
        response = api(anna_client, "delete", detail_api(coll.pk))
    assert response.status_code == 200
    assert not Collection.objects.filter(pk=coll.pk).exists()
    assert not storage.exists(name)


def test_delete_document_removes_file_and_chunks(
    anna_client, coll, django_capture_on_commit_callbacks
):
    doc = make_document(coll)
    make_chunk(doc)
    storage, name = doc.file.storage, doc.file.name
    with django_capture_on_commit_callbacks(execute=True):
        response = api(anna_client, "delete", reverse("chat:api_document_detail", args=[doc.pk]))
    assert response.status_code == 200
    assert not storage.exists(name)
    assert not Chunk.objects.filter(document_id=doc.pk).exists()


# --- Teilen ---------------------------------------------------------------------------


def test_share_and_revoke(client, anna, ben, coll):
    group = UserGroup.objects.create(name="Eltern")
    ben.groups.add(group)
    c = client_for(client, anna)
    response = api(c, "post", shares_api(coll.pk), {"group": group.pk, "can_write": False})
    assert response.status_code == 200
    assert response.json()["shares"] == [
        {"group": group.pk, "group_name": "Eltern", "can_write": False}
    ]
    # Ändern statt doppelt anlegen
    response = api(c, "post", shares_api(coll.pk), {"group": group.pk, "can_write": True})
    assert response.json()["shares"][0]["can_write"] is True
    assert Share.objects.filter(collection=coll).count() == 1
    response = api(c, "delete", shares_api(coll.pk), {"group": group.pk})
    assert response.status_code == 200
    assert response.json()["shares"] == []


def test_share_requires_share_right(client):
    teen = make_user("tim", "teen")  # darf hochladen, aber nicht teilen
    coll = Collection.objects.create(owner=teen, name="Schule")
    group = UserGroup.objects.create(name="Familie2")
    c = client_for(client, teen)
    response = api(c, "post", shares_api(coll.pk), {"group": group.pk})
    assert response.status_code == 403
    assert not Share.objects.exists()
    html = c.get(reverse("chat:collection_detail", args=[coll.pk])).content.decode()
    assert 'id="collection-shares"' not in html


def test_share_only_by_owner(client, anna, ben, coll):
    share_with(coll, ben, can_write=True)
    group = UserGroup.objects.create(name="Andere")
    response = api(client_for(client, ben), "post", shares_api(coll.pk), {"group": group.pk})
    assert response.status_code == 403


@pytest.mark.parametrize("payload", [{}, {"group": "1"}, {"group": True}, {"group": 999999}])
def test_share_invalid_group(anna_client, coll, payload):
    assert api(anna_client, "post", shares_api(coll.pk), payload).status_code == 400


def test_share_invalid_mode(anna_client, coll):
    group = UserGroup.objects.create(name="G")
    response = api(anna_client, "post", shares_api(coll.pk), {"group": group.pk, "can_write": "ja"})
    assert response.status_code == 400


def test_revocation_takes_effect_immediately(client, anna, ben, coll):
    doc = make_document(coll)
    chunk = make_chunk(doc)
    share = share_with(coll, ben)
    c = client_for(client, ben)
    urls = [
        reverse("chat:collection_detail", args=[coll.pk]),
        reverse("chat:document_download", args=[doc.pk]),
        reverse("chat:document_chunk", args=[chunk.pk]),
        reverse("chat:api_collection_documents", args=[coll.pk]),
    ]
    for url in urls:
        assert c.get(url).status_code == 200, url
    assert coll.pk in [x["id"] for x in c.get(reverse("chat:api_collections")).json()]

    share.delete()
    for url in urls:
        assert c.get(url).status_code == 404, url
    assert c.get(reverse("chat:api_collections")).json() == []


def test_leaving_group_revokes(client, anna, ben, coll):
    share = share_with(coll, ben)
    c = client_for(client, ben)
    assert c.get(reverse("chat:collection_detail", args=[coll.pk])).status_code == 200
    ben.groups.remove(share.group)
    assert c.get(reverse("chat:collection_detail", args=[coll.pk])).status_code == 404


# --- Download -------------------------------------------------------------------------


def test_download_owner(anna_client, coll):
    doc = make_document(coll, title="Mein Handbuch: Teil 1.txt", content=b"Inhalt 123")
    response = anna_client.get(reverse("chat:document_download", args=[doc.pk]))
    assert response.status_code == 200
    disposition = response["Content-Disposition"]
    assert disposition.startswith("attachment;")
    assert "Mein Handbuch" in disposition
    assert response["Content-Type"].startswith("text/plain")
    assert response["X-Content-Type-Options"] == "nosniff"
    assert b"".join(response.streaming_content) == b"Inhalt 123"


def test_download_reader_allowed(client, anna, ben, coll):
    doc = make_document(coll)
    share_with(coll, ben)
    response = client_for(client, ben).get(reverse("chat:document_download", args=[doc.pk]))
    assert response.status_code == 200
    assert response["Content-Disposition"].startswith("attachment;")


def test_download_filename_safe(coll):
    from multigpt.chat.views_collections import download_filename

    doc = make_document(coll, title='../../etc/pa"ss\nwd')
    name = download_filename(doc)
    assert "/" not in name and '"' not in name and "\n" not in name
    assert name.endswith(".txt")


def test_download_requires_login(client, coll):
    doc = make_document(coll)
    assert client.get(reverse("chat:document_download", args=[doc.pk])).status_code == 302


# --- Abschnittsansicht ----------------------------------------------------------------


def test_chunk_view_escapes_and_shows_page(anna_client, coll):
    doc = make_document(coll, title="<i>Titel</i>")
    make_chunk(doc, text="erster", position=0)
    chunk = make_chunk(doc, text="<script>alert(1)</script> & mehr", page=7, position=1)
    response = anna_client.get(reverse("chat:document_chunk", args=[chunk.pk]))
    html = response.content.decode()
    assert response.status_code == 200
    assert "<script>alert(1)" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; mehr" in html
    assert "<i>Titel</i>" not in html
    assert "Seite 7" in html
    assert "Abschnitt 2 von 2" in html
    assert reverse("chat:document_download", args=[doc.pk]) in html
    assert reverse("chat:collection_detail", args=[coll.pk]) in html
    assert "Vorheriger Abschnitt" in html


def test_chunk_view_unknown_404(anna_client):
    assert anna_client.get(reverse("chat:document_chunk", args=[999999])).status_code == 404


# --- Chat: Auswahl und Quellen --------------------------------------------------------


@pytest.fixture
def ai_model():
    provider = Provider.objects.create(name="Testanbieter", kind=Provider.Kind.OPENAI_COMPAT)
    return AIModel.objects.create(provider=provider, model_id="m-1", display_name="Modell Eins")


def test_chat_has_collection_picker(anna_client, ai_model):
    html = anna_client.get(reverse("chat:index")).content.decode()
    assert 'id="collection-picker"' in html
    assert f'data-api-collections="{reverse("chat:api_collections")}"' in html
    assert "chat/collections_picker.js" in html


def test_document_source_rendered_with_chunk_link(anna_client, anna, coll, ai_model):
    doc = make_document(coll, title="Handbuch <b>X</b>")
    chunk = make_chunk(doc, page=4)
    conv = Conversation.objects.create(user=anna, title="Frage")
    services.append_message(conv, role=Message.Role.USER, content="Wie?")
    answer = services.append_message(
        conv, role=Message.Role.ASSISTANT, content="So [1].", model=ai_model
    )
    SourceRef.objects.create(
        message=answer, kind=SourceRef.Kind.DOCUMENT, title=doc.title, chunk=chunk, page=4
    )
    html = anna_client.get(reverse("chat:conversation", args=[conv.pk])).content.decode()
    assert reverse("chat:document_chunk", args=[chunk.pk]) in html
    assert "S. 4" in html
    assert "<b>X</b>" not in html


def test_picker_only_when_search_ready(anna_client, ai_model, settings):
    settings.DEBUG = False
    settings.RAG_FAKE_EMBEDDINGS = False
    html = anna_client.get(reverse("chat:index")).content.decode()
    assert 'data-search-ready="false"' in html
    html = anna_client.get(reverse("chat:collection_list")).content.decode()
    assert "noch nicht eingerichtet" in html

    settings.DEBUG = True
    settings.RAG_FAKE_EMBEDDINGS = True
    html = anna_client.get(reverse("chat:index")).content.decode()
    assert 'data-search-ready="true"' in html
    html = anna_client.get(reverse("chat:collection_list")).content.decode()
    assert "noch nicht eingerichtet" not in html


def test_pending_retry_note_shown(anna_client, coll):
    doc = make_document(coll, status="pending")
    Document.objects.filter(pk=doc.pk).update(error_text="Versuch 1 fehlgeschlagen: <Zeit>")
    html = anna_client.get(reverse("chat:collection_detail", args=[coll.pk])).content.decode()
    assert 'class="document-note"' in html
    assert "&lt;Zeit&gt;" in html
    data = anna_client.get(reverse("chat:api_collection_documents", args=[coll.pk])).json()
    assert data["documents"][0]["error_text"] == "Versuch 1 fehlgeschlagen: <Zeit>"
