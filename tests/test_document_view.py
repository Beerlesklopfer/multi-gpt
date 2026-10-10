"""„Dokument ansehen“: Originaldatei im Browser (PDF mit Seitensprung, Text, Bilder)."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from multigpt.chat.models import Collection, Document
from tests.test_collections_ui import client_for, make_chunk, make_user

pytestmark = pytest.mark.django_db

PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"


@pytest.fixture
def anna():
    return make_user("anna")


@pytest.fixture
def coll(anna):
    return Collection.objects.create(owner=anna, name="Anleitungen")


def make_doc(coll, name, content, title=None):
    return Document.objects.create(
        collection=coll,
        title=title or name,
        file=SimpleUploadedFile(name, content),
        status="indexed",
    )


def view(client, doc):
    return client.get(reverse("chat:document_view", args=[doc.pk]))


def test_pdf_inline(client, anna, coll):
    doc = make_doc(coll, "spurgeon.pdf", PDF, title="Auf Dein Wort.pdf")
    response = view(client_for(client, anna), doc)
    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert response["Content-Disposition"].startswith("inline;")
    assert "Auf Dein Wort.pdf" in response["Content-Disposition"]
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["Cache-Control"] == "private, no-store"
    # Die PDF-Betrachter der Browser verweigern sandboxed PDFs.
    assert "Content-Security-Policy" not in response
    assert b"".join(response.streaming_content) == PDF


@pytest.mark.parametrize("name", ["notiz.txt", "notiz.md"])
def test_text_inline_sandboxed(client, anna, coll, name):
    doc = make_doc(coll, name, b"<script>alert(1)</script>")
    response = view(client_for(client, anna), doc)
    assert response["Content-Type"] == "text/plain; charset=utf-8"
    assert response["Content-Disposition"].startswith("inline;")
    assert "sandbox" in response["Content-Security-Policy"]


def test_docx_falls_back_to_download(client, anna, coll):
    doc = make_doc(coll, "brief.docx", b"PK\x03\x04dummy")
    response = view(client_for(client, anna), doc)
    assert response["Content-Disposition"].startswith("attachment;")


def test_download_still_attachment(client, anna, coll):
    doc = make_doc(coll, "spurgeon.pdf", PDF)
    response = client_for(client, anna).get(reverse("chat:document_download", args=[doc.pk]))
    assert response["Content-Disposition"].startswith("attachment;")


def test_foreign_document_404(client, anna, coll):
    doc = make_doc(coll, "spurgeon.pdf", PDF)
    ben = make_user("ben")
    assert view(client_for(client, ben), doc).status_code == 404


def test_requires_login(client, coll):
    doc = make_doc(coll, "spurgeon.pdf", PDF)
    assert view(client, doc).status_code == 302


def test_chunk_page_links_to_pdf_page(client, anna, coll):
    doc = make_doc(coll, "spurgeon.pdf", PDF)
    chunk = make_chunk(doc, page=380)
    html = client_for(client, anna).get(reverse("chat:document_chunk", args=[chunk.pk]))
    url = reverse("chat:document_view", args=[doc.pk])
    assert f'href="{url}#page=380"' in html.content.decode()
    assert "Dokument ansehen (S. 380)" in html.content.decode()


def test_chunk_page_without_view_for_docx(client, anna, coll):
    doc = make_doc(coll, "brief.docx", b"PK\x03\x04dummy")
    chunk = make_chunk(doc, page=None)
    html = client_for(client, anna).get(reverse("chat:document_chunk", args=[chunk.pk]))
    assert "Dokument ansehen" not in html.content.decode()
    assert "Dokument herunterladen" in html.content.decode()


def test_collection_list_and_api_offer_view(client, anna, coll):
    pdf = make_doc(coll, "spurgeon.pdf", PDF)
    docx = make_doc(coll, "brief.docx", b"PK\x03\x04dummy")
    c = client_for(client, anna)
    html = c.get(reverse("chat:collection_detail", args=[coll.pk])).content.decode()
    assert reverse("chat:document_view", args=[pdf.pk]) in html
    assert reverse("chat:document_view", args=[docx.pk]) not in html
    data = c.get(reverse("chat:api_collection_documents", args=[coll.pk])).json()
    urls = {d["id"]: d["view_url"] for d in data["documents"]}
    assert urls[pdf.pk] == reverse("chat:document_view", args=[pdf.pk])
    assert urls[docx.pk] == ""
