"""Gleichwertigkeit Python-/Rust-Agent mit echtem Podman (Marker ``podman``, rs-e2e).

Dieselbe Kette wie ``test_runner_e2e_podman.py`` – Chat-Stream (Modell
gefälscht) → Werkzeugschleife → Zustellung → Runner-Dienst (uvicorn im
Prozess) → Agent als eigener Prozess mit Podman → Arbeitscontainer –, aber für
beide Agenten (``agent_variant``). Der Dienstkatalog ist mit einem
Wegwerf-Release-Schlüssel signiert (wie in der CI), sonst bekäme der Runner
keine Werkzeuge.

Zusätzlich die Sicherheitsprüfungen: Ist-Werte des echten Containers gegen die
feste Vorlage (``check_inspect`` der Python-Referenz als Orakel), keine
Geheimnisse des Rechners in der Umgebung, und der Zugang eines Telnet-Hosts ist
zu keinem Zeitpunkt im Arbeitscontainer lesbar (weder in Dateien noch in /proc).

Nur eigene Testobjekte: Der Agent setzt per ``MGPT_LABELS`` die Labels
``multigpt.test=1`` und ``multigpt.test.run=<Lauf>``; aufgeräumt werden nur
Container/Netze mit diesem Lauf-Label und eigenem Namensmuster. Kein ``prune``,
fremde Container (z. B. n8n) bleiben unberührt.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import threading
import time
import uuid

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from multigpt.billing import runtime
from multigpt.billing.models import AccountBudget, UsageEntry
from multigpt.chat import tooling
from multigpt.chat.mcp import bridge
from multigpt.chat.models import AIModel, Conversation, Provider, ToolCall
from multigpt.chat.providers import registry
from multigpt.runner import chat as runner_chat
from multigpt.runner import tokens, workspaces
from multigpt.runner.mcp_server import ensure_mcp_server
from multigpt.runner.models import Runner, RunnerHost, RunnerSession, RunnerWorkspace
from runner_agent import template
from tests.node_support import make_user
from tests.runner_parity_support import (
    VARIANTS,
    AgentProcess,
    GatewayServer,
    agent_env,
    catalog_env,
    make_release_key,
    require_variant,
    wait_for,
)
from tests.test_tool_loop import ScriptedAdapter, answer, call, confirm, events_of, tool_round

pytestmark = [
    pytest.mark.podman,
    pytest.mark.django_db(transaction=True, serialized_rollback=True),
]


def _socket() -> str | None:
    path = os.path.join(os.environ.get("XDG_RUNTIME_DIR", ""), "podman", "podman.sock")
    return path if os.path.exists(path) else None


def _work_image() -> str | None:
    wanted = os.environ.get("MGPT_TEST_WORK_IMAGE", "localhost/mgpt-test-work:dev")
    if "@sha256:" in wanted:
        return wanted
    if not shutil.which("podman"):
        return None
    proc = subprocess.run(
        ["podman", "image", "inspect", wanted, "--format", "{{json .RepoDigests}}"],
        capture_output=True,
        text=True,
    )
    digests = json.loads(proc.stdout or "[]") if proc.returncode == 0 else []
    return digests[0] if digests else None


def _podman(*args: str) -> str:
    return subprocess.run(["podman", *args], capture_output=True, text=True).stdout


def _own_containers(run_id: str, role: str | None = None) -> list[str]:
    filters = ["--filter", f"label=multigpt.test.run={run_id}", "--filter", "label=multigpt.test=1"]
    if role:
        filters += ["--filter", f"label=multigpt.role={role}"]
    names = _podman("ps", "-a", *filters, "--format", "{{.Names}}").split()
    return [n for n in names if template.NAME_RE.match(n)]


def _cleanup_run(run_id: str) -> None:
    """Nur Container und Netze dieses Laufs (Lauf-Label + eigenes Namensmuster)."""
    if not shutil.which("podman"):
        return
    for name in _own_containers(run_id):
        subprocess.run(["podman", "rm", "-f", "-t", "0", name], capture_output=True)
    nets = _podman(
        "network", "ls", "--filter", f"label=multigpt.test.run={run_id}", "--format", "{{.Name}}"
    ).split()
    for net in nets:
        if template.NETWORK_RE.match(net):
            subprocess.run(["podman", "network", "rm", net], capture_output=True)


@pytest.fixture(params=VARIANTS)
def agent_variant(request):
    require_variant(request.param)
    return request.param


@pytest.fixture
def stack(agent_variant, settings, tmp_path):
    """Runner-Dienst + Agent-Prozess (Python oder Rust) mit Podman für das Konto anna."""
    socket_path, image = _socket(), _work_image()
    if not socket_path or not image:
        pytest.skip("Podman-Socket oder Arbeits-Image fehlt (make runner-images)")
    settings.RUNNER_WORK_IMAGE = image
    settings.MEDIA_ROOT = tmp_path / "media"
    run_id = uuid.uuid4().hex[:12]
    server = GatewayServer()
    server.start()
    anna = make_user("anna")
    created = tokens.create_token(anna, "Parität")
    private, key_id = make_release_key(settings, tmp_path)
    env = agent_env(
        tmp_path,
        server.url,
        created.secret,
        MGPT_ENGINE="podman",
        MGPT_IMAGES=image,
        MGPT_PODMAN_SOCKET=socket_path,
        MGPT_LABELS=f"multigpt.test=1 multigpt.test.run={run_id}",
        MGPT_HOSTNAME=f"mgpt-test-{agent_variant}",
        **catalog_env(tmp_path, private, key_id),
    )
    agent = AgentProcess(agent_variant, env, tmp_path / "agent.log")
    try:
        wait_for(
            lambda: Runner.objects.filter(
                token=created.token, status=Runner.Status.ONLINE, catalog_state="valid"
            ).exists(),
            180,  # Podman-API unter Last träge; der Agent versucht es mit Backoff erneut
            "Agent verbindet sich mit gültigem Katalog",
            agent,
        )
        yield {
            "user": anna,
            "server": ensure_mcp_server(anna),
            "token": created.token,
            "agent": agent,
            "run_id": run_id,
        }
    finally:
        agent.stop(30)
        server.stop()
        bridge.shutdown()
        _cleanup_run(run_id)


@pytest.fixture
def scripted(monkeypatch):
    def install(script):
        calls = []
        monkeypatch.setattr(
            registry, "get_adapter", lambda provider: ScriptedAdapter(provider, script, calls)
        )
        return calls

    return install


def _model():
    provider = Provider.objects.create(name="Cloud", kind=Provider.Kind.ANTHROPIC)
    return AIModel.objects.create(
        provider=provider,
        model_id="claude-e2e",
        display_name="Claude",
        supports_tools=True,
        mcp_access=AIModel.McpAccess.ALL,
    )


def _send(client, conversation, model, server, content):
    return client.post(
        reverse("chat:api_messages", args=[conversation.pk]),
        json.dumps({"content": content, "model": model.pk, "mcp_servers": [server.pk]}),
        content_type="application/json",
    )


def _inspect(name: str) -> dict:
    return json.loads(_podman("inspect", "--type", "container", name))[0]


def test_acceptance_chain(stack, scripted):
    anna, server, agent = stack["user"], stack["server"], stack["agent"]
    client = Client()
    client.force_login(anna)
    model = _model()
    workspace = workspaces.create_workspace(anna, "paritaet")
    conversation = Conversation.objects.create(user=anna, title="Parität")
    runner_chat.set_state(anna, conversation, workspace=workspace.pk, mode="auto")

    def t(tool):
        return tooling.tool_name(server, tool)

    scripted(
        [
            tool_round(
                call(t("write"), {"path": "main.py", "content": "print(6*7)\n"}, "c1"),
                call(t("write"), {"path": "site/index.html", "content": "<h1>Hi</h1>"}, "c2"),
            ),
            tool_round(call(t("bash"), {"command": "python3 main.py"}, "c3")),
            tool_round(
                call(
                    t("bash"),
                    {"command": "git clone https://github.com/git/git.git /tmp/x 2>&1; echo RC=$?"},
                    "c4",
                )
            ),
            tool_round(call(t("serve"), {"path": "site"}, "c5")),
            tool_round(call(t("bash"), {"command": "env; id -u; ls -a ~ /run 2>&1"}, "c6")),
            answer("Fertig."),
        ]
    )
    events = events_of(_send(client, conversation, model, server, "Bitte bauen."))
    names = [name for name, _ in events]
    assert "runner_session" in names, (names, agent.log())
    calls = list(ToolCall.objects.order_by("pk"))
    assert [tc.status for tc in calls] == ["ok"] * 6, [
        (tc.tool, tc.status, tc.result) for tc in calls
    ]
    texts = [json.dumps(tc.result, ensure_ascii=False) for tc in calls]
    assert "42" in texts[2]
    # Netz aus: git clone scheitert (kein DNS, keine Route)
    assert "RC=0" not in texts[3] and "RC=" in texts[3]
    # Keine Geheimnisse des Rechners: feste Umgebung, Nutzer 1000, kein Socket, kein Token
    env_text = texts[5]
    for leak in ("XDG_RUNTIME_DIR", "MGPT_", "podman.sock", "SSH_AUTH_SOCK", "DBUS_"):
        assert leak not in env_text, leak

    session = RunnerSession.objects.get(owner=anna, status=RunnerSession.Status.RUNNING)
    assert session.network is False
    # Ist-Werte des echten Containers gegen die feste Vorlage (Python-Referenz als Orakel)
    work = _own_containers(stack["run_id"], "work")
    assert work == [template.container_name(str(session.uuid))], work
    info = _inspect(work[0])
    limits = session.limits_requested
    expected = template.Limits(
        cpus=float(limits["cpus"]),
        memory_mb=int(limits["memory_mb"]),
        pids=int(limits["pids"]),
        tmpfs_mb=int(limits["tmpfs_mb"]),
        network="none",
    )
    session.refresh_from_db()
    port = session.preview_port
    problems = template.check_inspect(info, expected, port)
    assert problems == [], problems
    assert info["Config"]["Labels"]["multigpt.test.run"] == stack["run_id"]
    assert info["HostConfig"]["ReadonlyRootfs"] is True

    # Arbeitsstand: Commit und Bundle auf dem Server
    wait_for(
        lambda: RunnerWorkspace.objects.get(pk=workspace.pk).size > 0,
        30,
        "Bundle auf dem Server",
        agent,
    )
    stored = RunnerWorkspace.objects.get(pk=workspace.pk)
    assert len(stored.head_commit) >= 40
    log = [c.subject for c in workspaces.history(stored)]
    assert any("main.py" in subject for subject in log), log

    # Vorschau über MultiGPT: nur mit Token, strenge CSP, keine Verzeichnisliste
    wait_for(
        lambda: RunnerSession.objects.get(pk=session.pk).preview_port is not None,
        20,
        "Vorschau meldet sich",
        agent,
    )
    session.refresh_from_db()
    anonymous = Client()
    base = f"/runner/vorschau/{session.uuid}/{session.preview_token}/"
    page = anonymous.get(base)
    assert page.status_code == 200, page.content
    assert page.content == b"<h1>Hi</h1>"
    assert page["Content-Security-Policy"].startswith("sandbox allow-scripts;")
    assert anonymous.get(f"/runner/vorschau/{session.uuid}/falsch/").status_code == 404
    assert anonymous.get(base + "../main.py").status_code == 404

    # Terminal über MultiGPT
    stream = client.get(
        reverse("runner:pty_stream", args=[session.uuid]), {"cols": "80", "rows": "24"}
    )
    assert stream.status_code == 200
    chunks = iter(stream.streaming_content)
    first = next(chunks).decode()
    pty = json.loads(first.split("data: ", 1)[1])["pty"]
    raw = first
    for chunk in chunks:
        raw += chunk.decode()
        if "event: data" in raw or "event: failure" in raw:
            break
    sent = client.post(
        reverse("runner:pty_input", args=[session.uuid, pty]),
        {"data": "echo MGPT-$((40+2))\r"},
        content_type="application/json",
    )
    assert sent.status_code == 200
    output = b""
    deadline = time.monotonic() + 30
    for chunk in chunks:
        text = chunk.decode()
        raw += text
        for block in text.split("\n\n"):
            if block.startswith("event: data"):
                output += base64.b64decode(json.loads(block.split("data: ", 1)[1])["data"])
        if b"MGPT-42" in output or time.monotonic() > deadline:
            break
    stream.close()
    assert b"MGPT-42" in output, raw[-2000:]

    # Laufzeit: Heartbeats buchen Minuten; bei 100 % Stopp, Arbeitsstand bleibt
    wait_for(
        lambda: RunnerSession.objects.get(pk=session.pk).counted_until is not None,
        45,
        "Heartbeat zählt Laufzeit",
        agent,
    )
    AccountBudget.objects.update_or_create(
        account=runtime.runtime_account(), user=anna, defaults={"monthly_minutes": 0}
    )
    assert runtime.enforce(anna)
    wait_for(
        lambda: not RunnerSession.objects.get(pk=session.pk).is_active,
        90,
        "Stopp wegen Kontingent",
        agent,
    )
    session.refresh_from_db()
    assert session.ended is not None
    assert UsageEntry.objects.filter(kind="runtime", user=anna).exists()
    assert RunnerWorkspace.objects.filter(pk=workspace.pk, size__gt=0).exists()
    assert not runtime.runtime_allows(anna)
    # Der Arbeitscontainer ist weg, der Agent läuft weiter
    wait_for(lambda: not _own_containers(stack["run_id"], "work"), 30, "Container entfernt")
    assert agent.returncode is None, agent.log()
    assert stack["token"].owner_id == anna.pk


# Sucht den Zugang in Dateien und Prozessen; der Suchbegriff steht nur rückwärts im Aufruf.
def _probe(secret: str) -> list[str]:
    return [
        "python3",
        "-c",
        "import os\n"
        f"s={secret[::-1]!r}[::-1].encode()\n"
        "hits=0\n"
        "for top in ('/tmp','/workspace','/scratch'):\n"
        "    for d,_,fs in os.walk(top):\n"
        "        for f in fs:\n"
        "            try: hits+=s in open(os.path.join(d,f),'rb').read()\n"
        "            except OSError: pass\n"
        "for p in os.listdir('/proc'):\n"
        "    for n in ('cmdline','environ'):\n"
        "        try: hits+=p.isdigit() and s in open(f'/proc/{p}/{n}','rb').read()\n"
        "        except OSError: pass\n"
        "print(hits)",
    ]


def test_telnet_credentials_never_in_work_container(stack, scripted, monkeypatch):
    anna, server, agent = stack["user"], stack["server"], stack["agent"]
    secret = "Geheim-Zugang-" + stack["run_id"][:8]
    anna.role.can_use_runner_hosts = True
    anna.role.save()
    host = RunnerHost.objects.create(
        name="altgeraet",
        address="192.0.2.10",  # TEST-NET: nie erreichbar, der Aufruf hängt bis zum Zeitlimit
        port=23,
        protocol=RunnerHost.Protocol.TELNET,
        username="admin",
        auth_method=RunnerHost.AuthMethod.PASSWORD,
        secret=secret,
        host_key_confirmed_at=timezone.now(),
    )
    host.allowed_users.add(anna)
    client = Client()
    client.force_login(anna)
    model = _model()
    workspace = workspaces.create_workspace(anna, "telnet")
    conversation = Conversation.objects.create(user=anna, title="Telnet")
    runner_chat.set_state(anna, conversation, workspace=workspace.pk, mode="auto")

    def t(tool):
        return tooling.tool_name(server, tool)

    scripted(
        [
            tool_round(call(t("bash"), {"command": "echo bereit"}, "c1")),
            tool_round(call(t("telnet"), {"host": "altgeraet", "input": "uptime"}, "c2")),
            answer("Fertig."),
        ]
    )
    events = events_of(_send(client, conversation, model, server, "Bitte prüfen."))
    names = [name for name, _ in events]
    assert "confirmation_required" in names, (names, agent.log())  # telnet fragt immer
    session = RunnerSession.objects.filter(owner=anna).order_by("-pk").first()
    assert session is not None and session.status == RunnerSession.Status.RUNNING, (
        session and (session.status, session.stop_reason),
        [(tc.tool, tc.status, tc.result) for tc in ToolCall.objects.order_by("pk")],
        agent.log(),
    )
    work = template.container_name(str(session.uuid))
    waiting = ToolCall.objects.get(status=ToolCall.Status.AWAITING_CONFIRMATION)

    result = {}

    def approve():
        result["events"] = events_of(confirm(client, conversation, {str(waiting.pk): "approve"}))

    thread = threading.Thread(target=approve)
    thread.start()
    leaks, helpers_seen = [], 0
    for _ in range(8):
        time.sleep(1)
        helpers_seen += bool(_own_containers(stack["run_id"], "tool"))
        out = subprocess.run(
            ["podman", "exec", work, *_probe(secret)], capture_output=True, text=True
        )
        leaks.append(out.stdout.strip() or out.stderr.strip()[:200])
    thread.join(180)
    assert not thread.is_alive()
    out = subprocess.run(["podman", "exec", work, *_probe(secret)], capture_output=True, text=True)
    leaks.append(out.stdout.strip())
    assert leaks == ["0"] * len(leaks), leaks
    assert helpers_seen > 0, "Hilfscontainer nie gesehen – lief telnet überhaupt?"
    done = ToolCall.objects.get(pk=waiting.pk)
    assert secret not in json.dumps(done.result, ensure_ascii=False)
    assert "192.0.2.10" not in json.dumps(done.arguments)
    # Hilfscontainer nach dem Aufruf entfernt
    wait_for(lambda: not _own_containers(stack["run_id"], "tool"), 30, "Hilfscontainer weg")
    # Der Arbeitscontainer hängt nicht am Relay-Netz
    nets = set(_inspect(work)["NetworkSettings"]["Networks"])
    assert nets == {template.network_name(str(session.uuid))}, nets
    assert secret not in agent.log()
