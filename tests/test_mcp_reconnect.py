"""Chat-Button: Zugriffe, CSRF, echte Neuverbindung und Runner-Sitzung."""

import pytest
from django.template.loader import render_to_string
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from multigpt.chat.mcp import reconnect
from multigpt.chat.mcp.config import ServerConfig
from multigpt.chat.models import Conversation, McpServer, Message, ToolCall
from multigpt.runner.mcp_server import ensure_mcp_server
from multigpt.runner.models import ChatWorkspace, Runner, RunnerSession, RunnerToken
from tests.test_tool_loop import make_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup(client, monkeypatch):
    user = make_user("admin", "admin")
    client.force_login(user)
    conversation = Conversation.objects.create(user=user, title="Reconnect")
    message = Message.objects.create(conversation=conversation, author=user, role="assistant")
    server = McpServer.objects.create(name="Test", transport="stdio", command="test-server")
    call = ToolCall.objects.create(message=message, server=server, tool="echo", status="error")
    calls = []
    monkeypatch.setattr(
        reconnect, "reconnect_with", lambda config, **kw: calls.append(config) or []
    )
    url = reverse("chat:api_tool_reconnect", args=[conversation.pk, call.pk])
    return user, conversation, server, call, calls, url


def test_reconnect_reloads_connection_without_repeating_tool(client, setup):
    user, _, server, call, calls, url = setup
    before = ServerConfig.from_model(server).fingerprint
    response = client.post(url)
    assert response.status_code == 200
    assert response.json()["message"] == "MCP-Verbindung neu aufgebaut."
    assert len(calls) == 1 and calls[0].connection_revision == 1
    server.refresh_from_db()
    assert ServerConfig.from_model(server).fingerprint != before
    call.refresh_from_db()
    assert call.status == "error"  # Kein Wiederholen eines möglicherweise schreibenden Aufrufs.
    request = RequestFactory().get("/")
    request.user = user
    html = render_to_string("chat/_tool_call.html", {"tc": call, "request": request})
    assert "MCP neu verbinden" not in html
    assert "data-tool-reconnect" not in html


def test_reconnect_requires_write_and_admin_for_shared_server(client, setup):
    user, _, server, _, calls, url = setup
    user.role = make_user("adult", "other").role
    user.save()
    assert client.post(url).status_code == 403
    assert calls == []
    server.refresh_from_db()
    assert server.connection_revision == 0


def test_foreign_chat_is_hidden(client, setup):
    _, _, _, _, calls, url = setup
    client.force_login(make_user("admin", "foreign"))
    assert client.post(url).status_code == 404
    assert calls == []


@pytest.mark.parametrize("status", ["running", "awaiting_confirmation"])
def test_running_call_is_not_interrupted(client, setup, status):
    _, _, _, call, calls, url = setup
    call.status = status
    call.save()
    assert client.post(url).status_code == 409
    assert calls == []


def test_post_and_csrf_required(client, setup):
    user, _, _, _, calls, url = setup
    assert client.get(url).status_code == 405
    protected = Client(enforce_csrf_checks=True)
    protected.force_login(user)
    assert protected.post(url).status_code == 403
    assert calls == []


def test_builtin_has_no_connection(client, setup):
    _, _, _, call, calls, url = setup
    call.server = None
    call.save()
    assert client.post(url).status_code == 403
    assert calls == []


def test_runner_reconnect_uses_existing_owned_session(client, setup):
    user, conversation, _, call, calls, url = setup
    server = ensure_mcp_server(user)
    call.server = server
    call.save()
    token = RunnerToken.objects.create(owner=user, prefix="test", token_hash="0" * 64)
    runner = Runner.objects.create(token=token, status="online")
    session = RunnerSession.objects.create(
        owner=user, runner=runner, conversation=conversation, status="running"
    )
    ChatWorkspace.objects.create(conversation=conversation, session=session)
    assert client.post(url).status_code == 200
    assert calls[0].runner_id == runner.pk
    assert calls[0].runner_session == str(session.uuid)
    assert calls[0].runner_pipe
    session.refresh_from_db()
    assert session.status == "running"


def test_runner_reconnect_fails_without_connected_runner(client, setup):
    user, _, _, call, calls, url = setup
    call.server = ensure_mcp_server(user)
    call.save()
    response = client.post(url)
    assert response.status_code == 502 and "nicht verbunden" in response.json()["error"]
    assert calls == []


def test_runner_reconnect_without_session_accepts_online_runner(client, setup):
    from django.utils import timezone

    user, _, _, call, calls, url = setup
    server = ensure_mcp_server(user)
    call.server = server
    call.save()
    token = RunnerToken.objects.create(owner=user, prefix="test", token_hash="0" * 64)
    Runner.objects.create(token=token, status="online", last_seen=timezone.now())
    response = client.post(url)
    assert response.status_code == 200 and "Sitzung startet" in response.json()["message"]
    assert calls == []
    server.refresh_from_db()
    assert server.online


def test_picker_exposes_reconnect_and_recovers_offline_server(client, setup):
    _, _, server, _, calls, _ = setup
    server.online = False
    server.last_error = "Nicht erreichbar"
    server.last_checked = timezone.now()
    server.save()
    url = reverse("chat:api_mcp_reconnect", args=[server.pk])
    listing = client.get(reverse("chat:api_mcp_servers")).json()
    entry = next(item for item in listing if item["id"] == server.pk)
    assert entry["reconnect_url"] == url and entry["online"] is False
    response = client.post(url, "{}", content_type="application/json")
    assert response.status_code == 200 and len(calls) == 1
    server.refresh_from_db()
    assert server.online and not server.last_error
    listing = client.get(reverse("chat:api_mcp_servers")).json()
    assert next(item for item in listing if item["id"] == server.pk)["online"] is True


def test_picker_does_not_offer_admin_reconnect_to_adult(client, setup):
    user, _, server, _, calls, _ = setup
    user.role = make_user("adult", "picker-adult").role
    user.save()
    url = reverse("chat:api_mcp_reconnect", args=[server.pk])
    listing = client.get(reverse("chat:api_mcp_servers")).json()
    entry = next(item for item in listing if item["id"] == server.pk)
    assert entry["reconnect_url"] is None
    assert client.post(url, "{}", content_type="application/json").status_code == 403
    assert calls == []


def test_runner_server_listed_online_when_runner_connected(client, setup):
    from django.utils import timezone

    user, _, _, _, _, _ = setup
    server = ensure_mcp_server(user)
    server.last_checked = timezone.now()
    server.online = False
    server.last_error = "alter Fehler"
    server.save()
    entry = lambda: next(  # noqa: E731
        s for s in client.get("/api/mcp-servers/").json() if s["id"] == server.pk
    )
    assert entry()["online"] is False
    token = RunnerToken.objects.create(owner=user, prefix="test", token_hash="0" * 64)
    Runner.objects.create(token=token, status="online", last_seen=timezone.now())
    assert entry()["online"] is True
