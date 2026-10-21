"""Eliezer als Modellserver: Speicherbedarf und freier Platz kommen von dort (``estimate``,
``status``) statt aus der eigenen Rechnung. Kein echter Eliezer: respx bildet ihn nach."""

import json
from decimal import Decimal

import httpx
import pytest
import respx

from multigpt.chat import lmstudio, model_manager
from multigpt.chat.models import AIModel, Provider

pytestmark = pytest.mark.django_db

ROOT = "http://eliezer.example.invalid:1234"
GB = 1_000_000_000
BIG = "qwen/qwen3.8-27b"
SMALL = "qwen/qwen3-coder-30b"
SERVER = {"Server": "eliezer/0.5.0"}


def entry(key, size_gb, loaded_context=None):
    item = {
        "type": "llm",
        "key": key,
        "display_name": key,
        "architecture": "qwen3",
        "params_string": "27B",
        "size_bytes": int(size_gb * GB),
        "max_context_length": 131072,
        "capabilities": {"vision": False},
        "loaded_instances": [],
    }
    if loaded_context:
        item["loaded_instances"] = [{"id": key, "config": {"context_length": loaded_context}}]
    return item


class FakeEliezer:
    """``/api/v1/models``, ``status``, ``estimate``, ``load``, ``unload`` mit festen Zahlen."""

    def __init__(self, mock, loaded=None, total_gb=20.0, budget_gb=22.0, max_context=None):
        self.loaded = dict(loaded or {})  # Schlüssel -> belegte GB
        self.total = int(total_gb * GB)
        self.budget = int(budget_gb * GB)
        self.max_context = max_context
        self.calls = []
        self.estimate_bodies = []
        self.free_after = None
        mock.get(f"{ROOT}/api/v1/models").mock(side_effect=self._list)
        mock.get(f"{ROOT}/api/v1/status").mock(side_effect=self._status)
        mock.post(f"{ROOT}/api/v1/models/estimate").mock(side_effect=self._estimate)
        mock.post(f"{ROOT}/api/v1/models/load").mock(side_effect=self._load)
        mock.post(f"{ROOT}/api/v1/models/unload").mock(side_effect=self._unload)

    def used(self):
        return int(sum(self.loaded.values()) * GB)

    def _list(self, request):
        models = [entry(k, 17, 32768 if k in self.loaded else None) for k in (BIG, SMALL)]
        return httpx.Response(200, json={"models": models}, headers=SERVER)

    def _status(self, request):
        models = [
            {"key": k, "state": "loaded", "device": "gpu", "vram_bytes": int(gb * GB)}
            for k, gb in self.loaded.items()
        ]
        return httpx.Response(200, json={"gpu": {}, "models": models}, headers=SERVER)

    def _estimate(self, request):
        body = json.loads(request.content)
        self.estimate_bodies.append(body)
        free = max(0, self.budget - self.used())
        after = self.budget if self.free_after is None else self.free_after
        limit = self.max_context
        context = body.get("context_length")
        data = {
            "model": body["model"],
            "context_length": context,
            "parallel": body.get("parallel"),
            "device": "gpu",
            "estimate": {
                "total_bytes": self.total,
                "basis": "calculated",
                "weights_bytes": self.total - 3 * GB,
                "kv_cache_bytes": 2 * GB,
                "overhead_bytes": GB,
            },
            "pools": {
                "gpu": {
                    "budget_bytes": self.budget,
                    "free_bytes": free,
                    "free_after_eviction_bytes": after,
                    "fits_budget": self.total <= self.budget,
                    "fits_now": self.total <= free,
                    "fits_after_eviction": self.total <= after,
                    "max_context_length": limit,
                }
            },
        }
        return httpx.Response(200, json=data, headers=SERVER)

    def _load(self, request):
        body = json.loads(request.content)
        self.calls.append(("load", body["model"], body.get("context_length")))
        self.loaded[body["model"]] = self.total / GB
        return httpx.Response(
            200,
            json={"type": "llm", "instance_id": body["model"], "status": "loaded"},
            headers=SERVER,
        )

    def _unload(self, request):
        body = json.loads(request.content)
        self.calls.append(("unload", body["instance_id"]))
        self.loaded.pop(body["instance_id"], None)
        return httpx.Response(200, json={"instance_id": body["instance_id"]}, headers=SERVER)


@pytest.fixture(autouse=True)
def _fresh():
    model_manager._verified.clear()
    lmstudio._servers.clear()
    yield
    model_manager._verified.clear()
    lmstudio._servers.clear()


@pytest.fixture
def provider():
    return Provider.objects.create(
        name="Eliezer",
        kind=Provider.Kind.LMSTUDIO,
        base_url=ROOT,
        is_local=True,
        manage_models=True,
        vram_budget_gb=Decimal("8"),  # absichtlich falsch: Eliezers Zahlen müssen gelten
        vram_reserve_gb=Decimal("2"),
        reported_models=[BIG, SMALL],
    )


def make(provider, model_id, **kwargs):
    return AIModel.objects.create(
        provider=provider, model_id=model_id, display_name=model_id, **kwargs
    )


def test_parse_estimate_reads_pools():
    est = lmstudio.parse_estimate(
        {
            "model": "m",
            "device": "gpu",
            "estimate": {"total_bytes": 5, "basis": "measured"},
            "pools": {
                "gpu": {"budget_bytes": 9, "free_bytes": 4, "free_after_eviction_bytes": 7},
                "cpu": {"budget_bytes": 100},
            },
        }
    )
    assert est.total == 5 and est.basis == "measured"
    assert est.pool.budget == 9 and est.pool.free == 4 and est.pool.free_after_eviction == 7
    with pytest.raises(lmstudio.LmsError):
        lmstudio.parse_estimate({"estimate": {}})


def test_server_kind_comes_from_header(provider):
    with respx.mock(assert_all_called=False) as mock:
        FakeEliezer(mock)
        assert not model_manager.eliezer(provider)
        lmstudio.list_models(provider)
        assert model_manager.eliezer(provider)


def test_loads_with_eliezers_numbers_not_own_budget(provider):
    """Der Anbieter ist auf 8 GB eingestellt, Eliezer meldet 22 GB Budget: es wird geladen."""
    ai = make(provider, BIG)
    with respx.mock(assert_all_called=False) as mock:
        fake = FakeEliezer(mock, total_gb=20.0, budget_gb=22.0)
        assert model_manager.ensure_loaded(ai, interactive=True) is True
    assert ("load", BIG, 32768) in fake.calls
    assert fake.estimate_bodies and fake.estimate_bodies[0]["model"] == BIG


def test_unloads_by_eliezers_free_bytes(provider):
    """Belegt durch andere: Bedarf von estimate, Belegung von status, keine eigene Summe."""
    other = make(provider, SMALL, load_priority=10)
    ai = make(provider, BIG, load_priority=20)
    with respx.mock(assert_all_called=False) as mock:
        fake = FakeEliezer(mock, loaded={SMALL: 18.0}, total_gb=20.0, budget_gb=22.0)
        assert model_manager.ensure_loaded(ai, interactive=True) is True
    assert fake.calls[0] == ("unload", SMALL)
    assert other.pk


def test_refuses_when_eliezer_cannot_evict_enough(provider):
    ai = make(provider, BIG)
    with respx.mock(assert_all_called=False) as mock:
        fake = FakeEliezer(mock, loaded={SMALL: 18.0}, total_gb=20.0, budget_gb=22.0)
        fake.free_after = 10 * GB  # Eliezer hält Teile fest (angeheftet, laufende Anfrage)
        with pytest.raises(model_manager.ModelUnavailable) as err:
            model_manager.ensure_loaded(ai, interactive=True)
    assert "10,0" in str(err.value) or "10.0" in str(err.value)
    assert not [c for c in fake.calls if c[0] in ("load", "unload")]


def test_context_follows_max_context_length_of_pool(provider):
    ai = make(provider, BIG)
    with respx.mock(assert_all_called=False) as mock:
        fake = FakeEliezer(mock, total_gb=10.0, budget_gb=22.0, max_context=16384)
        assert model_manager.ensure_loaded(ai, interactive=True) is True
    assert ("load", BIG, 16384) in fake.calls
    assert [b.get("context_length") for b in fake.estimate_bodies] == [32768, 16384]


def test_explicit_context_stays(provider):
    ai = make(provider, BIG, load_context_length=65536)
    with respx.mock(assert_all_called=False) as mock:
        fake = FakeEliezer(mock, total_gb=10.0, budget_gb=22.0, max_context=16384)
        assert model_manager.ensure_loaded(ai, interactive=True) is True
    assert ("load", BIG, 65536) in fake.calls


def test_older_eliezer_without_estimate_uses_own_calculation(provider):
    ai = make(provider, BIG)
    with respx.mock(assert_all_called=False) as mock:
        fake = FakeEliezer(mock)
        mock.post(f"{ROOT}/api/v1/models/estimate").mock(
            return_value=httpx.Response(404, json={"error": {"message": "nicht gefunden"}})
        )
        provider.vram_budget_gb = Decimal("24")
        provider.save()
        assert model_manager.ensure_loaded(ai, interactive=True) is True
    assert any(c[0] == "load" for c in fake.calls)


def test_admin_estimate_uses_eliezers_numbers(provider):
    from multigpt.chat import admin_lmstudio

    ai = make(provider, BIG)
    with respx.mock(assert_all_called=False) as mock:
        FakeEliezer(mock, loaded={SMALL: 18.0}, total_gb=12.0, budget_gb=22.0)
        provider.vram_budget_gb = None  # beim Anbieter ist nichts eingestellt: Eliezers Pool gilt
        provider.save()
        data = admin_lmstudio.estimate_payload(provider, ai, BIG, 32768)
    assert data["estimate"]["source"] == "Eliezer: calculated"
    assert data["estimate"]["total_gb"] == 12.0
    assert data["budget"]["usable_gb"] == 22.0
    assert data["budget"]["free_gb"] == 4.0  # 22 − 18 belegt durch andere
    assert "belegt durch andere" in data["budget_text"]
    assert "passt nicht" in data["warning"]  # 12 GB brauchen mehr als die 4 GB frei


def test_admin_notes_that_loading_makes_room_instead_of_warning(provider):
    """Weicht das andere Modell beim Laden, ist „passt nicht“ falsch: nur ein Hinweis."""
    from multigpt.chat import admin_lmstudio

    make(provider, SMALL)  # von MultiGPT verwaltet, gleiche Priorität: darf weichen
    ai = make(provider, BIG)
    with respx.mock(assert_all_called=False) as mock:
        FakeEliezer(mock, loaded={SMALL: 18.0}, total_gb=12.0, budget_gb=22.0)
        data = admin_lmstudio.estimate_payload(provider, ai, BIG, 32768)
    assert data["fits"] is False and data["fits_after_eviction"] is True
    assert not data["warning"]
    assert "Platz geschaffen" in data["budget_text"] and SMALL in data["budget_text"]
    assert (
        "Reserve" not in data["budget_text"]
        or "Reserve für Anzeige/System 0,0" in data["budget_text"]
    )


def test_model_on_cpu_does_not_occupy_gpu_pool(provider):
    """Läuft ein Modell im CPU-Pool, zählt es nicht als „belegt“ im GPU-Pool."""
    make(provider, SMALL)
    ai = make(provider, BIG)
    with respx.mock(assert_all_called=False) as mock:
        FakeEliezer(mock, loaded={SMALL: 18.0}, total_gb=12.0, budget_gb=22.0)
        mock.get(f"{ROOT}/api/v1/status").mock(
            return_value=httpx.Response(
                200,
                json={
                    "models": [
                        {"key": SMALL, "state": "loaded", "device": "cpu", "vram_bytes": 18 * GB}
                    ]
                },
                headers=SERVER,
            )
        )
        catalog = lmstudio.list_models(provider)
        pool = lmstudio.estimate(provider, BIG).pool
        snap = model_manager.snapshot(provider, catalog, pool=pool)
    assert pool.name == "gpu"
    assert snap.slots == []
    assert ai.pk


def test_image_type_maps_to_image_capability(provider):
    """Eliezers Typ „image“ wird die Fähigkeit „Bild“, „embedding“ bleibt Embedding, Rest Chat."""
    from multigpt.chat import capabilities
    from multigpt.chat.providers import registry

    image = entry("qwen-image-2.1", 14.6)
    image["type"] = "image"
    image.pop("max_context_length")  # bei Bildmodellen fehlt es absichtlich
    embed = entry("text-embedding-nomic-embed-text-v1.5", 0.3)
    embed["type"] = "embedding"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{ROOT}/api/v0/models").mock(return_value=httpx.Response(404, headers=SERVER))
        mock.get(f"{ROOT}/api/v1/models").mock(
            return_value=httpx.Response(
                200, json={"models": [image, embed, entry(BIG, 17)]}, headers=SERVER
            )
        )
        detected = registry.get_adapter(provider).model_capabilities()
    assert detected["qwen-image-2.1"].capability == capabilities.IMAGE
    assert detected["qwen-image-2.1"].tools is False
    assert detected[embed["key"]].capability == capabilities.EMBEDDING
    assert detected[BIG].capability == capabilities.CHAT


def test_image_edit_flag_presets_can_edit_images(provider):
    """Meldet Eliezer ``capabilities.image_edit``, ist das Modell gleich bearbeitungsfähig."""
    from multigpt.chat import capabilities, detect

    editable = entry("flux2-klein-4b", 8.9)
    editable["type"] = "image"
    editable["capabilities"] = {"vision": False, "image_generation": True, "image_edit": True}
    plain = entry("qwen-image-2.1", 14.6)
    plain["type"] = "image"
    plain["capabilities"] = {"vision": False, "image_generation": True}
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{ROOT}/api/v1/models").mock(
            return_value=httpx.Response(200, json={"models": [editable, plain]}, headers=SERVER)
        )
        catalog = {m.key: m for m in lmstudio.list_models(provider)}
    flux = detect.new_model(
        provider, "flux2-klein-4b", capabilities.from_catalog(catalog["flux2-klein-4b"])
    )
    qwen = detect.new_model(
        provider, "qwen-image-2.1", capabilities.from_catalog(catalog["qwen-image-2.1"])
    )
    assert flux.capability == "image" and flux.can_edit_images is True
    assert qwen.capability == "image" and qwen.can_edit_images is False


def test_select_models_dialog_preselects_image_for_eliezer(admin_client, provider):
    from django.urls import reverse

    image = entry("qwen-image-2.1", 14.6)
    image["type"] = "image"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{ROOT}/api/v0/models").mock(return_value=httpx.Response(404, headers=SERVER))
        mock.get(f"{ROOT}/api/v1/models").mock(
            return_value=httpx.Response(200, json={"models": [image]}, headers=SERVER)
        )
        mock.get(f"{ROOT}/v1/models").mock(
            return_value=httpx.Response(
                200, json={"object": "list", "data": [{"id": "qwen-image-2.1"}]}, headers=SERVER
            )
        )
        response = admin_client.get(
            reverse("admin:chat_provider_select_models", args=[provider.pk])
        )
    assert response.status_code == 200
    row = next(r for r in response.context["rows"] if r["model_id"] == "qwen-image-2.1")
    assert row["capability"] == "image"


def test_detect_action_updates_can_edit_images_from_the_report(provider):
    """„Fähigkeiten erkennen“ übernimmt capabilities.image_edit; ohne Meldung bleibt der Wert."""
    from multigpt.chat import detect

    flux = entry("flux2-klein-4b", 8.9)
    flux["type"] = "image"
    flux["capabilities"] = {"image_generation": True, "image_edit": True}
    qwen = entry("qwen-image-2.1", 14.6)
    qwen["type"] = "image"
    qwen["capabilities"] = {"image_generation": True}
    kwargs = {"capability": AIModel.Capability.IMAGE}
    flux_model = make(provider, "flux2-klein-4b", **kwargs)
    qwen_model = make(provider, "qwen-image-2.1", can_edit_images=True, **kwargs)  # fälschlich an
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{ROOT}/api/v0/models").mock(return_value=httpx.Response(404, headers=SERVER))
        mock.get(f"{ROOT}/api/v1/models").mock(
            return_value=httpx.Response(200, json={"models": [flux, qwen]}, headers=SERVER)
        )
        changes = detect.diff([flux_model, qwen_model])
        detect.apply(changes)
    flux_model.refresh_from_db()
    qwen_model.refresh_from_db()
    assert flux_model.can_edit_images is True
    assert qwen_model.can_edit_images is False  # Eliezer meldet kein image_edit


def eliezer_error(kind, message, status=409):
    return httpx.Response(
        status, json={"error": {"type": kind, "message": message}}, headers=SERVER
    )


@pytest.mark.parametrize(
    "kind,expect",
    [
        ("busy", "Das Modell ist gerade in Benutzung"),
        ("overloaded", "überlastet"),
        ("backend_error", "Laufzeit"),
        ("model_file", "Modelldatei"),
    ],
)
def test_eliezer_error_types_are_shown_in_german(provider, kind, expect):
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{ROOT}/api/v1/models/unload").mock(
            return_value=eliezer_error(kind, "An English message.")
        )
        with pytest.raises(lmstudio.LmsError) as err:
            lmstudio.unload(provider, "x")
    assert expect in err.value.message and "English" not in err.value.message


def test_insufficient_memory_is_german_and_keeps_the_numbers(provider):
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{ROOT}/api/v1/models/load").mock(
            return_value=eliezer_error("insufficient_memory", "needs 21.0 GB, free 9.2 GB")
        )
        with pytest.raises(lmstudio.LmsError) as err:
            lmstudio.load(provider, "x")
    assert "Nicht genug Speicher" in err.value.message and "21.0 GB" in err.value.message
