"""Runner Ende-zu-Ende mit echtem Podman (M14-15, Marker ``podman``, nur ``make test-runner``).

Spielt die Abnahme durch die ganze Kette: Chat-Stream (Modell gefälscht,
``ScriptedAdapter``) → Werkzeugschleife → Zustellung (LISTEN/NOTIFY) →
Runner-Dienst (uvicorn im Prozess) → echter Agent mit ``PodmanEngine`` →
Arbeitscontainer. Geprüft werden: Dateien und Python im Container, Commit und
Bundle auf dem Server, „Netz aus“ (``git clone`` scheitert), Vorschau über
MultiGPT mit Token und CSP, Terminal (PTY) über MultiGPT, Laufzeit-Buchung und
Stopp bei 100 % mit erhaltenem Arbeitsstand.

Nur eigene Testobjekte: Der Agent setzt das Label ``multigpt.test=1``; am Ende
werden nur Container/Netze mit ``multigpt.test=1`` UND ``multigpt.runner=1``
entfernt, und nur die dieses Laufs (Label ``multigpt.test.run``). Kein ``prune``,
andere Container (z. B. n8n) bleiben unberührt.

Voraussetzungen: ``systemctl --user start podman.socket`` und das Arbeits-Image
(``make runner-images`` bzw. ``MGPT_TEST_WORK_IMAGE``); sonst wird übersprungen.
"""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import socket
import subprocess
import threading
import time
import uuid

import pytest
from django.test import Client
from django.urls import reverse

from multigpt.billing import runtime
from multigpt.billing.models import AccountBudget, UsageEntry
from multigpt.chat import tooling
from multigpt.chat.mcp import bridge
from multigpt.chat.models import AIModel, Conversation, Provider, ToolCall
from multigpt.chat.providers import registry
from multigpt.runner import chat as runner_chat
from multigpt.runner import delivery, gateway, tokens, workspaces
from multigpt.runner.mcp_server import ensure_mcp_server
from multigpt.runner.models import Runner, RunnerSession, RunnerWorkspace
from tests.node_support import make_user
from tests.test_tool_loop import ScriptedAdapter, answer, call, events_of, tool_round

pytestmark = [
    pytest.mark.podman,
    pytest.mark.django_db(transaction=True, serialized_rollback=True),
]

agent_podman = pytest.importorskip("tests.test_runner_agent_podman")
# Eigenes Label je Lauf: Aufgeräumt wird nur, was dieser Lauf angelegt hat
# (parallele Podman-Tests anderer bleiben unberührt).
RUN_ID = uuid.uuid4().hex[:12]
TEST_LABELS = {**agent_podman.TEST_LABELS, "multigpt.test.run": RUN_ID}
START_TIMEOUT = 180


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _cleanup_run():
    """Nur Container und Netze dieses Laufs entfernen (Label multigpt.test.run)."""
    from runner_agent import template

    if not shutil.which("podman"):
        return
    label = f"label=multigpt.test.run={RUN_ID}"
    names = subprocess.run(
        ["podman", "ps", "-a", "--filter", label, "--filter", "label=multigpt.test=1",
         "--format", "{{.Names}}"],
        capture_output=True, text=True,
    ).stdout.split()  # fmt: skip
    for name in names:
        if template.NAME_RE.match(name):
            subprocess.run(["podman", "rm", "-f", "-t", "0", name], capture_output=True)
    nets = subprocess.run(
        ["podman", "network", "ls", "--filter", label, "--format", "{{.Name}}"],
        capture_output=True, text=True,
    ).stdout.split()  # fmt: skip
    for net in nets:
        if template.NETWORK_RE.match(net):
            subprocess.run(["podman", "network", "rm", net], capture_output=True)


def _wait(predicate, timeout, what):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise AssertionError(f"Zeitüberschreitung: {what}")


@pytest.fixture
def stack(settings, tmp_path):
    """Runner-Dienst (uvicorn) + echter Agent mit Podman für das Konto anna."""
    import uvicorn

    from runner_agent.agent import Agent
    from runner_agent.podman import PodmanEngine
    from tests.test_runner_agent import make_config

    socket_path, image = agent_podman._socket(), agent_podman._work_image()
    if not socket_path or not image:
        pytest.skip("Podman-Socket oder Arbeits-Image fehlt (make runner-images)")
    settings.RUNNER_WORK_IMAGE = image
    settings.MEDIA_ROOT = tmp_path / "media"

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            gateway.build_app(),
            host="127.0.0.1",
            port=port,
            ws="websockets",
            log_level="warning",
            lifespan="on",
        )
    )
    server_thread = threading.Thread(target=server.run, name="e2e-gateway", daemon=True)
    server_thread.start()
    _wait(lambda: server.started, 20, "Runner-Dienst startet")

    anna = make_user("anna")
    created = tokens.create_token(anna, "E2E")
    token_file = tmp_path / "token"
    token_file.write_text(created.secret)
    token_file.chmod(0o600)
    mcp_server = ensure_mcp_server(anna)

    config = make_config(
        url=f"ws://127.0.0.1:{port}/runner/ws",
        insecure_dev=True,
        token_file=str(token_file),
        images=frozenset({image}),
        labels=TEST_LABELS,
        podman_socket=socket_path,
        hostname="mgpt-test-e2e",
    )
    engine = PodmanEngine(config, socket_path)
    agent = Agent(config, engine)
    loop = asyncio.new_event_loop()
    agent_thread = threading.Thread(
        target=loop.run_until_complete, args=(agent.run(),), name="e2e-agent", daemon=True
    )
    agent_thread.start()
    _wait(
        lambda: Runner.objects.filter(token=created.token, status=Runner.Status.ONLINE).exists(),
        30,
        "Agent verbindet sich",
    )
    try:
        yield {"user": anna, "server": mcp_server, "runner_token": created.token}
    finally:
        loop.call_soon_threadsafe(agent.stop)
        agent_thread.join(30)
        if hasattr(engine, "aclose"):
            asyncio.run(engine.aclose())
        server.should_exit = True
        server_thread.join(30)
        bridge.shutdown()
        delivery.shutdown()
        _cleanup_run()


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


def test_acceptance_with_real_podman(stack, scripted):
    anna, server = stack["user"], stack["server"]
    client = Client()
    client.force_login(anna)
    model = _model()
    workspace = workspaces.create_workspace(anna, "e2e-spiel")
    conversation = Conversation.objects.create(user=anna, title="E2E")
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
            answer("Fertig."),
        ]
    )
    events = events_of(_send(client, conversation, model, server, "Bitte bauen."))
    names = [name for name, _ in events]
    assert "runner_session" in names, names
    by_order = list(ToolCall.objects.order_by("pk"))
    assert [tc.status for tc in by_order] == ["ok"] * 5, [
        (tc.tool, tc.status, (tc.result or {})) for tc in by_order
    ]
    texts = [json.dumps(tc.result, ensure_ascii=False) for tc in by_order]
    assert "42" in texts[2]
    # Netz aus: git clone scheitert (kein DNS, keine Route).
    assert "RC=0" not in texts[3] and "RC=" in texts[3]

    session = RunnerSession.objects.get(owner=anna, status=RunnerSession.Status.RUNNING)
    assert session.network is False
    # Arbeitsstand: jeder Werkzeugaufruf mit Änderungen ergibt einen Commit im Bundle.
    _wait(
        lambda: RunnerWorkspace.objects.get(pk=workspace.pk).size > 0,
        30,
        "Bundle auf dem Server",
    )
    stored = RunnerWorkspace.objects.get(pk=workspace.pk)
    assert len(stored.head_commit) >= 40
    log = [c.subject for c in workspaces.history(stored)]
    assert any("main.py" in subject for subject in log), log

    # Vorschau über MultiGPT: nur mit Token, strenge CSP, keine Verzeichnisliste.
    _wait(
        lambda: RunnerSession.objects.get(pk=session.pk).preview_port is not None,
        20,
        "Vorschau meldet sich",
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

    # Terminal über MultiGPT: Eingabe hin, Ausgabe zurück.
    stream = client.get(
        reverse("runner:pty_stream", args=[session.uuid]), {"cols": "80", "rows": "24"}
    )
    assert stream.status_code == 200
    chunks = iter(stream.streaming_content)
    first = next(chunks).decode()
    pty = json.loads(first.split("data: ", 1)[1])["pty"]
    raw = first
    # Wie runner_terminal.js: Eingaben erst nach der ersten Ausgabe (Prompt der Shell).
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

    # Laufzeit: Heartbeats buchen Minuten; bei 100 % Stopp, Arbeitsstand bleibt.
    _wait(
        lambda: RunnerSession.objects.get(pk=session.pk).counted_until is not None,
        45,
        "Heartbeat zählt Laufzeit",
    )
    AccountBudget.objects.update_or_create(
        account=runtime.runtime_account(), user=anna, defaults={"monthly_minutes": 0}
    )
    assert runtime.enforce(anna)
    _wait(
        lambda: not RunnerSession.objects.get(pk=session.pk).is_active,
        90,
        "Stopp wegen Kontingent",
    )
    session.refresh_from_db()
    assert session.ended is not None
    assert UsageEntry.objects.filter(kind="runtime", user=anna).exists()
    assert RunnerWorkspace.objects.filter(pk=workspace.pk, size__gt=0).exists()
    assert not runtime.runtime_allows(anna)
