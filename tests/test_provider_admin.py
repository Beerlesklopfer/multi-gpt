"""Anbieter im Admin: Fehlerursachen, „Verbindung jetzt prüfen“, Modellauswahl.

HTTP wird mit respx gemockt; keine echten Anbieteraufrufe.
"""

import errno
import json
import re
import socket
import ssl

import httpx
import pytest
import respx
from django.contrib.messages import get_messages
from django.urls import reverse
from django.utils import timezone

from multigpt.accounts.models import Role, User
from multigpt.chat import status
from multigpt.chat.models import AIModel, Provider
from multigpt.chat.providers import registry
from multigpt.chat.providers.base import ProviderAdapter, check_error_message, endpoint_of

PASSWORD = "Geheim-Test-1234"
BASE = "http://lmstudio.test:1234/v1"
SECRET = "sk-geheimer-key-1234"


def models_response(*ids):
    return httpx.Response(200, json={"object": "list", "data": [{"id": i} for i in ids]})


@pytest.fixture
def mock():
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def provider(db):
    return Provider.objects.create(
        name="LM Studio", kind=Provider.Kind.OPENAI_COMPAT, base_url=BASE, api_key=SECRET
    )


@pytest.fixture
def admin_client(client, django_user_model):
    user = django_user_model.objects.create_superuser(
        username="jo", password=PASSWORD, email="jo@example.invalid"
    )
    client.force_login(user)
    return client


def texts(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


def check(provider, timeout=2):
    return registry.get_adapter(provider).check(timeout=timeout)


def _with_cause(cause):
    """ConnectError mit Ursache wie von httpcore (für ``check_error_message``).

    Über respx lässt sich die Ursache nicht durchreichen (respx setzt
    ``__cause__`` beim erneuten Werfen neu); dort zählt der Fehlertext.
    """
    exc = httpx.ConnectError("Verbindung fehlgeschlagen")
    exc.__cause__ = cause
    return exc


def _refused():
    return httpx.ConnectError("[Errno 111] Connection refused")


# --- check(): Ursachen ---------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("side_effect", "expected"),
    [
        (_refused(), "Verbindung abgelehnt: lmstudio.test:1234 nimmt keine Verbindung an"),
        (
            httpx.ConnectError("[Errno -2] Name or service not known"),
            "Rechnername unbekannt: lmstudio.test lässt sich nicht auflösen",
        ),
        (
            httpx.ConnectError("[Errno 113] No route to host"),
            "Rechner nicht erreichbar: Keine Verbindung zu lmstudio.test:1234",
        ),
        (httpx.ConnectTimeout("t"), "Zeitüberschreitung: lmstudio.test:1234 antwortet nicht"),
        (httpx.ReadTimeout("t"), "Zeitüberschreitung: lmstudio.test:1234 hat nicht rechtzeitig"),
        (httpx.ConnectError("whatever"), "Keine Verbindung: lmstudio.test:1234"),
        (httpx.RemoteProtocolError("weg"), "Verbindung abgebrochen: lmstudio.test:1234"),
    ],
)
def test_check_network_causes(provider, mock, side_effect, expected):
    mock.get(f"{BASE}/models").mock(side_effect=side_effect)
    result = check(provider)
    assert result.online is False
    assert result.models == []
    assert result.error.startswith(expected)
    assert SECRET not in result.error


@pytest.mark.parametrize(
    ("cause", "expected"),
    [
        (ConnectionRefusedError(errno.ECONNREFUSED, "x"), "Verbindung abgelehnt: h.test:8080"),
        (socket.gaierror(socket.EAI_NONAME, "x"), "Rechnername unbekannt: h.test "),
        (OSError(errno.EHOSTUNREACH, "x"), "Rechner nicht erreichbar:"),
        (ssl.SSLError(1, "x"), "TLS-Fehler: Die sichere Verbindung zu h.test:8080"),
    ],
)
def test_check_error_message_from_cause_chain(cause, expected):
    message = check_error_message(_with_cause(cause), "http://h.test:8080/v1")
    assert message.startswith(expected)


def test_check_error_message_bad_url():
    assert check_error_message(httpx.UnsupportedProtocol("x"), "ftp://h").startswith(
        "Ungültige Basis-URL:"
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (401, "Zugang abgelehnt (HTTP 401): Bitte den API-Key prüfen."),
        (403, "Zugang abgelehnt (HTTP 403): Bitte den API-Key prüfen."),
        (404, "Adresse nicht gefunden (HTTP 404): Bitte die Basis-URL prüfen – fehlt z. B. „/v1“?"),
        (429, "Zu viele Anfragen (HTTP 429)"),
        (502, "Serverfehler beim Anbieter (HTTP 502)"),
        (418, "Anfrage abgelehnt (HTTP 418)"),
    ],
)
def test_check_http_causes_without_raw_text(provider, mock, status_code, expected):
    mock.get(f"{BASE}/models").mock(
        return_value=httpx.Response(
            status_code, json={"error": {"message": f"Rohtext vom Anbieter {SECRET}"}}
        )
    )
    result = check(provider)
    assert result.online is False
    assert result.error.startswith(expected)
    assert "Rohtext" not in result.error
    assert SECRET not in result.error


@pytest.mark.django_db
def test_check_invalid_response(provider, mock):
    mock.get(f"{BASE}/models").mock(return_value=httpx.Response(200, text="<html>Login</html>"))
    result = check(provider)
    assert result.error.startswith("Ungültige Antwort:")
    assert "Login" not in result.error


@pytest.mark.django_db
def test_check_google_bad_key_is_auth(mock):
    provider = Provider.objects.create(name="G", kind=Provider.Kind.GOOGLE, api_key=SECRET)
    mock.get(url__regex=r".*/models.*").mock(
        return_value=httpx.Response(
            400,
            json={
                "error": {
                    "status": "INVALID_ARGUMENT",
                    "details": [{"reason": "API_KEY_INVALID"}],
                }
            },
        )
    )
    assert check(provider).error.startswith("Zugang abgelehnt (HTTP 401)")


@pytest.mark.django_db
def test_check_anthropic_online(mock):
    provider = Provider.objects.create(name="A", kind=Provider.Kind.ANTHROPIC, api_key=SECRET)
    mock.get(url__regex=r".*/models.*").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "claude-x"}], "has_more": False})
    )
    result = check(provider)
    assert result.online is True
    assert result.models == ["claude-x"]
    assert result.error is None


@pytest.mark.django_db
def test_check_online_deduplicates(provider, mock):
    mock.get(f"{BASE}/models").mock(return_value=models_response("a", "b", "a"))
    result = check(provider)
    assert result.online is True
    assert result.models == ["a", "b"]


def test_check_default_adapter_without_list_models():
    result = ProviderAdapter(Provider(name="x", kind="openai_compat")).check()
    assert result.online is False
    assert result.error.startswith("Keine Modellliste:")


def test_endpoint_hides_credentials():
    assert endpoint_of("https://user:pw@host.test/v1") == ("host.test", "host.test:443")
    assert endpoint_of("http://[::1]:1234/v1") == ("::1", "[::1]:1234")
    assert "pw" not in check_error_message(
        httpx.ConnectError("Connection refused"), "https://user:pw@host.test/v1"
    )


# --- last_error und Statusendpunkt ---------------------------------------------


@pytest.mark.django_db
def test_last_error_set_and_cleared(provider, mock):
    route = mock.get(f"{BASE}/models")
    route.mock(side_effect=_refused())
    status.force_check(provider)
    provider.refresh_from_db()
    assert provider.online is False
    assert provider.last_error.startswith("Verbindung abgelehnt:")
    assert provider.last_checked is not None

    route.mock(return_value=models_response("llama"))
    status.force_check(provider)
    provider.refresh_from_db()
    assert provider.online is True
    assert provider.last_error == ""
    assert provider.reported_models == ["llama"]


@pytest.mark.django_db
def test_status_endpoint_reports_error_without_secrets(client, mock):
    provider = Provider.objects.create(
        name="LM",
        kind=Provider.Kind.OPENAI_COMPAT,
        base_url=BASE,
        api_key=SECRET,
        is_local=True,
        check_status=True,
    )
    user = User.objects.create_user("erw", password=PASSWORD, role=Role.objects.get(key="adult"))
    client.force_login(user)
    mock.get(f"{BASE}/models").mock(
        return_value=httpx.Response(401, json={"error": {"message": f"bad key {SECRET}"}})
    )
    response = client.get(reverse("chat:api_provider_status"))
    entry = response.json()[0]
    assert entry["id"] == provider.pk
    assert entry["online"] is False
    assert entry["error"] == "Zugang abgelehnt (HTTP 401): Bitte den API-Key prüfen."
    assert SECRET not in response.content.decode()
    assert "bad key" not in response.content.decode()

    mock.get(f"{BASE}/models").mock(return_value=models_response("m"))
    status.invalidate(provider.pk)
    entry = client.get(reverse("chat:api_provider_status")).json()[0]
    assert entry["online"] is True
    assert entry["error"] is None


@pytest.mark.django_db
def test_status_indicator_shows_cause(client):
    Provider.objects.create(
        name="LM",
        kind=Provider.Kind.OPENAI_COMPAT,
        base_url=BASE,
        is_local=True,
        check_status=True,
        last_checked=timezone.now(),
    )
    Provider.objects.filter(name="LM").update(
        last_error="Verbindung abgelehnt: lmstudio.test:1234 nimmt keine Verbindung an."
    )
    user = User.objects.create_user("erw", password=PASSWORD, role=Role.objects.get(key="adult"))
    client.force_login(user)
    html = client.get(reverse("chat:index")).content.decode()
    assert "LM offline – Verbindung abgelehnt" in html
    assert 'data-error="Verbindung abgelehnt: lmstudio.test:1234' in html


# --- Admin: Jetzt prüfen ---------------------------------------------------------


@pytest.mark.django_db
def test_change_page_has_tools(admin_client, provider):
    html = admin_client.get(reverse("admin:chat_provider_change", args=[provider.pk])).content
    html = html.decode()
    assert reverse("admin:chat_provider_check", args=[provider.pk]) in html
    assert reverse("admin:chat_provider_select_models", args=[provider.pk]) in html
    assert "Verbindung jetzt prüfen" in html
    assert "Modelle bequemer über" in html
    assert SECRET not in html


@pytest.mark.django_db
def test_check_button_forces_check(admin_client, provider, mock):
    route = mock.get(f"{BASE}/models").mock(return_value=models_response("a", "b", "c"))
    # Frisch geprüft: Der Cache würde eine normale Prüfung verhindern.
    Provider.objects.filter(pk=provider.pk).update(last_checked=timezone.now())
    url = reverse("admin:chat_provider_check", args=[provider.pk])
    response = admin_client.post(url)
    assert response.status_code == 302
    assert response.url == reverse("admin:chat_provider_change", args=[provider.pk])
    assert route.call_count == 1
    assert "„LM Studio“: Online – 3 Modelle gemeldet." in texts(response)
    provider.refresh_from_db()
    assert provider.online is True
    assert provider.last_online is not None


@pytest.mark.django_db
def test_check_button_reports_offline(admin_client, provider, mock):
    mock.get(f"{BASE}/models").mock(return_value=httpx.Response(404))
    response = admin_client.post(reverse("admin:chat_provider_check", args=[provider.pk]))
    [message] = texts(response)
    assert message.startswith("„LM Studio“: Offline: Adresse nicht gefunden (HTTP 404)")
    provider.refresh_from_db()
    assert provider.last_error.startswith("Adresse nicht gefunden")


@pytest.mark.django_db
def test_check_requires_post(admin_client, provider):
    response = admin_client.get(reverse("admin:chat_provider_check", args=[provider.pk]))
    assert response.status_code == 405


@pytest.mark.django_db
def test_save_checks_automatically(
    admin_client, provider, mock, django_capture_on_commit_callbacks
):
    mock.get(f"{BASE}/models").mock(side_effect=_refused())
    url = reverse("admin:chat_provider_change", args=[provider.pk])
    data = {
        "name": "LM Studio",
        "kind": "openai_compat",
        "base_url": BASE,
        "api_key": "",
        "active": "on",
        "ai_models-TOTAL_FORMS": "0",
        "ai_models-INITIAL_FORMS": "0",
    }
    # Die Prüfung läuft erst nach dem Commit der Admin-Transaktion.
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        response = admin_client.post(url, data)
    assert len(callbacks) == 1
    assert response.status_code == 302
    assert any("Offline: Verbindung abgelehnt" in t for t in texts(response))
    provider.refresh_from_db()
    assert provider.api_key == SECRET  # leer gelassen: Key bleibt
    assert provider.last_error.startswith("Verbindung abgelehnt")


@pytest.mark.django_db
def test_changelist_columns_and_action(admin_client, provider, mock):
    mock.get(f"{BASE}/models").mock(side_effect=httpx.ConnectTimeout("t"))
    changelist = reverse("admin:chat_provider_changelist")
    response = admin_client.post(
        changelist, {"action": "check_selected_action", "_selected_action": [provider.pk]}
    )
    assert response.status_code == 302
    assert any("Offline: Zeitüberschreitung" in t for t in texts(response))
    html = admin_client.get(changelist).content.decode()
    assert "Zeitüberschreitung" in html
    assert "Modelle auswählen" in html
    assert "icon-no.svg" in html


# --- Admin: Modelle auswählen ----------------------------------------------------


@pytest.mark.django_db
def test_select_page_lists_models(admin_client, provider, mock):
    AIModel.objects.create(
        provider=provider, model_id="llama-3", display_name="Llama", active=False
    )
    AIModel.objects.create(provider=provider, model_id="alt", display_name="Alt")
    mock.get(f"{BASE}/models").mock(
        return_value=models_response("llama-3", "text-embedding-3", "whisper-1", "gpt-4o")
    )
    url = reverse("admin:chat_provider_select_models", args=[provider.pk])
    response = admin_client.get(url)
    assert response.status_code == 200
    rows = {r["model_id"]: r for r in response.context["rows"]}
    assert set(rows) == {"llama-3", "text-embedding-3", "whisper-1", "gpt-4o"}
    assert rows["llama-3"]["existing"] is True
    assert rows["llama-3"]["display_name"] == "Llama"
    assert rows["llama-3"]["active"] is False
    assert rows["text-embedding-3"]["capability"] == "embedding"
    assert rows["whisper-1"]["capability"] == "stt"
    assert rows["gpt-4o"]["capability"] == "chat"
    assert response.context["missing"] == ["alt"]
    html = response.content.decode()
    assert 'name="take" value="llama-3" class="take" id="take-' in html
    assert "vorhanden" in html
    assert "admin_select_models.js" in html
    assert "<script>" not in html.split("</head>")[1]  # kein Inline-JS im Inhalt

    filtered = admin_client.get(url + "?q=GPT")
    assert [r["model_id"] for r in filtered.context["rows"]] == ["gpt-4o"]


@pytest.mark.django_db
def test_select_page_shows_fetch_error(admin_client, provider, mock):
    mock.get(f"{BASE}/models").mock(return_value=httpx.Response(401, text=f"nope {SECRET}"))
    response = admin_client.get(reverse("admin:chat_provider_select_models", args=[provider.pk]))
    assert response.status_code == 200
    html = response.content.decode()
    assert "Offline: Zugang abgelehnt (HTTP 401): Bitte den API-Key prüfen." in html
    assert SECRET not in html


@pytest.mark.django_db
def test_adopt_creates_and_updates_without_deleting(admin_client, provider, mock):
    AIModel.objects.create(
        provider=provider, model_id="llama-3", display_name="Llama", active=False
    )
    AIModel.objects.create(provider=provider, model_id="alt", display_name="Alt")
    AIModel.objects.create(provider=provider, model_id="same", display_name="Same")
    mock.get(f"{BASE}/models").mock(
        return_value=models_response("llama-3", "gpt-4o", "text-embedding-3", "same", "skip")
    )
    url = reverse("admin:chat_provider_select_models", args=[provider.pk])
    admin_client.get(url)  # ruft die Liste ab und merkt sie sich
    response = admin_client.post(
        url,
        {
            "take": ["llama-3", "gpt-4o", "text-embedding-3", "same", "fremd"],
            "name:llama-3": "Llama 3",
            "cap:llama-3": "chat",
            "active": ["llama-3", "gpt-4o", "same"],
            "name:gpt-4o": "GPT-4o",
            "cap:gpt-4o": "chat",
            "name:text-embedding-3": "",
            "cap:text-embedding-3": "unsinn",
            "name:same": "Same",
            "cap:same": "chat",
            "name:skip": "nicht angehakt",
            "name:fremd": "nicht gemeldet",
        },
    )
    assert response.status_code == 302
    assert response.url == reverse("admin:chat_provider_change", args=[provider.pk])
    assert "2 Modelle übernommen, 1 aktualisiert." in texts(response)
    models = {m.model_id: m for m in provider.ai_models.all()}
    assert set(models) == {"llama-3", "alt", "same", "gpt-4o", "text-embedding-3"}
    assert models["llama-3"].display_name == "Llama 3"
    assert models["llama-3"].active is True
    assert models["gpt-4o"].display_name == "GPT-4o"
    assert models["gpt-4o"].active is True
    assert models["text-embedding-3"].display_name == "text-embedding-3"
    assert models["text-embedding-3"].capability == "embedding"
    assert models["text-embedding-3"].active is False
    assert models["alt"].display_name == "Alt"  # nicht gemeldet, bleibt


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["get", "post"])
def test_non_admin_gets_403(client, provider, mock, method):
    route = mock.get(f"{BASE}/models").mock(return_value=models_response("x"))
    user = User.objects.create_user("erw", password=PASSWORD, role=Role.objects.get(key="adult"))
    client.force_login(user)
    for name in ("admin:chat_provider_select_models", "admin:chat_provider_check"):
        response = getattr(client, method)(reverse(name, args=[provider.pk]), {"take": ["x"]})
        assert response.status_code == 403
    assert route.call_count == 0
    assert not AIModel.objects.exists()


# --- Admin: Combobox für „Modell-ID“ ---------------------------------------------


def _reported_choices(response):
    html = response.content.decode()
    match = re.search(
        r'<script id="reported-model-choices" type="application/json">(.*?)</script>', html, re.S
    )
    assert match, "#reported-model-choices fehlt"
    return json.loads(match.group(1))


@pytest.mark.django_db
def test_change_page_offers_reported_models(admin_client, provider):
    AIModel.objects.create(provider=provider, model_id="gpt-4o", display_name="GPT-4o")
    provider.reported_models = ["whisper-1", "gpt-4o", "text-embedding-3-small"]
    provider.save(update_fields=["reported_models"])
    response = admin_client.get(reverse("admin:chat_provider_change", args=[provider.pk]))
    assert response.status_code == 200
    assert _reported_choices(response) == [
        {"id": "gpt-4o", "capability": "chat", "exists": True},
        {"id": "text-embedding-3-small", "capability": "embedding", "exists": False},
        {"id": "whisper-1", "capability": "stt", "exists": False},
    ]
    html = response.content.decode()
    assert "chat/admin_model_combobox.js" in html
    # Kein Inline-JS im Inhalt: jedes <script> lädt eine Datei oder trägt JSON-Daten.
    body = html.split("</head>", 1)[1]
    for tag in re.findall(r"<script\b[^>]*>", body):
        assert "src=" in tag or 'type="application/json"' in tag, tag
    assert "onclick" not in body


@pytest.mark.django_db
def test_change_page_without_reported_models(admin_client, provider):
    provider.reported_models = []
    provider.save(update_fields=["reported_models"])
    response = admin_client.get(reverse("admin:chat_provider_change", args=[provider.pk]))
    assert response.status_code == 200
    assert _reported_choices(response) == []
    assert "chat/admin_model_combobox.js" in response.content.decode()


@pytest.mark.django_db
def test_add_page_without_combobox(admin_client):
    response = admin_client.get(reverse("admin:chat_provider_add"))
    assert response.status_code == 200
    html = response.content.decode()
    assert "reported-model-choices" not in html
    assert "admin_model_combobox.js" not in html


@pytest.mark.django_db
def test_change_page_loads_submit_once_guard(admin_client, provider):
    url = reverse("admin:chat_provider_change", args=[provider.pk])
    assert "chat/admin_submit_once.js" in admin_client.get(url).content.decode()


@pytest.mark.django_db
def test_aimodel_form_offers_reported_models_per_provider(admin_client, provider):
    provider.reported_models = ["nomic-embed-text-v1.5", "qwen3-8b"]
    provider.save(update_fields=["reported_models"])
    for url in (reverse("admin:chat_aimodel_add"), reverse("admin:chat_aimodel_add") + "?_popup=1"):
        html = admin_client.get(url).content.decode()
        match = re.search(r'id="reported-model-choices"[^>]*>(.*?)</script>', html, re.S)
        assert match
        data = json.loads(match.group(1))
        choices = data["by_provider"][str(provider.pk)]
        assert [c["id"] for c in choices] == ["nomic-embed-text-v1.5", "qwen3-8b"]
        assert choices[0]["capability"] == "embedding"
        assert "chat/admin_model_combobox.js" in html


def test_check_message_names_provider_error_code_without_secret(mock):
    from multigpt.chat.providers.base import check_error_message
    from multigpt.chat.providers.openai_compat import OpenAICompatAdapter

    body = {
        "error": {
            "message": "You have insufficient permissions for this operation. Key sk-abc…xyz",
            "type": "invalid_request_error",
            "code": "insufficient_permissions",
        }
    }
    mock.get("https://api.example.test/v1/models").mock(return_value=httpx.Response(403, json=body))
    provider = Provider(name="X", kind="openai_compat", base_url="https://api.example.test/v1")
    provider.api_key = "sk-geheim"
    try:
        OpenAICompatAdapter(provider).list_models(timeout=2)
    except Exception as exc:  # noqa: BLE001
        text = check_error_message(exc, "https://api.example.test/v1")
    assert "HTTP 403" in text
    assert "insufficient_permissions" in text
    assert "sk-" not in text
