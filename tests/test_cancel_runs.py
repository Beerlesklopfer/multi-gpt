"""Abbrechen von Aufträgen und Läufen (IndexRun): Admin, Worker, Nutzerseite,
Verzeichnisquellen."""

import io
import shutil
from datetime import timedelta
from pathlib import Path
from unittest import mock

import pytest
from django.urls import reverse
from django.utils import timezone

from multigpt.accounts.models import Role, User, UserGroup
from multigpt.chat.models import (
    EMBEDDING_DIMENSIONS,
    Chunk,
    Collection,
    Document,
    IndexRun,
    Job,
    Share,
)
from multigpt.chat.rag import ingest, jobs
from multigpt.rag import crawl, services
from multigpt.rag.models import DirectorySource

pytestmark = pytest.mark.django_db

DATA = Path(__file__).resolve().parent / "data"
PASSWORD = "Geheim-Test-1234"


def fake_vectors(texts):
    return [[0.1] + [0.0] * (EMBEDDING_DIMENSIONS - 1) for _ in texts]


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"


@pytest.fixture(autouse=True)
def _check_always(monkeypatch):
    # An jeder Prüfstelle nach dem Abbruch fragen (sonst höchstens alle 5 s).
    monkeypatch.setattr(jobs, "CANCEL_CHECK_EVERY", 0)


@pytest.fixture
def embed():
    with mock.patch.object(ingest, "_embed", side_effect=fake_vectors) as m:
        yield m


def make_user(username, role_key="adult"):
    return User.objects.create_user(
        username, password=PASSWORD, role=Role.objects.get(key=role_key)
    )


@pytest.fixture
def anna():
    return make_user("anna")


@pytest.fixture
def coll(anna):
    return Collection.objects.create(owner=anna, name="Haus")


@pytest.fixture
def admin_client(client):
    user = User.objects.create_superuser(
        username="jo", password=PASSWORD, email="jo@example.invalid"
    )
    client.force_login(user)
    return client


def make_document(collection, name="sample.txt", title=None):
    doc = Document(collection=collection, title=title or name)
    doc.file.save(name, io.BytesIO((DATA / name).read_bytes()), save=False)
    doc.save()
    return doc


def long_document(collection):
    sentence = "Die Miete wird monatlich gezahlt. "
    text = "\n\n".join(f"Absatz {i}: " + sentence * 40 for i in range(20))
    doc = Document(collection=collection, title="lang.txt")
    doc.file.save("lang.txt", io.BytesIO(text.encode()), save=False)
    doc.save()
    return doc


def claimed(doc, run=None):
    jobs.enqueue_index(doc, run)
    job = jobs.claim_next()
    assert job is not None
    return job


def request_cancel(job):
    Job.objects.filter(pk=job.pk).update(cancel_requested=True)


def action(client, model_name, name, ids):
    url = reverse(f"admin:rag_{model_name}_changelist")
    data = {"action": name, "_selected_action": [str(i) for i in ids]}
    return client.post(url, data, follow=True)


# --- Einzelne Aufträge ---------------------------------------------------------------


def test_pending_job_removed_immediately(coll):
    doc = make_document(coll)
    job = jobs.enqueue_index(doc)
    assert jobs.cancel_jobs([job.pk]) == (1, 0)
    assert not Job.objects.exists()
    doc.refresh_from_db()
    assert doc.status == Document.Status.ERROR
    assert doc.error_text == jobs.CANCELLED_DOCUMENT


def test_running_job_marked_then_worker_stops_between_embedding_batches(coll, monkeypatch):
    monkeypatch.setattr(ingest, "EMBED_BATCH", 1)
    doc = long_document(coll)
    job = claimed(doc)
    assert jobs.cancel_jobs([job.pk]) == (0, 1)
    job.refresh_from_db()
    assert job.cancel_requested and job.status == Job.Status.RUNNING
    Job.objects.filter(pk=job.pk).update(cancel_requested=False)

    calls = []

    def embed_then_cancel(texts):
        calls.append(len(texts))
        if len(calls) == 1:
            request_cancel(job)  # Abbruch kommt während des ersten Pakets
        return fake_vectors(texts)

    with mock.patch.object(ingest, "_embed", side_effect=embed_then_cancel):
        assert jobs.run_job(job) == "cancelled"
    assert calls == [1]  # beim nächsten Paket beendet
    assert not Job.objects.filter(pk=job.pk).exists()
    doc.refresh_from_db()
    assert doc.status == Document.Status.ERROR
    assert doc.error_text == "Indexierung abgebrochen."
    assert not Chunk.objects.filter(document=doc).exists()


def test_cancel_between_ocr_pages_keeps_old_chunks_untouched(coll, embed, caplog):
    caplog.set_level("INFO", logger="multigpt")
    doc = make_document(coll, "sample.pdf", title="Geheimer Titel")
    Chunk.objects.create(
        document=doc, position=0, text="alt", page=1, embedding=[0.2] * EMBEDDING_DIMENSIONS
    )
    job = claimed(doc)
    pages = []

    def reader():
        def read(path, number):
            pages.append(number)
            request_cancel(job)  # während der OCR von Seite 2
            return "Text aus der OCR " * 20

        return read

    with mock.patch.object(ingest, "_page_reader", side_effect=reader):
        assert jobs.run_job(job) == "cancelled"
    assert pages == [2]  # Seite 3 wurde nicht mehr gelesen
    embed.assert_not_called()
    # Keine halben Abschnitte: die alten bleiben unverändert.
    assert list(Chunk.objects.filter(document=doc).values_list("text", flat=True)) == ["alt"]
    doc.refresh_from_db()
    assert doc.status == Document.Status.ERROR and doc.error_text == jobs.CANCELLED_DOCUMENT
    assert f"Job {job.pk} abgebrochen" in caplog.text
    assert "Geheimer Titel" not in caplog.text


def test_sigterm_still_requeues_without_cancel(coll, embed):
    doc = make_document(coll, "sample.pdf")
    job = claimed(doc)
    assert jobs.run_job(job, should_stop=lambda: True) == Job.Status.PENDING
    job.refresh_from_db()
    assert job.status == Job.Status.PENDING and not job.cancel_requested


def test_stale_job_removed_immediately(coll):
    doc = make_document(coll)
    job = claimed(doc)
    Job.objects.filter(pk=job.pk).update(locked_at=timezone.now() - timedelta(hours=1))
    assert jobs.cancel_jobs([job.pk]) == (1, 0)
    assert not Job.objects.exists()
    doc.refresh_from_db()
    assert doc.status == Document.Status.ERROR


def test_worker_finding_job_gone_stops_cleanly(coll, embed):
    # Hängend entfernt, der Worker lebte aber noch: Er beendet sich an der Prüfstelle.
    doc = make_document(coll, "sample.pdf")
    job = claimed(doc)
    Job.objects.filter(pk=job.pk).delete()
    assert jobs.run_job(job) == "cancelled"
    assert not Chunk.objects.exists()


def test_requeue_stale_finishes_requested_cancel(coll):
    doc = make_document(coll)
    job = claimed(doc)
    Job.objects.filter(pk=job.pk).update(
        cancel_requested=True, locked_at=timezone.now() - timedelta(hours=1)
    )
    assert jobs.requeue_stale() == 1
    assert not Job.objects.exists()
    doc.refresh_from_db()
    assert doc.error_text == jobs.CANCELLED_DOCUMENT


def test_admin_cancel_action_and_button(admin_client, coll):
    waiting_doc = make_document(coll, title="A")
    running_doc = make_document(coll, "sample.md", title="B")
    running = claimed(running_doc)
    waiting = jobs.enqueue_index(waiting_doc)
    response = action(admin_client, "jobproxy", "cancel_action", [waiting.pk, running.pk])
    html = response.content.decode()
    assert "1 Auftrag abgebrochen und entfernt" in html
    assert "nach dem aktuellen Schritt beendet" in html
    assert not Job.objects.filter(pk=waiting.pk).exists()
    running.refresh_from_db()
    assert running.cancel_requested
    detail = admin_client.get(reverse("admin:rag_jobproxy_change", args=[running.pk]))
    assert "wird abgebrochen" in detail.content.decode()
    overview = admin_client.get(reverse("admin:rag_ragoverview_changelist")).content.decode()
    assert "davon wird abgebrochen" in overview

    # Knopf auf der Detailseite (POST), hier für einen hängenden Auftrag.
    Job.objects.filter(pk=running.pk).update(locked_at=timezone.now() - timedelta(hours=1))
    url = reverse("admin:rag_jobproxy_cancel", args=[running.pk])
    assert admin_client.get(url).status_code == 405
    admin_client.post(url)
    assert not Job.objects.filter(pk=running.pk).exists()


# --- Nutzerseite --------------------------------------------------------------------


def cancel_url(doc):
    return reverse("chat:api_document_cancel", args=[doc.pk])


def test_user_cancel_rights(client, coll, anna):
    doc = make_document(coll)
    jobs.enqueue_index(doc)
    reader, stranger = make_user("lea", "teen"), make_user("max")
    group = UserGroup.objects.create(name="Kinder")
    reader.groups.add(group)
    Share.objects.create(collection=coll, group=group, can_write=False)

    client.force_login(stranger)
    assert client.post(cancel_url(doc)).status_code == 404
    client.force_login(reader)
    assert client.post(cancel_url(doc)).status_code == 403
    assert Job.objects.count() == 1

    client.force_login(anna)
    response = client.post(cancel_url(doc))
    assert response.status_code == 200
    data = response.json()
    assert data["cancel"] == "cancelled" and data["status"] == "error"
    assert data["error_text"] == jobs.CANCELLED_DOCUMENT
    assert not Job.objects.exists()
    # Nicht mehr in Arbeit -> 400.
    assert client.post(cancel_url(doc)).status_code == 400


def test_user_cancel_running_and_page(client, coll, anna):
    doc = make_document(coll)
    job = claimed(doc)
    client.force_login(anna)
    page = client.get(reverse("chat:collection_detail", args=[coll.pk])).content.decode()
    assert f'data-document-cancel="{doc.pk}"' in page
    data = client.post(cancel_url(doc)).json()
    assert data["cancel"] == "cancelling" and data["cancelling"] is True
    job.refresh_from_db()
    assert job.cancel_requested
    listing = client.get(reverse("chat:api_collection_documents", args=[coll.pk])).json()
    assert listing["documents"][0]["cancelling"] is True
    page = client.get(reverse("chat:collection_detail", args=[coll.pk])).content.decode()
    assert "Wird abgebrochen" in page


def test_delete_during_indexing_cancels_job(client, coll, anna, embed):
    doc = make_document(coll, "sample.pdf")
    job = claimed(doc)
    client.force_login(anna)
    deleted = []

    def reader():
        def read(path, number):
            # Während der OCR von Seite 2 löscht die Besitzerin das Dokument.
            response = client.delete(reverse("chat:api_document_detail", args=[doc.pk]))
            deleted.append(response.status_code)
            assert Job.objects.get(pk=job.pk).cancel_requested
            return ""

        return read

    with mock.patch.object(ingest, "_page_reader", side_effect=reader):
        assert jobs.run_job(job) == "cancelled"
    assert deleted == [200]
    assert not Job.objects.exists() and not Chunk.objects.exists()
    embed.assert_not_called()

    # Wartender Auftrag wird beim Löschen entfernt.
    other = make_document(coll)
    jobs.enqueue_index(other)
    client.delete(reverse("chat:api_document_detail", args=[other.pk]))
    assert not Job.objects.exists()


# --- Läufe: Neuindexierung und Upload ---------------------------------------------------


def test_reindex_collection_run_counts_and_finishes(coll, embed):
    docs = [make_document(coll, n) for n in ("sample.txt", "sample.md")]
    assert services.reindex_collection(coll) == 2
    run = IndexRun.objects.get()
    assert run.kind == IndexRun.Kind.REINDEX_COLLECTION and run.docs_queued == 2
    assert set(run.jobs.values_list("payload__document_id", flat=True)) == {d.pk for d in docs}
    jobs.work_once()
    run.refresh_from_db()
    assert run.status == IndexRun.Status.RUNNING and run.docs_done == 1
    assert "1 von 2 Dokumenten indexiert" in run.progress_text()
    jobs.work_once()
    run.refresh_from_db()
    assert run.status == IndexRun.Status.FINISHED and run.docs_done == 2
    assert run.finished is not None


def test_run_counts_errors(coll, embed):
    doc = make_document(coll)
    doc.file.save("leer.txt", io.BytesIO(b"   "), save=True)
    services.reindex_all()
    jobs.work_once()
    run = IndexRun.objects.get()
    assert run.kind == IndexRun.Kind.REINDEX_ALL
    assert run.status == IndexRun.Status.FINISHED and run.docs_failed == 1
    assert "1 Fehler" in run.progress_text()


def test_empty_reindex_run_finishes_immediately(coll):
    services.reindex_collection(coll)
    assert IndexRun.objects.get().status == IndexRun.Status.FINISHED


def test_cancel_run_during_indexing_keeps_finished_documents(admin_client, coll, embed):
    docs = [make_document(coll, n) for n in ("sample.txt", "sample.md", "sample.docx")]
    services.reindex_collection(coll)
    run = IndexRun.objects.get()
    jobs.work_once()  # erstes Dokument fertig
    running = jobs.claim_next()  # zweites läuft gerade
    response = action(admin_client, "indexrunproxy", "cancel_action", [run.pk])
    assert "nach dem aktuellen Schritt beendet" in response.content.decode()
    run.refresh_from_db()
    assert run.status == IndexRun.Status.CANCELLING
    # Wartender Auftrag des dritten Dokuments ist weg, der laufende markiert.
    assert list(run.jobs.filter(status__in=jobs.OPEN).values_list("pk", flat=True)) == [running.pk]
    assert jobs.run_job(running) == "cancelled"
    run.refresh_from_db()
    assert run.status == IndexRun.Status.CANCELLED and run.finished is not None
    assert (run.docs_done, run.docs_cancelled) == (1, 2)
    states = {d.pk: Document.objects.get(pk=d.pk).status for d in docs}
    assert states[docs[0].pk] == Document.Status.INDEXED
    assert states[docs[1].pk] == states[docs[2].pk] == Document.Status.ERROR
    assert Chunk.objects.filter(document=docs[0]).exists()
    # Zähler eingefroren: weitere Meldungen ändern nichts.
    jobs.job_ended(running, "done")
    run.refresh_from_db()
    assert run.docs_done == 1
    # Detailseite ohne Knopf, Liste zeigt den Lauf.
    detail = admin_client.get(reverse("admin:rag_indexrunproxy_change", args=[run.pk]))
    assert "Lauf abbrechen" not in detail.content.decode()
    listing = admin_client.get(reverse("admin:rag_indexrunproxy_changelist"))
    assert "abgebrochen" in listing.content.decode()


def test_upload_creates_run_and_user_cancel_ends_it(client, coll, anna):
    client.force_login(anna)
    url = reverse("chat:api_collection_documents", args=[coll.pk])
    with open(DATA / "sample.txt", "rb") as fh:
        response = client.post(url, {"file": fh})
    assert response.status_code == 201
    run = IndexRun.objects.get()
    assert run.kind == IndexRun.Kind.UPLOAD and run.started_by == anna
    assert client.get(url).json()["runs"][0]["id"] == run.pk
    client.post(reverse("chat:api_document_cancel", args=[response.json()["id"]]))
    run.refresh_from_db()
    assert run.status == IndexRun.Status.CANCELLED and run.docs_cancelled == 1


# --- Läufe: Verzeichnisquellen --------------------------------------------------------


@pytest.fixture
def docs_dir(settings, tmp_path):
    root = tmp_path / "nas"
    (root / "docs").mkdir(parents=True)
    settings.RAG_SOURCE_ROOTS = [str(root)]
    return root / "docs"


def put(directory, name, sample="sample.txt"):
    shutil.copyfile(DATA / sample, directory / name)


def test_scan_run_counts_and_finishes(docs_dir, coll, embed):
    for i in range(3):
        put(docs_dir, f"d{i}.txt")
    source = DirectorySource.objects.create(collection=coll, path=str(docs_dir))
    scan = crawl.enqueue_scan(source)
    run = scan.run
    assert run.kind == IndexRun.Kind.DIRECTORY_SCAN and run.source == source
    assert run.started_by is None  # automatisch
    jobs.work_once()  # Scan
    run.refresh_from_db()
    assert (run.files_found, run.files_checked, run.files_new, run.docs_queued) == (3, 3, 3, 3)
    assert run.status == IndexRun.Status.RUNNING
    assert set(Job.objects.values_list("run_id", flat=True)) == {run.pk}
    for _ in range(3):
        jobs.work_once()
    run.refresh_from_db()
    assert run.status == IndexRun.Status.FINISHED and run.docs_done == 3


def test_no_second_run_while_one_is_open(docs_dir, coll, embed):
    put(docs_dir, "a.txt")
    source = DirectorySource.objects.create(collection=coll, path=str(docs_dir))
    assert crawl.enqueue_scan(source) is not None
    jobs.work_once()  # Scan fertig, Indexierung steht noch aus
    assert crawl.enqueue_scan(source) is None
    now = timezone.now() + timedelta(days=1)
    assert crawl.enqueue_due_scans(now) == 0
    jobs.work_once()  # Indexierung fertig -> Lauf beendet
    assert crawl.enqueue_due_scans(now) == 1
    assert IndexRun.objects.count() == 2


def test_open_run_without_jobs_is_closed_before_next_scan(docs_dir, coll):
    source = DirectorySource.objects.create(collection=coll, path=str(docs_dir))
    crawl.enqueue_scan(source)
    Job.objects.all().delete()  # z. B. von Hand gelöscht
    assert crawl.enqueue_scan(source) is not None


def test_cancel_scan_midway_deletes_nothing(admin_client, docs_dir, coll, embed):
    for i in range(6):
        put(docs_dir, f"d{i}.txt")
    source = DirectorySource.objects.create(collection=coll, path=str(docs_dir))
    crawl.run_scan(source)  # erster Lauf ohne Lauf-Objekt: 6 Dokumente
    for doc in Document.objects.filter(source=source):
        doc.status = Document.Status.INDEXED
        doc.save()
    Job.objects.all().delete()
    # Jetzt sind 4 Dateien „verschwunden“ (z. B. Freigabe halb eingehängt) …
    for i in range(2, 6):
        (docs_dir / f"d{i}.txt").unlink()
    put(docs_dir, "neu.txt")
    scan = crawl.enqueue_scan(source)
    claimed_scan = jobs.claim_next()
    assert claimed_scan.pk == scan.pk
    checked = []
    original = crawl._hash_and_check

    def hash_then_cancel(fh, name):
        checked.append(name)
        # … und der Verwalter bricht beim ersten geänderten/neuen Eintrag ab.
        action(admin_client, "directorysource", "cancel_scan_action", [source.pk])
        return original(fh, name)

    with mock.patch.object(crawl, "_hash_and_check", side_effect=hash_then_cancel):
        assert jobs.run_job(claimed_scan) == "cancelled"
    assert len(checked) == 1
    # Nichts gelöscht, obwohl Dateien fehlen; Dateien unberührt.
    assert Document.objects.filter(source=source).count() >= 6
    assert sorted(p.name for p in docs_dir.iterdir()) == ["d0.txt", "d1.txt", "neu.txt"]
    run = IndexRun.objects.get()
    assert run.status == IndexRun.Status.CANCELLED
    assert run.files_deleted == 0 and run.files_found == 3
    assert not Job.objects.filter(status__in=jobs.OPEN).exists()
    source.refresh_from_db()
    assert source.last_error == "Einlesen abgebrochen."
    # Nach dem Abbruch ist ein neuer Lauf möglich.
    assert crawl.enqueue_scan(source) is not None


def test_cancel_scan_after_last_file_deletes_nothing(docs_dir, coll):
    put(docs_dir, "a.txt")
    put(docs_dir, "b.txt")
    source = DirectorySource.objects.create(collection=coll, path=str(docs_dir))
    crawl.run_scan(source)
    (docs_dir / "b.txt").unlink()
    run = IndexRun.objects.create(kind=IndexRun.Kind.DIRECTORY_SCAN, source=source)
    calls = []

    def stop():
        calls.append(1)
        return len(calls) > 1  # erst nach der letzten Datei

    with pytest.raises(crawl.extract.Interrupted):
        crawl.scan_source(source, stop, run=run)
    assert Document.objects.filter(source=source).count() == 2


def test_cancel_scan_pending_and_source_display(admin_client, docs_dir, coll):
    source = DirectorySource.objects.create(collection=coll, path=str(docs_dir))
    crawl.enqueue_scan(source)
    page = admin_client.get(reverse("admin:rag_directorysource_changelist")).content.decode()
    assert "wird eingelesen" in page and "Einlesen abbrechen" in page
    overview = admin_client.get(reverse("admin:rag_ragoverview_changelist")).content.decode()
    assert "Laufende Läufe" in overview and "Lauf #" in overview
    action(admin_client, "directorysource", "cancel_scan_action", [source.pk])
    assert not Job.objects.exists()
    assert IndexRun.objects.get().status == IndexRun.Status.CANCELLED
    source.refresh_from_db()
    assert source.last_error == jobs.CANCELLED_SCAN


def test_owner_sees_scan_progress_but_cannot_cancel_run(client, docs_dir, coll, anna):
    source = DirectorySource.objects.create(collection=coll, path=str(docs_dir))
    scan = crawl.enqueue_scan(source)
    client.force_login(anna)
    page = client.get(reverse("chat:collection_detail", args=[coll.pk])).content.decode()
    assert f"Lauf #{scan.run_id}" in page
    # Verwaltung der Läufe nur für Verwalter.
    url = reverse("admin:rag_indexrunproxy_cancel", args=[scan.run_id])
    response = client.post(url)
    assert response.status_code in (302, 403)
    assert IndexRun.objects.get().status == IndexRun.Status.RUNNING
