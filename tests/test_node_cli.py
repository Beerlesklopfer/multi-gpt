"""``mgpt-ctl index …`` und ``mgpt-ctl apikey …`` (M15)."""

import io
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from multigpt.chat.models import Collection, Document, IndexRun, Job
from multigpt.node import keys
from multigpt.node.models import ApiKey
from multigpt.rag.models import DirectorySource
from tests.node_support import make_user

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path / "media")


def run(*args) -> tuple[str, str]:
    out, err = io.StringIO(), io.StringIO()
    call_command(*args, stdout=out, stderr=err)
    return out.getvalue(), err.getvalue()


def test_apikey_create_list_revoke():
    make_user("anna")
    out, err = run("apikey", "create", "anna", "--name", "n8n", "--scopes", "docs.read,usage.read")
    secret = out.strip()
    key = ApiKey.objects.get()
    assert keys.authenticate(secret) == key
    assert "nur dieses eine Mal" in err and secret not in err
    assert key.scopes == ["docs.read", "usage.read"]
    assert key.expires_at is not None  # Standard 90 Tage
    listing, _ = run("apikey", "list")
    assert key.display_prefix in listing and secret not in listing
    out, _ = run("apikey", "revoke", key.display_prefix)
    assert "widerrufen" in out
    assert keys.authenticate(secret) is None
    out, _ = run("apikey", "revoke", str(key.pk))
    assert "schon widerrufen" in out


def test_apikey_create_errors():
    make_user("tim", "teen")
    with pytest.raises(CommandError, match="keine API-Keys"):
        run("apikey", "create", "tim", "--name", "x", "--scopes", "docs.read")
    make_user("anna")
    with pytest.raises(CommandError, match="Unbekannte"):
        run("apikey", "create", "anna", "--name", "x", "--scopes", "admin")
    with pytest.raises(CommandError, match="--expires"):
        run("apikey", "create", "anna", "--name", "x", "--scopes", "all", "--expires", "bald")
    out, _ = run("apikey", "create", "anna", "--name", "x", "--scopes", "all", "--expires", "never")
    assert ApiKey.objects.get(name="x").expires_at is None


def test_index_status_follow_stops_after_ticks():
    anna = make_user("anna")
    collection = Collection.objects.create(owner=anna, name="Haus")
    Document.objects.create(collection=collection, title="A")
    run("index", "reindex", "--collection", str(collection.pk))
    out, _ = run("index", "status", "--follow", "--interval", "0", "--ticks", "3")
    assert out.count("MultiGPT – Indexierung") == 3
    assert "Offene Läufe: 1" in out
    assert "Sammlung neu indexieren" in out
    assert "„Haus“" in out


def test_index_status_follow_ctrl_c(monkeypatch):
    calls = {"n": 0}

    def sleep(seconds):
        calls["n"] += 1
        raise KeyboardInterrupt

    monkeypatch.setattr("multigpt.node.management.commands.index.time.sleep", sleep)
    out, _ = run("index", "status", "--follow")
    assert "Beendet." in out and calls["n"] == 1


def test_index_status_single_run_and_runs_list():
    anna = make_user("anna")
    collection = Collection.objects.create(owner=anna, name="Haus")
    Document.objects.create(collection=collection, title="A")
    Document.objects.create(collection=collection, title="B")
    out, _ = run("index", "reindex")
    run_obj = IndexRun.objects.get()
    assert f"Lauf #{run_obj.pk}" in out and run_obj.kind == IndexRun.Kind.REINDEX_ALL
    out, _ = run("index", "status", "--run", str(run_obj.pk))
    assert "Dokumente eingereiht" in out and " 2" in out
    out, _ = run("index", "runs", "--open")
    assert f"#{run_obj.pk}" in out
    out, _ = run("index", "cancel", str(run_obj.pk))
    assert "2 Aufträge entfernt" in out
    with pytest.raises(CommandError, match="schon beendet"):
        run("index", "cancel", str(run_obj.pk))
    with pytest.raises(CommandError, match="nicht gefunden"):
        run("index", "status", "--run", "99999")


def test_existing_reindex_command_still_works():
    anna = make_user("anna")
    collection = Collection.objects.create(owner=anna, name="Haus")
    doc = Document.objects.create(collection=collection, title="A")
    out, _ = run("reindex", "--document", str(doc.pk))
    assert "1 Dokument(e) zur Indexierung eingereiht" in out
    assert Job.objects.get().payload == {"document_id": doc.pk}


def test_index_scan(settings, tmp_path):
    anna = make_user("anna")
    collection = Collection.objects.create(owner=anna, name="NAS")
    settings.RAG_SOURCE_ROOTS = []
    source = DirectorySource.objects.create(collection=collection, path=str(tmp_path))
    with pytest.raises(CommandError, match="aus"):
        run("index", "scan", str(source.pk))
    settings.RAG_SOURCE_ROOTS = [str(tmp_path)]
    out, _ = run("index", "scan", str(source.pk))
    run_obj = IndexRun.objects.get(source=source)
    assert f"Lauf #{run_obj.pk} gestartet" in out
    out, _ = run("index", "scan", str(source.pk))
    assert "läuft schon" in out
    out, _ = run("index", "sources")
    assert f"#{source.pk}" in out and str(tmp_path) not in out


def test_index_upload_as_account(tmp_path):
    anna = make_user("anna")
    collection = Collection.objects.create(owner=anna, name="Haus")
    good = tmp_path / "notiz.txt"
    good.write_text("Hallo")
    bad = tmp_path / "x.exe"
    bad.write_bytes(b"MZ")
    with pytest.raises(CommandError, match="1 Datei"):
        run("index", "upload", "Haus", str(good), str(bad), "--as", "anna")
    document = Document.objects.get()
    assert document.collection == collection and document.title == "notiz.txt"
    assert IndexRun.objects.get().started_by == anna
    # Fremde Sammlung: nicht gefunden (Rechte des Kontos)
    make_user("bernd")
    with pytest.raises(CommandError, match="nicht gefunden"):
        run("index", "upload", str(collection.pk), str(good), "--as", "bernd")
    with pytest.raises(CommandError, match="gesperrt"):
        run("index", "upload", "Haus", str(Path(good)), "--as", "niemand")
