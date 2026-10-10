"""Statusprüfung lokaler Anbieter (Plan 8a/12, M4-03, M4-05).

HTTP wird mit respx gemockt; für die Sperre bei parallelen Aufrufen ersetzt ein
Fake-Adapter den Netzabruf.
"""

import json
import threading
import time
from datetime import timedelta

import httpx
import pytest
import respx
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from multigpt.accounts.models import Role, User
from multigpt.chat import status
from multigpt.chat.models import AIModel, Conversation, Message, Provider
from multigpt.chat.providers import registry
from multigpt.chat.providers.base import ProviderAdapter

pytestmark = pytest.mark.django_db

PASSWORD = "Geheim-Test-1234"
BASE = "http://lmstudio.test:1234/v1"


def models_response(*ids):
    return httpx.Response(200, json={"object": "list", "data": [{"id": i} for i in ids]})


@pytest.fixture
def mock():
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def lmstudio():
    return Provider.objects.create(
        name="LM Studio",
        kind=Provider.Kind.OPENAI_COMPAT,
        base_url=BASE,
        is_local=True,
        check_status=True,
    )


def login(client, role_key, username="nutzer"):
    user = User.objects.create_user(
        username, password=PASSWORD, role=Role.objects.get(key=role_key)
    )
    client.force_login(user)
    return user


@pytest.fixture
def adult(client):
    return login(client, "adult", "erwachsen")


def get_status(client):
    response = client.get(reverse("chat:api_provider_status"))
    assert response.status_code == 200
    return response.json()


# --- Statusendpunkt ------------------------------------------------------------------


def test_requires_login(client):
    response = client.get(reverse("chat:api_provider_status"))
    assert response.status_code == 403


def test_online(client, adult, lmstudio, mock):
    route = mock.get(f"{BASE}/models").mock(
        return_value=models_response("llama-3", "text-embedding-nomic")
    )
    # Anbieter ohne Statusprüfung oder inaktiv tauchen nicht auf.
    Provider.objects.create(name="Cloud", kind="openai_compat")
    Provider.objects.create(name="Aus", kind="openai_compat", check_status=True, active=False)

    data = get_status(client)
    assert route.call_count == 1
    assert len(data) == 1
    entry = data[0]
    assert entry["id"] == lmstudio.pk
    assert entry["name"] == "LM Studio"
    assert entry["is_local"] is True
    assert entry["online"] is True
    assert entry["models"] == ["llama-3", "text-embedding-nomic"]
    assert entry["last_online"] is not None
    lmstudio.refresh_from_db()
    assert lmstudio.online is True
    assert entry["last_online"] == lmstudio.last_online.isoformat()


@pytest.mark.parametrize(
    "exc", [httpx.ConnectError("Connection refused"), httpx.ConnectTimeout("t")]
)
def test_offline_connection_refused_or_timeout(client, adult, lmstudio, mock, exc):
    mock.get(f"{BASE}/models").mock(side_effect=exc)
    data = get_status(client)
    assert data[0]["online"] is False
    assert data[0]["models"] == []
    assert data[0]["last_online"] is None
    assert data[0]["last_checked"] is not None


def test_read_timeout_is_offline(client, adult, lmstudio, mock):
    mock.get(f"{BASE}/models").mock(side_effect=httpx.ReadTimeout("langsam"))
    assert get_status(client)[0]["online"] is False


def test_offline_keeps_last_online(client, adult, lmstudio, mock):
    earlier = timezone.now() - timedelta(hours=2)
    Provider.objects.filter(pk=lmstudio.pk).update(online=True, last_online=earlier)
    mock.get(f"{BASE}/models").mock(side_effect=httpx.ConnectError("refused"))
    entry = get_status(client)[0]
    assert entry["online"] is False
    assert entry["last_online"] == earlier.isoformat()


def test_check_uses_short_timeout(lmstudio, monkeypatch):
    seen = {}

    class Adapter(ProviderAdapter):
        def list_models(self, timeout=None):
            seen["timeout"] = timeout
            return ["m"]

    monkeypatch.setattr(registry, "get_adapter", Adapter)
    status.refresh_all()
    assert seen["timeout"] == 2


def test_cache_within_15_seconds(client, adult, lmstudio, mock):
    route = mock.get(f"{BASE}/models").mock(return_value=models_response("llama-3"))
    get_status(client)
    # Zweiter Aufruf innerhalb von 15 s: kein Netzabruf, gleiches Ergebnis.
    mock.get(f"{BASE}/models").mock(side_effect=httpx.ConnectError("refused"))
    assert get_status(client)[0]["online"] is True
    assert route.call_count == 1
    # Älter als 15 s: neu prüfen.
    Provider.objects.filter(pk=lmstudio.pk).update(
        last_checked=timezone.now() - timedelta(seconds=16)
    )
    assert get_status(client)[0]["online"] is False
    assert route.call_count == 2


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_no_double_check_in_parallel(monkeypatch):
    """Mehrere gleichzeitige Aufrufe (eigene DB-Verbindungen): nur einer prüft."""
    provider = Provider.objects.create(
        name="LM Studio", kind="openai_compat", is_local=True, check_status=True
    )
    calls = []
    entered = threading.Event()
    release = threading.Event()

    class SlowAdapter(ProviderAdapter):
        def list_models(self, timeout=None):
            calls.append(timeout)
            entered.set()
            release.wait(5)
            return ["llama-3"]

    monkeypatch.setattr(registry, "get_adapter", SlowAdapter)
    results = []

    def worker():
        try:
            results.append([p.online for p in status.refresh_all()])
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    assert entered.wait(5)
    # Alle außer dem prüfenden Aufruf kehren zurück, ohne zu prüfen.
    deadline = time.monotonic() + 5
    while sum(t.is_alive() for t in threads) > 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sum(t.is_alive() for t in threads) == 1
    release.set()
    for t in threads:
        t.join(5)
    assert len(calls) == 1
    assert len(results) == 4
    provider.refresh_from_db()
    assert provider.online is True
    # Die Prüfung lief außerhalb einer Transaktion: Ergebnis ist für alle sichtbar.
    assert provider.reported_models == ["llama-3"]


# --- Lokale Modelle ohne Admin-Pflege -------------------------------------------------


def test_reported_models_are_created_and_kept(client, adult, lmstudio, mock):
    hidden = AIModel.objects.create(
        provider=lmstudio, model_id="versteckt", display_name="Versteckt", active=False
    )
    mock.get(f"{BASE}/models").mock(
        return_value=models_response("llama-3", "qwen", "text-embedding-nomic", "versteckt")
    )
    get_status(client)
    llama = AIModel.objects.get(provider=lmstudio, model_id="llama-3")
    assert llama.display_name == "llama-3"
    assert llama.capability == AIModel.Capability.CHAT
    assert llama.active is True
    embedding = AIModel.objects.get(provider=lmstudio, model_id="text-embedding-nomic")
    assert embedding.capability == AIModel.Capability.EMBEDDING
    # Vom Verwalter deaktivierte Modelle bleiben aus.
    hidden.refresh_from_db()
    assert hidden.active is False

    models = {m["display_name"]: m for m in client.get(reverse("chat:api_models")).json()}
    assert set(models) == {"llama-3", "qwen"}
    assert models["qwen"]["online"] is True
    assert models["qwen"]["available"] is True
    assert models["qwen"]["provider_id"] == lmstudio.pk

    # qwen wird nicht mehr gemeldet: bleibt bestehen, ist aber nicht wählbar.
    mock.get(f"{BASE}/models").mock(return_value=models_response("llama-3"))
    Provider.objects.filter(pk=lmstudio.pk).update(last_checked=None)
    get_status(client)
    assert AIModel.objects.filter(provider=lmstudio, model_id="qwen").exists()
    models = {m["display_name"]: m for m in client.get(reverse("chat:api_models")).json()}
    assert models["qwen"]["available"] is False
    assert models["llama-3"]["available"] is True


def test_models_offline_and_cloud_flags(client, adult, lmstudio):
    cloud = Provider.objects.create(name="Cloud", kind="openai_compat")
    AIModel.objects.create(provider=cloud, model_id="gpt", display_name="GPT")
    AIModel.objects.create(provider=lmstudio, model_id="llama-3", display_name="Llama")
    Provider.objects.filter(pk=lmstudio.pk).update(online=False, reported_models=["llama-3"])
    models = {m["display_name"]: m for m in client.get(reverse("chat:api_models")).json()}
    assert models["GPT"]["online"] is True
    assert models["GPT"]["available"] is True
    assert models["Llama"]["online"] is False
    assert models["Llama"]["available"] is False


def test_new_local_models_only_for_roles_with_all_models(client, lmstudio, mock):
    mock.get(f"{BASE}/models").mock(return_value=models_response("llama-3"))
    status.refresh_all()
    login(client, "teen", "jugend")
    assert client.get(reverse("chat:api_models")).json() == []
    # Freigabe durch den Verwalter.
    Role.objects.get(key="teen").allowed_models.add(AIModel.objects.get(model_id="llama-3"))
    assert [m["display_name"] for m in client.get(reverse("chat:api_models")).json()] == ["llama-3"]


# --- Senden an lokale Modelle (M4-05) ---------------------------------------------------


@pytest.fixture
def conversation(adult):
    return Conversation.objects.create(user=adult)


@pytest.fixture
def llama(lmstudio):
    return AIModel.objects.create(provider=lmstudio, model_id="llama-3", display_name="Llama")


def post(client, conversation, **data):
    return client.post(
        reverse("chat:api_messages", args=[conversation.pk]),
        json.dumps(data),
        content_type="application/json",
    )


def test_send_to_offline_model_rejected(client, conversation, lmstudio, llama, mock):
    route = mock.get(f"{BASE}/models").mock(side_effect=httpx.ConnectError("refused"))
    response = post(client, conversation, content="Hallo", model=llama.pk)
    assert response.status_code == 503
    assert response.json()["error"].startswith("LM Studio ist offline")
    assert route.call_count == 1  # Status war unbekannt -> sofort geprüft
    assert not Message.objects.filter(conversation=conversation).exists()
    # Innerhalb von 15 s kein weiterer Netzabruf.
    assert post(client, conversation, content="Hallo", model=llama.pk).status_code == 503
    assert route.call_count == 1


def test_send_to_unreported_model_rejected(client, conversation, lmstudio, llama, mock):
    mock.get(f"{BASE}/models").mock(return_value=models_response("anderes"))
    response = post(client, conversation, content="Hallo", model=llama.pk)
    assert response.status_code == 409
    assert "Llama" in response.json()["error"]
    assert not Message.objects.filter(conversation=conversation).exists()


def sse_events(response):
    raw = b"".join(response.streaming_content).decode()
    events = []
    for block in raw.split("\n\n"):
        if block.strip():
            lines = dict(line.split(": ", 1) for line in block.split("\n"))
            events.append((lines["event"], json.loads(lines["data"])))
    return events


class BrokenStream(httpx.SyncByteStream):
    """Liefert erst Text, dann reißt die Verbindung ab (LM Studio beendet)."""

    def __iter__(self):
        chunk = {"choices": [{"index": 0, "delta": {"content": "Halbe "}, "finish_reason": None}]}
        yield f"data: {json.dumps(chunk)}\n\n".encode()
        raise httpx.RemoteProtocolError("peer closed connection")


def test_lmstudio_goes_offline_mid_stream(client, conversation, lmstudio, llama, mock):
    mock.get(f"{BASE}/models").mock(return_value=models_response("llama-3"))
    mock.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, stream=BrokenStream())
    )
    response = post(client, conversation, content="Hallo", model=llama.pk)
    assert response.status_code == 200
    events = sse_events(response)
    assert [n for n, _ in events] == ["start", "delta", "error", "usage", "done"]
    assert "unterbrochen" in events[2][1]["message"]
    assert events[-1][1] == {"status": "aborted"}
    answer = Message.objects.get(pk=events[0][1]["assistant_message_id"])
    assert answer.status == Message.Status.ABORTED
    assert answer.content == "Halbe "
    assert answer.error
    assert answer.cost == 0  # lokale Modelle kosten nichts
    # Nächste Statusabfrage prüft sofort neu.
    lmstudio.refresh_from_db()
    assert lmstudio.last_checked is None


def test_unreachable_before_text_stays_error(client, conversation, lmstudio, llama, mock):
    mock.get(f"{BASE}/models").mock(return_value=models_response("llama-3"))
    mock.post(f"{BASE}/chat/completions").mock(side_effect=httpx.ConnectError("refused"))
    events = sse_events(post(client, conversation, content="Hallo", model=llama.pk))
    assert events[-1][1] == {"status": "error"}


# --- Cloud-Anbieter mit fehlgeschlagener Prüfung --------------------------------------

CLOUD = "http://cloud.test/v1"


@pytest.fixture
def cloud_offline():
    """Cloud-Anbieter ohne ``check_status``, letzte Prüfung (Admin) fehlgeschlagen."""
    provider = Provider.objects.create(
        name="OpenAI", kind=Provider.Kind.OPENAI_COMPAT, base_url=CLOUD, api_key="sk-x"
    )
    Provider.objects.filter(pk=provider.pk).update(
        online=False,
        last_checked=timezone.now(),
        last_error="API-Key abgelaufen (HTTP 401): Bitte beim Anbieter einen neuen Key erzeugen.",
    )
    provider.refresh_from_db()
    return provider


@pytest.fixture
def gpt(cloud_offline):
    return AIModel.objects.create(provider=cloud_offline, model_id="gpt-5.5", display_name="GPT")


def test_cloud_offline_model_greyed(client, adult, gpt):
    (model,) = client.get(reverse("chat:api_models")).json()
    assert model["online"] is False
    assert model["available"] is False


def test_cloud_never_checked_counts_as_online(client, adult):
    provider = Provider.objects.create(name="Neu", kind="openai_compat")
    AIModel.objects.create(provider=provider, model_id="m", display_name="M")
    (model,) = client.get(reverse("chat:api_models")).json()
    assert (model["online"], model["available"]) == (True, True)


def test_send_to_offline_cloud_model_rejected(client, conversation, gpt, mock):
    route = mock.get(f"{CLOUD}/models").mock(return_value=httpx.Response(401, json={}))
    response = post(client, conversation, content="Hallo", model=gpt.pk)
    assert response.status_code == 503
    assert response.json()["error"].startswith("OpenAI ist offline (API-Key abgelaufen (HTTP 401))")
    assert route.call_count == 0  # Neuprüfung erst nach 60 s
    assert not Message.objects.filter(conversation=conversation).exists()


def test_offline_cloud_rechecked_and_recovers(client, adult, cloud_offline, gpt, mock):
    route = mock.get(f"{CLOUD}/models").mock(return_value=models_response("gpt-5.5"))
    # Innerhalb von 60 s keine Neuprüfung, Anbieter erscheint offline in der Statusleiste.
    (entry,) = get_status(client)
    assert (entry["name"], entry["online"]) == ("OpenAI", False)
    assert route.call_count == 0
    Provider.objects.filter(pk=cloud_offline.pk).update(
        last_checked=timezone.now() - timedelta(seconds=status.OFFLINE_RECHECK_SECONDS + 1)
    )
    (entry,) = get_status(client)
    assert route.call_count == 1
    assert entry["online"] is True  # einmal als „jetzt online“ gemeldet …
    assert get_status(client) == []  # … danach nicht mehr in der Statusleiste
    (model,) = client.get(reverse("chat:api_models")).json()
    assert model["available"] is True


def test_online_cloud_without_check_status_not_polled(client, adult, mock):
    provider = Provider.objects.create(name="Gemini", kind="openai_compat", base_url=CLOUD)
    Provider.objects.filter(pk=provider.pk).update(online=True, last_checked=timezone.now())
    route = mock.get(f"{CLOUD}/models").mock(return_value=models_response())
    assert get_status(client) == []
    assert route.call_count == 0
