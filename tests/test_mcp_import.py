"""MCP-Server aus „mcpServers“-JSON importieren (Admin)."""

import json

import pytest
from django.contrib.messages import get_messages
from django.urls import reverse

from multigpt.chat.mcp.config import parse_credentials
from multigpt.chat.mcp.importer import apply_import, parse_config
from multigpt.chat.models import McpServer

PASSWORD = "Geheim-Test-1234"
TOKEN = "geheimes-token-123456"
URL = "https://intranet.bernau.family/mcp-server/http"


def config(servers):
    return json.dumps({"mcpServers": servers})


N8N = {"n8n-mcp": {"type": "http", "url": URL, "headers": {"Authorization": f"Bearer {TOKEN}"}}}
N8N_PLACEHOLDER = {
    "n8n-mcp": {
        "type": "http",
        "url": URL,
        "headers": {"Authorization": "Bearer <YOUR_ACCESS_TOKEN_HERE>"},
    }
}


@pytest.fixture
def admin_client(client, django_user_model):
    user = django_user_model.objects.create_superuser(
        username="jo", password=PASSWORD, email="jo@example.invalid"
    )
    client.force_login(user)
    return client


def texts(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


# --- Zerlegen ---------------------------------------------------------------------


def test_parse_http_with_headers():
    (entry,) = parse_config(config(N8N))
    assert (entry.name, entry.transport, entry.url, entry.error) == ("n8n-mcp", "http", URL, "")
    assert parse_credentials(entry.credentials, "http").headers == {
        "Authorization": f"Bearer {TOKEN}"
    }
    assert entry.placeholders == []
    assert TOKEN not in repr(entry)


@pytest.mark.parametrize(
    "value", ["Bearer <YOUR_ACCESS_TOKEN_HERE>", "Bearer ${N8N_TOKEN}", "YOUR_TOKEN", "changeme"]
)
def test_parse_placeholder_not_stored(value):
    servers = {"x": {"type": "http", "url": URL, "headers": {"Authorization": value}}}
    (entry,) = parse_config(config(servers))
    assert entry.placeholders == ["Authorization"]
    assert entry.credentials == ""


def test_parse_stdio_command_args_env():
    servers = {
        "fs": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/srv/a b"],
            "env": {"API_KEY": "k-123"},
        }
    }
    (entry,) = parse_config(config(servers))
    assert entry.transport == "stdio"
    assert entry.command == "npx -y @modelcontextprotocol/server-filesystem '/srv/a b'"
    assert parse_credentials(entry.credentials, "stdio").env == {"API_KEY": "k-123"}


@pytest.mark.parametrize(
    ("wrapper", "spec"),
    [
        ("servers", {"type": "streamable-http", "url": URL}),  # VS Code
        (None, {"url": URL}),  # ohne Hülle, Typ aus der URL
        ("mcpServers", {"type": "streamableHttp", "serverUrl": URL}),
    ],
)
def test_parse_variants(wrapper, spec):
    data = {wrapper: {"s": spec}} if wrapper else {"s": spec}
    (entry,) = parse_config(json.dumps(data))
    assert (entry.transport, entry.url, entry.error) == ("http", URL, "")


@pytest.mark.parametrize(
    ("spec", "fragment"),
    [
        ({"type": "sse", "url": URL}, "SSE"),
        ({"type": "http"}, "URL"),
        ({"type": "http", "url": "javascript:alert(1)"}, "ungültig"),
        ({"type": "stdio"}, "command"),
        ({"type": "ftp", "url": URL}, "Unbekannter Typ"),
        ({"type": "http", "url": URL, "headers": {"X": "a\r\nb"}}, "Header"),
        ("kaputt", "kein JSON-Objekt"),
    ],
)
def test_parse_entry_errors(spec, fragment):
    (entry,) = parse_config(config({"s": spec}))
    assert fragment in entry.error


@pytest.mark.parametrize("text", ["", "{", "[]", '{"mcpServers": {}}'])
def test_parse_config_errors(text):
    with pytest.raises(ValueError):
        parse_config(text)


# --- Übernehmen ---------------------------------------------------------------------


@pytest.mark.django_db
def test_apply_creates_active_server():
    (result,) = apply_import(parse_config(config(N8N)), update_existing=False)
    assert result.action == "created"
    server = McpServer.objects.get(name="n8n-mcp")
    assert server.active is True
    assert server.url == URL
    assert server.known_tools == []  # alles mit Rückfrage bis zur Einstufung
    assert TOKEN in server.credentials


@pytest.mark.django_db
def test_apply_placeholder_creates_inactive():
    (result,) = apply_import(parse_config(config(N8N_PLACEHOLDER)), update_existing=False)
    server = McpServer.objects.get(name="n8n-mcp")
    assert server.active is False
    assert server.credentials == ""
    assert "Platzhalter" in result.message and "Authorization" in result.message


@pytest.mark.django_db
def test_apply_existing_skipped_or_updated():
    server = McpServer.objects.create(
        name="n8n-mcp",
        transport="http",
        url="https://alt.example/mcp",
        credentials="alt-token",
        known_tools=["suche"],
    )
    (result,) = apply_import(parse_config(config(N8N)), update_existing=False)
    assert result.action == "skipped"
    server.refresh_from_db()
    assert server.url == "https://alt.example/mcp"

    (result,) = apply_import(parse_config(config(N8N)), update_existing=True)
    assert result.action == "updated"
    server.refresh_from_db()
    assert server.url == URL
    assert server.known_tools == ["suche"]
    assert TOKEN in server.credentials

    # Platzhalter überschreiben gespeicherte Zugangsdaten nicht.
    apply_import(parse_config(config(N8N_PLACEHOLDER)), update_existing=True)
    server.refresh_from_db()
    assert TOKEN in server.credentials


# --- Admin ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_admin_changelist_links_import(admin_client):
    html = admin_client.get(reverse("admin:chat_mcpserver_changelist")).content.decode()
    assert reverse("admin:chat_mcpserver_import") in html


@pytest.mark.django_db
def test_admin_import_page(admin_client):
    response = admin_client.get(reverse("admin:chat_mcpserver_import"))
    assert response.status_code == 200
    html = response.content.decode()
    assert "admin_mcp_import.js" in html
    assert 'id="mcp-import-preview"' in html


@pytest.mark.django_db
def test_admin_import_post(admin_client):
    response = admin_client.post(
        reverse("admin:chat_mcpserver_import"), {"config": config(N8N)}, follow=True
    )
    assert McpServer.objects.filter(name="n8n-mcp", active=True).exists()
    messages = " ".join(texts(response))
    assert "„n8n-mcp“: angelegt." in messages
    assert "Rückfrage" in messages
    assert TOKEN not in response.content.decode()


@pytest.mark.django_db
def test_admin_import_invalid_does_not_echo_token(admin_client):
    broken = config(N8N)[:-1]  # ungültiges JSON mit Token
    response = admin_client.post(reverse("admin:chat_mcpserver_import"), {"config": broken})
    assert response.status_code == 200
    html = response.content.decode()
    assert "Kein gültiges JSON" in html
    assert TOKEN not in html
    assert not McpServer.objects.exists()


@pytest.mark.django_db
def test_admin_import_requires_permission(client, django_user_model):
    user = django_user_model.objects.create_user(username="kind", password=PASSWORD)
    user.is_staff = True
    user.save()
    client.force_login(user)
    response = client.post(reverse("admin:chat_mcpserver_import"), {"config": config(N8N)})
    assert response.status_code == 403
    assert not McpServer.objects.exists()
