"""RAG-Indexierung (M7, Agent ingest): Typprüfung, Extraktion, OCR-Pfad,
Zerteilung, Upload-Endpunkt, Job-Worker.

Kein echter Anbieteraufruf: ``ingest._embed`` wird gemockt. Der OCR-Pfad
läuft gemockt; ein Integrationstest mit echtem tesseract wird ohne
installiertes tesseract (mit Sprachpaket deu) übersprungen.
"""

import io
import os
import shutil
import signal
import subprocess
import threading
import zipfile
from datetime import timedelta
from pathlib import Path
from unittest import mock

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import connection, transaction
from django.urls import reverse
from django.utils import timezone

from multigpt.accounts.models import Role, User, UserGroup
from multigpt.chat.models import EMBEDDING_DIMENSIONS, Chunk, Collection, Document, Job, Share
from multigpt.chat.rag import chunking, extract, ingest, jobs, upload
from multigpt.chat.rag.extract import Page

DATA = Path(__file__).resolve().parent / "data"
PASSWORD = "Geheim-Test-1234"


def _tesseract_with_deu() -> bool:
    if not (shutil.which("tesseract") and shutil.which("pdftoppm")):
        return False
    out = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True)
    return "deu" in out.stdout.split()


# --- Hilfen ---------------------------------------------------------------------


def fake_vectors(texts):
    return [[float(len(t) % 7 + 1)] + [0.0] * (EMBEDDING_DIMENSIONS - 1) for t in texts]


@pytest.fixture
def media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    return settings.MEDIA_ROOT


@pytest.fixture
def embed():
    with mock.patch.object(ingest, "_embed", side_effect=fake_vectors) as m:
        yield m


def make_user(username, role_key="adult"):
    return User.objects.create_user(
        username, password=PASSWORD, role=Role.objects.get(key=role_key)
    )


def make_document(collection, name="sample.pdf", title=None):
    doc = Document(collection=collection, title=title or name)
    doc.file.save(name, io.BytesIO((DATA / name).read_bytes()), save=False)
    doc.save()
    return doc


def detect(data: bytes, name: str) -> str:
    return extract.detect_kind(io.BytesIO(data), name)


# --- Typprüfung -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("sample.pdf", "pdf"),
        ("scanned.pdf", "pdf"),
        ("sample.docx", "docx"),
        ("sample.txt", "text"),
        ("sample_cp1252.txt", "text"),
        ("sample.md", "text"),
    ],
)
def test_detect_kind_samples(name, kind):
    assert detect((DATA / name).read_bytes(), name) == kind


def test_detect_kind_uppercase_extension():
    assert detect((DATA / "sample.pdf").read_bytes(), "BRIEF.PDF") == "pdf"


@pytest.mark.parametrize(
    ("data", "name", "fragment"),
    [
        (b"Nur Text, kein PDF", "rechnung.pdf", "passt nicht zur Dateiendung"),
        ((DATA / "sample.pdf").read_bytes(), "notiz.txt", "passt nicht zur Dateiendung"),
        ((DATA / "sample.docx").read_bytes(), "x.pdf", "passt nicht zur Dateiendung"),
        (b"MZ\x90\x00\x03\x00\x00\x00", "programm.exe", "nicht unterstützt"),
        (b"\x7fELF\x02\x01\x01\x00\x00\x00", "text.txt", "nicht unterstützt"),
        (b"\x89PNG\r\n\x1a\n\x00\x00", "bild.md", "passt nicht zur Dateiendung"),
        (b"GIF89a\x01\x00\x01\x00", "bild.gif", "nicht unterstützt"),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "alt.docx", ".doc"),
        (b"ohne Endung", "README", "nicht unterstützt"),
    ],
)
def test_detect_kind_rejects(data, name, fragment):
    with pytest.raises(extract.UnsupportedFile) as exc:
        detect(data, name)
    assert fragment in str(exc.value)


def test_detect_kind_zip_without_word_content():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("hallo.txt", "kein Word")
    with pytest.raises(extract.UnsupportedFile):
        detect(buf.getvalue(), "fake.docx")


def test_detect_kind_empty_file():
    with pytest.raises(extract.ExtractionError, match="leer"):
        detect(b"", "leer.txt")


def test_detect_kind_utf8_cut_at_sample_boundary():
    # Ein mehrbytiges Zeichen genau an der Grenze des 64-KB-Ausschnitts.
    data = b"a" * (64 * 1024 - 1) + "ä".encode() + b" Ende"
    assert detect(data, "lang.txt") == "text"


# --- Extraktion je Format ------------------------------------------------------------


def test_extract_pdf_text_layer_and_ocr_for_empty_page():
    calls = []

    def fake_ocr(path, number):
        calls.append(number)
        return "Gescannte Seite: Die Heizung wird im Oktober eingeschaltet."

    pages = extract.extract(str(DATA / "sample.pdf"), "pdf", ocr=fake_ocr)
    assert [p.number for p in pages] == [1, 2, 3]
    assert calls == [2]  # nur die Seite ohne Textebene
    assert "Waschmaschine" in pages[0].text and not pages[0].ocr
    assert "Heizung" in pages[1].text and pages[1].ocr
    assert "Größer" in pages[2].text


def test_extract_scanned_pdf_uses_ocr():
    pages = extract.extract(
        str(DATA / "scanned.pdf"), "pdf", ocr=lambda p, n: "Die Kaltmiete beträgt 850 Euro."
    )
    assert len(pages) == 1 and pages[0].ocr and "Kaltmiete" in pages[0].text


def test_extract_scanned_pdf_without_ocr_installed_is_error():
    def missing(path, number):
        raise extract.OcrUnavailable("tesseract")

    with pytest.raises(extract.ExtractionError, match="tesseract-ocr"):
        extract.extract(str(DATA / "scanned.pdf"), "pdf", ocr=missing)


def test_extract_mixed_pdf_without_ocr_keeps_text_pages():
    def missing(path, number):
        raise extract.OcrUnavailable("tesseract")

    pages = extract.extract(str(DATA / "sample.pdf"), "pdf", ocr=missing)
    assert pages[1].text == "" and "Waschmaschine" in pages[0].text


def test_extract_pdf_stops_between_pages():
    with pytest.raises(extract.Interrupted):
        extract.extract(str(DATA / "sample.pdf"), "pdf", should_stop=lambda: True)


def test_extract_broken_pdf(tmp_path):
    path = tmp_path / "kaputt.pdf"
    path.write_bytes(b"%PDF-1.4\n das ist kein echtes PDF")
    with pytest.raises(extract.ExtractionError, match="beschädigt"):
        extract.extract(str(path), "pdf")


def test_extract_encrypted_pdf(tmp_path):
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(DATA / "sample.pdf"))
    writer.encrypt(user_password="geheim", owner_password="besitzer", algorithm="AES-128")
    path = tmp_path / "geschuetzt.pdf"
    with open(path, "wb") as fh:
        writer.write(fh)
    with pytest.raises(extract.ExtractionError, match="passwortgeschützt"):
        extract.extract(str(path), "pdf", ocr=lambda p, n: "")


def test_extract_docx_paragraphs_and_table():
    (page,) = extract.extract(str(DATA / "sample.docx"), "docx")
    assert page.number is None
    assert "Rezeptsammlung" in page.text
    assert "säuerliche Äpfel" in page.text
    assert "Mehl | 500 g" in page.text
    assert page.text.index("Apfelkuchen") < page.text.index("Mehl") < page.text.index("backen")


@pytest.mark.parametrize("name", ["sample.txt", "sample_cp1252.txt"])
def test_extract_text_encodings(name):
    (page,) = extract.extract(str(DATA / name), "text")
    assert page.number is None and "Käse und Äpfel" in page.text


def test_extract_markdown():
    (page,) = extract.extract(str(DATA / "sample.md"), "text")
    assert page.text.startswith("# Urlaubsplanung") and "Sonnencreme" in page.text


def test_clean_text():
    raw = "Die Verbin-\ndung steht.\r\nEin- und Ausgang\n\n\n\nEnde   mit\t Tabs"
    assert extract.clean_text(raw) == ("Die Verbindung steht.\nEin- und Ausgang\n\nEnde mit Tabs")


# --- OCR-Aufruf (gemockt) und Integration -------------------------------------------


def test_ocr_pdf_page_commands(settings):
    settings.OCR_LANGUAGES = "deu+eng"
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        if cmd[0] == "pdftoppm":
            Path(cmd[-1] + ".png").write_bytes(b"\x89PNG")
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        return subprocess.CompletedProcess(cmd, 0, b"Erkannter Text", b"")

    with mock.patch.object(extract.subprocess, "run", side_effect=fake_run):
        assert extract.ocr_pdf_page("/x/doc.pdf", 4) == "Erkannter Text"
    render, ocr = seen
    assert render[0] == "pdftoppm" and render[render.index("-f") + 1] == "4"
    assert render[render.index("-l") + 1] == "4" and "/x/doc.pdf" in render
    assert ocr[0] == "tesseract" and ocr[ocr.index("-l") + 1] == "deu+eng"
    assert ocr[2] == "-"  # Ausgabe auf stdout


def test_ocr_pdf_page_missing_program():
    with (
        mock.patch.object(extract.subprocess, "run", side_effect=FileNotFoundError),
        pytest.raises(extract.OcrUnavailable),
    ):
        extract.ocr_pdf_page("/x/doc.pdf", 1)


def test_ocr_pdf_page_tesseract_error_gives_empty_text():
    def fake_run(cmd, **kwargs):
        if cmd[0] == "pdftoppm":
            Path(cmd[-1] + ".png").write_bytes(b"\x89PNG")
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        return subprocess.CompletedProcess(cmd, 1, b"", b"Failed loading language 'deu'")

    with mock.patch.object(extract.subprocess, "run", side_effect=fake_run):
        assert extract.ocr_pdf_page("/x/doc.pdf", 1) == ""


@pytest.mark.skipif(not _tesseract_with_deu(), reason="tesseract mit Sprachpaket deu fehlt")
def test_ocr_integration_scanned_pdf():
    pages = extract.extract(str(DATA / "scanned.pdf"), "pdf")
    assert pages[0].ocr
    assert "Kaltmiete" in pages[0].text and "850" in pages[0].text


# --- Zerteilung -----------------------------------------------------------------------


def _words(n, page=None, prefix="wort"):
    return Page(page, " ".join(f"{prefix}{i:04d}" for i in range(n)))


def test_estimate_tokens():
    assert chunking.estimate_tokens("") == 0
    assert chunking.estimate_tokens("ab cd") == 2
    assert chunking.estimate_tokens("Donaudampfschifffahrt") == 6  # 21 Zeichen / 4


def test_split_respects_size_and_overlap():
    # "wort0001" = 8 Zeichen = 2 Tokens
    chunks = chunking.split_pages([_words(1000)], chunk_tokens=100, overlap_tokens=20)
    assert len(chunks) > 1
    assert [c.position for c in chunks] == list(range(len(chunks)))
    for c in chunks:
        assert c.tokens <= 100
        assert chunking.estimate_tokens(c.text) == c.tokens
    for a, b in zip(chunks, chunks[1:], strict=False):
        tail = a.text.split()[-10:]  # 20 Tokens Überlappung = 10 Wörter
        assert b.text.split()[:10] == tail
    # Alles abgedeckt: erstes und letztes Wort vorhanden
    assert chunks[0].text.startswith("wort0000") and chunks[-1].text.endswith("wort0999")


def test_split_without_overlap_covers_text_exactly_once():
    chunks = chunking.split_pages([_words(500)], chunk_tokens=50, overlap_tokens=0)
    words = [w for c in chunks for w in c.text.split()]
    assert words == [f"wort{i:04d}" for i in range(500)]


def test_split_small_text_one_chunk():
    chunks = chunking.split_pages([Page(None, "Kurzer Text.")])
    assert len(chunks) == 1 and chunks[0].text == "Kurzer Text." and chunks[0].page is None


def test_split_page_numbers():
    pages = [_words(40, page=1, prefix="a"), Page(2, ""), _words(40, page=3, prefix="b")]
    # "a0001" = 2 Tokens -> 80 Tokens je Seite
    chunks = chunking.split_pages(pages, chunk_tokens=60, overlap_tokens=10)
    assert {c.page for c in chunks} == {1, 3}
    for c in chunks:
        a_tokens = sum(2 for w in c.text.split() if w.startswith("a"))
        b_tokens = sum(2 for w in c.text.split() if w.startswith("b"))
        assert c.page == (1 if a_tokens >= b_tokens else 3)
    assert chunks[0].page == 1 and chunks[-1].page == 3


def test_split_prefers_sentence_boundary():
    sentence = "Dies ist ein Satz mit einigen Wörtern darin. "
    text = sentence * 40
    chunks = chunking.split_pages([Page(1, text)], chunk_tokens=50, overlap_tokens=0)
    for c in chunks[:-1]:
        assert c.text.endswith("darin.")


def test_split_long_word_is_cut():
    blob = "x" * 5000
    chunks = chunking.split_pages([Page(None, blob)], chunk_tokens=100, overlap_tokens=0)
    assert all(c.tokens <= 100 for c in chunks)
    assert "".join(c.text for c in chunks).replace(" ", "") == blob


def test_split_empty():
    assert chunking.split_pages([Page(1, ""), Page(2, "  ")]) == []


# --- Upload-Endpunkt ------------------------------------------------------------------


@pytest.fixture
def owner(db):
    return make_user("anna")


@pytest.fixture
def collection(owner):
    return Collection.objects.create(owner=owner, name="Haus")


def _url(collection):
    return reverse("chat:api_collection_documents", args=[collection.pk])


def _post(client, collection, name, data=None, content_type="application/octet-stream"):
    data = (DATA / name).read_bytes() if data is None else data
    return client.post(_url(collection), {"file": SimpleUploadedFile(name, data, content_type)})


@pytest.mark.django_db
def test_upload_success_creates_document_and_job(client, owner, collection, media):
    client.force_login(owner)
    response = _post(client, collection, "sample.pdf")
    assert response.status_code == 201, response.content
    body = response.json()
    doc = Document.objects.get(pk=body["id"])
    assert body["title"] == "sample.pdf" and body["status"] == "pending"
    assert doc.collection == collection and doc.status == Document.Status.PENDING
    # Dateiname nicht übernommen: zufälliger Name, nur die Endung bleibt.
    stored = Path(doc.file.name)
    assert stored.parent.as_posix() == f"documents/{owner.pk}"
    assert stored.suffix == ".pdf" and "sample" not in stored.name and len(stored.stem) == 32
    assert (Path(media) / doc.file.name).read_bytes() == (DATA / "sample.pdf").read_bytes()
    job = Job.objects.get()
    assert job.kind == Job.Kind.INDEX_DOCUMENT and job.payload == {"document_id": doc.pk}
    assert job.status == Job.Status.PENDING


@pytest.mark.django_db
def test_upload_title_strips_path_and_control_chars(client, owner, collection, media):
    client.force_login(owner)
    response = _post(client, collection, "x.txt", data=b"Hallo Welt")
    assert response.status_code == 201
    assert upload.title_from_filename("C:\\Users\\a\\Brief\x07 an  Oma.txt") == "Brief an Oma.txt"
    assert upload.title_from_filename("../../etc/passwd.txt") == "passwd.txt"
    assert upload.title_from_filename("") == "Dokument"


@pytest.mark.django_db
def test_upload_wrong_type_despite_pdf_extension(client, owner, collection, media):
    client.force_login(owner)
    response = _post(client, collection, "rechnung.pdf", data=b"<html>kein PDF</html>")
    assert response.status_code == 415
    assert "passt nicht zur Dateiendung" in response.json()["error"]
    assert not Document.objects.exists() and not Job.objects.exists()


@pytest.mark.django_db
def test_upload_unsupported_extension(client, owner, collection, media):
    client.force_login(owner)
    response = _post(client, collection, "bild.gif", data=b"GIF89a\x01\x00\x01\x00")
    assert response.status_code == 415
    assert "Erlaubt sind: PDF, DOCX, TXT, MD, JPG, PNG, TIFF, WEBP" in response.json()["error"]


@pytest.mark.django_db
def test_upload_too_large(client, owner, collection, media, settings):
    settings.DOCUMENT_MAX_UPLOAD_MB = 1
    client.force_login(owner)
    response = _post(client, collection, "gross.txt", data=b"a" * (1024 * 1024 + 1))
    assert response.status_code == 413
    assert "höchstens 1 MB" in response.json()["error"]
    assert not Document.objects.exists()
    assert not any(Path(media).rglob("*.txt")) if Path(media).exists() else True


@pytest.mark.django_db
def test_upload_empty_and_missing_file(client, owner, collection, media):
    client.force_login(owner)
    assert _post(client, collection, "leer.txt", data=b"").status_code == 400
    response = client.post(_url(collection), {})
    assert response.status_code == 400 and response.json()["error"] == "Bitte eine Datei auswählen."


@pytest.mark.django_db
def test_upload_without_upload_right(client, collection, media):
    guest = make_user("gast", "guest")
    collection.owner = guest
    collection.save()
    client.force_login(guest)
    response = _post(client, collection, "sample.txt")
    assert response.status_code == 403
    assert response.json()["error"] == "Du darfst keine Dokumente hochladen."
    assert not Document.objects.exists()


@pytest.mark.django_db
def test_upload_foreign_collection_is_404(client, collection, media):
    other = make_user("bert")
    client.force_login(other)
    response = _post(client, collection, "sample.txt")
    assert response.status_code == 404
    assert client.get(_url(collection)).status_code == 404
    assert not Document.objects.exists()


@pytest.mark.django_db
def test_upload_shared_read_only_and_write(client, collection, media):
    other = make_user("bert")
    group = UserGroup.objects.create(name="Lesegruppe")
    other.groups.add(group)
    share = Share.objects.create(collection=collection, group=group, can_write=False)
    client.force_login(other)
    response = _post(client, collection, "sample.txt")
    assert response.status_code == 403
    assert response.json()["error"] == "Du darfst diese Sammlung nur lesen."
    listing = client.get(_url(collection)).json()
    assert listing["can_write"] is False and listing["can_upload"] is False

    share.can_write = True
    share.save()
    assert _post(client, collection, "sample.txt").status_code == 201


@pytest.mark.django_db
def test_upload_requires_login(client, collection, media):
    assert _post(client, collection, "sample.txt").status_code == 403


@pytest.mark.django_db
def test_documents_list(client, owner, collection, media):
    client.force_login(owner)
    _post(client, collection, "sample.md")
    body = client.get(_url(collection)).json()
    assert body["can_write"] is True and body["can_upload"] is True
    assert body["max_upload_mb"] == 25 and ".pdf" in body["allowed_extensions"]
    (doc,) = body["documents"]
    assert doc["title"] == "sample.md" and doc["status"] == "pending"


# --- Indexierung und Worker ------------------------------------------------------------


@pytest.mark.django_db
def test_index_document_stores_chunks_with_pages(collection, media, embed):
    doc = make_document(collection, "sample.pdf")
    with mock.patch.object(extract, "ocr_pdf_page", return_value="Seite zwei per OCR erkannt."):
        result = ingest.index_document(doc)
    doc.refresh_from_db()
    assert doc.status == Document.Status.INDEXED and doc.error_text == ""
    chunks = list(Chunk.objects.filter(document=doc).order_by("position"))
    assert result.chunks == len(chunks) >= 1 and result.ocr_pages == 1
    assert chunks[0].page == 1 and "Waschmaschine" in chunks[0].text
    assert len(chunks[0].embedding) == EMBEDDING_DIMENSIONS
    embed.assert_called()


@pytest.mark.django_db
def test_reindex_replaces_chunks(collection, media, embed):
    doc = make_document(collection, "sample.txt")
    ingest.index_document(doc)
    ingest.index_document(doc)
    assert Chunk.objects.filter(document=doc).count() == 1


def _claimed_job(doc):
    jobs.enqueue_index(doc)
    job = jobs.claim_next()
    assert job is not None
    return job


@pytest.mark.django_db
def test_claim_next_marks_running_and_skips_future(collection, media):
    doc = make_document(collection, "sample.txt")
    job = jobs.enqueue_index(doc)
    Job.objects.filter(pk=job.pk).update(run_after=timezone.now() + timedelta(minutes=5))
    assert jobs.claim_next() is None
    Job.objects.filter(pk=job.pk).update(run_after=timezone.now())
    claimed = jobs.claim_next()
    assert claimed.pk == job.pk
    claimed.refresh_from_db()
    assert claimed.status == Job.Status.RUNNING and claimed.attempts == 1
    assert claimed.locked_at is not None
    assert jobs.claim_next() is None


@pytest.mark.django_db
def test_enqueue_deduplicates_pending(collection, media):
    doc = make_document(collection, "sample.txt")
    first = jobs.enqueue_index(doc)
    second = jobs.enqueue_index(doc)
    assert first.pk == second.pk and Job.objects.count() == 1


@pytest.mark.django_db
def test_run_job_success(collection, media, embed):
    doc = make_document(collection, "sample.docx")
    job = _claimed_job(doc)
    assert jobs.run_job(job) == Job.Status.DONE
    job.refresh_from_db()
    doc.refresh_from_db()
    assert job.status == Job.Status.DONE and job.locked_at is None
    assert doc.status == Document.Status.INDEXED
    assert Chunk.objects.filter(document=doc, page__isnull=True).exists()


@pytest.mark.django_db
def test_run_job_retry_with_backoff_then_fail(collection, media, settings):
    from multigpt.chat.rag.embeddings import EmbeddingError

    settings.JOB_MAX_ATTEMPTS = 3
    doc = make_document(collection, "sample.txt")
    error = EmbeddingError("Der Anbieter ist überlastet (HTTP 429).", retryable=True)
    with mock.patch.object(ingest, "_embed", side_effect=error):
        for attempt in (1, 2):
            if attempt == 1:
                job = _claimed_job(doc)
            else:
                Job.objects.filter(pk=job.pk).update(run_after=timezone.now())
                job = jobs.claim_next()
            before = timezone.now()
            assert jobs.run_job(job) == Job.Status.PENDING
            job.refresh_from_db()
            doc.refresh_from_db()
            assert job.attempts == attempt
            assert "HTTP 429" in job.last_error
            expected = jobs.backoff(attempt)
            assert before + expected <= job.run_after <= timezone.now() + expected
            assert doc.status == Document.Status.PENDING
            assert f"Versuch {attempt} fehlgeschlagen" in doc.error_text
        assert jobs.claim_next() is None  # Backoff: noch nicht fällig
        Job.objects.filter(pk=job.pk).update(run_after=timezone.now())
        job = jobs.claim_next()
        assert jobs.run_job(job) == Job.Status.FAILED
    job.refresh_from_db()
    doc.refresh_from_db()
    assert job.attempts == 3 and job.status == Job.Status.FAILED
    assert doc.status == Document.Status.ERROR
    assert doc.error_text == "Der Anbieter ist überlastet (HTTP 429)."
    assert not Chunk.objects.filter(document=doc).exists()


def test_backoff_grows_and_is_capped():
    assert jobs.backoff(1) == timedelta(seconds=30)
    assert jobs.backoff(2) == timedelta(minutes=2)
    assert jobs.backoff(3) == timedelta(minutes=8)
    assert jobs.backoff(20) == timedelta(hours=1)


@pytest.mark.django_db
def test_run_job_permanent_error_no_retry(collection, media, embed):
    doc = make_document(collection, "sample.txt")
    doc.file.save("leer.txt", io.BytesIO(b"   \n\n  "), save=True)
    job = _claimed_job(doc)
    assert jobs.run_job(job) == Job.Status.FAILED
    doc.refresh_from_db()
    assert doc.status == Document.Status.ERROR
    assert doc.error_text == "Im Dokument wurde kein Text gefunden."
    embed.assert_not_called()


@pytest.mark.django_db
def test_run_job_not_configured_is_permanent(collection, media):
    from multigpt.chat.rag.embeddings import EmbeddingNotConfigured

    doc = make_document(collection, "sample.txt")
    job = _claimed_job(doc)
    with mock.patch.object(ingest, "_embed", side_effect=EmbeddingNotConfigured()):
        assert jobs.run_job(job) == Job.Status.FAILED
    doc.refresh_from_db()
    assert doc.status == Document.Status.ERROR and "Embedding" in doc.error_text


@pytest.mark.django_db
def test_run_job_unexpected_error_is_retried_without_details(collection, media, caplog):
    doc = make_document(collection, "sample.txt")
    job = _claimed_job(doc)
    secret = "GEHEIMER-INHALT"
    with mock.patch.object(ingest, "_embed", side_effect=RuntimeError(secret)):
        assert jobs.run_job(job) == Job.Status.PENDING
    job.refresh_from_db()
    assert job.last_error == jobs.GENERIC_ERROR
    assert secret not in caplog.text and "RuntimeError" in caplog.text


@pytest.mark.django_db
def test_run_job_interrupted_is_requeued_without_counting(collection, media, embed):
    doc = make_document(collection, "sample.pdf")
    job = _claimed_job(doc)
    assert jobs.run_job(job, should_stop=lambda: True) == Job.Status.PENDING
    job.refresh_from_db()
    assert job.attempts == 0 and job.locked_at is None and job.run_after <= timezone.now()
    doc.refresh_from_db()
    assert doc.status == Document.Status.PENDING


@pytest.mark.django_db
def test_run_job_document_deleted(collection, media, embed):
    doc = make_document(collection, "sample.txt")
    job = _claimed_job(doc)
    doc.delete()
    assert jobs.run_job(job) == Job.Status.DONE


@pytest.mark.django_db
def test_requeue_stale(collection, media):
    doc = make_document(collection, "sample.txt")
    job = _claimed_job(doc)
    Job.objects.filter(pk=job.pk).update(locked_at=timezone.now() - timedelta(hours=1))
    assert jobs.requeue_stale() == 1
    job.refresh_from_db()
    assert job.status == Job.Status.PENDING and "unterbrochen" in job.last_error


@pytest.mark.django_db
def test_logs_contain_no_content_or_filenames(collection, media, embed, caplog):
    caplog.set_level("DEBUG", logger="multigpt")
    doc = make_document(collection, "sample.txt", title="Geheime Einkaufsliste")
    jobs.enqueue_index(doc)
    call_command("run_worker", "--once")
    doc.refresh_from_db()
    assert doc.status == Document.Status.INDEXED
    assert "Einkaufsliste" not in caplog.text and "Käse" not in caplog.text
    assert f"Dokument {doc.pk} indexiert" in caplog.text


@pytest.mark.django_db
def test_run_worker_once_processes_all_due_jobs(collection, media, embed):
    docs = [make_document(collection, n) for n in ("sample.txt", "sample.md", "sample.docx")]
    for d in docs:
        jobs.enqueue_index(d)
    call_command("run_worker", "--once")
    assert set(Document.objects.values_list("status", flat=True)) == {Document.Status.INDEXED}
    assert set(Job.objects.values_list("status", flat=True)) == {Job.Status.DONE}


@pytest.mark.django_db
def test_run_worker_stops_on_sigterm(collection, media):
    calls = []

    def fake_work_once(should_stop):
        calls.append(should_stop())
        os.kill(os.getpid(), signal.SIGTERM)
        return None

    with mock.patch.object(jobs, "work_once", side_effect=fake_work_once):
        call_command("run_worker", "--poll", "30")  # ohne --once: endet nur per Signal
    assert calls == [False]
    assert signal.getsignal(signal.SIGTERM) is not None


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_two_workers_never_claim_same_job(media):
    owner = User.objects.create_user("anna", password=PASSWORD)
    collection = Collection.objects.create(owner=owner, name="Haus")
    job_a = jobs.enqueue_index(make_document(collection, "sample.txt"))
    job_b = jobs.enqueue_index(make_document(collection, "sample.md"))

    locked = threading.Event()
    release = threading.Event()
    seen = {}

    def other_worker():
        try:
            with transaction.atomic():
                job = (
                    Job.objects.select_for_update(skip_locked=True)
                    .filter(status=Job.Status.PENDING)
                    .order_by("run_after", "id")
                    .first()
                )
                seen["other"] = job.pk
                locked.set()
                release.wait(10)
        finally:
            connection.close()

    thread = threading.Thread(target=other_worker)
    thread.start()
    assert locked.wait(10)
    try:
        mine = jobs.claim_next()  # die Zeile des anderen Workers ist gesperrt
        assert mine is not None and mine.pk != seen["other"]
        assert {mine.pk, seen["other"]} == {job_a.pk, job_b.pk}
        assert jobs.claim_next() is None
    finally:
        release.set()
        thread.join(10)


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_concurrent_claims_are_unique(media):
    owner = User.objects.create_user("anna", password=PASSWORD)
    collection = Collection.objects.create(owner=owner, name="Haus")
    doc = make_document(collection, "sample.txt")
    for _ in range(12):
        Job.objects.create(kind=Job.Kind.INDEX_DOCUMENT, payload={"document_id": doc.pk})
    claimed: list[int] = []
    lock = threading.Lock()

    def worker():
        try:
            while (job := jobs.claim_next()) is not None:
                with lock:
                    claimed.append(job.pk)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert len(claimed) == 12 and len(set(claimed)) == 12
