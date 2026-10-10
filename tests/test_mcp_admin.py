"""MCP-Server im Admin (M4a-03) und Format der Zugangsdaten."""

import time

import pytest
from django.contrib.messages import get_messages
from django.urls import reverse
from mcp import Client

from multigpt.chat.mcp import bridge
from multigpt.chat.mcp import client as mcp_client
from multigpt.chat.mcp.config import ServerConfig, parse_credentials, split_command
from multigpt.chat.mcp.errors import McpError
from multigpt.chat.models import McpServer, ToolCall
from tests import mcp_test_server

# --- Zugangsdaten ------------------------------------------------------------


def test_parse_credentials_formats():
    assert parse_credentials("", "stdio").env == {}
    assert parse_credentials('{"env": {"A": "1"}}', "stdio").env == {"A": "1"}
    assert parse_credentials('{"A": "1"}', "stdio").env == {"A": "1"}
    assert parse_credentials("tok", "http").headers == {"Authorization": "Bearer tok"}
    assert parse_credentials('{"bearer_token": "t"}', "http").headers == {
        "Authorization": "Bearer t"
    }
    assert parse_credentials('{"headers": {"X-Key": "k"}}', "http").headers == {"X-Key": "k"}
    assert parse_credentials('{"X-Key": "k"}', "http").headers == {"X-Key": "k"}


@pytest.mark.parametrize(
    ("raw", "transport"),
    [
        ("kein-json", "stdio"),
        ("[1, 2]", "stdio"),
        ('{"env": {"A": 1}}', "stdio"),
        ('{"headers": {"X": "y"}}', "stdio"),
        ('{"env": {"A": "1"}}', "http"),
        ('{"headers": {"X\\nY": "y"}}', "http"),
        ('{"bearer_token": ""}', "http"),
    ],
)
def test_parse_credentials_rejects(raw, transport):
    with pytest.raises(ValueError):
        parse_credentials(raw, transport)


def test_split_command_without_shell():
    assert split_command('python "mein server.py" --x') == ["python", "mein server.py", "--x"]
    assert split_command("a; rm -rf /") == ["a;", "rm", "-rf", "/"]
    with pytest.raises(ValueError):
        split_command('python "offen')
    with pytest.raises(ValueError):
        split_command("   ")


def test_server_config_hides_secrets_and_fingerprints():
    server = McpServer(
        pk=7, name="S", transport="http", url="http://x/mcp", credentials="geheimes-token"
    )
    config = ServerConfig.from_model(server)
    assert "geheimes-token" not in repr(config)
    server.credentials = "anderes-token"
    assert ServerConfig.from_model(server).fingerprint != config.fingerprint


def test_server_config_bad_setup_is_mcp_error():
    server = McpServer(pk=8, name="S", transport="stdio", command="x", credentials="kein-json")
    with pytest.raises(McpError, match="falsch eingerichtet"):
        ServerConfig.from_model(server)


# --- Modellfelder ------------------------------------------------------------


@pytest.mark.django_db
def test_new_fields_defaults():
    server = McpServer.objects.create(name="F", transport="stdio", command="x")
    assert server.known_tools == []
    assert server.timeout_seconds == 30
    assert ToolCall._meta.get_field("provider_call_id").max_length == 200


# --- Admin -------------------------------------------------------------------


@pytest.fixture
def admin_client(client, django_user_model, password):
    admin_user = django_user_model.objects.create_superuser(
        username="jo", password=password, email="jo@example.invalid"
    )
    client.force_login(admin_user)
    return client


@pytest.fixture
def inproc(monkeypatch):
    bridge.shutdown()
    monkeypatch.setattr(
        mcp_client, "_make_client", lambda config: Client(mcp_test_server.server, cache=None)
    )
    yield
    bridge.shutdown()


@pytest.fixture
def server(db):
    return McpServer.objects.create(
        name="Test",
        transport="stdio",
        command="python server.py",
        known_tools=["echo"],
        tools_requiring_confirmation=["crash"],
    )


def _form_data(server, **extra):
    data = {
        "name": server.name,
        "transport": server.transport,
        "command": server.command,
        "url": server.url,
        "credentials": "",
        "active": "on",
        "timeout_seconds": "30",
        "tools_requiring_confirmation": '["crash"]',
        "known_tools": '["echo"]',
    }
    data.update(extra)
    return data


@pytest.mark.django_db
def test_change_form_does_not_connect_by_default(admin_client, server, monkeypatch):
    def boom(config):  # pragma: no cover - darf nicht aufgerufen werden
        raise AssertionError("verbunden")

    monkeypatch.setattr(mcp_client, "_make_client", boom)
    url = reverse("admin:chat_mcpserver_change", args=[server.pk])
    html = admin_client.get(url).content.decode()
    assert "Jetzt prüfen" in html  # ersetzt den Link „Werkzeugliste abrufen“ (?tools=1)
    assert not bridge.is_running()


@pytest.mark.django_db
def test_change_form_shows_tool_list_with_rating(admin_client, server, inproc):
    admin_client.post(reverse("admin:chat_mcpserver_check", args=[server.pk]))
    url = reverse("admin:chat_mcpserver_change", args=[server.pk])
    html = admin_client.get(url).content.decode()
    assert "<code>echo</code>" in html
    assert '<option value="auto" selected>ohne Rückfrage</option>' in html  # echo
    assert '<option value="confirm" selected>mit Rückfrage</option>' in html  # crash
    assert '<option value="" selected>nicht eingestuft</option>' in html  # z. B. add


@pytest.mark.django_db
def test_change_form_shows_connection_error(admin_client, server):
    bridge.shutdown()
    server.command = "/nicht/vorhanden"
    server.save()
    response = admin_client.post(
        reverse("admin:chat_mcpserver_check", args=[server.pk]), follow=True
    )
    assert "Programm nicht gefunden" in response.content.decode()
    bridge.shutdown()


@pytest.mark.django_db
def test_admin_action_check_connection(admin_client, server, inproc):
    response = admin_client.post(
        reverse("admin:chat_mcpserver_changelist"),
        {"action": "check_connection_action", "_selected_action": [server.pk]},
        follow=True,
    )
    texts = [str(m) for m in get_messages(response.wsgi_request)]
    assert any("Verbindung in Ordnung" in t and "add" in t for t in texts), texts


@pytest.mark.django_db
def test_admin_adopt_listed_tools(admin_client, server, inproc):
    url = reverse("admin:chat_mcpserver_change", args=[server.pk])
    response = admin_client.post(url, _form_data(server, adopt_listed_tools="on"))
    assert response.status_code == 302
    server.refresh_from_db()
    assert server.known_tools[0] == "echo"
    assert {"add", "fail", "sleep"} <= set(server.known_tools)
    assert server.tools_requiring_confirmation == ["crash"]


@pytest.mark.django_db
def test_admin_rejects_bad_credentials(admin_client, server):
    url = reverse("admin:chat_mcpserver_change", args=[server.pk])
    response = admin_client.post(url, _form_data(server, credentials="kein-json"))
    assert response.status_code == 200
    assert "müssen ein JSON-Objekt sein" in response.content.decode()
    assert "kein-json" not in response.content.decode()


@pytest.mark.django_db
def test_admin_rejects_bad_tool_lists(admin_client, server):
    url = reverse("admin:chat_mcpserver_change", args=[server.pk])
    response = admin_client.post(url, _form_data(server, known_tools='{"a": 1}'))
    assert response.status_code == 200
    assert "JSON-Liste von Werkzeugnamen" in response.content.decode()


@pytest.mark.django_db
def test_deleting_server_closes_session(server, inproc):
    from multigpt.chat import mcp

    mcp.list_tools(server)
    assert bridge.run(_stats(), timeout=5)[server.pk]["connected"]
    pk = server.pk
    server.delete()
    for _ in range(100):
        if pk not in bridge.run(_stats(), timeout=5):
            break
        time.sleep(0.02)
    assert pk not in bridge.run(_stats(), timeout=5)


async def _stats():
    return mcp_client.connection_stats()
