"""Runner über M15-Keys (Entscheidung 8o): ``ask`` mit ``workspace`` und ``runner.use``.

Nur Arbeitsstände des Kontos, ohne Netz, ohne ssh/telnet, ohne Rückfragen.
"""

import pytest
from django.utils import timezone

from multigpt.chat import tooling
from multigpt.chat.mcp import bridge
from multigpt.chat.models import AIModel, Provider, ToolCall
from multigpt.chat.providers import registry
from multigpt.node import scopes as S
from multigpt.runner import chat as runner_chat
from multigpt.runner import mcp_transport
from multigpt.runner.mcp_server import ensure_mcp_server
from multigpt.runner.models import (
    Runner,
    RunnerHost,
    RunnerSession,
    RunnerToken,
    RunnerWorkspace,
)
from tests.node_support import call, make_key, make_user, structured, text
from tests.runner_fakes import ALL_SERVICES, FakeLink
from tests.test_tool_loop import ScriptedAdapter, answer, tool_round
from tests.test_tool_loop import call as tool_call_event

pytestmark = pytest.mark.django_db

IMAGE = "ghcr.io/test/work@sha256:" + "0" * 64


@pytest.fixture
def link(settings, monkeypatch, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.RUNNER_WORK_IMAGE = IMAGE
    bridge.shutdown()
    fake = FakeLink()
    mcp_transport.set_link(fake)
    fake.started = []

    def start(runner, frame, session):
        fake.started.append(frame)
        return {"type": "session_state", "session": frame["session"], "state": "running"}

    monkeypatch.setattr(runner_chat, "_start_remote", start)
    yield fake
    bridge.shutdown()
    mcp_transport.set_link(None)


@pytest.fixture
def scripted(monkeypatch):
    def install(script):
        calls = []
        monkeypatch.setattr(
            registry, "get_adapter", lambda provider: ScriptedAdapter(provider, script, calls)
        )
        return calls

    return install


@pytest.fixture
def anna():
    user = make_user("anna")
    user.role.runner_network = True  # über Keys trotzdem ohne Netz
    user.role.can_use_runner_hosts = True
    user.role.save()
    return user


@pytest.fixture
def setup(anna):
    provider = Provider.objects.create(name="Cloud", kind=Provider.Kind.ANTHROPIC)
    AIModel.objects.create(
        provider=provider,
        model_id="claude-test",
        display_name="Claude",
        supports_tools=True,
        mcp_access=AIModel.McpAccess.ALL,
    )
    server = ensure_mcp_server(anna)
    token = RunnerToken.objects.create(owner=anna, name="PC", prefix="abc", token_hash="0" * 64)
    Runner.objects.create(
        token=token,
        name="pc",
        status=Runner.Status.ONLINE,
        last_seen=timezone.now(),
        catalog_state="valid",
        services=ALL_SERVICES,
    )
    RunnerWorkspace.objects.create(owner=anna, name="spiel")
    host = RunnerHost.objects.create(
        name="nas", address="192.0.2.10", protocol=RunnerHost.Protocol.TELNET, port=23
    )
    host.allowed_users.add(anna)
    return server


def test_ask_with_workspace_runs_without_network(client, anna, setup, scripted, link):
    server = setup
    _, secret = make_key(anna, [S.CHAT_ASK, S.RUNNER_USE])
    bash = tooling.tool_name(server, "bash")
    calls = scripted([tool_round(tool_call_event(bash, {"command": "echo hi"}, "c1")), answer()])
    _, result = call(client, secret, "ask", {"prompt": "Bau was.", "workspace": "spiel"})
    assert not result["isError"], result
    assert text(result) == "Fertig."
    offered = {t.name for t in calls[0]["tools"]}
    assert bash in offered
    assert tooling.tool_name(server, "telnet") not in offered  # nie über Keys
    assert all(name.startswith(tooling.tool_name(server, "")[:6]) for name in offered)
    session = RunnerSession.objects.get()
    assert session.api_key_id is not None and session.network is False
    assert link.started[0]["network"] == "none" and "allow" not in link.started[0]
    assert ToolCall.objects.get().status == ToolCall.Status.OK
    assert structured(result)["status"] == "complete"


def test_ask_workspace_needs_scope(client, anna, setup, scripted, link):
    _, secret = make_key(anna, [S.CHAT_ASK])
    _, result = call(client, secret, "ask", {"prompt": "x", "workspace": "spiel"})
    assert result["isError"]
    assert "runner.use" in text(result)


def test_ask_only_own_workspace(client, anna, setup, scripted, link):
    other = make_user("bert")
    RunnerWorkspace.objects.create(owner=other, name="fremd")
    _, secret = make_key(anna, [S.CHAT_ASK, S.RUNNER_USE])
    _, result = call(client, secret, "ask", {"prompt": "x", "workspace": "fremd"})
    assert result["isError"]
    assert "nicht gefunden" in text(result)


def test_chat_state_api(client, anna, setup):
    from multigpt.chat.models import Conversation

    conversation = Conversation.objects.create(user=anna)
    workspace = RunnerWorkspace.objects.get(name="spiel")
    client.force_login(anna)
    url = f"/runner/api/chat/{conversation.pk}/"
    data = client.get(url).json()
    assert data["available"] and data["workspace_id"] is None
    assert [w["name"] for w in data["workspaces"]] == ["spiel"]
    response = client.post(
        url, {"workspace": workspace.pk, "mode": "ask"}, content_type="application/json"
    )
    assert response.status_code == 200, response.content
    assert response.json()["workspace_id"] == workspace.pk
    assert response.json()["effective_mode"] == "ask"
    # Netz an (Rolle): „alles automatisch“ nur für Verwalter
    response = client.post(url, {"mode": "auto"}, content_type="application/json")
    assert response.status_code == 400
    other = make_user("bert")
    client.force_login(other)
    assert client.get(url).status_code == 404


def test_key_conditions_survive_round_limit_confirmation(
    client, anna, setup, scripted, link, monkeypatch
):
    """Nach „weiterarbeiten?“ (im Web bestätigt) gelten weiter die Bedingungen des Keys."""
    from multigpt.chat.models import Conversation
    from tests.test_tool_loop import confirm, events_of

    monkeypatch.setattr(runner_chat, "MAX_ROUNDS", 1)
    server = setup
    _, secret = make_key(anna, [S.CHAT_ASK, S.RUNNER_USE])
    bash = tooling.tool_name(server, "bash")
    calls = scripted(
        [
            tool_round(tool_call_event(bash, {"command": "echo 1"}, "c1")),
            tool_round(tool_call_event(bash, {"command": "echo 2"}, "c2")),
            answer(),
        ]
    )
    _, result = call(client, secret, "ask", {"prompt": "Bau was.", "workspace": "spiel"})
    data = structured(result)
    assert data["status"] == "awaiting_confirmation"
    waiting = ToolCall.objects.get(status=ToolCall.Status.AWAITING_CONFIRMATION)
    client.force_login(anna)
    conversation = Conversation.objects.get(pk=data["chat_id"])
    events_of(confirm(client, conversation, {str(waiting.pk): "approve"}))
    assert len(calls) >= 2
    offered = {t.name for t in calls[1]["tools"]}
    assert tooling.tool_name(server, "telnet") not in offered  # weiter ohne ssh/telnet
    assert all(name.startswith(tooling.tool_name(server, "")[:6]) for name in offered)
    session = RunnerSession.objects.get()
    assert session.network is False and session.api_key_id is not None
