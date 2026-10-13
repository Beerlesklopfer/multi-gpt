"""Modus „Paketquellen“ mit echtem Podman (Marker ``podman`` und ``network``, pkg-e2e).

Kette wie ``test_runner_parity_podman.py`` – Chat (Modell gefälscht) →
Werkzeugschleife → Runner-Dienst (uvicorn im Prozess) → **Rust-Agent** als
eigener Prozess → Podman –, hier nur für den Rust-Agent (der Python-Agent kennt
den Modus nicht). Geprüft wird:

1. ``apt_install(["jq"])`` installiert als root im Container, ``jq`` läuft danach
   als Nutzer 1000;
2. ``pip install`` und ``npm install`` über den Proxy (``six``, ``left-pad``)
   landen in ``/workspace`` und stehen in ``.gitignore``;
3. nicht freigegebene Hosts, IP-Literale, Ziele im Heimnetz und direkte
   Verbindungen ohne Proxy werden abgewiesen, DNS gibt es im Container nicht;
4. Nutzer 1000 kann apt/dpkg nicht selbst ausführen, ``sudo`` gibt es nicht und
   wirkt auch nach ``apt_install(["sudo"])`` nicht (keine setuid-Programme);
5. eine neue Sitzung desselben Arbeitsstands installiert die Pakete aus
   ``.mgpt/apt-packages`` automatisch nach;
6. ohne Rollenrecht bleibt das Netz aus (kein Proxy, kein ``apt_install``).

Die Tests brauchen Internet zu den Paketquellen (Marker ``network``) und werden
ohne Netz übersprungen. Ausführen::

    DB_TEST_NAME=test_multigpt_pkg_podman .venv/bin/pytest -m "podman and network" \\
        tests/test_runner_packages_podman.py

Nur eigene Testobjekte: Labels ``multigpt.test=1`` und ``multigpt.test.run=<Lauf>``
über ``MGPT_LABELS``; aufgeräumt werden nur Container und Netze mit Lauf-Label und
eigenem Namensmuster. Kein ``prune``, fremde Container bleiben unberührt.
"""

from __future__ import annotations

import json
import socket
import uuid

import pytest
from django.test import Client
from django.urls import reverse

from multigpt.chat import tooling
from multigpt.chat.mcp import bridge
from multigpt.chat.models import AIModel, Conversation, ToolCall
from multigpt.runner import chat as runner_chat
from multigpt.runner import control, tokens, workspaces
from multigpt.runner.mcp_server import ensure_mcp_server
from multigpt.runner.models import Runner, RunnerSession, RunnerWorkspace
from runner_agent import template
from tests.node_support import make_user
from tests.runner_parity_support import (
    AgentProcess,
    GatewayServer,
    agent_env,
    catalog_env,
    make_release_key,
    require_variant,
    wait_for,
)
from tests.test_runner_parity_podman import (
    _cleanup_run,
    _inspect,
    _model,
    _own_containers,
    _socket,
    _work_image,
)
from tests.test_tool_loop import ScriptedAdapter, answer, call, confirm, events_of, tool_round

pytestmark = [
    pytest.mark.podman,
    pytest.mark.network,
    pytest.mark.django_db(transaction=True, serialized_rollback=True),
]


def _online(host: str, port: int = 443) -> bool:
    try:
        with socket.create_connection((host, port), timeout=5):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module", autouse=True)
def _need_internet():
    missing = [h for h in ("deb.debian.org", "pypi.org", "registry.npmjs.org") if not _online(h)]
    if missing:
        pytest.skip(f"kein Internet zu den Paketquellen ({', '.join(missing)})")


@pytest.fixture
def stack(settings, tmp_path):
    """Runner-Dienst + Rust-Agent mit Podman für das Konto anna (Rolle mit Paketquellen)."""
    require_variant("rust")
    socket_path, image = _socket(), _work_image()
    if not socket_path or not image:
        pytest.skip("Podman-Socket oder Arbeits-Image fehlt (make runner-images)")
    settings.RUNNER_WORK_IMAGE = image
    settings.MEDIA_ROOT = tmp_path / "media"
    run_id = uuid.uuid4().hex[:12]
    server = GatewayServer()
    server.start()
    anna = make_user("anna")
    # Paketquellen an, allgemeines Netz aus: der Modus kommt nur aus dem neuen Recht.
    anna.role.runner_packages = True
    anna.role.runner_network = False
    anna.role.save()
    created = tokens.create_token(anna, "Paketquellen")
    private, key_id = make_release_key(settings, tmp_path)
    env = agent_env(
        tmp_path,
        server.url,
        created.secret,
        MGPT_ENGINE="podman",
        MGPT_IMAGES=image,
        MGPT_PODMAN_SOCKET=socket_path,
        MGPT_LABELS=f"multigpt.test=1 multigpt.test.run={run_id}",
        MGPT_HOSTNAME="mgpt-test-pkg",
        **catalog_env(tmp_path, private, key_id),
    )
    agent = AgentProcess("rust", env, tmp_path / "agent.log")
    try:
        wait_for(
            lambda: Runner.objects.filter(
                token=created.token, status=Runner.Status.ONLINE, catalog_state="valid"
            ).exists(),
            180,
            "Agent verbindet sich mit gültigem Katalog",
            agent,
        )
        yield {
            "user": anna,
            "server": ensure_mcp_server(anna),
            "agent": agent,
            "run_id": run_id,
        }
    finally:
        agent.stop(30)
        server.stop()
        bridge.shutdown()
        _cleanup_run(run_id)


class Chat:
    """Ein Werkzeugaufruf je Chat-Runde (Modell gefälscht), Rückfragen werden bestätigt."""

    def __init__(self, stack, monkeypatch, title: str, workspace):
        self.stack = stack
        self.monkeypatch = monkeypatch
        self.server = stack["server"]
        self.client = Client()
        self.client.force_login(stack["user"])
        self.conversation = Conversation.objects.create(user=stack["user"], title=title)
        runner_chat.set_state(stack["user"], self.conversation, workspace=workspace.pk, mode="auto")
        self.calls: list[dict] = []
        self.n = 0

    def _install(self, script):
        calls = self.calls
        self.monkeypatch.setattr(
            "multigpt.chat.providers.registry.get_adapter",
            lambda provider: ScriptedAdapter(provider, script, calls),
        )

    def tool(self, name: str, args: dict, timeout_note: str = "") -> ToolCall:
        self.n += 1
        call_id = f"c{self.n}"
        self.calls.clear()
        self._install(
            [
                tool_round(call(tooling.tool_name(self.server, name), args, call_id)),
                answer("Fertig."),
            ]
        )
        model = AIModel.objects.get(model_id="claude-e2e")
        response = self.client.post(
            reverse("chat:api_messages", args=[self.conversation.pk]),
            json.dumps(
                {"content": f"Schritt {self.n}", "model": model.pk, "mcp_servers": [self.server.pk]}
            ),
            content_type="application/json",
        )
        events = events_of(response)
        for _ in range(3):
            waiting = list(
                ToolCall.objects.filter(
                    message__conversation=self.conversation,
                    status=ToolCall.Status.AWAITING_CONFIRMATION,
                )
            )
            if not waiting:
                break
            events += events_of(
                confirm(self.client, self.conversation, {str(t.pk): "approve" for t in waiting})
            )
        tool_call = (
            ToolCall.objects.filter(message__conversation=self.conversation).order_by("-pk").first()
        )
        assert tool_call is not None, (events, self.stack["agent"].log())
        return tool_call

    def offered(self) -> str:
        """Werkzeugliste, die das Modell in der letzten Runde sah (als Text)."""
        return json.dumps([c.get("tools") for c in self.calls], default=str)


def _text(tool_call: ToolCall) -> str:
    return json.dumps(tool_call.result, ensure_ascii=False)


def _bash(chat: Chat, command: str) -> str:
    tc = chat.tool("bash", {"command": command})
    assert tc.status == "ok", (tc.status, tc.result, chat.stack["agent"].log())
    return _text(tc)


# Prüft Ziele über den Proxy (Umgebung HTTPS_PROXY/HTTP_PROXY) und direkt; eine Zeile je Ziel.
PROBE = r"""
import os, socket, urllib.request
def via_proxy(url):
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return f"OK {r.status}"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    except Exception as e:
        return f"ERR {type(e).__name__}"
def direct(host, port):
    try:
        socket.create_connection((host, port), timeout=5).close()
        return "OK connect"
    except Exception as e:
        return f"ERR {type(e).__name__}"
def dns(name):
    try:
        return "OK " + socket.gethostbyname(name)
    except Exception as e:
        return f"ERR {type(e).__name__}"
checks = {
    "allowed_https": via_proxy("https://pypi.org/simple/six/"),
    "allowed_http": via_proxy("http://deb.debian.org/debian/"),
    "host_https": via_proxy("https://example.com/"),
    "host_http": via_proxy("http://example.com/"),
    "sub_of_allowed": via_proxy("https://evil.pypi.org.example.com/"),
    "ip_public": via_proxy("http://1.1.1.1/"),
    "ip_public_https": via_proxy("https://1.1.1.1/"),
    "ip_home_192": via_proxy("http://192.168.1.1/"),
    "ip_home_10": via_proxy("http://10.0.0.1/"),
    "ip_loopback": via_proxy("http://127.0.0.2:3128/"),
    "ip_metadata": via_proxy("http://169.254.169.254/"),
    "ipv6_literal": via_proxy("http://[2606:4700:4700::1111]/"),
    "decimal_ip": via_proxy("http://3232235777/"),
    "allowed_wrong_port": via_proxy("https://pypi.org:8443/"),
    "direct_public": direct("1.1.1.1", 443),
    "direct_home": direct("192.168.1.1", 80),
    "dns": dns("pypi.org"),
}
for k, v in checks.items():
    print(f"{k}={v}")
"""


def _probe(chat: Chat) -> dict[str, str]:
    tc = chat.tool("bash", {"command": f"python3 - <<'EOF'\n{PROBE}\nEOF"})
    assert tc.status == "ok", (tc.result, chat.stack["agent"].log())
    text = _text(tc).replace("\\n", "\n")
    found = {}
    for line in text.splitlines():
        key, sep, value = line.strip(' "').partition("=")
        if sep and key in PROBE:
            found[key] = value.split('"')[0].strip()
    return found


def _session(user) -> RunnerSession:
    return RunnerSession.objects.filter(owner=user).order_by("-pk").first()


def test_packages_mode_end_to_end(stack, monkeypatch):
    anna, agent, run_id = stack["user"], stack["agent"], stack["run_id"]
    _model()
    workspace = workspaces.create_workspace(anna, "pakete")
    chat = Chat(stack, monkeypatch, "Pakete", workspace)

    # --- Sitzung im Modus Paketquellen -------------------------------------------------
    out = _bash(chat, "id -u; echo HP=$HTTPS_PROXY")
    assert "1000" in out
    session = _session(anna)
    assert session.status == RunnerSession.Status.RUNNING, (session.stop_reason, agent.log())
    assert session.limits_requested.get("packages") is True, session.limits_requested
    assert session.limits_requested.get("network") is False, session.limits_requested
    assert "apt_install" in chat.offered(), "apt_install fehlt in der Werkzeugliste"
    work = _own_containers(run_id, "work")
    assert work == [template.container_name(str(session.uuid))], work
    info = _inspect(work[0])
    host = info["HostConfig"]
    # Gelockerte Vorlage genau so weit wie vorgesehen (Python-Orakel)
    limits = session.limits_requested
    expected = template.Limits(
        cpus=float(limits["cpus"]),
        memory_mb=int(limits["memory_mb"]),
        pids=int(limits["pids"]),
        tmpfs_mb=int(limits["tmpfs_mb"]),
        network="packages",
    )
    session.refresh_from_db()
    assert template.check_inspect(info, expected, session.preview_port) == []
    assert host["ReadonlyRootfs"] is False
    caps = {c.upper().removeprefix("CAP_") for c in host.get("CapAdd") or []}
    assert caps <= set(template.CAPS_PACKAGES), caps
    assert "no-new-privileges" in (host.get("SecurityOpt") or [])
    nets = set(info["NetworkSettings"]["Networks"])
    assert nets == {template.network_name(str(session.uuid))}, nets
    assert info["Config"]["User"] == "1000:1000"
    # Proxy: Relay-Container am Sitzungsnetz und Ausgang, schreibgeschützt, ohne Capabilities
    proxies = _own_containers(run_id, "relay")
    assert proxies == [template.container_name(str(session.uuid), "relay")], proxies
    pinfo = _inspect(proxies[0])
    assert pinfo["HostConfig"]["ReadonlyRootfs"] is True
    assert not pinfo["HostConfig"].get("CapAdd")
    assert set(pinfo["NetworkSettings"]["Networks"]) == {
        template.network_name(str(session.uuid), "net"),
        template.network_name(str(session.uuid), "out"),
    }

    # (4) Nutzer 1000 hat keine Rechte für apt/dpkg, sudo gibt es nicht
    out = _bash(
        chat,
        "apt-get install -y --no-install-recommends jq >/dev/null 2>&1; echo APT=$?; "
        "dpkg --configure -a >/dev/null 2>&1; echo DPKG=$?; "
        "touch /var/lib/dpkg/x 2>/dev/null; echo TOUCH=$?; "
        "command -v sudo >/dev/null && echo SUDO=yes || echo SUDO=no; "
        "find / -xdev -perm /6000 -type f 2>/dev/null | head -5 | sed 's/^/SUID:/'; echo END",
    )
    assert "APT=0" not in out and "APT=" in out, out
    assert "DPKG=0" not in out, out
    assert "TOUCH=0" not in out, out
    assert "SUDO=no" in out, out
    assert "SUID:" not in out, out

    # (1) apt_install als root, jq danach als Nutzer 1000
    tc = chat.tool("apt_install", {"packages": ["jq"]})
    assert tc.status == "ok", (tc.result, agent.log())
    out = _bash(chat, "jq --version; id -u; echo '{\"a\":42}' | jq .a; cat .mgpt/apt-packages")
    assert "jq-" in out and "1000" in out and "42" in out, out
    assert "jq" in out.split("jq-", 1)[1], out  # .mgpt/apt-packages nennt jq
    # Ungültige Paketnamen lehnt schon der Server bzw. Agent ab
    bad = chat.tool("apt_install", {"packages": ["jq; touch /tmp/x"]})
    assert bad.status != "ok" or "error" in _text(bad).lower(), _text(bad)

    # (4b) auch ein installiertes sudo verschafft keine Rechte (no-new-privileges, kein setuid)
    tc = chat.tool("apt_install", {"packages": ["sudo"]})
    out = _bash(
        chat,
        "sudo -n true >/dev/null 2>&1; echo SUDORC=$?; "
        "find / -xdev -perm /6000 -type f 2>/dev/null | head -5 | sed 's/^/SUID:/'; "
        "grep NoNewPrivs /proc/self/status",
    )
    assert "SUDORC=0" not in out and "SUDORC=" in out, out
    assert "NoNewPrivs:\\t1" in out or "NoNewPrivs:\t1" in out, out
    assert "SUID:" not in out, (tc.status, out)

    # (2) pip und npm über den Proxy, Ergebnis in /workspace und in .gitignore
    out = _bash(
        chat,
        "python3 -m venv .venv >/dev/null && .venv/bin/pip install -q six 2>&1 | tail -3; "
        ".venv/bin/python -c 'import six, sys; print(\"SIX\", six.__version__, six.__file__)'",
    )
    assert "SIX " in out and "/workspace/.venv/" in out, out
    out = _bash(
        chat,
        "npm install --no-audit --no-fund left-pad 2>&1 | tail -3; "
        'node -e \'console.log("PAD[" + require("left-pad")("7", 3, "0") + "]")\'; '
        "ls -d /workspace/node_modules/left-pad",
    )
    assert "PAD[007]" in out and "/workspace/node_modules/left-pad" in out, out
    out = _bash(chat, "cat .gitignore; git status --porcelain --ignored | head -20")
    assert ".venv" in out and "node_modules" in out, out
    out = _bash(
        chat, "git ls-files | grep -E '^(\\.venv|node_modules)/' | wc -l | sed 's/^/TRACKED=/'"
    )
    assert "TRACKED=0" in out, out

    # (3) Nur freigegebene Hosts, keine IP-Literale, kein Heimnetz, kein Weg am Proxy vorbei
    found = _probe(chat)
    assert found.get("allowed_https", "").startswith("OK"), found
    assert found.get("allowed_http", "").startswith("OK"), found
    for key in (
        "host_https",
        "host_http",
        "sub_of_allowed",
        "ip_public",
        "ip_public_https",
        "ip_home_192",
        "ip_home_10",
        "ip_loopback",
        "ip_metadata",
        "ipv6_literal",
        "decimal_ip",
        "allowed_wrong_port",
        "direct_public",
        "direct_home",
        "dns",
    ):
        assert key in found and not found[key].startswith("OK"), (key, found)

    # Bundle mit .mgpt/apt-packages und .gitignore auf dem Server
    wait_for(
        lambda: RunnerWorkspace.objects.get(pk=workspace.pk).size > 0,
        60,
        "Bundle auf dem Server",
        agent,
    )

    # --- (5) Neue Sitzung desselben Arbeitsstands: Pakete werden nachinstalliert --------
    old = template.container_name(str(session.uuid))
    control.stop_session(session.uuid)
    wait_for(lambda: not _session(anna).is_active, 90, "Sitzung beendet", agent)
    wait_for(lambda: old not in _own_containers(run_id), 60, "Container entfernt", agent)
    wait_for(lambda: not _own_containers(run_id, "relay"), 60, "Proxy entfernt", agent)
    chat2 = Chat(stack, monkeypatch, "Pakete 2", workspace)
    out = _bash(
        chat2,
        "cat .mgpt/apt-packages; for i in $(seq 90); do command -v jq >/dev/null && break; "
        "sleep 2; done; jq --version; echo '[1,2]' | jq length",
    )
    session2 = _session(anna)
    assert session2.pk != session.pk and session2.status == RunnerSession.Status.RUNNING
    assert "jq-" in out and "2" in out.split("jq-", 1)[1], (out, agent.log())
    out = _bash(chat2, "ls .venv/bin/python node_modules 2>&1 | head -3; echo END")
    # .venv/node_modules sind ignoriert und kommen nicht über das Bundle zurück
    assert "No such file" in out or "cannot access" in out, out

    assert agent.returncode is None, agent.log()


def test_without_role_right_network_stays_off(stack, monkeypatch):
    anna, agent, run_id = stack["user"], stack["agent"], stack["run_id"]
    anna.role.runner_packages = False
    anna.role.runner_network = False
    anna.role.save()
    control.rights_changed(anna.pk)
    _model()
    workspace = workspaces.create_workspace(anna, "ohne-pakete")
    chat = Chat(stack, monkeypatch, "Ohne Pakete", workspace)
    out = _bash(chat, "env | grep -ci proxy; echo END")
    session = _session(anna)
    assert session.status == RunnerSession.Status.RUNNING, (session.stop_reason, agent.log())
    assert session.limits_requested.get("packages") is False, session.limits_requested
    assert session.network is False
    assert "apt_install" not in chat.offered()
    found = _probe(chat)
    assert found, found
    assert all(not v.startswith("OK") for v in found.values()), found
    work = _own_containers(run_id, "work")
    info = _inspect(work[0])
    assert info["HostConfig"]["ReadonlyRootfs"] is True
    assert not info["HostConfig"].get("CapAdd")
    assert not _own_containers(run_id, "relay"), "ohne Recht kein Proxy"
    # apt_install direkt aufgerufen (Modell kennt den Namen): abgewiesen, nichts installiert
    tc = chat.tool("apt_install", {"packages": ["jq"]})
    assert tc.status != "ok" or "error" in _text(tc).lower(), _text(tc)
    out = _bash(chat, "command -v jq || echo NOJQ")
    assert "NOJQ" in out, out
