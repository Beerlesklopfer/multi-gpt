"""Paketquellen im Runner – Server-Seite (Nutzer, 2026-10-13).

Rollenrecht ``runner_packages`` und Grenze ``runner_rootfs_mb``, Netzmodus
``packages`` im Protokoll (``package_hosts``/``rootfs_mb``), Abgleich der
Ist-Grenzen, Messung der Container-Schicht im Heartbeat, Werkzeug
``apt_install`` (Prüfung, Einstufung, Angebot nur mit Paketquellen) und der
Schalter je Chat. Container laufen hier nicht (``FakeLink`` wie in
test_runner_tools_chat.py).
"""

# ruff: noqa: F811

import json
from pathlib import Path

import pytest
from django.utils import timezone

from multigpt.accounts.models import Role
from multigpt.chat.models import ToolCall
from multigpt.runner import chat as runner_chat
from multigpt.runner import limits, protocol, store
from multigpt.runner import tools as runner_tools
from multigpt.runner.models import (
    ChatWorkspace,
    Runner,
    RunnerAudit,
    RunnerHost,
    RunnerSession,
)
from tests.runner_fakes import ALL_SERVICES, PACKAGE_SERVICES
from tests.test_runner_tools_chat import (  # noqa: F401 (Fixtures)
    IMAGE,
    adult,
    ai_model,
    conversation,
    link,
    runner,
    scripted,
    send,
    server,
    tname,
    tool_calls,
    workspace,
)
from tests.test_tool_loop import answer, call, confirm, events_of, make_user, names, tool_round

pytestmark = pytest.mark.django_db

HOSTS = ["deb.debian.org", "pypi.org"]


def start_frame(**changes) -> dict:
    frame = {
        "type": "session_start",
        "session": "s1",
        "workspace": "1",
        "image": IMAGE,
        "cpus": 1,
        "memory_mb": 1024,
        "pids": 256,
        "tmpfs_mb": 512,
        "network": "packages",
        "package_hosts": list(HOSTS),
        "rootfs_mb": 4096,
        "command_seconds": 600,
        "output_bytes": 65536,
        "idle_minutes": 15,
    }
    frame.update(changes)
    return {k: v for k, v in frame.items() if v is not None}


# --- Protokoll ------------------------------------------------------------------------------


def test_protocol_session_start_packages():
    frame = protocol.validate(start_frame(), protocol.SERVER)
    assert frame["package_hosts"] == HOSTS and frame["rootfs_mb"] == 4096


@pytest.mark.parametrize(
    "changes",
    [
        {"package_hosts": None},  # Pflicht bei packages
        {"package_hosts": []},
        {"rootfs_mb": None},
        {"rootfs_mb": 32},  # unter MIN_ROOTFS_MB
        {"network": "none"},  # package_hosts/rootfs_mb nur bei packages
        {"network": "none", "package_hosts": None},  # rootfs_mb allein
        {"package_hosts": ["192.168.1.10"]},  # keine IP
        {"package_hosts": ["deb.debian.org:80"]},  # kein Port
        {"package_hosts": ["*.debian.org"]},  # kein Joker
        {"package_hosts": ["Deb.Debian.org"]},  # nur klein
        {"package_hosts": ["localhost"]},
        {"package_hosts": [f"h{i}.example.org" for i in range(51)]},
        {"allow": [{"host": "nas.local", "port": 23}]},  # Relay schließt sich aus
    ],
)
def test_protocol_session_start_packages_rejected(changes):
    with pytest.raises(protocol.ProtocolError):
        protocol.validate(start_frame(**changes), protocol.SERVER)


def test_protocol_limits_and_heartbeat_fields():
    state = protocol.validate(
        {
            "type": "session_state",
            "session": "s1",
            "state": "running",
            "limits": {
                "cpus": 1,
                "memory_mb": 1024,
                "pids": 256,
                "tmpfs_mb": 512,
                "network": "packages",
                "rootfs_mb": 4096,
            },
        },
        protocol.AGENT,
    )
    assert state["limits"]["rootfs_mb"] == 4096
    beat = protocol.validate(
        {
            "type": "heartbeat",
            "sessions": [{"session": "s1", "state": "running", "rootfs_used_mb": 12}],
        },
        protocol.AGENT,
    )
    assert beat["sessions"][0]["rootfs_used_mb"] == 12


def test_protocol_copies_identical():
    root = Path(__file__).resolve().parent.parent
    server = (root / "multigpt" / "runner" / "protocol.py").read_bytes()
    assert server == (root / "runner_agent" / "protocol.py").read_bytes()


# --- Rolle und Grenzen ----------------------------------------------------------------------


def test_start_values_of_the_right():
    values = dict(Role.objects.values_list("key", "runner_packages"))
    assert values["admin"] is True and values["adult"] is True
    assert not any(v for k, v in values.items() if k not in ("admin", "adult"))
    assert set(Role.objects.values_list("runner_rootfs_mb", flat=True)) == {4096}


def test_session_limits_packages():
    anna = make_user("adult", "anna")
    result = limits.session_limits(anna)
    assert result["packages"] is True and result["network"] is False
    assert result["rootfs_mb"] == 4096
    # über API-Keys mit Rollenrecht ebenso (kein freies Netz)
    assert limits.session_limits(anna, via_api_key=True)["packages"] is True
    # freies Netz geht vor
    Role.objects.filter(key="adult").update(runner_network=True)
    anna.refresh_from_db()
    result = limits.session_limits(anna)
    assert result["packages"] is False and "rootfs_mb" not in result
    # ohne Recht
    Role.objects.filter(key="adult").update(runner_network=False, runner_packages=False)
    anna.refresh_from_db()
    assert limits.session_limits(anna)["packages"] is False
    assert limits.session_limits(make_user("teen", "tom"))["packages"] is False


def test_package_hosts_setting_is_filtered(settings):
    settings.RUNNER_PACKAGE_HOSTS = ["deb.debian.org", "10.0.0.1", "PyPI.org", "x:80", "pypi.org"]
    assert limits.package_hosts() == ["deb.debian.org", "pypi.org"]


def test_default_package_hosts_are_valid():
    from django.conf import settings

    hosts = limits.package_hosts()
    assert hosts == list(settings.RUNNER_PACKAGE_HOSTS)
    assert "deb.debian.org" in hosts and "static.crates.io" in hosts


def test_limits_mismatch_packages():
    base = {"cpus": 1, "memory_mb": 1024, "pids": 256, "tmpfs_mb": 512}
    want = {**base, "network": False, "packages": True, "rootfs_mb": 4096}
    ok = {**base, "network": "packages", "rootfs_mb": 4096}
    assert store._limits_mismatch(want, ok) == []
    assert store._limits_mismatch(want, {**ok, "rootfs_mb": 2048}) == []  # strenger
    assert store._limits_mismatch(want, {**ok, "rootfs_mb": 8192}) == ["rootfs_mb"]
    assert store._limits_mismatch(want, {**base, "network": "packages"}) == ["rootfs_mb"]
    assert store._limits_mismatch(want, {**ok, "network": "default"}) == ["network"]
    # ohne Paketquellen: packages ist eine Abweichung, allowlist nicht
    plain = {**base, "network": False, "packages": False}
    assert store._limits_mismatch(plain, ok) == ["packages"]
    assert store._limits_mismatch(plain, {**base, "network": "allowlist"}) == []
    # alte Soll-Werte ohne „packages“ (vor der Migration gestartet)
    assert store._limits_mismatch({**base, "network": False}, {**base, "network": "none"}) == []


# --- Heartbeat: Container-Schicht -----------------------------------------------------------


def test_heartbeat_stops_session_over_rootfs_limit(adult, runner, workspace, monkeypatch):
    monkeypatch.setattr(store, "fresh", lambda: None)  # Testtransaktion offen lassen
    session = RunnerSession.objects.create(
        owner=adult,
        workspace=workspace,
        runner=runner,
        status=RunnerSession.Status.RUNNING,
        limits_requested={"network": False, "packages": True, "rootfs_mb": 100},
    )
    item = {"session": str(session.uuid), "state": "running", "rootfs_used_mb": 80}
    beat = store.heartbeat(runner.pk, runner.token_id, [item])
    assert beat.code is None and beat.frames == []
    session.refresh_from_db()
    assert session.limits_actual["rootfs_used_mb"] == 80
    beat = store.heartbeat(runner.pk, runner.token_id, [{**item, "rootfs_used_mb": 101}])
    assert beat.frames == [
        {"type": "session_stop", "session": str(session.uuid), "reason": "mismatch"}
    ]
    session.refresh_from_db()
    assert session.status == RunnerSession.Status.STOPPING
    assert "Container-Schicht" in session.stop_reason
    assert RunnerAudit.objects.filter(kind=RunnerAudit.Kind.LIMITS).exists()


# --- Werkzeug apt_install -------------------------------------------------------------------


def test_clean_packages():
    clean = runner_tools.clean_arguments
    assert clean("apt_install", {"packages": ["ffmpeg", "g++", "libc6-dev", "ffmpeg"]}) == {
        "packages": ["ffmpeg", "g++", "libc6-dev"]
    }
    for bad in (
        [],
        "ffmpeg",
        ["-y"],
        ["--allow-unauthenticated"],
        ["Ffmpeg"],
        ["ffmpeg=1.0"],
        ["../x"],
        ["a b"],
        ["x" * 64],
        [1],
        [f"p{i}" for i in range(21)],
    ):
        with pytest.raises(runner_tools.ArgumentError):
            clean("apt_install", {"packages": bad})
    assert clean("apt_install", {"packages": ["x" * 63]})


def test_apt_install_confirmation_like_commands():
    need = runner_tools.needs_confirmation
    assert need("apt_install", runner_tools.MODE_ASK) is True
    assert need("apt_install", runner_tools.MODE_AUTO_EDITS) is True
    assert need("apt_install", runner_tools.MODE_AUTO_ALL) is False
    assert runner_tools.get("apt_install").kind == runner_tools.COMMAND
    assert "apt_install" in runner_tools.CHANGING_TOOLS


def test_specs_offer_apt_install_only_with_packages():
    hosts = {"telnet": ["altgeraet"]}

    def offered(**kw):
        return {s.name for s in runner_tools.specs(remote=True, hosts=hosts, **kw)}

    assert "apt_install" not in offered()
    assert "telnet" in offered()
    with_packages = offered(packages=True)
    assert "apt_install" in with_packages and "telnet" not in with_packages


def test_catalog_json_matches_server_catalog():
    root = Path(__file__).resolve().parent.parent
    data = json.loads((root / "runner-rs" / "catalog.json").read_text())
    entry = next((s for s in data["services"] if s["name"] == "apt_install"), None)
    if entry is None:
        pytest.skip("apt_install noch nicht in runner-rs/catalog.json (pkg-rust)")
    tool = runner_tools.get("apt_install")
    assert entry["kind"] == tool.kind
    assert entry["input_schema"] == tool.parameters


def telnet_host(user):
    host = RunnerHost.objects.create(
        name="altgeraet", address="192.0.2.20", port=23, protocol=RunnerHost.Protocol.TELNET
    )
    host.allowed_users.add(user)
    return host


# --- Chat -----------------------------------------------------------------------------------


@pytest.fixture
def pkg_runner(runner):
    Runner.objects.filter(pk=runner.pk).update(services=PACKAGE_SERVICES)
    runner.refresh_from_db()
    return runner


def test_session_start_with_packages(client, conversation, server, pkg_runner, scripted, link):
    calls = scripted(
        [tool_round(call(tname(server, "apt_install"), {"packages": ["jq"]}, "c1")), answer()]
    )
    events = events_of(send(client, conversation, mcp_servers=[server.pk]))
    assert tname(server, "apt_install") in {t.name for t in calls[0]["tools"]}
    # Vorgabe ohne Netz: alles automatisch – apt_install ohne Rückfrage
    assert "confirmation_required" not in names(events)
    frame = link.started[0]
    protocol.validate(frame, protocol.SERVER)
    assert frame["network"] == "packages" and frame["rootfs_mb"] == 4096
    assert frame["package_hosts"] == limits.package_hosts()
    assert "allow" not in frame
    assert ("apt_install", {"packages": ["jq"]}) in link.workspace.calls
    assert tool_calls() == [("apt_install", "ok")]
    session = RunnerSession.objects.get()
    assert session.packages and session.limits_requested["rootfs_mb"] == 4096
    state = runner_chat.get_state(conversation.user, conversation)
    assert state["packages"] and state["packages_runner"] and state["session"]["packages"]


def test_apt_install_asks_in_edits_mode(client, conversation, server, pkg_runner, scripted, link):
    ChatWorkspace.objects.update(mode="edits")
    scripted([tool_round(call(tname(server, "apt_install"), {"packages": ["jq"]}, "c1")), answer()])
    events_of(send(client, conversation, mcp_servers=[server.pk]))
    waiting = ToolCall.objects.get(status=ToolCall.Status.AWAITING_CONFIRMATION)
    assert waiting.tool == "apt_install"
    events_of(confirm(client, conversation, {str(waiting.pk): "approve"}))
    assert ("apt_install", {"packages": ["jq"]}) in link.workspace.calls


def test_runner_without_apt_install_gets_no_packages(
    client, conversation, server, runner, scripted, link
):
    assert "apt_install" not in ALL_SERVICES
    calls = scripted(
        [tool_round(call(tname(server, "apt_install"), {"packages": ["jq"]}, "c1")), answer()]
    )
    events_of(send(client, conversation, mcp_servers=[server.pk]))
    assert tname(server, "apt_install") not in {t.name for t in calls[0]["tools"]}
    assert [status for _, status in tool_calls()] == ["error"]
    assert not link.started  # abgewiesen vor dem Sitzungsstart


def test_chat_switch_turns_packages_off(
    client, adult, conversation, server, pkg_runner, scripted, link
):
    telnet_host(adult)
    state = runner_chat.set_state(adult, conversation, packages=False)
    assert state["packages_selected"] is False
    calls = scripted([tool_round(call(tname(server, "bash"), {"command": "ls"}, "c1")), answer()])
    events_of(send(client, conversation, mcp_servers=[server.pk]))
    offered = {t.name for t in calls[0]["tools"]}
    assert tname(server, "apt_install") not in offered
    assert tname(server, "telnet") in offered  # ohne Paketquellen wieder Fernzugriff
    frame = link.started[0]
    assert frame["network"] == "allowlist" and "package_hosts" not in frame
    assert not RunnerSession.objects.get().packages
    # Umschalten nur ohne laufende Sitzung
    with pytest.raises(ValueError):
        runner_chat.set_state(adult, conversation, packages=True)
    with pytest.raises(ValueError):
        runner_chat.set_state(adult, conversation, packages="ja")


def test_packages_hide_remote_tools(
    client, adult, conversation, server, pkg_runner, scripted, link
):
    telnet_host(adult)
    calls = scripted(
        [
            tool_round(call(tname(server, "telnet"), {"host": "altgeraet", "input": "ls"}, "c1")),
            answer(),
        ]
    )
    events_of(send(client, conversation, mcp_servers=[server.pk]))
    assert tname(server, "telnet") not in {t.name for t in calls[0]["tools"]}
    assert [status for _, status in tool_calls()] == ["error"]
    assert not any(name == "telnet" for name, _ in link.workspace.calls)


def test_chat_api_switch(client, adult, conversation):
    url = f"/runner/api/chat/{conversation.pk}/"
    response = client.post(url, {"packages": False}, content_type="application/json")
    assert response.status_code == 200, response.content
    assert response.json()["packages_selected"] is False
    assert ChatWorkspace.objects.get(conversation=conversation).packages is False


def test_session_api_and_page_show_packages(client, adult, workspace, runner):
    session = RunnerSession.objects.create(
        owner=adult,
        workspace=workspace,
        runner=runner,
        status=RunnerSession.Status.RUNNING,
        started=timezone.now(),
        limits_requested={"network": False, "packages": True, "rootfs_mb": 4096},
    )
    data = client.get(f"/runner/api/sitzungen/{session.uuid}/").json()
    assert data["packages"] is True
    page = client.get(f"/runner/sitzung/{session.uuid}/").content.decode()
    assert "nur Paketquellen" in page


def test_python_agent_rejects_packages():
    import asyncio

    from runner_agent import session as agent_session

    manager = agent_session.SessionManager.__new__(agent_session.SessionManager)
    with pytest.raises(agent_session.SessionError) as err:
        asyncio.run(manager._start(protocol.validate(start_frame(), protocol.SERVER)))
    assert "Paketquellen" in str(err.value)
