"""Runner im Chat (M14-06, 8j/8t/8u): Werkzeuge, Rückfrage-Modi, Sitzung, Runden.

Anbieter: Fake-Adapter mit vorgegebenen Runden. Runner: ``FakeLink`` aus
tests/runner_fakes.py statt Zustellung, ``_start_remote`` ersetzt den
Sitzungsstart (keine echten Container).
"""

import pytest
from django.conf import settings
from django.utils import timezone

from multigpt.chat import tooling
from multigpt.chat.mcp import bridge
from multigpt.chat.models import AIModel, Conversation, Message, Provider, ToolCall
from multigpt.chat.providers import registry
from multigpt.runner import chat as runner_chat
from multigpt.runner import mcp_transport, tokens, trust
from multigpt.runner import tools as runner_tools
from multigpt.runner.mcp_server import ensure_mcp_server
from multigpt.runner.models import (
    ChatWorkspace,
    Runner,
    RunnerAudit,
    RunnerHost,
    RunnerSession,
    RunnerToken,
    RunnerWorkspace,
)
from tests.runner_fakes import ALL_SERVICES, FakeLink
from tests.test_tool_loop import (
    ScriptedAdapter,
    answer,
    call,
    confirm,
    events_of,
    make_user,
    names,
    tool_round,
)
from tests.test_tool_loop import send as loop_send

pytestmark = pytest.mark.django_db

IMAGE = "ghcr.io/test/work@sha256:" + "0" * 64


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
def link(settings, monkeypatch, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.RUNNER_WORK_IMAGE = IMAGE
    bridge.shutdown()
    fake = FakeLink()
    mcp_transport.set_link(fake)
    started = []

    def start(runner, frame, session):
        started.append(frame)
        return {"type": "session_state", "session": frame["session"], "state": "running"}

    monkeypatch.setattr(runner_chat, "_start_remote", start)
    fake.started = started
    yield fake
    bridge.shutdown()
    mcp_transport.set_link(None)


@pytest.fixture
def ai_model():
    provider = Provider.objects.create(name="Cloud", kind=Provider.Kind.ANTHROPIC)
    return AIModel.objects.create(
        provider=provider,
        model_id="claude-test",
        display_name="Claude",
        supports_tools=True,
        mcp_access=AIModel.McpAccess.ALL,
    )


@pytest.fixture
def adult(client):
    user = make_user("adult", "anna")
    client.force_login(user)
    return user


@pytest.fixture
def server(adult):
    return ensure_mcp_server(adult)


@pytest.fixture
def runner(adult):
    token = RunnerToken.objects.create(owner=adult, name="PC", prefix="abc", token_hash="0" * 64)
    return Runner.objects.create(
        token=token,
        name="pc",
        status=Runner.Status.ONLINE,
        last_seen=timezone.now(),
        catalog_state="valid",
        services=ALL_SERVICES,
    )


@pytest.fixture
def workspace(adult):
    return RunnerWorkspace.objects.create(owner=adult, name="spiel")


@pytest.fixture
def conversation(adult, workspace, server, runner, ai_model):
    conversation = Conversation.objects.create(user=adult)
    ChatWorkspace.objects.create(conversation=conversation, workspace=workspace)
    return conversation


def send(client, conversation, **data):
    data.setdefault("model", AIModel.objects.get(model_id="claude-test").pk)
    return loop_send(client, conversation, **data)


def tname(server, tool):
    return tooling.tool_name(server, tool)


def tool_calls(message_id=None):
    qs = ToolCall.objects.order_by("pk")
    return [(tc.tool, tc.status) for tc in qs]


# --- Angebot ----------------------------------------------------------------------------


def test_catalog_offered_with_workspace(client, adult, conversation, server, scripted, link):
    calls = scripted([answer()])
    events_of(send(client, conversation, mcp_servers=[server.pk]))
    offered = {t.name for t in calls[0]["tools"]}
    for tool in ("read", "write", "edit", "glob", "grep", "bash", "serve", "todo"):
        assert tname(server, tool) in offered
    # ssh/telnet nur mit freigegebenen Hosts
    assert tname(server, "ssh") not in offered
    assert not link.started  # Sitzung erst beim ersten Aufruf


def test_no_tools_without_workspace(client, adult, server, runner, ai_model, scripted, link):
    conversation = Conversation.objects.create(user=adult)
    calls = scripted([answer()])
    events_of(send(client, conversation, mcp_servers=[server.pk]))
    offered = {t.name for t in calls[0]["tools"] or []}
    assert not any(name.startswith(tname(server, "")[:6]) for name in offered)


def test_other_account_does_not_see_runner(client, conversation, server, ai_model, scripted, link):
    other = make_user("adult", "bert")
    client.force_login(other)
    own = Conversation.objects.create(user=other)
    calls = scripted([answer()])
    events_of(send(client, own, mcp_servers=[server.pk]))
    offered = {t.name for t in calls[0]["tools"] or []}
    assert tname(server, "bash") not in offered
    assert server.pk not in [s.pk for s in tooling.available_servers(other)]


def test_model_without_mcp_access_gets_no_runner(
    client, conversation, server, ai_model, scripted, link
):
    ai_model.mcp_access = AIModel.McpAccess.NONE
    ai_model.save()
    calls = scripted([answer()])
    events_of(send(client, conversation, mcp_servers=[server.pk]))
    offered = {t.name for t in calls[0]["tools"] or []}
    assert tname(server, "bash") not in offered


# --- Ausführung und Modi ---------------------------------------------------------------


def test_auto_mode_runs_without_confirmation(client, conversation, server, scripted, link):
    scripted(
        [
            tool_round(
                call(tname(server, "write"), {"path": "a.py", "content": "print(1)"}, "c1"),
                call(tname(server, "bash"), {"command": "echo hallo"}, "c2"),
            ),
            answer(),
        ]
    )
    events = events_of(send(client, conversation, mcp_servers=[server.pk]))
    assert "confirmation_required" not in names(events)
    assert link.workspace.files == {"a.py": "print(1)"}
    assert tool_calls() == [("write", "ok"), ("bash", "ok")]
    # Sitzung genau einmal gestartet, mit Sperre und gemerkt im Chat
    assert len(link.started) == 1
    frame = link.started[0]
    assert frame["network"] == "none" and frame["image"] == IMAGE
    session = RunnerSession.objects.get()
    assert session.status == RunnerSession.Status.RUNNING
    assert RunnerWorkspace.objects.get().active_session_id == session.pk
    assert ChatWorkspace.objects.get().session_id == session.pk
    assert RunnerAudit.objects.filter(kind="tool").count() == 2
    assert "runner_session" in names(events)


def test_ask_mode_confirms_edits_and_commands(client, conversation, server, scripted, link):
    ChatWorkspace.objects.update(mode="ask")
    scripted(
        [
            tool_round(
                call(tname(server, "read"), {"path": "a.py"}, "c1"),
                call(tname(server, "write"), {"path": "a.py", "content": "x"}, "c2"),
            ),
            answer(),
        ]
    )
    events = events_of(send(client, conversation, mcp_servers=[server.pk]))
    assert names(events)[-1] == "done"
    assert events[-1][1]["status"] == Message.Status.AWAITING_CONFIRMATION
    waiting = ToolCall.objects.filter(status=ToolCall.Status.AWAITING_CONFIRMATION)
    assert [tc.tool for tc in waiting] == ["write"]
    assert link.workspace.files == {}


def test_edits_mode_confirms_only_commands(client, conversation, server, scripted, link):
    ChatWorkspace.objects.update(mode="edits")
    scripted(
        [
            tool_round(call(tname(server, "write"), {"path": "a", "content": "x"}, "c1")),
            tool_round(call(tname(server, "bash"), {"command": "ls"}, "c2")),
            answer(),
        ]
    )
    events = events_of(send(client, conversation, mcp_servers=[server.pk]))
    assert link.workspace.files == {"a": "x"}
    waiting = ToolCall.objects.get(status=ToolCall.Status.AWAITING_CONFIRMATION)
    assert waiting.tool == "bash"
    events = events_of(confirm(client, conversation, {str(waiting.pk): "approve"}))
    assert events[-1][1]["status"] == Message.Status.COMPLETE
    assert ("bash", {"command": "ls", "timeout_s": 120}) in link.workspace.calls


def test_network_defaults_to_command_confirmation(
    client, adult, conversation, server, scripted, link
):
    adult.role.runner_network = True
    adult.role.save()
    scripted([tool_round(call(tname(server, "bash"), {"command": "ls"}, "c1")), answer()])
    events = events_of(send(client, conversation, mcp_servers=[server.pk]))
    assert "confirmation_required" in names(events)
    # „alles automatisch“ mit Netz nur für Verwalter
    with pytest.raises(ValueError):
        runner_chat.set_state(adult, conversation, mode="auto")


def test_invalid_arguments_are_rejected_before_runner(client, conversation, server, scripted, link):
    scripted([tool_round(call(tname(server, "read"), {"path": 5}, "c1")), answer()])
    events_of(send(client, conversation, mcp_servers=[server.pk]))
    tc = ToolCall.objects.get()
    assert tc.status == ToolCall.Status.ERROR
    assert "Ungültige Argumente" in tc.result_text
    assert not link.started


def test_todo_is_server_side_and_visible(client, conversation, server, scripted, link):
    items = [{"content": "Spiel bauen", "status": "in_progress"}]
    scripted([tool_round(call(tname(server, "todo"), {"items": items}, "c1")), answer()])
    events = events_of(send(client, conversation, mcp_servers=[server.pk]))
    assert ("runner_todo", {"items": items}) in events
    assert ChatWorkspace.objects.get().todo == items
    assert not link.started and not link.sent  # nie an den Runner


def test_busy_workspace(client, adult, conversation, workspace, server, scripted, link):
    other = RunnerSession.objects.create(
        owner=adult,
        workspace=workspace,
        status=RunnerSession.Status.RUNNING,
        network=True,
        limits_requested={"image": runner_chat._work_image()},  # nur das Netz weicht ab
    )
    RunnerWorkspace.objects.filter(pk=workspace.pk).update(active_session=other)
    scripted([tool_round(call(tname(server, "bash"), {"command": "ls"}, "c1")), answer()])
    events_of(send(client, conversation, mcp_servers=[server.pk]))
    assert ToolCall.objects.get().result_text == runner_chat.MSG_BUSY


def test_offline_runner_message(client, conversation, runner, server, scripted, link):
    Runner.objects.filter(pk=runner.pk).update(status=Runner.Status.OFFLINE)
    scripted([tool_round(call(tname(server, "bash"), {"command": "ls"}, "c1")), answer()])
    events_of(send(client, conversation, mcp_servers=[server.pk]))
    assert ToolCall.objects.get().result_text == runner_chat.MSG_NO_RUNNER


def test_session_is_reused(client, conversation, server, scripted, link):
    scripted(
        [
            tool_round(call(tname(server, "bash"), {"command": "echo 1"}, "c1")),
            tool_round(call(tname(server, "bash"), {"command": "echo 2"}, "c2")),
            answer(),
        ]
    )
    events_of(send(client, conversation, mcp_servers=[server.pk]))
    assert len(link.started) == 1
    assert RunnerSession.objects.count() == 1


# --- setup_host/telnet ------------------------------------------------------------------------


@pytest.fixture
def host(adult):
    adult.role.can_use_runner_hosts = True
    adult.role.save()
    host = RunnerHost.objects.create(
        name="nas",
        address="192.0.2.10",
        port=22,
        protocol=RunnerHost.Protocol.SSH,
        username="admin",
        secret="GEHEIM-SCHLUESSEL",
        host_key="ssh-ed25519 AAAA",
        host_key_confirmed_at=timezone.now(),
    )
    host.allowed_users.add(adult)
    return host


@pytest.fixture
def dist(settings, tmp_path):
    """Runner-Programme im Download-Verzeichnis und Adresse des Servers."""
    import hashlib

    base = tmp_path / "dist"
    base.mkdir()
    data = b"\x7fELF-test"
    (base / "multigpt-runner-linux-amd64").write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    (base / "SHA256SUMS").write_text(f"{digest}  multigpt-runner-linux-amd64\n")
    settings.RUNNER_DIST_DIR = str(base)
    settings.RUNNER_PUBLIC_URL = "https://mgpt.example"
    return digest


def test_setup_host_always_asks_and_hides_access(
    client, conversation, server, host, scripted, link, dist
):
    calls = scripted(
        [tool_round(call(tname(server, "setup_host"), {"host": "nas"}, "c1")), answer()]
    )
    events = events_of(send(client, conversation, mcp_servers=[server.pk]))
    offered = [t.name for t in calls[0]["tools"]]
    assert tname(server, "ssh") not in offered  # ssh im Alltag gibt es nicht mehr
    spec = next(t for t in calls[0]["tools"] if t.name == tname(server, "setup_host"))
    assert spec.parameters["properties"]["host"]["enum"] == ["nas"]
    assert "192.0.2.10" not in str(calls[0]["tools"])
    assert "confirmation_required" in names(events)  # auch im Modus „alles automatisch“
    waiting = ToolCall.objects.get()
    before = RunnerToken.objects.count()
    events_of(confirm(client, conversation, {str(waiting.pk): "approve"}))
    sent = link.workspace.remote[0]
    assert sent["host"]["address"] == "192.0.2.10"
    assert sent["host"]["secret"] == "GEHEIM-SCHLUESSEL"
    pairing = sent["pairing"]
    assert pairing["url"] == "https://mgpt.example"
    assert pairing["download"] == {
        "amd64": {"url": "https://mgpt.example/runner/download/amd64", "sha256": dist}
    }
    assert pairing["server_key"] == trust.public_key_text()
    assert pairing["images"] == [settings.RUNNER_WORK_IMAGE]  # für pair --images
    # Frischer Kopplungs-Token für das Zielsystem, gültig nach Erfolg
    assert RunnerToken.objects.count() == before + 1
    new = RunnerToken.objects.order_by("-pk").first()
    assert new.name == "Einrichtung nas" and new.is_usable()
    assert tokens.authenticate(pairing["token"]) == new
    waiting.refresh_from_db()
    assert waiting.arguments == {"host": "nas"}
    for secret in ("GEHEIM", "192.0.2.10", pairing["token"]):
        assert secret not in str(waiting.result)
    audit = RunnerAudit.objects.get(kind="remote")
    assert "GEHEIM" not in audit.arguments and pairing["token"] not in audit.arguments
    # 8w: Container erreicht nur den freigegebenen Host
    assert link.started[0]["network"] == "allowlist"
    assert link.started[0]["allow"] == [{"host": "192.0.2.10", "port": 22}]


def test_setup_host_failure_revokes_pairing_token(
    client, conversation, server, host, scripted, link, dist
):
    link.workspace.fail_setup = True
    scripted([tool_round(call(tname(server, "setup_host"), {"host": "nas"}, "c1")), answer()])
    events_of(send(client, conversation, mcp_servers=[server.pk]))
    waiting = ToolCall.objects.get()
    events_of(confirm(client, conversation, {str(waiting.pk): "approve"}))
    waiting.refresh_from_db()
    assert waiting.status == ToolCall.Status.ERROR
    new = RunnerToken.objects.get(name="Einrichtung nas")
    assert new.revoked_at is not None


def test_setup_host_unknown_host(client, conversation, server, host, scripted, link, dist):
    scripted([tool_round(call(tname(server, "setup_host"), {"host": "router"}, "c1")), answer()])
    events_of(send(client, conversation, mcp_servers=[server.pk]))
    waiting = ToolCall.objects.get()
    events_of(confirm(client, conversation, {str(waiting.pk): "approve"}))
    waiting.refresh_from_db()
    assert "router" in waiting.result_text and waiting.status == ToolCall.Status.ERROR
    assert not link.workspace.remote
    assert not RunnerToken.objects.filter(name__startswith="Einrichtung").exists()


def test_services_of_the_runner_limit_the_offer(
    client, conversation, server, runner, scripted, link
):
    """Nur Dienste aus dem gültig signierten Katalog des Runners; todo bleibt."""
    Runner.objects.filter(pk=runner.pk).update(services=["read"], catalog_state="valid")
    calls = scripted([answer()])
    events_of(send(client, conversation, mcp_servers=[server.pk]))
    offered = {t.name for t in calls[0]["tools"]}
    assert tname(server, "read") in offered and tname(server, "todo") in offered
    assert tname(server, "write") not in offered and tname(server, "bash") not in offered


def test_unsigned_runner_gets_no_tools_and_calls_are_refused(
    client, conversation, server, runner, scripted, link
):
    Runner.objects.filter(pk=runner.pk).update(services=[], catalog_state="unsigned")
    calls = scripted(
        [tool_round(call(tname(server, "bash"), {"command": "echo x"}, "c1")), answer()]
    )
    events_of(send(client, conversation, mcp_servers=[server.pk]))
    offered = {t.name for t in calls[0]["tools"]}
    assert offered <= {tname(server, "todo")}
    assert not [c for c in link.workspace.calls if c[0] == "bash"]


def test_needs_confirmation_matrix():
    ask, edits, auto = (
        runner_tools.MODE_ASK,
        runner_tools.MODE_AUTO_EDITS,
        runner_tools.MODE_AUTO_ALL,
    )
    assert not runner_tools.needs_confirmation("read", ask)
    assert runner_tools.needs_confirmation("write", ask)
    assert not runner_tools.needs_confirmation("write", edits)
    assert runner_tools.needs_confirmation("bash", edits)
    assert not runner_tools.needs_confirmation("bash", auto)
    assert runner_tools.needs_confirmation("setup_host", auto)
    assert runner_tools.needs_confirmation("telnet", auto)
    assert runner_tools.needs_confirmation("unbekannt", auto)
    assert runner_tools.effective_mode("", network=False, is_admin=False) == auto
    assert runner_tools.effective_mode("", network=True, is_admin=False) == edits
    assert runner_tools.effective_mode(auto, network=True, is_admin=False) == edits
    assert runner_tools.effective_mode(auto, network=True, is_admin=True) == auto


# --- Runden (8u) -------------------------------------------------------------------------


def test_round_limit_asks_to_continue(client, conversation, server, scripted, link, monkeypatch):
    monkeypatch.setattr(runner_chat, "MAX_ROUNDS", 3)
    bash = tname(server, "bash")
    script = [tool_round(call(bash, {"command": f"echo {i}"}, f"c{i}")) for i in range(6)]
    calls = scripted([*script, answer()])
    events = events_of(send(client, conversation, mcp_servers=[server.pk]))
    required = dict(events)["confirmation_required"]
    assert required["reason"] == "round_limit"
    assert len(calls) == 3
    # Alle bisherigen Aufrufe liefen ohne tool_choice "none".
    assert all(c["params"].get("tool_choice") == "auto" for c in calls)
    waiting = ToolCall.objects.get(status=ToolCall.Status.AWAITING_CONFIRMATION)
    events_of(confirm(client, conversation, {str(waiting.pk): "approve"}))
    assert len(calls) == 6  # drei weitere Runden, dann wieder Rückfrage
    waiting = ToolCall.objects.get(status=ToolCall.Status.AWAITING_CONFIRMATION)
    events = events_of(confirm(client, conversation, {str(waiting.pk): "reject"}))
    assert calls[-1]["params"].get("tool_choice") == "none"
    assert events[-1][1]["status"] == Message.Status.COMPLETE


# --- Auswahl (API für die Oberfläche) --------------------------------------------------------


def test_set_state_validates_owner(adult, conversation, workspace):
    other = make_user("adult", "bert")
    foreign = RunnerWorkspace.objects.create(owner=other, name="fremd")
    with pytest.raises(ValueError):
        runner_chat.set_state(adult, conversation, workspace=foreign.pk)
    state = runner_chat.set_state(adult, conversation, workspace=workspace.pk, mode="ask")
    assert state["workspace_id"] == workspace.pk
    assert state["mode"] == "ask" and state["effective_mode"] == "ask"
    state = runner_chat.set_state(adult, conversation, mode="")
    assert state["mode_default"] and state["effective_mode"] == "auto"


def test_missing_image_digest_releases_lock(
    client, conversation, workspace, server, scripted, link, settings
):
    settings.RUNNER_WORK_IMAGE = "ghcr.io/test/work:latest"
    scripted([tool_round(call(tname(server, "bash"), {"command": "ls"}, "c1")), answer()])
    events_of(send(client, conversation, mcp_servers=[server.pk]))
    assert ToolCall.objects.get().result_text == runner_chat.MSG_NO_IMAGE
    assert RunnerWorkspace.objects.get(pk=workspace.pk).active_session_id is None
    assert RunnerSession.objects.get().status == RunnerSession.Status.FAILED
    assert not link.started


def test_runner_tools_stay_core_on_demand(client, conversation, server, ai_model, scripted, link):
    from multigpt.chat import tool_select

    ai_model.tools_on_demand = tool_select.ALWAYS
    ai_model.save()
    calls = scripted([answer()])
    events_of(send(client, conversation, mcp_servers=[server.pk]))
    offered = {t.name for t in calls[0]["tools"]}
    assert tool_select.FIND_TOOLS in offered
    assert tname(server, "bash") in offered and tname(server, "publish") in offered


# --- publish (Scratchpad des Runners) ---------------------------------------------------

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360606060000000050001a5f645400000000049454e44ae426082"
)


def test_publish_stores_attachment(client, conversation, server, scripted, link):
    from multigpt.chat.models import Attachment

    link.workspace.blobs["ergebnis.png"] = PNG
    link.workspace.files["seite.html"] = "<script>alert(1)</script>"
    publish = tname(server, "publish")
    scripted(
        [
            tool_round(
                call(publish, {"path": "/scratch/ergebnis.png"}, "c1"),
                call(publish, {"path": "seite.html", "name": "Vorschau"}, "c2"),
            ),
            answer(),
        ]
    )
    events = events_of(send(client, conversation, mcp_servers=[server.pk]))
    assert "confirmation_required" not in names(events)  # Modus auto
    image, page = Attachment.objects.order_by("pk")
    assert image.kind == Attachment.Kind.IMAGE and image.owner_id is None
    assert page.kind == Attachment.Kind.FILE
    assert page.mime_type == "application/octet-stream"  # HTML nie als Dokument
    assert page.original_name == "Vorschau"
    results = [d for n, d in events if n == "tool_result"]
    assert results[0]["attachment_ids"] == [image.pk]
    # Server gibt Übertragung und Grenze mit
    sent = [args for tool, args in link.workspace.calls if tool == "publish"][0]
    assert sent["transfer"] and sent["max_bytes"] == 20 * 1024 * 1024


def test_publish_asks_only_in_ask_mode():
    assert runner_tools.needs_confirmation("publish", runner_tools.MODE_ASK)
    assert not runner_tools.needs_confirmation("publish", runner_tools.MODE_AUTO_EDITS)
    assert not runner_tools.needs_confirmation("publish", runner_tools.MODE_AUTO_ALL)


def test_publish_size_limit(client, conversation, server, scripted, link, settings):
    settings.RUNNER_PUBLISH_MAX_MB = 1
    link.workspace.blobs["gross.bin"] = b"x" * (1024 * 1024 + 1)
    scripted([tool_round(call(tname(server, "publish"), {"path": "gross.bin"}, "c1")), answer()])
    events_of(send(client, conversation, mcp_servers=[server.pk]))
    assert ToolCall.objects.get().status == ToolCall.Status.ERROR


def test_publish_collects_file_chunks():
    import base64
    import hashlib

    from multigpt.runner import publish

    data = b"hallo welt" * 1000

    class Sub:
        def __init__(self, frames):
            self.frames = list(frames)

        def get(self, timeout=None):
            return self.frames.pop(0)

    frames = [
        {
            "type": "file_chunk",
            "seq": 0,
            "data": base64.b64encode(data[:4000]).decode(),
            "last": False,
        },
        {
            "type": "file_chunk",
            "seq": 1,
            "data": base64.b64encode(data[4000:]).decode(),
            "last": True,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "name": "a.txt",
        },
    ]
    assert publish.collect(Sub(frames), 10**6) == (data, "a.txt", "")
    frames[1]["sha256"] = "0" * 64
    with pytest.raises(publish.PublishError):
        publish.collect(Sub(frames), 10**6)
    with pytest.raises(publish.PublishError):
        publish.collect(Sub(frames), 100)  # zu groß


def test_session_start_carries_scratch(
    client, adult, conversation, workspace, server, scripted, link
):
    RunnerWorkspace.objects.filter(pk=workspace.pk).update(scratch_mode="local")
    adult.role.runner_scratch_mb = 300
    adult.role.save()
    scripted([tool_round(call(tname(server, "bash"), {"command": "ls"}, "c1")), answer()])
    events_of(send(client, conversation, mcp_servers=[server.pk]))
    frame = link.started[0]
    assert frame["scratch_mode"] == "local" and frame["scratch_mb"] == 300
    assert frame["workspace_uuid"] == str(RunnerWorkspace.objects.get(pk=workspace.pk).uuid)
    assert "/" not in frame["workspace_uuid"]  # nie ein Pfad


def test_set_scratch_mode_only_without_session(adult, conversation, workspace):
    state = runner_chat.set_state(adult, conversation, scratch_mode="local")
    assert state["workspace_scratch_mode"] == "local"
    assert {m["value"] for m in state["scratch_modes"]} == {"container", "local"}
    session = RunnerSession.objects.create(
        owner=adult, workspace=workspace, status=RunnerSession.Status.RUNNING
    )
    RunnerWorkspace.objects.filter(pk=workspace.pk).update(active_session=session)
    with pytest.raises(ValueError):
        runner_chat.set_state(adult, conversation, scratch_mode="container")


def test_running_session_is_not_reused_after_image_change(settings):
    """Befund 2026-10-14: Die laufende Sitzung blieb nach einem Wechsel des Arbeits-Images
    bestehen; die Werkzeuge liefen weiter im alten Container."""
    from multigpt.runner import chat as runner_chat

    class Session:
        limits_requested = {"image": "ghcr.io/x/work@sha256:" + "a" * 64}

    settings.RUNNER_WORK_IMAGE = "ghcr.io/x/work@sha256:" + "a" * 64
    assert runner_chat._same_image(Session())
    settings.RUNNER_WORK_IMAGE = "ghcr.io/x/work@sha256:" + "b" * 64
    assert not runner_chat._same_image(Session())
    Session.limits_requested = {}  # Sitzung aus der Zeit vor dieser Änderung
    assert not runner_chat._same_image(Session())
