"""Kontextkopf je Abschnitt (Dokumentname, Ordnerpfad, Seite) in Embedding und
Volltextsuche, Datenmigration 0016.

Kein echter Anbieteraufruf: Embeddings über respx, ``ingest._embed``-Mock oder
die Schein-Embeddings (``RAG_FAKE_EMBEDDINGS``).
"""

import importlib
import io
import json
import shutil
from pathlib import Path
from unittest import mock

import httpx
import pytest
import respx
from django.apps import apps

from multigpt.accounts.models import Role, User
from multigpt.chat.models import (
    EMBEDDING_DIMENSIONS,
    AIModel,
    Chunk,
    Collection,
    Document,
    Job,
    Provider,
    RagSettings,
)
from multigpt.chat.rag import chunking, ingest, jobs, search
from multigpt.rag import crawl
from multigpt.rag.models import DirectorySource

pytestmark = pytest.mark.django_db

DATA = Path(__file__).resolve().parent / "data"
BASE = "https://api.example.invalid/v1"
SAMPLE_TEXT = (DATA / "sample.txt").read_text(encoding="utf-8")

migration = importlib.import_module("multigpt.chat.migrations.0016_chunk_heading")


def make_user(username):
    return User.objects.create_user(username, role=Role.objects.get(key="adult"))


@pytest.fixture
def anna():
    return make_user("anna")


@pytest.fixture
def bernd():
    return make_user("bernd")


@pytest.fixture
def coll(anna):
    return Collection.objects.create(owner=anna, name="Ordner")


@pytest.fixture
def media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    return settings.MEDIA_ROOT


def upload(collection, title, name="sample.txt"):
    doc = Document(collection=collection, title=title)
    doc.file.save(name, io.BytesIO((DATA / name).read_bytes()), save=False)
    doc.save()
    return doc


def vector(*weights):
    v = [0.0] * EMBEDDING_DIMENSIONS
    for i, w in enumerate(weights):
        v[i] = float(w)
    return v


def capture_embed():
    """Mock für ``ingest._embed``, der die Eingaben festhält."""
    seen: list[str] = []

    def fake(texts):
        seen.extend(texts)
        return [vector(1) for _ in texts]

    return seen, mock.patch.object(ingest, "_embed", side_effect=fake)


def indexed_chunk(collection, title, text, heading=None, source_path=""):
    doc = Document.objects.create(
        collection=collection, title=title, file="documents/x.pdf", status="indexed"
    )
    if heading is None:
        heading = chunking.heading(title, source_path, 1)
    return Chunk.objects.create(
        document=doc, position=0, text=text, heading=heading, page=1, embedding=vector(1)
    )


def text_ranking(user, query):
    return search._text_ranking(search.accessible_chunks(user), query, 20)


# --- Kopf -----------------------------------------------------------------------------


def test_heading_title_only_without_extension():
    assert chunking.heading("Jahresbericht.pdf") == "Dokument: Jahresbericht"
    assert chunking.heading("Notizen", page=3) == "Dokument: Notizen\nSeite: 3"
    # Unbekannte Endungen bleiben Teil des Namens.
    assert chunking.heading("Version 2.1 final") == "Dokument: Version 2.1 final"


def test_heading_with_source_path():
    text = chunking.heading("quittung.txt", "Steuern/Belege 2023/quittung.txt", 2)
    assert text == ("Dokument: quittung\nPfad: Steuern / Belege 2023 / quittung\nSeite: 2")


def test_heading_long_path_is_cut_at_the_front():
    path = "/".join(["ordner"] * 200) + "/datei.md"
    line = chunking.heading("datei.md", path).splitlines()[1]
    assert line.startswith("Pfad: … ") and line.endswith("ordner / datei")
    assert len(line) <= len("Pfad: … ") + chunking.MAX_HEADING_LINE


# --- Indexierung ----------------------------------------------------------------------


@respx.mock
def test_embedding_input_has_prefix_heading_and_text(coll, media):
    provider = Provider.objects.create(
        name="LM", kind=Provider.Kind.OPENAI_COMPAT, base_url=BASE, api_key="sk-test"
    )
    model = AIModel.objects.create(
        provider=provider,
        model_id="text-embedding-nomic-embed-text-v1.5",
        display_name="nomic",
        capability=AIModel.Capability.EMBEDDING,
    )
    cfg = RagSettings.load()
    cfg.embedding_model = model
    cfg.document_prefix = "search_document: "
    cfg.save()
    route = respx.post(f"{BASE}/embeddings").mock(
        return_value=httpx.Response(
            200,
            json={
                "object": "list",
                "data": [{"object": "embedding", "index": 0, "embedding": vector(1)}],
            },
        )
    )
    doc = upload(coll, "Nebenkostenabrechnung.txt")

    ingest.index_document(doc)

    chunk = Chunk.objects.get(document=doc)
    assert chunk.heading == "Dokument: Nebenkostenabrechnung"
    assert chunk.text == SAMPLE_TEXT.strip()  # gespeicherter Text unverändert
    assert "Nebenkosten" not in chunk.text
    body = json.loads(route.calls[0].request.content)
    assert body["input"] == ["search_document: Dokument: Nebenkostenabrechnung\n\n" + chunk.text]


def test_title_word_found_by_full_text_after_indexing(anna, coll, media):
    seen, patch = capture_embed()
    with patch:
        ingest.index_document(upload(coll, "Zahnarztrechnung.txt"))
    chunk = Chunk.objects.get()
    assert seen == [f"{chunk.heading}\n\n{chunk.text}"]
    assert text_ranking(anna, "Wo ist die Zahnarztrechnung?") == [chunk.pk]


def test_fake_embeddings_vector_search_finds_title(settings, anna, coll, media):
    settings.DEBUG = True
    settings.RAG_FAKE_EMBEDDINGS = True
    ingest.index_document(upload(coll, "Zahnarztrechnung.txt"))
    ingest.index_document(upload(coll, "Gartenplanung.txt"))
    wanted = Chunk.objects.get(document__title="Zahnarztrechnung.txt")

    hits = search.search(anna, "Zahnarztrechnung", None, top_k=2, hybrid=False)

    assert hits[0].chunk_id == wanted.pk and hits[0].score > hits[1].score
    assert hits[0].text == SAMPLE_TEXT.strip()


def test_directory_source_path_in_heading_and_search(settings, tmp_path, anna, coll):
    root = tmp_path / "nas"
    target = root / "docs" / "Steuern" / "Belege 2023" / "a.txt"
    target.parent.mkdir(parents=True)
    shutil.copyfile(DATA / "sample.txt", target)
    settings.RAG_SOURCE_ROOTS = [str(root)]
    settings.MEDIA_ROOT = tmp_path / "media"
    source = DirectorySource.objects.create(collection=coll, path=str(root / "docs"))
    crawl.scan_source(source)
    doc = Document.objects.get(source=source)
    seen, patch = capture_embed()
    with patch:
        ingest.index_document(doc)

    chunk = Chunk.objects.get(document=doc)
    assert chunk.heading == "Dokument: a\nPfad: Steuern / Belege 2023 / a"  # .txt: ohne Seite
    assert seen == [f"{chunk.heading}\n\n{chunk.text}"]
    assert chunk.text == SAMPLE_TEXT.strip()
    # Wort nur im Ordnerpfad (deutscher Stamm: Steuern -> steu, Belege -> beleg).
    assert text_ranking(anna, "Steuern") == [chunk.pk]
    assert text_ranking(anna, "Beleg") == [chunk.pk]


# --- Volltextsuche --------------------------------------------------------------------


def test_title_match_ranks_before_text_match(anna, coll):
    in_text = indexed_chunk(coll, "Sonstiges.pdf", "Der Mietvertrag liegt im Schrank.")
    in_title = indexed_chunk(coll, "Mietvertrag.pdf", "Die Kaution beträgt drei Monatsmieten.")
    indexed_chunk(coll, "Rezept.pdf", "Mehl, Zucker und Eier verrühren.")

    assert text_ranking(anna, "Mietvertrag") == [in_title.pk, in_text.pk]


def test_hybrid_search_promotes_title_match(anna, coll, monkeypatch):
    other = indexed_chunk(coll, "Rezept.pdf", "Mehl und Zucker verrühren.")
    titled = indexed_chunk(coll, "Mietvertrag.pdf", "Die Kaution beträgt drei Monatsmieten.")
    Chunk.objects.filter(pk=other.pk).update(embedding=vector(1))
    Chunk.objects.filter(pk=titled.pk).update(embedding=vector(1, 1))
    monkeypatch.setattr(search, "embed_query", lambda text: vector(1))

    plain = search.search(anna, "Mietvertrag", None, top_k=2, hybrid=False)
    hybrid = search.search(anna, "Mietvertrag", None, top_k=2, hybrid=True)

    assert [h.chunk_id for h in plain] == [other.pk, titled.pk]
    assert [h.chunk_id for h in hybrid] == [titled.pk, other.pk]
    assert hybrid[0].text == "Die Kaution beträgt drei Monatsmieten."


def test_chunk_without_heading_stays_searchable_by_text(anna, coll):
    old = indexed_chunk(coll, "Alt.pdf", "Die Heizung wurde gewartet.", heading="")
    assert text_ranking(anna, "Heizung") == [old.pk]
    assert text_ranking(anna, "Alt") == []


def test_title_of_foreign_collection_not_found(anna, bernd, coll, monkeypatch):
    foreign = Collection.objects.create(owner=bernd, name="Privat")
    indexed_chunk(foreign, "Geheimprojekt.pdf", "Nur für Bernd.")
    own = indexed_chunk(coll, "Notizen.pdf", "Etwas über das Geheimprojekt.")
    monkeypatch.setattr(search, "embed_query", lambda text: vector(1))

    assert text_ranking(anna, "Geheimprojekt") == [own.pk]
    for hybrid in (True, False):
        hits = search.search(anna, "Geheimprojekt", [coll.pk, foreign.pk], 10, hybrid=hybrid)
        assert [h.chunk_id for h in hits] == [own.pk]


# --- Titeländerung / Pfadänderung -----------------------------------------------------


def test_moved_source_file_is_new_document_with_new_heading(settings, tmp_path, coll):
    """Pfadänderung bei Verzeichnisquellen: neues Dokument, also neuer Indexjob."""
    root = tmp_path / "nas"
    (root / "docs" / "Alt").mkdir(parents=True)
    shutil.copyfile(DATA / "sample.txt", root / "docs" / "Alt" / "a.txt")
    settings.RAG_SOURCE_ROOTS = [str(root)]
    source = DirectorySource.objects.create(collection=coll, path=str(root / "docs"))
    crawl.scan_source(source)
    (root / "docs" / "Neu").mkdir()
    (root / "docs" / "Alt" / "a.txt").rename(root / "docs" / "Neu" / "a.txt")

    crawl.scan_source(source)

    doc = Document.objects.get(source=source)
    assert doc.source_path == "Neu/a.txt"
    assert Job.objects.filter(
        kind=Job.Kind.INDEX_DOCUMENT, status=Job.Status.PENDING, payload__document_id=doc.pk
    ).exists()


# --- Datenmigration 0016 --------------------------------------------------------------


def test_migration_requeues_indexed_documents(coll):
    indexed = indexed_chunk(coll, "A.pdf", "Text A", heading="").document
    with_job = indexed_chunk(coll, "B.pdf", "Text B", heading="").document
    open_job = jobs.enqueue_index(with_job)
    Document.objects.filter(pk=with_job.pk).update(status="indexed")
    pending = Document.objects.create(collection=coll, title="C.pdf", status="pending")
    failed = Document.objects.create(
        collection=coll, title="D.pdf", status="error", error_text="kaputt"
    )

    migration.requeue_indexed(apps, None)

    def job_ids(doc):
        return list(
            Job.objects.filter(
                kind=Job.Kind.INDEX_DOCUMENT, payload__document_id=doc.pk
            ).values_list("pk", flat=True)
        )

    assert len(job_ids(indexed)) == 1
    assert job_ids(with_job) == [open_job.pk]  # kein doppelter Auftrag
    assert job_ids(pending) == [] and job_ids(failed) == []
    indexed.refresh_from_db()
    failed.refresh_from_db()
    assert indexed.status == Document.Status.PENDING
    assert failed.status == Document.Status.ERROR and failed.error_text == "kaputt"
    # Abschnitte bleiben bis zur Neuindizierung erhalten.
    assert Chunk.objects.count() == 2


def test_requeued_document_gets_heading_after_worker(coll, media):
    doc = upload(coll, "Stromvertrag.txt")
    Chunk.objects.create(document=doc, position=0, text="alt", embedding=vector(1))
    Document.objects.filter(pk=doc.pk).update(status="indexed")
    migration.requeue_indexed(apps, None)
    seen, patch = capture_embed()
    with patch:
        job = jobs.claim_next()
        assert jobs.run_job(job) == Job.Status.DONE
    chunk = Chunk.objects.get(document=doc)
    assert chunk.heading == "Dokument: Stromvertrag" and chunk.text == SAMPLE_TEXT.strip()
