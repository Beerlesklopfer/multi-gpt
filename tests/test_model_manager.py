"""Modellverwaltung für LM Studio (M7-09, erweitert 0.3.2): Schätzung, Budget,
Entladeregeln, Sperre, Backoff, Laden vor Chat/Embeddings/OCR, Leerlauf,
Admin und CLI.

Kein echtes LM Studio: Die REST-API wird mit respx nachgebildet (``FakeLms``).
"""

import io
import json
import threading
import time
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
import respx
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connections
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from multigpt.accounts.models import Role, User
from multigpt.chat import lmstudio, model_manager, services, vram
from multigpt.chat.models import (
    EMBEDDING_DIMENSIONS,
    AIModel,
    Conversation,
    IndexRun,
    Message,
    ModelLease,
    ModelRuntime,
    Provider,
    RagSettings,
)
from multigpt.chat.rag import embeddings, ocr

pytestmark = pytest.mark.django_db

BASE = "http://lms.example.invalid:1234/v1"
ROOT = "http://lms.example.invalid:1234"
PASSWORD = "Geheim-Test-1234"
GPT = "openai/gpt-oss-20b"
OLMOCR = "allenai/olmocr-2-7b"
NOMIC = "text-embedding-nomic-embed-text-v1.5"
QWEN = "qwen/qwen3-coder-30b"

# Antwortformat wie beim LM Studio des Nutzers (lesend geprüft, gekürzt).
CATALOG = {
    GPT: {
        "type": "llm",
        "key": GPT,
        "display_name": "GPT-OSS 20B",
        "architecture": "gpt-oss",
        "quantization": {"name": "MXFP4", "bits_per_weight": 4},
        "size_bytes": 12109664217,
        "params_string": "20B",
        "max_context_length": 131072,
        "capabilities": {"vision": False, "trained_for_tool_use": True},
    },
    OLMOCR: {
        "type": "llm",
        "key": OLMOCR,
        "display_name": "Olmocr 2 7B",
        "architecture": "qwen2vl",
        "quantization": {"name": "Q4_K_M", "bits_per_weight": 4},
        "size_bytes": 6037267971,
        "params_string": "7B",
        "max_context_length": 128000,
        "capabilities": {"vision": True, "trained_for_tool_use": False},
    },
    NOMIC: {
        "type": "embedding",
        "key": NOMIC,
        "display_name": "Nomic Embed Text v1.5",
        "quantization": {"name": "Q4_K_M", "bits_per_weight": 4},
        "size_bytes": 84106624,
        "params_string": None,
        "max_context_length": 2048,
    },
    QWEN: {
        "type": "llm",
        "key": QWEN,
        "architecture": "qwen3moe",
        "size_bytes": 18632365388,
        "params_string": "30B-A3B",
        "max_context_length": 262144,
    },
}


class FakeLms:
    """LM Studio nachgebildet: geladene Instanzen, Aufrufprotokoll, Ladefehler."""

    def __init__(self, mock, loaded=None):
        self.mock = mock
        self.loaded: dict[str, int] = dict(loaded or {})  # Modell -> Kontext
        self.calls: list[tuple] = []
        self.fail_load = ""
        mock.get(f"{ROOT}/api/v1/models").mock(side_effect=self._list)
        mock.post(f"{ROOT}/api/v1/models/load").mock(side_effect=self._load)
        mock.post(f"{ROOT}/api/v1/models/unload").mock(side_effect=self._unload)

    def _list(self, request):
        self.calls.append(("list",))
        models = []
        for key, item in CATALOG.items():
            instances = []
            if key in self.loaded:
                instances = [{"id": key, "config": {"context_length": self.loaded[key]}}]
            models.append({**item, "loaded_instances": instances})
        return httpx.Response(200, json={"models": models})

    def _load(self, request):
        body = json.loads(request.content)
        self.calls.append(("load", body["model"], body.get("context_length"), body))
        if self.fail_load:
            return httpx.Response(500, json={"error": {"message": self.fail_load}})
        self.loaded[body["model"]] = body.get("context_length") or 2048
        return httpx.Response(
            200, json={"type": "llm", "instance_id": body["model"], "status": "loaded"}
        )

    def _unload(self, request):
        body = json.loads(request.content)
        self.calls.append(("unload", body["instance_id"]))
        self.loaded.pop(body["instance_id"], None)
        return httpx.Response(200, json={"instance_id": body["instance_id"]})

    @contextmanager
    def router(self):
        """Weitere Routen (Embeddings, Chat) am selben Mock."""
        yield self.mock

    def actions(self):
        return [c[:2] for c in self.calls if c[0] != "list"]


@pytest.fixture(autouse=True)
def _fresh_cache():
    model_manager._verified.clear()
    yield
    model_manager._verified.clear()


@pytest.fixture
def lms():
    with respx.mock(assert_all_called=False) as mock:
        yield FakeLms(mock)


@pytest.fixture
def provider():
    return Provider.objects.create(
        name="LM Studio",
        kind=Provider.Kind.LMSTUDIO,
        base_url=BASE,
        is_local=True,
        manage_models=True,
        vram_budget_gb=Decimal("24"),
        vram_reserve_gb=Decimal("2"),
        reported_models=[GPT, OLMOCR, NOMIC, QWEN],
    )


def make(provider, model_id, capability=AIModel.Capability.CHAT, **kwargs):
    return AIModel.objects.create(
        provider=provider, model_id=model_id, display_name=model_id, capability=capability, **kwargs
    )


@pytest.fixture
def gpt(provider):
    return make(provider, GPT)


@pytest.fixture
def olmocr_model(provider):
    model = make(provider, OLMOCR, AIModel.Capability.OCR, active=False)
    cfg = RagSettings.load()
    cfg.ocr_backend = RagSettings.OcrBackend.OLMOCR
    cfg.ocr_model = model
    cfg.ocr_fallback_tesseract = False
    cfg.save()
    return model


@pytest.fixture
def nomic(provider):
    model = make(provider, NOMIC, AIModel.Capability.EMBEDDING, pinned=True)
    cfg = RagSettings.load()
    cfg.embedding_model = model
    cfg.save()
    return model


@pytest.fixture
def admin_client(client):
    user = User.objects.create_superuser(username="jo", password=PASSWORD, email="jo@x.invalid")
    client.force_login(user)
    return client


def slot(label, gb, **kwargs):
    model = AIModel(model_id=label, load_priority=kwargs.pop("priority", 50))
    model.pk = kwargs.pop("pk", abs(hash(label)) % 10_000 + 1)
    return model_manager.Slot(
        instance_id=label,
        key=label,
        ai_model=None if kwargs.pop("foreign", False) else model,
        context=None,
        bytes=int(gb * vram.GB),
        source="",
        priority=model.load_priority,
        **kwargs,
    )


def snap(slots, budget=24, reserve=2, run_open=False):
    return model_manager.Snapshot(
        Provider(name="x"),
        budget=int(budget * vram.GB),
        reserve=int(reserve * vram.GB),
        slots=slots,
        run_open=run_open,
    )


# --- Schätzung ------------------------------------------------------------------------


def test_kv_formula_for_known_architectures():
    # olmOCR (Qwen2.5-VL 7B): 2 × 28 Schichten × 4 KV-Köpfe × 128 × 2 Byte = 57 344 Byte/Token.
    arch = vram.arch_for("qwen2vl", "7B", "llm", OLMOCR)
    assert vram.kv_bytes(arch, 8192) == 57344 * 8192
    # gpt-oss-20b: 12 volle Schichten (2 × 8 × 64 × 2 Byte), 12 mit Fenster 128.
    arch = vram.arch_for("gpt-oss", "20B", "llm", GPT)
    per_layer = 2 * 8 * 64 * 2
    expected = 12 * per_layer * 32768 + 12 * per_layer * (128 + vram.SWA_EXTRA_TOKENS)
    assert vram.kv_bytes(arch, 32768) == expected


def test_estimate_total_and_measured_value_wins():
    est = vram.estimate(
        size_bytes=CATALOG[GPT]["size_bytes"], context=32768, architecture="gpt-oss", params="20B"
    )
    assert est.total == est.weights + est.kv + est.overhead
    assert 13.0 < est.total / vram.GB < 14.0
    measured = vram.estimate(size_bytes=1, context=32768, measured_gb=Decimal("15.5"))
    assert measured.total == int(15.5 * vram.GB) and measured.measured


def test_unknown_model_uses_conservative_fallback():
    est = vram.estimate(size_bytes=5 * vram.GB, context=32768, architecture="mystery", params="8B")
    # 36 Schichten × 8 KV-Köpfe × 128: mehr als bei gängigen GQA-Modellen gleicher Größe.
    assert est.kv == 2 * 36 * 8 * 128 * 2 * 32768
    assert est.source == "Näherung nach Größe"


def test_context_stages_limited_to_max_context():
    assert vram.stages(2048) == [2048]
    assert vram.stages(128000) == [2048, 4096, 8192, 16384, 32768, 65536]
    assert vram.stages(None) == list(vram.CONTEXT_STAGES)


def test_default_contexts():
    assert vram.default_context(AIModel.Capability.CHAT, 131072) == 32768
    assert vram.default_context(AIModel.Capability.OCR, 128000) == 8192
    assert vram.default_context(AIModel.Capability.EMBEDDING, 2048) == 2048
    assert vram.default_context(AIModel.Capability.CHAT, 8192) == 8192


def test_parse_models_real_format():
    data = {"models": [{**CATALOG[GPT], "loaded_instances": [
        {"id": GPT, "config": {"context_length": 32768, "parallel": 4}}
    ]}]}  # fmt: skip
    (entry,) = lmstudio.parse_models(data)
    assert entry.size_bytes == CATALOG[GPT]["size_bytes"]
    assert entry.instances[0].context_length == 32768 and entry.instances[0].parallel == 4
    with pytest.raises(lmstudio.LmsError):
        lmstudio.parse_models({"data": []})


# --- Budget und Entladeregeln -------------------------------------------------------------


def test_plan_fits_without_unloading():
    requester = AIModel(model_id="neu", load_priority=50)
    assert model_manager.plan(snap([slot("a", 10)]), requester, int(5 * vram.GB), True) == []


def test_reserve_counts_and_never_over_budget():
    requester = AIModel(model_id="neu", load_priority=50)
    # 24 GB Budget − 2 GB Reserve = 22 GB; 13 + 10 = 23 > 22 -> Platz schaffen nötig.
    with pytest.raises(model_manager.ModelUnavailable) as exc:
        model_manager.plan(snap([slot("a", 13, pinned=True)]), requester, int(10 * vram.GB), True)
    assert "Zu wenig Grafikspeicher" in exc.value.message
    assert "a (13,0 GB, angeheftet)" in exc.value.message


def test_too_big_for_budget():
    requester = AIModel(model_id="riesig", load_priority=50)
    with pytest.raises(model_manager.ModelUnavailable) as exc:
        model_manager.plan(snap([]), requester, int(23 * vram.GB), True)
    assert "mehr als das Budget" in exc.value.message


def test_eviction_order_priority_then_least_recently_used():
    now = timezone.now()
    slots = [
        slot("pinned", 4, pinned=True),
        slot("busy", 4, busy=True),
        slot("fremd", 2, foreign=True),
        slot("neu", 4, last_used=now),
        slot("alt", 4, last_used=now - timedelta(hours=1)),
        slot("unwichtig", 3, priority=10, last_used=now),
    ]
    requester = AIModel(model_id="x", load_priority=50)
    chosen = model_manager.plan(snap(slots), requester, int(6 * vram.GB), True)
    # frei 22 − 21 = 1 GB; erst Priorität 10, dann das ältere gleicher Priorität.
    assert [s.label for s in chosen] == ["unwichtig", "alt"]


def test_never_partial_unload_when_not_enough():
    slots = [slot("klein", 2), slot("pinned", 18, pinned=True)]
    requester = AIModel(model_id="x", load_priority=50)
    with pytest.raises(model_manager.ModelUnavailable) as exc:
        model_manager.plan(snap(slots), requester, int(8 * vram.GB), True)
    assert "pinned (18,0 GB, angeheftet)" in exc.value.message


def test_indexing_does_not_evict_chat_model_of_same_priority():
    chat = slot("chat", 13)
    requester = AIModel(model_id="ocr", load_priority=50)
    with pytest.raises(model_manager.ModelUnavailable):
        model_manager.plan(snap([chat]), requester, int(10 * vram.GB), interactive=False)


def test_chat_evicts_idle_indexing_model_but_not_during_run():
    ocr_slot = slot("ocr", 7, indexing=True)
    requester = AIModel(model_id="chat", load_priority=50)
    need = int(16 * vram.GB)
    assert model_manager.plan(snap([ocr_slot]), requester, need, True) == [ocr_slot]
    with pytest.raises(model_manager.ModelUnavailable) as exc:
        model_manager.plan(snap([ocr_slot], run_open=True), requester, need, True)
    assert "Indexierung läuft" in exc.value.message


def test_indexing_models_may_replace_each_other_during_run():
    figure = slot("figure", 9, indexing=True)
    requester = AIModel(model_id="ocr", load_priority=50)
    plan = model_manager.plan(snap([figure], run_open=True), requester, int(15 * vram.GB), False)
    assert plan == [figure]


# --- Laden über die API ---------------------------------------------------------------------


def test_ensure_loaded_loads_with_profile_without_ttl(lms, gpt):
    assert model_manager.ensure_loaded(gpt, interactive=True) is True
    load = next(c for c in lms.calls if c[0] == "load")
    assert load[1] == GPT and load[2] == 32768
    assert "ttl" not in load[3]
    rt = ModelRuntime.objects.get(ai_model=gpt)
    assert rt.loaded_at and rt.size_bytes == CATALOG[GPT]["size_bytes"]
    # Schon geladen: kein zweites Laden.
    model_manager._verified.clear()
    assert model_manager.ensure_loaded(gpt, interactive=True) is False


def test_not_managed_provider_is_left_alone(lms, gpt, provider):
    provider.manage_models = False
    provider.save()
    gpt.refresh_from_db()
    assert model_manager.ensure_loaded(gpt, interactive=True) is False
    assert lms.calls == []


def test_unloads_before_loading_when_needed(lms, provider, gpt):
    qwen = make(provider, QWEN, load_context_length=8192)
    lms.loaded = {GPT: 32768}
    model_manager.ensure_loaded(qwen, interactive=True)
    assert lms.actions() == [("unload", GPT), ("load", QWEN)]


def test_chat_model_stays_loaded_for_indexing(lms, provider, gpt, olmocr_model):
    provider.vram_budget_gb = Decimal("20")
    provider.save()
    olmocr_model.refresh_from_db()
    lms.loaded = {GPT: 32768}
    with pytest.raises(model_manager.ModelUnavailable) as exc:
        model_manager.ensure_loaded(olmocr_model, interactive=False)
    assert "Zu wenig Grafikspeicher" in exc.value.message and GPT in exc.value.message
    assert lms.actions() == []  # nichts entladen, nichts geladen
    assert GPT in lms.loaded


def test_load_error_sets_backoff(lms, gpt):
    lms.fail_load = "Failed to load model: insufficient memory"
    with pytest.raises(model_manager.ModelUnavailable) as exc:
        model_manager.ensure_loaded(gpt, interactive=True)
    assert "insufficient memory" in exc.value.message
    rt = ModelRuntime.objects.get(ai_model=gpt)
    assert rt.load_failures == 1 and rt.retry_after > timezone.now()
    lms.fail_load = ""
    with pytest.raises(model_manager.ModelUnavailable) as exc:
        model_manager.ensure_loaded(gpt, interactive=True)
    assert "Neuer Ladeversuch ab" in exc.value.message
    assert [c[0] for c in lms.calls].count("load") == 1  # keine sofortige Wiederholung
    # Von Hand (Verwalter) geht es trotz Sperre.
    assert model_manager.load_now(gpt) is True


def test_backoff_grows():
    model = AIModel(model_id="x")
    delays = []
    for failures in range(1, 8):
        delays.append(
            min(model_manager.BACKOFF_BASE * 2 ** (failures - 1), model_manager.BACKOFF_MAX)
        )
    assert delays[0] == timedelta(minutes=1) and delays[-1] == timedelta(minutes=30)
    assert model.auto_load


def test_auto_load_off_refuses(lms, gpt):
    gpt.auto_load = False
    gpt.save()
    with pytest.raises(model_manager.ModelUnavailable) as exc:
        model_manager.ensure_loaded(gpt, interactive=True)
    assert "nicht automatisch" in exc.value.message
    assert lms.actions() == []


def test_unreachable_lmstudio_does_not_interfere(gpt):
    with respx.mock() as mock:
        mock.get(f"{ROOT}/api/v1/models").mock(side_effect=httpx.ConnectError("aus"))
        assert model_manager.ensure_loaded(gpt, interactive=True) is False


# --- Sperre --------------------------------------------------------------------------------


def _other_connection():
    other = connections.create_connection("default")
    other.ensure_connection()
    other.inc_thread_sharing()  # Test: ein Thread spielt den anderen Prozess
    return other


def test_lock_blocks_parallel_loading(lms, gpt, provider, monkeypatch):
    monkeypatch.setattr(model_manager, "LOCK_WAIT", 0.3)
    other = _other_connection()
    try:
        with other.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_lock(%s, %s)", [model_manager.LOCK_NAMESPACE, provider.pk]
            )
        with pytest.raises(model_manager.ModelUnavailable) as exc:
            model_manager.ensure_loaded(gpt, interactive=True)
        assert exc.value.message == model_manager.MSG_BUSY
        assert lms.actions() == []
    finally:
        other.close()


def test_waiting_request_sees_model_loaded_by_other_process(lms, gpt, provider, monkeypatch):
    monkeypatch.setattr(model_manager, "LOCK_WAIT", 10)
    other = _other_connection()
    with other.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_lock(%s, %s)", [model_manager.LOCK_NAMESPACE, provider.pk]
        )

    def other_process_loads():
        time.sleep(0.4)
        lms.loaded[GPT] = 32768  # der andere Prozess hat geladen …
        with other.cursor() as cursor:  # … und gibt die Sperre frei
            cursor.execute(
                "SELECT pg_advisory_unlock(%s, %s)", [model_manager.LOCK_NAMESPACE, provider.pk]
            )

    thread = threading.Thread(target=other_process_loads)
    thread.start()
    try:
        assert model_manager.ensure_loaded(gpt, interactive=True) is False
    finally:
        thread.join()
        other.close()
    assert lms.actions() == []


# --- Vor dem Gebrauch: Embeddings, OCR, Chat ------------------------------------------------


def test_embeddings_load_model_first(lms, nomic):
    with lms.router() as mock:
        route = mock.post(f"{BASE}/embeddings").mock(
            return_value=httpx.Response(
                200, json={"data": [{"index": 0, "embedding": [1.0] * EMBEDDING_DIMENSIONS}]}
            )
        )
        embeddings.embed_texts(["Hallo"])
    assert lms.actions() == [("load", NOMIC)]
    assert route.called
    assert not ModelLease.objects.exists()  # Belegung wieder frei


def test_embedding_without_room_waits(lms, provider, nomic, gpt):
    provider.vram_budget_gb = Decimal("15")
    provider.save()
    nomic.refresh_from_db()
    lms.loaded = {GPT: 32768}
    IndexRun.objects.create(kind=IndexRun.Kind.UPLOAD)
    with pytest.raises(embeddings.EmbeddingError) as exc:
        embeddings.embed_texts(["Hallo"])
    assert exc.value.unreachable  # Auftrag wartet ohne Obergrenze
    assert "Zu wenig Grafikspeicher" in exc.value.message


def test_ocr_loads_olmocr_before_reading(lms, olmocr_model):
    answer = {"choices": [{"index": 0, "message": {"content": "Text"}, "finish_reason": "stop"}]}
    with lms.router() as mock:
        route = mock.post(f"{BASE}/chat/completions").mock(
            return_value=httpx.Response(200, json=answer)
        )
        assert ocr._ask(olmocr_model, b"\x89PNG") == "Text"
    assert lms.actions() == [("load", OLMOCR)]
    assert next(c for c in lms.calls if c[0] == "load")[2] == 8192
    assert route.called


def test_ocr_without_room_is_unreachable(lms, provider, olmocr_model, gpt):
    provider.vram_budget_gb = Decimal("15")
    provider.save()
    olmocr_model.refresh_from_db()
    lms.loaded = {GPT: 32768}
    with pytest.raises(ocr.OcrError) as exc:
        ocr._ask(olmocr_model, b"\x89PNG")
    assert exc.value.unreachable and "olmocr" in exc.value.message.lower()


def _sse(text):
    events = [
        {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
    return httpx.Response(200, text=body, headers={"Content-Type": "text/event-stream"})


@pytest.fixture
def chat_user():
    return User.objects.create_user("anna", password=PASSWORD, role=Role.objects.get(key="adult"))


def test_chat_loads_model_with_status_and_holds_lease(lms, gpt, chat_user):
    conversation = Conversation.objects.create(user=chat_user)
    seen_lease = []
    with lms.router() as mock:

        def answer(request):
            seen_lease.append(ModelLease.objects.filter(ai_model=gpt).exists())
            return _sse("Hallo!")

        mock.post(f"{BASE}/chat/completions").mock(side_effect=answer)
        turn = services.prepare_turn(chat_user, conversation, gpt, content="Hi")
        events = list(services.run_turn(turn))
    statuses = [d["text"] for name, d in events if name == "status"]
    assert f"Lade {GPT} …" in statuses
    assert lms.actions() == [("load", GPT)]
    assert seen_lease == [True]
    assert not ModelLease.objects.exists()
    answer = Message.objects.get(role="assistant")
    assert answer.content == "Hallo!"
    assert answer.tool_state["stats"]["ttft_ms"] >= 0  # Statistik im Kopf (chat/stats.py)
    client = Client()
    client.force_login(chat_user)
    html = client.get(reverse("chat:conversation", args=[conversation.pk])).content.decode()
    assert 'class="chat-message-stats"' in html and "erstes Token nach" in html


def test_chat_without_room_shows_clear_error(lms, provider, gpt, chat_user):
    qwen = make(provider, QWEN, load_context_length=8192)
    gpt.pinned = True
    gpt.save()
    lms.loaded = {GPT: 32768}
    conversation = Conversation.objects.create(user=chat_user)
    turn = services.prepare_turn(chat_user, conversation, qwen, content="Hi")
    events = list(services.run_turn(turn))
    errors = [d["message"] for name, d in events if name == "error"]
    assert errors and "Zu wenig Grafikspeicher" in errors[0] and "angeheftet" in errors[0]
    assert Message.objects.get(role="assistant").status == Message.Status.ERROR
    assert lms.actions() == []


def test_plan_offers_swap_only_for_pinned_or_priority():
    requester = AIModel(model_id="neu", load_priority=50)
    need = int(10 * vram.GB)
    with pytest.raises(model_manager.SwapPossible) as exc:
        model_manager.plan(snap([slot("a", 13, pinned=True)]), requester, need, True)
    assert exc.value.unload == ["a"]
    # Mit Bestätigung weicht das angeheftete Modell.
    pinned = snap([slot("a", 13, pinned=True)])
    chosen = model_manager.plan(pinned, requester, need, True, swap=True)
    assert [s.label for s in chosen] == ["a"]
    # In Benutzung: kein Angebot, auch nicht mit swap.
    busy = snap([slot("a", 13, busy=True)])
    with pytest.raises(model_manager.ModelUnavailable) as exc:
        model_manager.plan(busy, requester, need, True)
    assert not isinstance(exc.value, model_manager.SwapPossible)
    with pytest.raises(model_manager.ModelUnavailable):
        model_manager.plan(busy, requester, need, True, swap=True)
    # Indexierung (nicht interaktiv): nie ein Angebot.
    with pytest.raises(model_manager.ModelUnavailable) as exc:
        model_manager.plan(snap([slot("a", 13, pinned=True)]), requester, need, False)
    assert not isinstance(exc.value, model_manager.SwapPossible)


def test_chat_offers_swap_and_swaps_on_confirmation(lms, provider, gpt, chat_user, client):
    qwen = make(provider, QWEN, load_context_length=8192)
    gpt.pinned = True
    gpt.save()
    lms.loaded = {GPT: 32768}
    conversation = Conversation.objects.create(user=chat_user)
    turn = services.prepare_turn(chat_user, conversation, qwen, content="Hi")
    list(services.run_turn(turn))
    failed = Message.objects.get(role="assistant")
    assert failed.tool_state["swap"] == {"unload": [GPT]}
    client.force_login(chat_user)
    html = client.get(reverse("chat:conversation", args=[conversation.pk])).content.decode()
    assert "data-swap-message" in html and f"{GPT} entladen und" in html
    with lms.router() as mock:
        mock.post(f"{BASE}/chat/completions").mock(return_value=_sse("Hallo!"))
        turn = services.prepare_turn(
            chat_user, conversation, qwen, regenerate=True, regenerate_of=failed.pk
        )
        turn.swap = True
        list(services.run_turn(turn))
    assert lms.actions() == [("unload", GPT), ("load", QWEN)]
    answer = Message.objects.filter(role="assistant").exclude(pk=failed.pk).get()
    assert answer.content == "Hallo!" and "swap" not in answer.tool_state
    gpt.refresh_from_db()
    assert gpt.pinned  # Einstellung bleibt


def test_lease_released_when_client_aborts(lms, gpt, chat_user):
    lms.loaded = {GPT: 32768}
    conversation = Conversation.objects.create(user=chat_user)
    with lms.router() as mock:
        mock.post(f"{BASE}/chat/completions").mock(return_value=_sse("Hallo!"))
        gen = services.run_turn(services.prepare_turn(chat_user, conversation, gpt, content="Hi"))
        next(gen)  # start
        assert ModelLease.objects.filter(ai_model=gpt).exists()
        gen.close()
    assert not ModelLease.objects.exists()
    assert Message.objects.get(role="assistant").status == Message.Status.ABORTED


def test_run_note_while_loading(lms, nomic):
    run = IndexRun.objects.create(kind=IndexRun.Kind.UPLOAD)
    notes = []

    def watch(request):
        notes.append(IndexRun.objects.get(pk=run.pk).note)
        return lms._load(request)

    with lms.router() as mock:
        mock.post(f"{ROOT}/api/v1/models/load").mock(side_effect=watch)
        mock.post(f"{BASE}/embeddings").mock(
            return_value=httpx.Response(
                200, json={"data": [{"index": 0, "embedding": [1.0] * EMBEDDING_DIMENSIONS}]}
            )
        )
        token = model_manager.current_run.set(run.pk)
        try:
            embeddings.embed_texts(["Hallo"])
        finally:
            model_manager.current_run.reset(token)
    assert notes == [f"Lade {NOMIC} …"]
    run.refresh_from_db()
    assert run.note == ""


# --- Leerlauf ----------------------------------------------------------------------------------


def test_idle_unload_by_worker(lms, provider, gpt, nomic, olmocr_model):
    lms.loaded = {GPT: 32768, NOMIC: 2048, OLMOCR: 8192}
    old = timezone.now() - timedelta(hours=2)
    for model, key in ((gpt, GPT), (nomic, NOMIC), (olmocr_model, OLMOCR)):
        ModelRuntime.objects.create(ai_model=model, last_used=old, loaded_instance=key)
    model_manager.acquire_lease(olmocr_model)  # läuft gerade
    assert model_manager.unload_idle() == 1
    assert lms.actions() == [("unload", GPT)]  # nomic angeheftet, olmOCR belegt


def test_idle_first_seen_starts_clock(lms, gpt):
    lms.loaded = {GPT: 32768}
    ModelRuntime.objects.create(ai_model=gpt, loaded_instance=GPT)
    assert model_manager.unload_idle() == 0
    assert ModelRuntime.objects.get(ai_model=gpt).last_used is not None
    assert lms.actions() == []


def test_idle_keeps_indexing_models_during_run(lms, olmocr_model):
    lms.loaded = {OLMOCR: 8192}
    ModelRuntime.objects.create(ai_model=olmocr_model, last_used=timezone.now() - timedelta(days=1))
    IndexRun.objects.create(kind=IndexRun.Kind.UPLOAD)
    assert model_manager.unload_idle() == 0


def test_indexing_models_unloaded_soon_after_run(lms, gpt, olmocr_model):
    """Nach dem Lauf: Indexierungsmodelle nach 2 Minuten weg, das Chatmodell nicht."""
    lms.loaded = {GPT: 32768, OLMOCR: 8192}
    recent = timezone.now() - timedelta(minutes=3)
    for model, key in ((gpt, GPT), (olmocr_model, OLMOCR)):
        ModelRuntime.objects.create(ai_model=model, last_used=recent, loaded_instance=key)
    assert model_manager.unload_idle() == 1
    assert lms.actions() == [("unload", OLMOCR)]


def test_indexing_grace_and_opt_out(lms, olmocr_model):
    lms.loaded = {OLMOCR: 8192}
    runtime = ModelRuntime.objects.create(
        ai_model=olmocr_model, last_used=timezone.now() - timedelta(minutes=1)
    )
    assert model_manager.unload_idle() == 0  # Schonfrist
    runtime.last_used = timezone.now() - timedelta(minutes=10)
    runtime.save()
    olmocr_model.unload_after_indexing = False
    olmocr_model.save()
    assert model_manager.unload_idle() == 0  # dann gilt der Leerlauf (30 min)
    assert lms.actions() == []


def test_expired_leases_are_removed(gpt):
    ModelLease.objects.create(ai_model=gpt, expires=timezone.now() - timedelta(minutes=1))
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{ROOT}/api/v1/models").mock(side_effect=httpx.ConnectError("aus"))
        model_manager.unload_idle()
    assert not ModelLease.objects.exists()


# --- Anbieter-Einstellungen ------------------------------------------------------------------


def test_provider_validation():
    compat = Provider(name="o", kind=Provider.Kind.OPENAI_COMPAT, is_local=True)
    compat.manage_models = True
    compat.vram_budget_gb = Decimal("16")
    with pytest.raises(ValidationError):
        compat.clean()  # Modellverwaltung nur mit der Schnittstelle „LM Studio“
    provider = Provider(name="x", kind=Provider.Kind.LMSTUDIO)
    provider.manage_models = True
    with pytest.raises(ValidationError):
        provider.clean()
    provider.vram_budget_gb = Decimal("2")
    with pytest.raises(ValidationError):
        provider.clean()  # Reserve 2 >= Budget 2
    provider.vram_budget_gb = Decimal("16")
    provider.clean()
    assert provider.is_local  # LM Studio ist immer lokal
    cloud = Provider(name="c", kind=Provider.Kind.ANTHROPIC, manage_models=True)
    cloud.vram_budget_gb = Decimal("16")
    with pytest.raises(ValidationError):
        cloud.clean()


# --- Admin --------------------------------------------------------------------------------------


def vram_json(client, **params):
    response = client.get(reverse("admin:rag_overview_vram"), params)
    return response


def test_vram_view_estimate_and_warning(admin_client, lms, provider, gpt, olmocr_model):
    lms.loaded = {OLMOCR: 8192}
    data = vram_json(admin_client, model=f"m{gpt.pk}", context="65536").json()
    assert data["stages"][-1] == 131072
    assert data["context"] == 65536
    assert data["fits"] is True
    provider.vram_budget_gb = Decimal("20")
    provider.save()
    data = vram_json(admin_client, model=f"m{gpt.pk}", context="65536").json()
    assert data["fits"] is False and data["warning"].startswith("passt nicht:")
    assert "olmocr" in data["budget_text"]


def test_vram_view_limits_stages_for_reported_model(admin_client, lms, provider):
    data = vram_json(admin_client, model=f"r{provider.pk}:{NOMIC}", capability="embedding").json()
    assert data["stages"] == [2048]
    assert data["context"] == 2048


def test_vram_view_only_for_admins(client, lms, gpt):
    user = User.objects.create_user("kind", password=PASSWORD, role=Role.objects.get(key="adult"))
    client.force_login(user)
    response = vram_json(client, model=f"m{gpt.pk}")
    assert response.status_code in (302, 403)


def test_rag_settings_save_context_and_reject_too_big(admin_client, lms, nomic, olmocr_model):
    from multigpt.chat import admin_lmstudio

    admin_lmstudio.refresh_catalogs()
    url = reverse("admin:rag_ragsettingsproxy_change", args=[RagSettings.SINGLETON_PK])
    page = admin_client.get(url)
    assert page.status_code == 200
    form = page.context["adminform"].form
    data = {}
    for name, field in form.fields.items():
        value = form.initial.get(name, field.initial)
        if isinstance(value, bool):
            if value:
                data[name] = "on"
        elif value is not None:
            data[name] = value
    data.update(ocr_context="16384", embedding_context="2048")
    response = admin_client.post(url, data)
    assert response.status_code == 302, response.context["adminform"].form.errors
    olmocr_model.refresh_from_db()
    assert olmocr_model.load_context_length == 16384
    data.update(embedding_context="8192")  # nomic kann höchstens 2048
    response = admin_client.post(url, data)
    assert response.status_code == 200
    assert "embedding_context" in response.context["adminform"].form.errors


def test_lmstudio_page_and_pin(admin_client, lms, provider, gpt, nomic):
    lms.loaded = {GPT: 32768}
    page = admin_client.get(reverse("admin:rag_overview_lmstudio"))
    assert page.status_code == 200
    body = page.content.decode()
    assert GPT in body and "vram-bar" in body and "angeheftet" in body
    response = admin_client.post(
        reverse("admin:rag_overview_lmstudio"), {"action": "pin", "model": gpt.pk}
    )
    assert response.status_code == 302
    gpt.refresh_from_db()
    assert gpt.pinned


def test_lmstudio_page_marks_model_bigger_than_budget(admin_client, lms, provider):
    make(provider, QWEN)
    provider.vram_budget_gb = Decimal("16")
    provider.save()
    body = admin_client.get(reverse("admin:rag_overview_lmstudio")).content.decode()
    assert "größer als das Budget" in body


def test_lmstudio_page_load_and_unload(admin_client, lms, provider, gpt):
    url = reverse("admin:rag_overview_lmstudio")
    admin_client.post(url, {"action": "load", "model": gpt.pk})
    admin_client.post(url, {"action": "unload", "provider": provider.pk, "instance": GPT})
    assert lms.actions() == [("load", GPT), ("unload", GPT)]


def test_model_admin_context_choices(admin_client, lms, provider, gpt):
    from multigpt.chat import admin_lmstudio

    admin_lmstudio.refresh_catalogs()
    page = admin_client.get(reverse("admin:chat_aimodel_change", args=[gpt.pk]))
    field = page.context["adminform"].form.fields["load_context_length"]
    values = [v for v, _ in field.choices]
    assert values[-1] == "131072" and "" in values
    matrix = admin_client.get(reverse("admin:chat_aimodel_changelist"))
    assert matrix.status_code == 200 and b"vram-context" in matrix.content


# --- CLI ---------------------------------------------------------------------------------


def test_cli_ps_and_budget(lms, provider, gpt, nomic):
    lms.loaded = {GPT: 32768, NOMIC: 2048}
    out = io.StringIO()
    call_command("lms", "ps", stdout=out)
    text = out.getvalue()
    assert GPT in text and "angeheftet" in text and "frei" in text
    out = io.StringIO()
    call_command("lms", "budget", stdout=out)
    assert "Budget 24,0 GB" in out.getvalue()


def test_cli_load_and_unload(lms, gpt):
    out = io.StringIO()
    call_command("lms", "load", GPT, "--context", "16384", stdout=out)
    assert "geladen" in out.getvalue()
    gpt.refresh_from_db()
    assert gpt.load_context_length == 16384
    call_command("lms", "unload", GPT, stdout=io.StringIO())
    assert lms.actions() == [("load", GPT), ("unload", GPT)]


def test_cli_load_requires_management(lms, provider, gpt):
    provider.manage_models = False
    provider.save()
    with pytest.raises(CommandError):
        call_command("lms", "load", GPT)


def test_parallel_never_sent_for_embedding_models(nomic, gpt):
    """Am echten LM Studio: parallel bei Embedding-Modellen -> HTTP 400."""
    nomic.load_parallel = 2
    gpt.load_parallel = 2
    assert model_manager.load_parallel(nomic) is None
    assert model_manager.load_parallel(gpt) == 2


def test_idle_never_unloads_instances_loaded_elsewhere(lms, gpt, olmocr_model):
    """Zweite MultiGPT-Installation am selben LM Studio (C33): deren Instanzen bleiben."""
    lms.loaded = {GPT: 32768, OLMOCR: 8192}
    old = timezone.now() - timedelta(hours=2)
    ModelRuntime.objects.create(ai_model=gpt, last_used=old)  # nie selbst geladen
    ModelRuntime.objects.create(ai_model=olmocr_model, last_used=old, loaded_instance="anders")
    assert model_manager.unload_idle() == 0
    assert lms.actions() == []


def test_load_remembers_own_instance(lms, olmocr_model):
    model_manager.load_now(olmocr_model)
    assert ModelRuntime.objects.get(ai_model=olmocr_model).loaded_instance


def test_own_marker_cleared_when_gone_or_unloaded(lms, gpt, olmocr_model):
    """Nach dem Entladen bzw. wenn das Modell weg ist, gilt die gleichnamige Instanz
    einer anderen Installation nicht mehr als eigene."""
    lms.loaded = {GPT: 32768}
    old = timezone.now() - timedelta(hours=2)
    ModelRuntime.objects.create(ai_model=gpt, last_used=old, loaded_instance=GPT)
    ModelRuntime.objects.create(ai_model=olmocr_model, last_used=old, loaded_instance=OLMOCR)
    assert model_manager.unload_idle() == 1  # GPT (eigen) entladen, olmOCR ist nicht geladen
    assert not ModelRuntime.objects.exclude(loaded_instance="").exists()
    lms.loaded = {GPT: 32768}  # jetzt von einer anderen Installation geladen
    assert model_manager.unload_idle() == 0


def test_migration_moves_lm_studio_providers_to_new_interface():
    """0042: bisherige LM-Studio-Anbieter (OpenAI-kompatibel) bekommen die Schnittstelle."""
    from importlib import import_module

    from django.apps import apps

    migration = import_module("multigpt.chat.migrations.0042_provider_lmstudio")
    kinds = {
        "verwaltet": dict(manage_models=True, is_local=True, base_url="http://pc:8080/v1"),
        "port": dict(is_local=True, base_url="http://pc:1234/v1"),
        "ollama": dict(is_local=True, base_url="http://pc:11434/v1"),
        "cloud": dict(base_url="https://api.openai.com/v1"),
    }
    for name, fields in kinds.items():
        Provider.objects.create(name=name, kind=Provider.Kind.OPENAI_COMPAT, **fields)
    migration.to_lmstudio(apps, None)
    result = dict(Provider.objects.values_list("name", "kind"))
    assert result == {
        "verwaltet": "lmstudio",
        "port": "lmstudio",
        "ollama": "openai_compat",
        "cloud": "openai_compat",
    }
    assert Provider.objects.get(name="port").base_url == "http://pc:1234"


def test_lm_studio_interface_derives_both_paths_from_one_address():
    from multigpt.chat.providers.registry import get_adapter

    for url in ("http://pc:1234", "http://pc:1234/", "http://pc:1234/v1"):
        provider = Provider(name="l", kind=Provider.Kind.LMSTUDIO, base_url=url)
        assert get_adapter(provider).base_url == "http://pc:1234/v1"
        assert lmstudio.api_root(provider) == "http://pc:1234"
    empty = Provider(name="l", kind=Provider.Kind.LMSTUDIO)
    assert get_adapter(empty).base_url == "http://localhost:1234/v1"
