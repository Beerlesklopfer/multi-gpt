"""Verzeichnisquellen (Agent crawler): Wurzelprüfung, Einlesen, Periodik,
Download vom Pfad, Admin und Nutzeransicht."""

import os
import shutil
from datetime import timedelta
from pathlib import Path
from unittest import mock

import pytest
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from multigpt.accounts.models import Role, User, UserGroup
from multigpt.chat.models import EMBEDDING_DIMENSIONS, Chunk, Collection, Document, Job, Share
from multigpt.chat.rag import ingest, jobs
from multigpt.rag import crawl, paths
from multigpt.rag.models import DirectorySource

pytestmark = pytest.mark.django_db

DATA = Path(__file__).resolve().parent / "data"
PASSWORD = "Geheim-Test-1234"


# --- Hilfen -------------------------------------------------------------------------


@pytest.fixture
def nas(settings, tmp_path):
    root = tmp_path / "nas"
    (root / "docs").mkdir(parents=True)
    settings.RAG_SOURCE_ROOTS = [str(root)]
    settings.MEDIA_ROOT = tmp_path / "media"
    return root


@pytest.fixture
def docs(nas):
    return nas / "docs"


def make_user(username, role_key="adult"):
    return User.objects.create_user(
        username, password=PASSWORD, role=Role.objects.get(key=role_key)
    )


@pytest.fixture
def anna():
    return make_user("anna")


@pytest.fixture
def coll(anna):
    return Collection.objects.create(owner=anna, name="Archiv")


def make_source(collection, path, **extra):
    return DirectorySource.objects.create(collection=collection, path=str(path), **extra)


def put(directory: Path, name: str, sample: str = "sample.txt") -> Path:
    target = directory / name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(DATA / sample, target)
    return target


def touch_later(path: Path, seconds: int = 10):
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + seconds * 1_000_000_000))


def titles(source):
    return sorted(Document.objects.filter(source=source).values_list("source_path", flat=True))


def fake_vectors(texts):
    return [[0.1] + [0.0] * (EMBEDDING_DIMENSIONS - 1) for _ in texts]


# --- Wurzelprüfung ------------------------------------------------------------------


def test_disabled_without_roots(settings, tmp_path):
    settings.RAG_SOURCE_ROOTS = []
    assert not paths.enabled()
    with pytest.raises(paths.SourcePathError, match="ausgeschaltet"):
        paths.check_directory(str(tmp_path))
    assert crawl.enqueue_due_scans() == 0


def test_relative_roots_are_ignored(settings, tmp_path):
    settings.RAG_SOURCE_ROOTS = ["relativ/pfad", str(tmp_path)]
    assert paths.allowed_roots() == [os.path.realpath(tmp_path)]


def test_directory_inside_root_ok(docs):
    assert paths.check_directory(str(docs)) == os.path.realpath(docs)


def test_outside_root_rejected(nas, tmp_path):
    outside = tmp_path / "anderswo"
    outside.mkdir()
    with pytest.raises(paths.SourcePathError, match="außerhalb"):
        paths.check_directory(str(outside))


def test_prefix_sibling_is_outside(nas, tmp_path):
    sibling = tmp_path / "nas-geheim"  # beginnt mit demselben Präfix
    sibling.mkdir()
    with pytest.raises(paths.SourcePathError, match="außerhalb"):
        paths.check_directory(str(sibling))


def test_dotdot_rejected(nas, tmp_path):
    (tmp_path / "geheim").mkdir()
    with pytest.raises(paths.SourcePathError, match="außerhalb"):
        paths.check_directory(str(nas / "docs" / ".." / ".." / "geheim"))


def test_relative_path_rejected(nas):
    with pytest.raises(paths.SourcePathError, match="absoluten"):
        paths.check_directory("docs")


def test_symlinked_directory_to_outside_rejected(nas, tmp_path):
    outside = tmp_path / "geheim"
    outside.mkdir()
    (nas / "link").symlink_to(outside)
    with pytest.raises(paths.SourcePathError, match="außerhalb"):
        paths.check_directory(str(nas / "link"))


def test_missing_directory(nas):
    with pytest.raises(paths.SourcePathError, match="gibt es nicht"):
        paths.check_directory(str(nas / "fehlt"))


@pytest.mark.skipif(os.geteuid() == 0, reason="root darf alles lesen")
def test_unreadable_directory(docs):
    locked = docs / "gesperrt"
    locked.mkdir()
    locked.chmod(0o000)
    try:
        with pytest.raises(paths.SourcePathError, match="Leserechte"):
            paths.check_directory(str(locked))
    finally:
        locked.chmod(0o755)


def test_open_file_rejects_file_symlink_and_dotdot(docs, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("geheim")
    (docs / "link.txt").symlink_to(secret)
    put(docs, "ok.txt")
    with paths.open_file(str(docs), "ok.txt") as fh:
        assert fh.read()
    with pytest.raises(paths.SourcePathError):
        paths.open_file(str(docs), "link.txt")
    with pytest.raises(paths.SourcePathError):
        paths.open_file(str(docs), "../../secret.txt")
    with pytest.raises(paths.SourcePathError):
        paths.open_file(str(docs), str(secret))


def test_open_file_rejects_symlinked_parent_inside_root(docs, nas):
    # Auch ein Symlink innerhalb der Wurzel wird nicht verfolgt (TOCTOU-sicher).
    put(nas / "other", "a.txt")
    (docs / "sub").symlink_to(nas / "other")
    with pytest.raises(paths.SourcePathError):
        paths.open_file(str(docs), "sub/a.txt")


def test_open_file_rejects_fifo(docs):
    os.mkfifo(docs / "pipe.txt")
    with pytest.raises(paths.SourcePathError, match="reguläres"):
        paths.open_file(str(docs), "pipe.txt")


def test_root_removed_later_blocks_access(settings, docs, coll, tmp_path):
    source = make_source(coll, docs)
    put(docs, "a.txt")
    crawl.run_scan(source)
    settings.RAG_SOURCE_ROOTS = [str(tmp_path / "anders")]
    source.refresh_from_db()
    assert crawl.run_scan(source) == paths.MSG_OUTSIDE
    assert Document.objects.filter(source=source).count() == 1  # nichts gelöscht
    doc = Document.objects.get(source=source)
    with pytest.raises(paths.SourcePathError):
        crawl.open_document(doc)


# --- Einlesen -------------------------------------------------------------------------


def test_scan_new_changed_unchanged_deleted(docs, coll):
    source = make_source(coll, docs)
    put(docs, "a.txt")
    put(docs, "sub/b.md", "sample.md")
    put(docs, "c.pdf", "sample.pdf")

    result = crawl.scan_source(source)
    assert (result.new, result.changed, result.deleted) == (3, 0, 0)
    assert titles(source) == ["a.txt", "c.pdf", "sub/b.md"]
    doc_a = Document.objects.get(source=source, source_path="a.txt")
    assert not doc_a.file
    assert doc_a.title == "a.txt"
    assert doc_a.collection_id == coll.pk
    assert doc_a.status == Document.Status.PENDING
    assert len(doc_a.source_sha256) == 64
    assert doc_a.source_size == (docs / "a.txt").stat().st_size
    assert Job.objects.filter(kind=Job.Kind.INDEX_DOCUMENT).count() == 3

    # Unverändert: kein neuer Job, nichts gelesen.
    with mock.patch.object(crawl, "_hash_and_check") as hashed:
        result = crawl.scan_source(source)
    hashed.assert_not_called()
    assert (result.new, result.changed, result.unchanged) == (0, 0, 3)

    # Nur mtime geändert, Inhalt gleich -> unverändert, mtime nachgeführt.
    touch_later(docs / "a.txt")
    result = crawl.scan_source(source)
    assert (result.changed, result.unchanged) == (0, 3)
    doc_a.refresh_from_db()
    assert doc_a.source_mtime == crawl.mtime_of((docs / "a.txt").stat())

    # Inhalt geändert -> geändert + Indexierungsjob.
    Job.objects.all().delete()
    Document.objects.filter(pk=doc_a.pk).update(status=Document.Status.INDEXED)
    (docs / "a.txt").write_text("Ganz neuer Inhalt der Datei.", encoding="utf-8")
    touch_later(docs / "a.txt", 20)
    result = crawl.scan_source(source)
    assert (result.new, result.changed, result.unchanged) == (0, 1, 2)
    doc_a.refresh_from_db()
    assert doc_a.status == Document.Status.PENDING
    assert Job.objects.filter(payload__document_id=doc_a.pk).count() == 1

    # Gelöscht -> Dokument samt Abschnitten weg, offener Job entfernt.
    Chunk.objects.create(
        document=doc_a, position=0, text="x", page=None, embedding=[0.1] * EMBEDDING_DIMENSIONS
    )
    (docs / "a.txt").unlink()
    result = crawl.scan_source(source)
    assert result.deleted == 1
    assert not Document.objects.filter(pk=doc_a.pk).exists()
    assert not Chunk.objects.exists()
    assert not Job.objects.filter(payload__document_id=doc_a.pk).exists()


def test_scan_does_not_touch_files(docs, coll):
    source = make_source(coll, docs)
    target = put(docs, "a.txt")
    before = (target.read_bytes(), target.stat().st_mtime_ns)
    crawl.scan_source(source)
    assert (target.read_bytes(), target.stat().st_mtime_ns) == before
    assert sorted(p.name for p in docs.iterdir()) == ["a.txt"]


def test_patterns_hidden_and_non_recursive(docs, coll):
    put(docs, "a.txt")
    put(docs, "b.md", "sample.md")
    put(docs, ".versteckt.txt")
    put(docs, "~$entwurf.txt")
    put(docs, ".git/x.txt")
    put(docs, "Entwürfe/e.txt")
    put(docs, "sub/s.txt")
    put(docs, "bild.png")
    source = make_source(coll, docs, include_patterns="*.txt", exclude_patterns="~$*, Entwürfe")
    crawl.scan_source(source)
    assert titles(source) == ["a.txt", "sub/s.txt"]

    flat = make_source(
        Collection.objects.create(owner=coll.owner, name="Flach"), docs, recursive=False
    )
    crawl.scan_source(flat)
    assert titles(flat) == ["a.txt", "b.md"]


def test_symlinks_are_not_followed(docs, coll, tmp_path):
    outside = tmp_path / "geheim"
    put(outside, "geheim.txt")
    (docs / "link.txt").symlink_to(outside / "geheim.txt")
    (docs / "linkdir").symlink_to(outside)
    put(docs, "a.txt")
    source = make_source(coll, docs)
    crawl.scan_source(source)
    assert titles(source) == ["a.txt"]


def test_size_limit_skips(settings, docs, coll):
    settings.DOCUMENT_MAX_UPLOAD_MB = 1
    (docs / "gross.txt").write_bytes(b"a" * (1024 * 1024 + 1))
    put(docs, "a.txt")
    source = make_source(coll, docs)
    result = crawl.scan_source(source)
    assert titles(source) == ["a.txt"]
    assert result.skipped == 1


def test_type_check_by_content(docs, coll):
    (docs / "falsch.pdf").write_text("Das ist kein PDF, sondern Text.", encoding="utf-8")
    (docs / "leer.txt").write_bytes(b"")
    (docs / "binaer.txt").write_bytes(b"\x00\x01\x02\xff" * 100)
    put(docs, "echt.pdf", "sample.pdf")
    put(docs, "wort.docx", "sample.docx")
    source = make_source(coll, docs)
    result = crawl.scan_source(source)
    assert titles(source) == ["echt.pdf", "wort.docx"]
    assert result.skipped == 3


def test_error_of_single_file_is_counted_and_scan_continues(docs, coll):
    put(docs, "a.txt")
    put(docs, "b.txt")
    source = make_source(coll, docs)
    real_open = crawl.paths.open_file

    def flaky(base, rel):
        if rel == "a.txt":
            raise PermissionError("nein")
        return real_open(base, rel)

    with mock.patch.object(crawl.paths, "open_file", side_effect=flaky):
        result = crawl.scan_source(source)
    assert result.errors == 1
    assert titles(source) == ["b.txt"]


def test_error_keeps_existing_document(docs, coll):
    put(docs, "a.txt")
    source = make_source(coll, docs)
    crawl.scan_source(source)
    touch_later(docs / "a.txt")
    with mock.patch.object(crawl.paths, "open_file", side_effect=PermissionError):
        result = crawl.scan_source(source)
    assert result.errors == 1
    assert titles(source) == ["a.txt"]


def test_max_files_truncates_without_deleting(settings, docs, coll):
    for i in range(3):
        put(docs, f"{i}.txt")
    source = make_source(coll, docs)
    crawl.scan_source(source)
    settings.RAG_SOURCE_MAX_FILES = 2
    (docs / "0.txt").unlink()
    put(docs, "3.txt")
    note = crawl.run_scan(source)
    source.refresh_from_db()
    assert source.last_result["truncated"] is True
    assert "Mehr als 2 Dateien" in note
    assert titles(source) == ["0.txt", "1.txt", "2.txt"]  # nichts gelöscht


def test_empty_directory_keeps_documents(docs, coll):
    put(docs, "a.txt")
    source = make_source(coll, docs)
    crawl.scan_source(source)
    (docs / "a.txt").unlink()
    note = crawl.run_scan(source)
    assert "eingehängt" in note
    assert titles(source) == ["a.txt"]


def test_run_scan_records_result(docs, coll):
    put(docs, "a.txt")
    source = make_source(coll, docs)
    assert crawl.run_scan(source) == ""
    source.refresh_from_db()
    assert source.last_scan_started and source.last_scan_finished
    assert source.last_result["new"] == 1
    assert source.last_error == ""


def test_missing_directory_recorded(docs, coll):
    source = make_source(coll, docs)
    shutil.rmtree(docs)
    crawl.run_scan(source)
    source.refresh_from_db()
    assert "gibt es nicht" in source.last_error


def test_logs_contain_no_file_names(docs, coll, caplog):
    put(docs, "Steuererklaerung-Geheim.txt")
    source = make_source(coll, docs)
    with caplog.at_level("DEBUG"):
        crawl.run_scan(source)
    assert "Steuererklaerung" not in caplog.text
    assert str(docs) not in caplog.text


# --- Indexierung und Worker ---------------------------------------------------------


def test_index_reads_from_path(docs, coll):
    put(docs, "a.txt")
    source = make_source(coll, docs)
    crawl.scan_source(source)
    doc = Document.objects.get(source=source)
    with mock.patch.object(ingest, "_embed", side_effect=fake_vectors):
        result = ingest.index_document(doc)
    assert result.chunks >= 1
    doc.refresh_from_db()
    assert doc.status == Document.Status.INDEXED


def test_index_fails_cleanly_for_missing_file(docs, coll):
    put(docs, "a.txt")
    source = make_source(coll, docs)
    crawl.scan_source(source)
    doc = Document.objects.get(source=source)
    (docs / "a.txt").unlink()
    with pytest.raises(ingest.IngestError, match="fehlt"):
        ingest.index_document(doc)


def test_index_refuses_swapped_symlink(docs, coll, tmp_path):
    put(docs, "a.txt")
    source = make_source(coll, docs)
    crawl.scan_source(source)
    doc = Document.objects.get(source=source)
    secret = tmp_path / "secret.txt"
    secret.write_text("geheim " * 20)
    (docs / "a.txt").unlink()
    (docs / "a.txt").symlink_to(secret)
    with pytest.raises(ingest.IngestError):
        ingest.index_document(doc)


def test_worker_runs_scan_job_and_indexes(docs, coll):
    put(docs, "a.txt")
    source = make_source(coll, docs)
    with mock.patch.object(ingest, "_embed", side_effect=fake_vectors):
        call_command("run_worker", "--once")
    scan = Job.objects.get(kind=Job.Kind.SCAN_DIRECTORY)
    assert scan.status == Job.Status.DONE
    doc = Document.objects.get(source=source)
    assert doc.status == Document.Status.INDEXED


def test_scan_job_for_deleted_source(nas):
    job = Job.objects.create(kind=Job.Kind.SCAN_DIRECTORY, payload={"source_id": 999})
    claimed = jobs.claim_next()
    assert jobs.run_job(claimed) == Job.Status.DONE
    job.refresh_from_db()
    assert job.last_error == crawl.MSG_SOURCE_GONE


# --- Periodik -----------------------------------------------------------------------


def test_due_and_dedup(docs, coll):
    now = timezone.now()
    source = make_source(coll, docs, interval_minutes=60)
    assert crawl.is_due(source, now)
    assert crawl.enqueue_due_scans(now) == 1
    assert crawl.enqueue_due_scans(now) == 0  # offener Job -> keiner dazu
    assert crawl.enqueue_scan(source) is None
    assert Job.objects.filter(kind=Job.Kind.SCAN_DIRECTORY).count() == 1

    # Läuft der Job, bleibt es bei einem.
    Job.objects.update(status=Job.Status.RUNNING)
    assert crawl.enqueue_due_scans(now) == 0

    # Erledigt, kurz danach: nicht fällig; nach dem Intervall wieder fällig.
    Job.objects.update(status=Job.Status.DONE)
    DirectorySource.objects.filter(pk=source.pk).update(last_scan_started=now)
    source.refresh_from_db()
    assert not crawl.is_due(source, now + timedelta(minutes=59))
    assert crawl.enqueue_due_scans(now + timedelta(minutes=59)) == 0
    assert crawl.is_due(source, now + timedelta(minutes=60))
    assert crawl.enqueue_due_scans(now + timedelta(minutes=61)) == 1


def test_paused_source_not_due(docs, coll):
    source = make_source(coll, docs, active=False)
    assert not crawl.is_due(source)
    assert crawl.enqueue_due_scans() == 0


# --- Download und Nutzeransicht -------------------------------------------------------


@pytest.fixture
def source_doc(docs, coll):
    put(docs, "a.txt")
    source = make_source(coll, docs)
    crawl.scan_source(source)
    return Document.objects.get(source=source)


def test_download_from_path_with_rights(client, source_doc, docs):
    client.force_login(source_doc.collection.owner)
    response = client.get(reverse("chat:document_download", args=[source_doc.pk]))
    assert response.status_code == 200
    assert b"".join(response.streaming_content) == (docs / "a.txt").read_bytes()
    assert "a.txt" in response["Content-Disposition"]


def test_download_denied_for_stranger(client, source_doc):
    client.force_login(make_user("bert"))
    response = client.get(reverse("chat:document_download", args=[source_doc.pk]))
    assert response.status_code == 404


def test_download_shared_reader(client, source_doc):
    bert = make_user("bert")
    group = UserGroup.objects.create(name="Familie2")
    bert.groups.add(group)
    Share.objects.create(collection=source_doc.collection, group=group)
    client.force_login(bert)
    assert client.get(reverse("chat:document_download", args=[source_doc.pk])).status_code == 200


def test_download_rechecks_root(settings, client, source_doc, tmp_path):
    client.force_login(source_doc.collection.owner)
    settings.RAG_SOURCE_ROOTS = [str(tmp_path / "anders")]
    response = client.get(reverse("chat:document_download", args=[source_doc.pk]))
    assert response.status_code == 404


def test_download_refuses_symlink(client, source_doc, docs, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("geheim")
    (docs / "a.txt").unlink()
    (docs / "a.txt").symlink_to(secret)
    client.force_login(source_doc.collection.owner)
    response = client.get(reverse("chat:document_download", args=[source_doc.pk]))
    assert response.status_code == 404


def test_user_view_shows_notice_and_no_delete_button(client, source_doc, coll):
    client.force_login(coll.owner)
    html = client.get(reverse("chat:collection_detail", args=[coll.pk])).content.decode()
    assert "Wird aus einem Serververzeichnis eingelesen" in html
    assert f'data-document-delete="{source_doc.pk}"' not in html
    assert 'id="collection-delete"' not in html
    data = client.get(reverse("chat:api_collection_documents", args=[coll.pk])).json()
    assert data["documents"][0]["from_source"] is True
    assert data["documents"][0]["size"] == source_doc.source_size


def test_user_view_keeps_delete_for_uploaded(client, coll):
    from django.core.files.uploadedfile import SimpleUploadedFile

    doc = Document.objects.create(
        collection=coll, title="x.txt", file=SimpleUploadedFile("x.txt", b"Hallo Welt")
    )
    client.force_login(coll.owner)
    html = client.get(reverse("chat:collection_detail", args=[coll.pk])).content.decode()
    assert f'data-document-delete="{doc.pk}"' in html
    assert "Serververzeichnis" not in html


def test_api_refuses_deleting_source_document_and_collection(client, source_doc, coll):
    client.force_login(coll.owner)
    response = client.delete(reverse("chat:api_document_detail", args=[source_doc.pk]))
    assert response.status_code == 403
    assert Document.objects.filter(pk=source_doc.pk).exists()
    response = client.delete(reverse("chat:api_collection_detail", args=[coll.pk]))
    assert response.status_code == 403
    assert Collection.objects.filter(pk=coll.pk).exists()


# --- Admin --------------------------------------------------------------------------


@pytest.fixture
def admin_client(client):
    user = User.objects.create_superuser(username="jo", password=PASSWORD, email="jo@x.invalid")
    client.force_login(user)
    return client


def _add(client, **data):
    base = {
        "collection": "",
        "new_collection_name": "",
        "new_collection_owner": "",
        "path": "",
        "recursive": "on",
        "include_patterns": "*.pdf, *.docx, *.txt, *.md",
        "exclude_patterns": ".*, ~$*",
        "interval_minutes": "60",
        "active": "on",
    }
    base.update(data)
    return client.post(reverse("admin:rag_directorysource_add"), base)


def test_admin_add_with_existing_collection(admin_client, docs, coll):
    response = _add(admin_client, collection=str(coll.pk), path=str(docs / ".." / "docs"))
    assert response.status_code == 302, response.content.decode()[:3000]
    source = DirectorySource.objects.get()
    assert source.path == os.path.realpath(docs)
    assert source.collection == coll
    assert crawl.open_scan_job(source) is not None  # gleich eingereiht


def test_admin_add_with_new_collection(admin_client, docs, anna):
    response = _add(
        admin_client,
        new_collection_name="NAS-Archiv",
        new_collection_owner=str(anna.pk),
        path=str(docs),
    )
    assert response.status_code == 302
    source = DirectorySource.objects.get()
    assert source.collection.name == "NAS-Archiv"
    assert source.collection.owner == anna


def test_admin_add_validation(admin_client, docs, nas, tmp_path, coll, anna):
    outside = tmp_path / "anderswo"
    outside.mkdir()
    html = _add(admin_client, collection=str(coll.pk), path=str(outside)).content.decode()
    assert "außerhalb der erlaubten Verzeichnisse" in html
    html = _add(admin_client, path=str(docs)).content.decode()
    assert "Bitte eine Sammlung wählen" in html
    html = _add(
        admin_client,
        collection=str(coll.pk),
        new_collection_name="X",
        new_collection_owner=str(anna.pk),
        path=str(docs),
    ).content.decode()
    assert "nicht beides" in html
    html = _add(
        admin_client, collection=str(coll.pk), path=str(docs), interval_minutes="2"
    ).content.decode()
    assert "5" in html and not DirectorySource.objects.exists()
    html = _add(admin_client, collection=str(coll.pk), path=str(nas / "fehlt")).content.decode()
    assert "gibt es nicht" in html
    assert not DirectorySource.objects.exists()


def test_admin_hint_when_roots_empty(settings, admin_client):
    settings.RAG_SOURCE_ROOTS = []
    html = admin_client.get(
        reverse("admin:rag_directorysource_changelist"), follow=True
    ).content.decode()
    assert "RAG_SOURCE_ROOTS ist leer" in html


def test_admin_list_and_actions(admin_client, docs, coll):
    put(docs, "a.txt")
    source = make_source(coll, docs, active=True)
    crawl.run_scan(source)
    html = admin_client.get(reverse("admin:rag_directorysource_changelist")).content.decode()
    assert "Archiv" in html and "1 neu" in html

    url = reverse("admin:rag_directorysource_changelist")
    admin_client.post(url, {"action": "pause_action", "_selected_action": [source.pk]})
    source.refresh_from_db()
    assert not source.active
    admin_client.post(url, {"action": "activate_action", "_selected_action": [source.pk]})
    source.refresh_from_db()
    assert source.active
    admin_client.post(url, {"action": "scan_now_action", "_selected_action": [source.pk]})
    admin_client.post(url, {"action": "scan_now_action", "_selected_action": [source.pk]})
    assert Job.objects.filter(kind=Job.Kind.SCAN_DIRECTORY, status=Job.Status.PENDING).count() == 1

    # Detailseite: Pfad nicht änderbar.
    html = admin_client.get(
        reverse("admin:rag_directorysource_change", args=[source.pk])
    ).content.decode()
    assert 'name="path"' not in html

    # Übersicht zeigt die Quelle.
    overview = admin_client.get(reverse("admin:rag_ragoverview_changelist")).content.decode()
    assert "Verzeichnisquellen" in overview and "Archiv" in overview

    # Löschen der Quelle entfernt die Dokumente, nicht die Dateien.
    admin_client.post(
        url, {"action": "delete_selected", "_selected_action": [source.pk], "post": "yes"}
    )
    assert not DirectorySource.objects.exists()
    assert not Document.objects.exists()
    assert (docs / "a.txt").exists()


@pytest.mark.parametrize("role_key", ["adult", "teen", "guest"])
def test_admin_only_for_admins(client, docs, coll, role_key):
    source = make_source(coll, docs)
    client.force_login(make_user(f"u_{role_key}", role_key))
    for url in (
        reverse("admin:rag_directorysource_changelist"),
        reverse("admin:rag_directorysource_add"),
        reverse("admin:rag_directorysource_change", args=[source.pk]),
    ):
        assert client.get(url).status_code in (302, 403), url
    client.post(
        reverse("admin:rag_directorysource_changelist"),
        {"action": "scan_now_action", "_selected_action": [source.pk]},
    )
    assert not Job.objects.exists()


def test_admin_role_has_access(client, docs, coll):
    client.force_login(make_user("verwalter", "admin"))
    assert client.get(reverse("admin:rag_directorysource_changelist")).status_code == 200
