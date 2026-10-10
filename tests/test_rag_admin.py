"""Admin-Abschnitt „Dokumente (RAG)“: Übersicht, Aktionen, Rechte, Privatsphäre."""

import re
from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from multigpt.accounts.admin_site import FamilyAdminSite
from multigpt.accounts.models import Role, User, UserGroup
from multigpt.chat.models import (
    EMBEDDING_DIMENSIONS,
    AIModel,
    Chunk,
    Collection,
    Document,
    Job,
    Provider,
    RagSettings,
    Share,
)
from multigpt.rag import services
from multigpt.rag.models import (
    CollectionProxy,
    DocumentProxy,
    JobProxy,
    RagOverview,
    RagSettingsProxy,
)

pytestmark = pytest.mark.django_db

PASSWORD = "Geheim-Test-1234"
PROBE = "GEHEIMER-PROBETEXT-7f3a"


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
def admin_client(client):
    user = User.objects.create_superuser(
        username="jo", password=PASSWORD, email="jo@example.invalid"
    )
    client.force_login(user)
    return client


@pytest.fixture
def coll(anna):
    return Collection.objects.create(owner=anna, name="Mietvertrag")


def make_document(collection, title="Vertrag.txt", status=Document.Status.INDEXED, error=""):
    return Document.objects.create(
        collection=collection,
        title=title,
        file=SimpleUploadedFile("egal.txt", f"Inhalt {PROBE} der Datei".encode()),
        status=status,
        error_text=error,
    )


def make_chunk(document, position=0):
    return Chunk.objects.create(
        document=document,
        position=position,
        text=f"Abschnitt mit {PROBE}",
        page=1,
        embedding=[0.1] * EMBEDDING_DIMENSIONS,
    )


def index_job(document, status=Job.Status.PENDING, **extra):
    return Job.objects.create(
        kind=Job.Kind.INDEX_DOCUMENT, payload={"document_id": document.pk}, status=status, **extra
    )


def overview_url():
    return reverse("admin:rag_ragoverview_changelist")


def action(client, model_name, name, ids, **extra):
    url = reverse(f"admin:rag_{model_name}_changelist")
    data = {"action": name, "_selected_action": [str(i) for i in ids], **extra}
    return client.post(url, data, follow=True)


# --- Abschnitt im Admin-Index -------------------------------------------------------


def test_section_lists_all_entries_overview_first(admin_client):
    index = admin_client.get(reverse("admin:index")).content.decode()
    assert "Dokumente (RAG)" in index
    page = admin_client.get(reverse("admin:app_list", args=["rag"]))
    assert page.status_code == 200
    names = [m["name"] for m in page.context["app_list"][0]["models"]]
    assert names == [
        "RAG-Übersicht",
        "Dokumente",
        "Sammlungen",
        "Verzeichnisquellen",
        "Läufe",
        "Indexierungsaufträge",
        "Einstellungen",
    ]
    html = page.content.decode()
    for model in ("ragoverview", "documentproxy", "collectionproxy", "jobproxy"):
        assert reverse(f"admin:rag_{model}_changelist") in html


def test_old_chat_entries_are_gone(admin_client):
    from django.contrib import admin

    registry = admin.site._registry
    for model in (Collection, Document, Job, RagSettings):
        assert model not in registry
    for model in (RagOverview, RagSettingsProxy, CollectionProxy, DocumentProxy, JobProxy):
        assert model in registry
    assert Share in registry  # Freigaben (auch für Chats) bleiben unter „Chat“.
    for name in ("document", "collection", "job", "ragsettings"):
        with pytest.raises(NoReverseMatch):
            reverse(f"admin:chat_{name}_changelist")
    chat = admin_client.get(reverse("admin:app_list", args=["chat"]))
    labels = [m["name"] for m in chat.context["app_list"][0]["models"]]
    assert not {"Dokumente", "Sammlungen", "Hintergrundjobs", "RAG-Einstellungen"} & set(labels)


def test_other_sections_keep_alphabetical_order(admin_client):
    page = admin_client.get(reverse("admin:app_list", args=["chat"]))
    names = [m["name"] for m in page.context["app_list"][0]["models"]]
    assert names == sorted(names)


# --- Übersicht --------------------------------------------------------------------


def test_overview_shows_counts(admin_client, coll, anna):
    other = Collection.objects.create(owner=anna, name="Rezepte")
    ok = make_document(coll)
    make_chunk(ok, 0)
    make_chunk(ok, 1)
    make_document(coll, "Wartet", Document.Status.PENDING)
    bad = make_document(other, "Kaputt", Document.Status.ERROR, "Kein Text gefunden.")
    index_job(bad, Job.Status.FAILED, last_error="Kein Text gefunden.")
    index_job(ok, Job.Status.DONE)
    later = index_job(ok, run_after=timezone.now() + timedelta(minutes=5))

    response = admin_client.get(overview_url())
    assert response.status_code == 200
    data = response.context["data"]
    assert data.collections == 2
    assert data.documents == 3
    assert data.documents_by_status == {"indexed": 1, "pending": 1, "error": 1}
    assert data.chunks == 2
    assert data.storage_bytes == 3 * len(f"Inhalt {PROBE} der Datei")
    assert data.missing_files == 0
    assert (data.jobs_due, data.jobs_waiting, data.jobs_running, data.jobs_failed) == (0, 1, 0, 1)
    assert data.last_done_job is not None and data.last_done_job != later
    html = response.content.decode()
    assert "nicht gesetzt" in html  # kein Embedding-Modell
    assert "Embedding testen" in html
    assert "Alles neu indexieren" in html
    assert "Worker läuft nicht?" not in html


def test_overview_shows_embedding_model_and_provider(admin_client):
    provider = Provider.objects.create(
        name="OpenAI", kind=Provider.Kind.OPENAI_COMPAT, online=True, last_checked=timezone.now()
    )
    model = AIModel.objects.create(
        provider=provider,
        model_id="text-embedding-3-small",
        display_name="Embedding klein",
        capability=AIModel.Capability.EMBEDDING,
    )
    cfg = RagSettings.load()
    cfg.embedding_model = model
    cfg.save()
    html = admin_client.get(overview_url()).content.decode()
    assert "text-embedding-3-small" in html and "OpenAI" in html and "online" in html


def test_worker_warning_when_jobs_overdue(admin_client, coll):
    doc = make_document(coll, status=Document.Status.PENDING)
    job = index_job(doc, run_after=timezone.now() - timedelta(minutes=10))
    html = admin_client.get(overview_url()).content.decode()
    assert "Worker läuft nicht?" in html
    assert "systemctl status multi-gpt-worker" in html and "make worker" in html

    # Ein Worker arbeitet gerade (frisches Lebenszeichen): kein Alarm.
    other = make_document(coll, "Zweites", Document.Status.PENDING)
    running = index_job(other, Job.Status.RUNNING, locked_at=timezone.now())
    assert "Worker läuft nicht?" not in admin_client.get(overview_url()).content.decode()

    # Lebenszeichen veraltet: Alarm, Auftrag gilt als hängend.
    running.locked_at = timezone.now() - timedelta(minutes=30)
    running.save()
    response = admin_client.get(overview_url())
    assert response.context["data"].jobs_stale == 1
    assert "Worker läuft nicht?" in response.content.decode()

    # Nur frisch fällig: noch kein Alarm.
    running.delete()
    job.run_after = timezone.now()
    job.save()
    assert "Worker läuft nicht?" not in admin_client.get(overview_url()).content.decode()


# --- Aktionen der Übersicht --------------------------------------------------------


def test_reindex_all_needs_confirmation_and_creates_jobs(admin_client, coll):
    docs = [make_document(coll, f"Dok {i}") for i in range(3)]
    url = reverse("admin:rag_overview_reindex_all")
    page = admin_client.get(url)
    assert page.status_code == 200
    assert "Ja, alles neu indexieren" in page.content.decode()
    assert Job.objects.count() == 0  # GET ändert nichts

    response = admin_client.post(url, follow=True)
    assert response.redirect_chain[-1][0] == overview_url()
    assert "3 Dokumente zur Indexierung eingereiht" in response.content.decode()
    assert Job.objects.filter(status=Job.Status.PENDING).count() == 3
    assert {j.payload["document_id"] for j in Job.objects.all()} == {d.pk for d in docs}
    assert set(Document.objects.values_list("status", flat=True)) == {Document.Status.PENDING}
    # Zweimal: wartende Aufträge werden wiederverwendet.
    admin_client.post(url)
    assert Job.objects.count() == 3


def test_retry_failed_requeues(admin_client, coll):
    bad = make_document(coll, "Kaputt", Document.Status.ERROR, "Fehler")
    index_job(bad, Job.Status.FAILED, attempts=5, last_error="Fehler")
    orphan = make_document(coll, "Ohne Auftrag", Document.Status.ERROR, "Fehler")
    url = reverse("admin:rag_overview_retry_failed")
    assert admin_client.get(url).status_code == 405
    admin_client.post(url)
    assert not Job.objects.filter(status=Job.Status.FAILED).exists()
    pending = Job.objects.filter(status=Job.Status.PENDING)
    assert {j.payload["document_id"] for j in pending} == {bad.pk, orphan.pk}
    assert all(j.attempts == 0 for j in pending)
    bad.refresh_from_db()
    assert (bad.status, bad.error_text) == (Document.Status.PENDING, "")


def test_reset_stale_requeues_hanging_jobs(admin_client, coll):
    doc = make_document(coll, status=Document.Status.PENDING)
    stale = index_job(
        doc, Job.Status.RUNNING, attempts=1, locked_at=timezone.now() - timedelta(minutes=30)
    )
    fresh_doc = make_document(coll, "Frisch", Document.Status.PENDING)
    fresh = index_job(fresh_doc, Job.Status.RUNNING, attempts=1, locked_at=timezone.now())
    url = reverse("admin:rag_overview_reset_stale")
    assert admin_client.get(url).status_code == 405
    admin_client.post(url)
    stale.refresh_from_db()
    fresh.refresh_from_db()
    assert stale.status == Job.Status.PENDING and stale.locked_at is None
    assert fresh.status == Job.Status.RUNNING


def test_check_embedding_without_model(admin_client):
    url = reverse("admin:rag_overview_check_embedding")
    assert admin_client.get(url).status_code == 405
    response = admin_client.post(url, follow=True)
    assert response.redirect_chain[-1][0] == overview_url()
    messages = [str(m) for m in response.context["messages"]]
    assert messages and "Embedding" in messages[0]


# --- Dokumente ----------------------------------------------------------------------


def test_document_list_shows_metadata(admin_client, coll):
    doc = make_document(coll, "Vertrag", Document.Status.ERROR, "Die Datei ist beschädigt. " * 10)
    make_chunk(doc)
    response = admin_client.get(reverse("admin:rag_documentproxy_changelist"))
    html = response.content.decode()
    assert "Vertrag" in html and "Mietvertrag" in html and "anna" in html
    assert "rag-status rag-status--error" in html
    assert "Die Datei ist beschädigt." in html
    # Filter nach Status, Sammlung und Besitzer
    for query in (
        "?status__exact=error",
        f"?collection__id__exact={coll.pk}",
        f"?collection__owner__id__exact={coll.owner_id}",
    ):
        assert (
            admin_client.get(reverse("admin:rag_documentproxy_changelist") + query).status_code
            == 200
        )


def test_document_actions(admin_client, coll):
    ok = make_document(coll, "Gut")
    bad = make_document(coll, "Schlecht", Document.Status.ERROR, "Fehler")
    index_job(bad, Job.Status.FAILED)

    action(admin_client, "documentproxy", "retry_action", [ok.pk, bad.pk])
    assert [j.payload["document_id"] for j in Job.objects.all()] == [bad.pk]
    assert Job.objects.get().status == Job.Status.PENDING  # fehlgeschlagener ersetzt
    ok.refresh_from_db()
    assert ok.status == Document.Status.INDEXED

    action(admin_client, "documentproxy", "reindex_action", [ok.pk, bad.pk])
    assert {j.payload["document_id"] for j in Job.objects.all()} == {ok.pk, bad.pk}
    assert Job.objects.count() == 2

    # Knopf auf der Detailseite
    other = make_document(coll, "Drittes")
    url = reverse("admin:rag_documentproxy_reindex", args=[other.pk])
    assert admin_client.get(url).status_code == 405
    admin_client.post(url)
    assert Job.objects.filter(payload__document_id=other.pk).exists()


def test_document_delete_needs_confirmation_and_removes_file(
    admin_client, coll, django_capture_on_commit_callbacks
):
    doc = make_document(coll)
    make_chunk(doc, 0)
    make_chunk(doc, 1)
    path = doc.file.path
    confirm = action(admin_client, "documentproxy", "delete_selected", [doc.pk])
    html = confirm.content.decode()
    assert "Abschnitte" in html and "Vertrag.txt" in html
    assert Document.objects.filter(pk=doc.pk).exists()

    with django_capture_on_commit_callbacks(execute=True):
        action(admin_client, "documentproxy", "delete_selected", [doc.pk], post="yes")
    assert not Document.objects.filter(pk=doc.pk).exists()
    assert not Chunk.objects.exists()
    import os

    assert not os.path.exists(path)


def test_document_single_delete_removes_file(
    admin_client, coll, django_capture_on_commit_callbacks
):
    doc = make_document(coll)
    path = doc.file.path
    url = reverse("admin:rag_documentproxy_delete", args=[doc.pk])
    assert "Vertrag.txt" in admin_client.get(url).content.decode()
    with django_capture_on_commit_callbacks(execute=True):
        admin_client.post(url, {"post": "yes"})
    import os

    assert not Document.objects.exists() and not os.path.exists(path)


def test_document_only_citation_editable(admin_client, coll):
    """Nur die Literaturangaben sind bearbeitbar (ragcite); Titel, Datei usw. nicht."""
    doc = make_document(coll)
    url = reverse("admin:rag_documentproxy_change", args=[doc.pk])
    page = admin_client.get(url)
    assert page.status_code == 200
    assert '<input type="file"' not in page.content.decode()
    before = Document.objects.get(pk=doc.pk)
    response = admin_client.post(
        url, {"title": "Neu", "bib_type": "book", "bib_authors": "Müller, Hans", "bib_date": "2024"}
    )
    assert response.status_code == 302
    after = Document.objects.get(pk=doc.pk)
    assert after.title == before.title
    assert (after.bib_type, after.bib_authors, after.bib_date) == ("book", "Müller, Hans", "2024")
    assert after.bib_edited
    assert admin_client.get(reverse("admin:rag_documentproxy_changelist") + "add/").status_code in (
        403,
        404,
    )


# --- Sammlungen ---------------------------------------------------------------------


def test_collection_list_and_reindex(admin_client, coll):
    group = UserGroup.objects.create(name="Eltern")
    Share.objects.create(collection=coll, group=group, can_write=True)
    docs = [make_document(coll, f"Dok {i}") for i in range(2)]
    make_chunk(docs[0])
    html = admin_client.get(reverse("admin:rag_collectionproxy_changelist")).content.decode()
    assert "Mietvertrag" in html and "Eltern (schreibend)" in html
    detail = admin_client.get(reverse("admin:rag_collectionproxy_change", args=[coll.pk]))
    assert "indexiert: 2" in detail.content.decode()

    action(admin_client, "collectionproxy", "reindex_action", [coll.pk])
    assert {j.payload["document_id"] for j in Job.objects.all()} == {d.pk for d in docs}


# --- Aufträge ----------------------------------------------------------------------


def test_job_actions(admin_client, coll):
    failed_doc = make_document(coll, "A", Document.Status.ERROR, "x")
    failed = index_job(failed_doc, Job.Status.FAILED, attempts=5, last_error="x")
    action(admin_client, "jobproxy", "retry_action", [failed.pk])
    job = Job.objects.get(payload__document_id=failed_doc.pk)
    assert job.status == Job.Status.PENDING and job.attempts == 0

    # Abbrechen: wartender Auftrag weg, Dokument bekommt Hinweis statt ewig zu warten.
    action(admin_client, "jobproxy", "cancel_action", [job.pk])
    assert not Job.objects.exists()
    failed_doc.refresh_from_db()
    assert failed_doc.status == Document.Status.ERROR
    assert failed_doc.error_text == services.CANCELLED_TEXT

    # Laufender Auftrag mit frischem Lebenszeichen bleibt, wird aber markiert:
    # Der Worker beendet ihn nach dem aktuellen Schritt.
    running = index_job(failed_doc, Job.Status.RUNNING, locked_at=timezone.now())
    response = action(admin_client, "jobproxy", "cancel_action", [running.pk])
    running.refresh_from_db()
    assert running.cancel_requested
    assert "nach dem aktuellen Schritt beendet" in response.content.decode()


def test_job_pages_show_ids_only(admin_client, coll):
    doc = make_document(coll)
    job = index_job(doc, last_error="Versuch 1 fehlgeschlagen")
    job.payload = {"document_id": doc.pk, "note": PROBE}
    job.save()
    html = admin_client.get(reverse("admin:rag_jobproxy_changelist")).content.decode()
    assert reverse("admin:rag_documentproxy_change", args=[doc.pk]) in html
    detail = admin_client.get(reverse("admin:rag_jobproxy_change", args=[job.pk])).content.decode()
    assert "Versuch 1 fehlgeschlagen" in detail
    assert PROBE not in html and PROBE not in detail


# --- Einstellungen ----------------------------------------------------------------


def test_settings_page_offers_reindex(admin_client, coll):
    page = admin_client.get(reverse("admin:rag_ragsettingsproxy_changelist"), follow=True)
    html = page.content.decode()
    assert "Embedding testen" in html
    assert reverse("admin:rag_overview_reindex_all") in html

    make_document(coll)
    url = reverse("admin:rag_ragsettingsproxy_change", args=[RagSettings.SINGLETON_PK])
    response = admin_client.post(
        url,
        {
            "embedding_model": "",
            "ocr_backend": "tesseract",
            "ocr_model": "",
            "figure_max_per_document": 50,
            "figure_max_per_page": 10,
            "figure_min_edge": 150,
            "figure_max_edge": 1024,
            "chunk_tokens": 600,
            "overlap_tokens": 50,
            "top_k": 6,
            "hybrid": "on",
        },
        follow=True,
    )
    assert response.status_code == 200
    assert RagSettings.load().chunk_tokens == 600
    text = " ".join(str(m) for m in response.context["messages"])
    assert "alles neu" in text.lower()


# --- Privatsphäre und Rechte -------------------------------------------------------


def _all_get_urls(coll, doc, job):
    return [
        reverse("admin:index"),
        reverse("admin:app_list", args=["rag"]),
        reverse("admin:app_list", args=["chat"]),
        overview_url(),
        reverse("admin:rag_overview_reindex_all"),
        reverse("admin:rag_documentproxy_changelist"),
        reverse("admin:rag_documentproxy_changelist") + "?q=Vertrag",
        reverse("admin:rag_documentproxy_change", args=[doc.pk]),
        reverse("admin:rag_documentproxy_delete", args=[doc.pk]),
        reverse("admin:rag_collectionproxy_changelist"),
        reverse("admin:rag_collectionproxy_change", args=[coll.pk]),
        reverse("admin:rag_jobproxy_changelist"),
        reverse("admin:rag_jobproxy_change", args=[job.pk]),
        reverse("admin:rag_ragsettingsproxy_change", args=[RagSettings.SINGLETON_PK]),
        reverse("admin:chat_share_changelist"),
    ]


def test_no_content_on_any_admin_page(admin_client, coll):
    doc = make_document(coll)
    make_chunk(doc)
    job = index_job(doc)
    for url in _all_get_urls(coll, doc, job):
        response = admin_client.get(url, follow=True)
        assert response.status_code == 200, url
        assert PROBE not in response.content.decode(), url
    confirm = action(admin_client, "documentproxy", "delete_selected", [doc.pk])
    assert PROBE not in confirm.content.decode()
    # Ohne Abschnittsliste: keine Links auf einzelne Abschnitte.
    assert not re.search(r"/admin/chat/chunk/", confirm.content.decode())


@pytest.mark.parametrize("role_key", ["adult", "teen", "guest"])
def test_non_admin_gets_403(client, coll, role_key):
    doc = make_document(coll, status=Document.Status.ERROR, error="x")
    job = index_job(doc, Job.Status.FAILED)
    client.force_login(make_user(f"u_{role_key}", role_key))
    for url in _all_get_urls(coll, doc, job):
        assert client.get(url).status_code == 403, url
    for url in (
        reverse("admin:rag_overview_reindex_all"),
        reverse("admin:rag_overview_retry_failed"),
        reverse("admin:rag_overview_reset_stale"),
        reverse("admin:rag_overview_check_embedding"),
        reverse("admin:rag_documentproxy_reindex", args=[doc.pk]),
    ):
        assert client.post(url).status_code == 403, url
    assert (
        client.post(
            reverse("admin:rag_documentproxy_changelist"),
            {"action": "delete_selected", "_selected_action": [doc.pk], "post": "yes"},
        ).status_code
        == 403
    )
    assert Document.objects.filter(pk=doc.pk).exists()
    assert list(Job.objects.values_list("status", flat=True)) == [Job.Status.FAILED]


def test_admin_role_without_superuser_has_access(client):
    user = make_user("verwalter", "admin")
    assert not user.is_superuser
    client.force_login(user)
    assert client.get(overview_url()).status_code == 200
    assert client.get(reverse("admin:rag_documentproxy_changelist")).status_code == 200


def test_actions_require_csrf(coll):
    from django.test import Client

    user = User.objects.create_superuser(username="jo2", password=PASSWORD, email="x@y.invalid")
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(user)
    make_document(coll)
    assert csrf_client.post(reverse("admin:rag_overview_reindex_all")).status_code == 403
    assert not Job.objects.exists()


def test_site_is_family_admin_site():
    from django.contrib import admin

    assert isinstance(admin.site, FamilyAdminSite)
