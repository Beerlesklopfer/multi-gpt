"""Dokumentverarbeitung lokal (M7-09): 768 Dimensionen, nomic-Präfixe, LM Studio
als Embedding-Anbieter, Auswahl gemeldeter Modelle im Admin, olmOCR mit
Tesseract als Ersatz, Warten bei nicht erreichbarem Anbieter.

Kein echter Anbieteraufruf: HTTP über respx, Tesseract gemockt.
"""

import base64
import io
import json
import shutil
from datetime import timedelta
from pathlib import Path
from unittest import mock

import httpx
import pytest
import respx
from django.urls import reverse
from django.utils import timezone

from multigpt.accounts.models import Role, User
from multigpt.chat.models import (
    EMBEDDING_DIMENSIONS,
    AIModel,
    Collection,
    Document,
    Job,
    Provider,
    RagSettings,
)
from multigpt.chat.providers.base import ProviderError
from multigpt.chat.providers.openai_compat import OpenAICompatAdapter
from multigpt.chat.rag import embeddings, extract, ingest, jobs, ocr
from multigpt.chat.rag.embeddings import EmbeddingError
from multigpt.rag import settings_form

pytestmark = pytest.mark.django_db

DATA = Path(__file__).resolve().parent / "data"
PASSWORD = "Geheim-Test-1234"
LMSTUDIO = "http://lmstudio.example.invalid:1234/v1"
SECRET = "sk-test-geheim-9876"
NOMIC = "text-embedding-nomic-embed-text-v1.5"
OLMOCR = "allenai/olmocr-2-7b"
needs_pdftoppm = pytest.mark.skipif(not shutil.which("pdftoppm"), reason="pdftoppm fehlt")

OLMOCR_ANSWER = (
    "---\nprimary_language: de\nis_rotation_valid: True\nrotation_correction: 0\n"
    "is_table: False\nis_diagram: False\n---\n"
    "Mietvertrag Seite 1\n\nDie Kaltmiete beträgt 850 Euro im Monat.\n"
    "Die Kaution beträgt drei Monatsmieten. Prüfnummer 4711."
)


def payload(vectors):
    return {"data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)]}


def vec(first=1.0, n=EMBEDDING_DIMENSIONS):
    return [first] + [0.0] * (n - 1)


def completion(text, finish="stop"):
    return {
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": finish}
        ]
    }


@pytest.fixture
def lmstudio():
    return Provider.objects.create(
        name="LM Studio",
        kind=Provider.Kind.OPENAI_COMPAT,
        base_url=LMSTUDIO,
        is_local=True,
        reported_models=["openai/gpt-oss-20b", NOMIC, OLMOCR],
    )


@pytest.fixture
def nomic(lmstudio):
    model = AIModel.objects.create(
        provider=lmstudio,
        model_id=NOMIC,
        display_name=NOMIC,
        capability=AIModel.Capability.EMBEDDING,
    )
    cfg = RagSettings.load()
    cfg.embedding_model = model
    cfg.document_prefix, cfg.query_prefix = embeddings.suggested_prefixes(NOMIC)
    cfg.save()
    return model


@pytest.fixture
def olmocr_model(lmstudio):
    model = AIModel.objects.create(
        provider=lmstudio, model_id=OLMOCR, display_name="olmOCR", active=False
    )
    cfg = RagSettings.load()
    cfg.ocr_backend = RagSettings.OcrBackend.OLMOCR
    cfg.ocr_model = model
    cfg.ocr_fallback_tesseract = False
    cfg.save()
    return model


@pytest.fixture
def admin_client(client):
    user = User.objects.create_superuser(
        username="jo", password=PASSWORD, email="jo@example.invalid"
    )
    client.force_login(user)
    return client


@pytest.fixture
def media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    return settings.MEDIA_ROOT


def scanned_document():
    owner = User.objects.create_user("anna", password=PASSWORD, role=Role.objects.get(key="adult"))
    coll = Collection.objects.create(owner=owner, name="Scans")
    doc = Document(collection=coll, title="scan")
    doc.file.save("scanned.pdf", io.BytesIO((DATA / "scanned.pdf").read_bytes()), save=False)
    doc.save()
    return doc


# --- Dimension und Präfixe ---------------------------------------------------------


def test_dimension_is_768():
    assert EMBEDDING_DIMENSIONS == 768
    field = __import__("multigpt.chat.models", fromlist=["Chunk"]).Chunk._meta.get_field(
        "embedding"
    )
    assert field.dimensions == 768


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("text-embedding-3-small", True),
        ("text-embedding-3-large", True),
        ("openai/text-embedding-3-small", True),
        ("text-embedding-ada-002", False),
        (NOMIC, False),
        ("nomic-embed-text", False),
    ],
)
def test_supports_dimensions_only_for_openai_v3(model_id, expected):
    assert embeddings.supports_dimensions(model_id) is expected


def test_suggested_prefixes():
    assert embeddings.suggested_prefixes(NOMIC) == ("search_document: ", "search_query: ")
    assert embeddings.suggested_prefixes("nomic-ai/nomic-embed-text-v1") == (
        "search_document: ",
        "search_query: ",
    )
    assert embeddings.suggested_prefixes("text-embedding-3-small") == ("", "")


@respx.mock
def test_nomic_prefixes_and_no_dimensions_in_request_body(nomic):
    route = respx.post(f"{LMSTUDIO}/embeddings").mock(
        side_effect=lambda request: httpx.Response(
            200, json=payload([vec() for _ in json.loads(request.content)["input"]])
        )
    )
    vectors = embeddings.embed_texts(["Kaution", "Miete"])
    assert len(vectors) == 2 and len(vectors[0]) == 768
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == NOMIC
    assert body["input"] == ["search_document: Kaution", "search_document: Miete"]
    assert "dimensions" not in body
    assert "Authorization" not in route.calls[0].request.headers  # LM Studio ohne Key

    embeddings.embed_query("Wie hoch ist die Kaution?")
    body = json.loads(route.calls[1].request.content)
    assert body["input"] == ["search_query: Wie hoch ist die Kaution?"]


@respx.mock
def test_nomic_with_wrong_dimension_gives_clear_error(nomic):
    respx.post(f"{LMSTUDIO}/embeddings").mock(
        return_value=httpx.Response(200, json=payload([vec(n=1536)]))
    )
    with pytest.raises(EmbeddingError) as info:
        embeddings.embed_texts(["x"])
    assert "1536 statt 768 Dimensionen" in info.value.message
    assert "passt nicht zur Datenbank" in info.value.message
    assert info.value.retryable is False


def test_local_embedding_provider_is_allowed(nomic):
    cfg = RagSettings.load()
    cfg.full_clean()  # keine Sperre für LM Studio mehr


@respx.mock
def test_embedding_check_with_lmstudio(nomic):
    route = respx.post(f"{LMSTUDIO}/embeddings").mock(
        return_value=httpx.Response(200, json=payload([vec()]))
    )
    level, text = embeddings.check()
    assert level == "ok" and "768 Dimensionen" in text
    assert json.loads(route.calls[0].request.content)["input"][0].startswith("search_query: ")


# --- LM Studio offline ------------------------------------------------------------


@respx.mock
def test_offline_embedding_is_unreachable(nomic):
    respx.post(f"{LMSTUDIO}/embeddings").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(EmbeddingError) as info:
        embeddings.embed_texts(["x"])
    assert info.value.unreachable is True and info.value.retryable is True


@respx.mock
def test_query_fails_fast_when_status_says_offline(nomic, lmstudio):
    lmstudio.check_status = True
    lmstudio.online = False
    lmstudio.last_checked = timezone.now()
    lmstudio.save()
    route = respx.post(f"{LMSTUDIO}/embeddings")
    with pytest.raises(EmbeddingError) as info:
        embeddings.embed_query("Frage")
    assert "nicht erreichbar" in info.value.message and info.value.unreachable
    assert not route.called


@respx.mock
def test_chat_answers_without_documents_when_lmstudio_offline(nomic, lmstudio):
    from multigpt.chat.rag import chat as rag_chat

    lmstudio.check_status = True
    lmstudio.last_checked = timezone.now()
    lmstudio.save()
    user = User.objects.create_user("bea", password=PASSWORD, role=Role.objects.get(key="adult"))
    coll = Collection.objects.create(owner=user, name="Ordner")
    turn = mock.Mock(options={"collections": [coll.pk]}, query="Frage", user=user)
    gen = rag_chat.document_context(turn, sources=mock.Mock())
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        result = stop.value
    assert any("ohne Dokumentquellen" in e[1]["text"] for e in events if e[0] == "status")
    assert result.notice and not result.entries


def test_unreachable_job_waits_without_counting_attempt(media, settings):
    settings.JOB_MAX_ATTEMPTS = 1
    doc = scanned_document()
    job = jobs.enqueue_index(doc)
    claimed = jobs.claim_next()
    assert claimed.pk == job.pk and claimed.attempts == 1
    error = ingest.IngestError("Der Anbieter ist nicht erreichbar.", unreachable=True)
    with mock.patch.object(ingest, "index_document", side_effect=error):
        status = jobs.run_job(claimed)
    claimed.refresh_from_db()
    doc.refresh_from_db()
    assert status == Job.Status.PENDING  # trotz JOB_MAX_ATTEMPTS = 1 nicht aufgegeben
    assert claimed.attempts == 0
    assert claimed.run_after >= timezone.now() + jobs.OFFLINE_RETRY - timedelta(seconds=5)
    assert doc.status == Document.Status.PENDING
    assert doc.error_text.startswith("Wartet: Der Anbieter ist nicht erreichbar.")


# --- olmOCR: Anfrage und Antwort ----------------------------------------------------------


@respx.mock
def test_describe_image_request_body():
    adapter = OpenAICompatAdapter(
        Provider(name="LM Studio", kind=Provider.Kind.OPENAI_COMPAT, base_url=LMSTUDIO)
    )
    route = respx.post(f"{LMSTUDIO}/chat/completions").mock(
        return_value=httpx.Response(200, json=completion("Text"))
    )
    png = b"\x89PNG\r\n\x1a\nbild"
    text = adapter.describe_image(OLMOCR, png, ocr.OLMOCR_PROMPT, temperature=0.1, max_tokens=8000)
    assert text == "Text"
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == OLMOCR and body["stream"] is False
    assert body["temperature"] == 0.1 and body["max_tokens"] == 8000
    content = body["messages"][0]["content"]
    assert body["messages"][0]["role"] == "user"
    assert content[0] == {"type": "text", "text": ocr.OLMOCR_PROMPT}
    url = content[1]["image_url"]["url"]
    assert content[1]["type"] == "image_url" and url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == png


def test_olmocr_prompt_is_v4_yaml_prompt():
    assert ocr.OLMOCR_PROMPT.startswith(
        "Attached is one page of a document that you must process. Just return the plain text"
    )
    assert "Convert equations to LateX and tables to HTML.\n" in ocr.OLMOCR_PROMPT
    assert ocr.OLMOCR_PROMPT.endswith(
        "primary_language, is_rotation_valid, rotation_correction, is_table, and is_diagram "
        "parameters."
    )
    assert ocr.OLMOCR_LONGEST_DIM == 1288


@pytest.mark.parametrize(
    ("side_effect", "unreachable", "retryable", "fragment"),
    [
        (httpx.ConnectError("refused"), True, True, "nicht erreichbar"),
        (httpx.Response(401, json={"error": {"message": SECRET}}), False, False, "API-Key"),
        (httpx.Response(503, text=f"kaputt {SECRET}"), False, True, "Serverfehler"),
        (httpx.Response(200, json={"choices": []}), False, False, "unerwartete Antwort"),
    ],
)
@respx.mock
def test_describe_image_errors_without_raw_text(side_effect, unreachable, retryable, fragment):
    adapter = OpenAICompatAdapter(
        Provider(name="X", kind=Provider.Kind.OPENAI_COMPAT, base_url=LMSTUDIO, api_key=SECRET)
    )
    respx.post(f"{LMSTUDIO}/chat/completions").mock(side_effect=[side_effect])
    with pytest.raises(ProviderError) as info:
        adapter.describe_image(OLMOCR, b"png", "prompt")
    assert getattr(info.value, "unreachable", False) is unreachable
    assert info.value.retryable is retryable
    assert fragment in str(info.value) and SECRET not in str(info.value)


def test_parse_olmocr_removes_front_matter_and_converts_tables():
    page = ocr.parse_olmocr(
        "---\nprimary_language: de\nis_rotation_valid: true\nrotation_correction: 0\n"
        "is_table: true\nis_diagram: false\n---\n# Nebenkosten\n\n"
        "<table><tr><th>Posten</th><th>Betrag</th></tr>"
        "<tr><td>Wasser</td><td>30 &euro;</td></tr></table>\n"
        "![Balkendiagramm der Kosten](page_10_20_300_200.png)\nEnde"
    )
    assert page.front_matter["primary_language"] == "de"
    assert page.front_matter["is_table"] is True
    assert "---" not in page.text and "primary_language" not in page.text
    assert "# Nebenkosten" in page.text
    assert "Posten | Betrag\nWasser | 30 €" in page.text
    assert "[Abbildung: Balkendiagramm der Kosten]" in page.text and "<t" not in page.text
    assert page.rotation_valid and page.rotation_correction == 0


def test_parse_olmocr_without_front_matter_and_rotation():
    assert ocr.parse_olmocr("Nur Text").text == "Nur Text"
    page = ocr.parse_olmocr(
        "---\nprimary_language: null\nis_rotation_valid: False\nrotation_correction: 90\n"
        "is_table: False\nis_diagram: False\n---\n"
    )
    assert page.text == "" and not page.rotation_valid and page.rotation_correction == 90


@needs_pdftoppm
def test_render_page_longest_edge_1288():
    png = ocr.render_page(str(DATA / "scanned.pdf"), 1)
    assert png.startswith(b"\x89PNG")
    width = int.from_bytes(png[16:20], "big")
    height = int.from_bytes(png[20:24], "big")
    assert max(width, height) == 1288
    rotated = ocr.render_page(str(DATA / "scanned.pdf"), 1, rotation=90)
    assert int.from_bytes(rotated[16:20], "big") == 1288  # jetzt quer


@needs_pdftoppm
@respx.mock
def test_olmocr_page_rereads_rotated_page(olmocr_model):
    wrong = "---\nis_rotation_valid: False\nrotation_correction: 90\n---\nnsinn"
    route = respx.post(f"{LMSTUDIO}/chat/completions").mock(
        side_effect=[
            httpx.Response(200, json=completion(wrong)),
            httpx.Response(200, json=completion(OLMOCR_ANSWER)),
        ]
    )
    text = ocr.olmocr_page(olmocr_model, str(DATA / "scanned.pdf"), 1)
    assert "850 Euro" in text and route.call_count == 2


@needs_pdftoppm
@respx.mock
def test_olmocr_truncated_answer_is_retried_warmer(olmocr_model):
    route = respx.post(f"{LMSTUDIO}/chat/completions").mock(
        side_effect=[
            httpx.Response(200, json=completion("Wort Wort Wort", finish="length")),
            httpx.Response(200, json=completion(OLMOCR_ANSWER)),
        ]
    )
    assert "850 Euro" in ocr.olmocr_page(olmocr_model, str(DATA / "scanned.pdf"), 1)
    temps = [json.loads(c.request.content)["temperature"] for c in route.calls]
    assert temps == [0.1, 0.2]


# --- olmOCR in der Indexierung ----------------------------------------------------------------


def fake_vectors(texts):
    return [vec() for _ in texts]


@needs_pdftoppm
@respx.mock
def test_index_scanned_pdf_with_olmocr(media, olmocr_model):
    route = respx.post(f"{LMSTUDIO}/chat/completions").mock(
        return_value=httpx.Response(200, json=completion(OLMOCR_ANSWER))
    )
    doc = scanned_document()
    with mock.patch.object(ingest, "_embed", side_effect=fake_vectors) as embed:
        result = ingest.index_document(doc)
    assert route.called and result.ocr_pages == 1
    texts = [t for call in embed.call_args_list for t in call.args[0]]
    joined = " ".join(texts)
    assert "Kaltmiete beträgt 850 Euro" in joined
    assert "primary_language" not in joined and "---" not in joined
    assert doc.chunks.exists()


@needs_pdftoppm
@respx.mock
def test_olmocr_offline_falls_back_to_tesseract(media, olmocr_model):
    cfg = RagSettings.load()
    cfg.ocr_fallback_tesseract = True
    cfg.save()
    route = respx.post(f"{LMSTUDIO}/chat/completions").mock(
        side_effect=httpx.ConnectError("refused")
    )
    tesseract = mock.Mock(return_value="Mietvertrag Kaltmiete 850 Euro im Monat laut Tesseract")
    doc = scanned_document()
    with (
        mock.patch.object(extract, "ocr_available", return_value=True),
        mock.patch.object(extract, "ocr_pdf_page", tesseract),
        mock.patch.object(ingest, "_embed", side_effect=fake_vectors),
    ):
        result = ingest.index_document(doc)
    assert route.call_count == 1 and tesseract.call_count == 1
    assert result.ocr_pages == 1
    assert "laut Tesseract" in doc.chunks.first().text


@needs_pdftoppm
@respx.mock
def test_olmocr_offline_without_fallback_is_retryable(media, olmocr_model):
    respx.post(f"{LMSTUDIO}/chat/completions").mock(side_effect=httpx.ConnectError("refused"))
    tesseract = mock.Mock(return_value="soll nicht genutzt werden")
    doc = scanned_document()
    with (
        mock.patch.object(extract, "ocr_available", return_value=True),
        mock.patch.object(extract, "ocr_pdf_page", tesseract),
        mock.patch.object(ingest, "_embed", side_effect=fake_vectors),
        pytest.raises(ingest.IngestError) as info,
    ):
        ingest.index_document(doc)
    assert info.value.retryable and info.value.unreachable
    assert "nicht erreichbar" in info.value.message
    assert not tesseract.called and not doc.chunks.exists()


def test_tesseract_backend_uses_tesseract():
    cfg = RagSettings.load()
    assert cfg.ocr_backend == RagSettings.OcrBackend.TESSERACT
    assert ocr.page_reader(cfg) is extract.ocr_pdf_page


def test_olmocr_without_model_and_without_fallback_fails():
    cfg = RagSettings.load()
    cfg.ocr_backend = RagSettings.OcrBackend.OLMOCR
    cfg.ocr_fallback_tesseract = False
    read = ocr.page_reader(cfg)
    with pytest.raises(ocr.OcrError, match="kein OCR-Modell"):
        read("egal.pdf", 1)


# --- Admin: Auswahl gemeldeter Modelle -------------------------------------------------------


def settings_post(**overrides):
    data = {
        "embedding_model": "",
        "document_prefix": "",
        "query_prefix": "",
        "ocr_backend": "tesseract",
        "ocr_model": "",
        "ocr_fallback_tesseract": "on",
        "chunk_tokens": 800,
        "overlap_tokens": 100,
        "top_k": 6,
        "hybrid": "on",
    }
    data.update(overrides)
    return data


def settings_url():
    return reverse("admin:rag_ragsettingsproxy_change", args=[RagSettings.SINGLETON_PK])


def test_choices_offer_reported_models_grouped_by_provider(lmstudio):
    existing = AIModel.objects.create(
        provider=lmstudio, model_id="openai/gpt-oss-20b", display_name="GPT-OSS"
    )
    emb = dict(settings_form.model_choices(settings_form.EMBEDDING)[1:])
    assert emb["LM Studio"] == [(f"r{lmstudio.pk}:{NOMIC}", f"LM Studio · {NOMIC} (neu)")]
    ocr_choices = dict(settings_form.model_choices(settings_form.OCR)[1:])
    assert ocr_choices["Empfohlen"] == [(f"r{lmstudio.pk}:{OLMOCR}", f"LM Studio · {OLMOCR} (neu)")]
    local = ocr_choices["LM Studio"]
    assert (f"m{existing.pk}", "LM Studio · GPT-OSS (openai/gpt-oss-20b)") in local
    assert all(NOMIC not in value and OLMOCR not in value for value, _ in local)


def test_ocr_choices_recommended_first_local_before_cloud(lmstudio):
    cloud = Provider.objects.create(
        name="Chat GPT",
        kind=Provider.Kind.OPENAI_COMPAT,
        api_key=SECRET,
        reported_models=[f"gpt-4o-{i}" for i in range(100)]
        + ["text-embedding-3-small", "qwen2.5-vl-72b", "mistral-ocr-latest"],
    )
    kept = AIModel.objects.create(provider=cloud, model_id="gpt-4o", display_name="GPT-4o")
    other_local = Provider.objects.create(
        name="Z-Rechner",
        kind=Provider.Kind.OPENAI_COMPAT,
        base_url="http://z.example.invalid/v1",
        is_local=True,
        reported_models=["allenai/olmocr-2-7b-q8", "llama-3"],
    )
    choices = settings_form.model_choices(settings_form.OCR)
    groups = [group for group, _ in choices[1:]]
    assert groups == ["Empfohlen", "LM Studio", "Z-Rechner", "Chat GPT"]
    recommended = [label for _, label in choices[1][1]]
    assert recommended == [
        f"LM Studio · {OLMOCR} (neu)",
        "Z-Rechner · allenai/olmocr-2-7b-q8 (neu)",
    ]
    cloud_values = [value for value, _ in dict(choices[1:])["Chat GPT"]]
    # Keine 100 Chatmodelle als „neu“, nur Vision-/OCR-Muster und Angelegtes.
    assert sorted(cloud_values) == sorted(
        [f"m{kept.pk}", f"r{cloud.pk}:qwen2.5-vl-72b", f"r{cloud.pk}:mistral-ocr-latest"]
    )
    # Lokal werden alle gemeldeten (außer Embedding usw.) angeboten.
    assert f"r{other_local.pk}:llama-3" in dict(dict(choices[1:])["Z-Rechner"])


def test_olmocr_is_preselected_when_no_ocr_model(lmstudio):
    form = settings_form.RagSettingsForm(instance=RagSettings.load())
    assert form.initial["ocr_model"] == f"r{lmstudio.pk}:{OLMOCR}"
    other = AIModel.objects.create(provider=lmstudio, model_id="llama-3", display_name="Llama")
    cfg = RagSettings.load()
    cfg.ocr_model = other
    cfg.save()
    form = settings_form.RagSettingsForm(instance=RagSettings.load())
    assert form.initial["ocr_model"] == f"m{other.pk}"  # gesetztes Modell bleibt


def test_prefix_labels_are_capitalized_correctly(admin_client):
    html = admin_client.get(settings_url()).content.decode()
    assert "Präfix für Abschnitte" in html and "Präfix für Suchanfragen" in html
    assert "Präfix für abschnitte" not in html and "Präfix für suchanfragen" not in html
    for label in ("Embedding-Modell", "OCR-Verfahren", "OCR-Modell", "Tesseract als Ersatz"):
        assert label in html
    assert "rag/settings_form.js" in html


def test_selecting_reported_embedding_model_creates_it(admin_client, lmstudio):
    page = admin_client.get(settings_url())
    html = page.content.decode()
    assert f"LM Studio · {NOMIC} (neu)" in html and "OCR testen" in html
    response = admin_client.post(
        settings_url(),
        settings_post(
            embedding_model=f"r{lmstudio.pk}:{NOMIC}",
            ocr_backend="olmocr",
            ocr_model=f"r{lmstudio.pk}:{OLMOCR}",
        ),
        follow=True,
    )
    assert response.status_code == 200
    assert not response.context.get("errors"), response.context.get("errors")
    cfg = RagSettings.load()
    emb = cfg.embedding_model
    assert emb.model_id == NOMIC and emb.provider == lmstudio
    assert emb.capability == AIModel.Capability.EMBEDDING and emb.active
    assert cfg.document_prefix == "search_document: " and cfg.query_prefix == "search_query: "
    ocr_model = cfg.ocr_model
    assert ocr_model.model_id == OLMOCR and ocr_model.capability == AIModel.Capability.CHAT
    assert ocr_model.active is False  # nur für OCR, nicht im Chat
    assert cfg.ocr_backend == "olmocr"
    messages = " ".join(str(m) for m in response.context["messages"])
    assert "angelegt" in messages
    # Erneutes Speichern mit den nun vorhandenen Modellen legt nichts doppelt an.
    admin_client.post(
        settings_url(),
        settings_post(
            embedding_model=f"m{emb.pk}",
            document_prefix="search_document: ",
            query_prefix="search_query: ",
            ocr_backend="olmocr",
            ocr_model=f"m{ocr_model.pk}",
        ),
    )
    assert AIModel.objects.filter(model_id__in=[NOMIC, OLMOCR]).count() == 2


def test_existing_model_keeps_manual_prefixes(admin_client, nomic):
    admin_client.post(
        settings_url(),
        settings_post(embedding_model=f"m{nomic.pk}", document_prefix="", query_prefix="x: "),
    )
    cfg = RagSettings.load()
    assert cfg.document_prefix == "" and cfg.query_prefix == "x: "


def test_unknown_reported_model_is_rejected(admin_client, lmstudio):
    response = admin_client.post(
        settings_url(), settings_post(embedding_model=f"r{lmstudio.pk}:boese-id")
    )
    assert response.status_code == 200  # Formular mit Fehler
    assert not AIModel.objects.filter(model_id="boese-id").exists()
    assert RagSettings.load().embedding_model is None


def test_olmocr_requires_model(admin_client):
    response = admin_client.post(settings_url(), settings_post(ocr_backend="olmocr"))
    assert "Vision-Modell" in response.content.decode()
    assert RagSettings.load().ocr_backend == "tesseract"


# --- Admin: OCR testen und Übersicht -----------------------------------------------------------


@needs_pdftoppm
@respx.mock
def test_admin_ocr_check_olmocr(admin_client, olmocr_model):
    route = respx.post(f"{LMSTUDIO}/chat/completions").mock(
        side_effect=[
            httpx.Response(200, json=completion(OLMOCR_ANSWER)),
            httpx.ConnectError("refused"),
        ]
    )
    url = reverse("admin:rag_ragsettingsproxy_check_ocr", args=[RagSettings.SINGLETON_PK])
    assert admin_client.get(url).status_code == 405
    text = admin_client.post(url, follow=True).content.decode()
    assert "OCR-Test mit olmOCR" in text and "erfolgreich" in text and "850" in text
    body = json.loads(route.calls[0].request.content)
    assert body["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png")
    text = admin_client.post(reverse("admin:rag_overview_check_ocr"), follow=True)
    text = text.content.decode()
    assert "fehlgeschlagen" in text and "nicht erreichbar" in text


def test_admin_ocr_check_names_exactly_what_is_missing(admin_client):
    url = reverse("admin:rag_overview_check_ocr")

    def which(program):
        return None if program == "tesseract" else f"/usr/bin/{program}"

    with mock.patch.object(ocr.shutil, "which", side_effect=which):
        text = admin_client.post(url, follow=True).content.decode()
    assert "OCR-Test mit Tesseract fehlgeschlagen: Tesseract ist nicht installiert" in text
    assert "pdftoppm" not in text


@needs_pdftoppm
@respx.mock
def test_ocr_check_olmocr_names_model_and_missing_fallback(olmocr_model):
    cfg = RagSettings.load()
    cfg.ocr_fallback_tesseract = True
    cfg.save()
    respx.post(f"{LMSTUDIO}/chat/completions").mock(
        side_effect=[httpx.Response(200, json=completion(OLMOCR_ANSWER)), httpx.ConnectError("x")]
    )
    with mock.patch.object(ocr.shutil, "which", side_effect=lambda p: None):
        level, text = ocr.check()
        assert level == "warning"
        assert text.startswith(f"OCR-Test mit olmOCR (LM Studio · {OLMOCR}) erfolgreich")
        assert "Tesseract (Ersatz) ist nicht nutzbar" in text
        level, text = ocr.check()
    assert level == "error"
    assert text.startswith(f"OCR-Test mit olmOCR (LM Studio · {OLMOCR}) fehlgeschlagen")
    assert "nicht erreichbar" in text and "Tesseract (Ersatz)" in text


@needs_pdftoppm
@respx.mock
def test_save_and_test_ocr_uses_form_values(admin_client, lmstudio):
    # Gespeichert ist Tesseract; im Formular steht olmOCR – getestet wird olmOCR.
    route = respx.post(f"{LMSTUDIO}/chat/completions").mock(
        return_value=httpx.Response(200, json=completion(OLMOCR_ANSWER))
    )
    response = admin_client.post(
        settings_url(),
        settings_post(
            ocr_backend="olmocr",
            ocr_model=f"r{lmstudio.pk}:{OLMOCR}",
            _save_and_test_ocr="Speichern und OCR testen",
        ),
        follow=True,
    )
    text = " ".join(str(m) for m in response.context["messages"])
    assert f"OCR-Test mit olmOCR (LM Studio · {OLMOCR}) erfolgreich" in text
    assert route.called
    assert response.redirect_chain[-1][0].endswith(settings_url())
    assert RagSettings.load().ocr_backend == "olmocr"


@respx.mock
def test_save_and_test_embedding_uses_form_values(admin_client, lmstudio):
    route = respx.post(f"{LMSTUDIO}/embeddings").mock(
        return_value=httpx.Response(200, json=payload([vec()]))
    )
    response = admin_client.post(
        settings_url(),
        settings_post(
            embedding_model=f"r{lmstudio.pk}:{NOMIC}",
            _save_and_test_embedding="Speichern und Embedding testen",
        ),
        follow=True,
    )
    text = " ".join(str(m) for m in response.context["messages"])
    assert "Embedding erfolgreich" in text and "768 Dimensionen" in text
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == NOMIC and body["input"][0].startswith("search_query: ")


def test_invalid_form_is_not_tested(admin_client):
    with mock.patch.object(ocr, "check") as check:
        response = admin_client.post(
            settings_url(), settings_post(ocr_backend="olmocr", _save_and_test_ocr="x")
        )
    assert response.status_code == 200 and "Vision-Modell" in response.content.decode()
    assert not check.called


def test_model_with_tesseract_backend_warns(admin_client, lmstudio):
    model = AIModel.objects.create(provider=lmstudio, model_id=OLMOCR, display_name="olm")
    response = admin_client.post(
        settings_url(), settings_post(ocr_model=f"m{model.pk}"), follow=True
    )
    text = " ".join(str(m) for m in response.context["messages"])
    assert "steht aber auf „Tesseract“" in text
    cfg = RagSettings.load()
    assert cfg.ocr_model == model and cfg.ocr_backend == "tesseract"  # kein Zwang


def test_overview_shows_ocr_and_dimension(admin_client, nomic, olmocr_model, lmstudio):
    lmstudio.online = True
    lmstudio.last_checked = timezone.now()
    lmstudio.save()
    html = admin_client.get(reverse("admin:rag_ragoverview_changelist")).content.decode()
    assert "OCR-Verfahren" in html and "olmOCR" in html and "OCR testen" in html
    assert "Vektordimension" in html and "768" in html
    assert "search_document:" in html
    assert "online" in html


def test_ocr_http_400_hints_at_non_vision_model():
    from multigpt.chat.providers.base import ProviderHTTPError
    from multigpt.chat.rag.ocr import _provider_error

    error = _provider_error(ProviderHTTPError("Anfrage abgelehnt (HTTP 400).", 400))
    assert "kein Vision-Modell" in str(error)
    assert "olmocr" in str(error)
    other = _provider_error(ProviderHTTPError("Serverfehler (HTTP 500).", 500, retryable=True))
    assert "Vision" not in str(other)


def test_describe_image_shows_local_provider_error_text():
    import httpx
    import respx

    from multigpt.chat.models import Provider
    from multigpt.chat.providers.base import ProviderHTTPError
    from multigpt.chat.providers.openai_compat import OpenAICompatAdapter

    local = Provider(name="LM Studio", kind="openai_compat", base_url="http://lm.test:1234/v1")
    cloud = Provider(name="Cloud", kind="openai_compat", base_url="http://lm.test:1234/v1")
    cloud.api_key = "sk-geheim"
    body = {"error": "Failed to load model 'allenai/olmocr-2-7b'. Error: out of memory"}
    with respx.mock() as router:
        router.post("http://lm.test:1234/v1/chat/completions").mock(
            return_value=httpx.Response(400, json=body)
        )
        for provider, expect in ((local, True), (cloud, False)):
            try:
                OpenAICompatAdapter(provider).describe_image("m", b"\x89PNG", "Lies.")
            except ProviderHTTPError as exc:
                assert ("Failed to load model" in str(exc)) is expect
                assert exc.retryable is expect
