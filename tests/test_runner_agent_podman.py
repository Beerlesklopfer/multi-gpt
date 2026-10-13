"""Runner-Agent mit echtem Podman (Marker ``podman``, nur ``make test-runner``).

Nutzt das lokal gebaute Arbeits-Image (``localhost/mgpt-test-work:dev`` von
``ops`` bzw. ``MGPT_TEST_WORK_IMAGE``) und legt ausschließlich eigene Objekte an:
Label ``multigpt.test=1`` zusätzlich zu ``multigpt.runner=1``. Am Ende werden nur
solche Objekte entfernt; andere Container (z. B. n8n) bleiben unberührt.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
import uuid

import pytest

from runner_agent import template
from runner_agent.podman import PodmanEngine
from runner_agent.tools import Toolbox
from tests.test_runner_agent import make_config
from tests.test_runner_agent_session import Harness

pytestmark = pytest.mark.podman

# Eigene Kennung je Testlauf: Aufgeräumt wird nur, was dieser Lauf angelegt hat
# (andere Podman-Tests, z. B. die Ende-zu-Ende-Tests von ui, laufen evtl. parallel).
RUN_ID = uuid.uuid4().hex
TEST_LABELS = {"multigpt.test": "1", "multigpt.test.run": RUN_ID}


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
    if proc.returncode != 0:
        return None
    digests = json.loads(proc.stdout or "[]")
    return digests[0] if digests else None


def _cleanup_own_objects():
    """Nur Objekte dieses Testlaufs (multigpt.test.run) mit multigpt.runner=1 entfernen."""
    if not shutil.which("podman"):
        return
    names = subprocess.run(
        ["podman", "ps", "-a", "--filter", f"label=multigpt.test.run={RUN_ID}",
         "--filter", "label=multigpt.runner=1", "--format", "{{.Names}}"],
        capture_output=True, text=True,
    ).stdout.split()  # fmt: skip
    for name in names:
        if template.NAME_RE.match(name):
            subprocess.run(["podman", "rm", "-f", "-t", "0", name], capture_output=True)
    nets = subprocess.run(
        ["podman", "network", "ls", "--filter", f"label=multigpt.test.run={RUN_ID}",
         "--format", "{{.Name}}"],
        capture_output=True, text=True,
    ).stdout.split()  # fmt: skip
    for net in nets:
        if template.NETWORK_RE.match(net):
            subprocess.run(["podman", "network", "rm", net], capture_output=True)


@pytest.fixture
def podman_harness(tmp_path):
    socket_path, image = _socket(), _work_image()
    if not socket_path or not image:
        pytest.skip("Podman-Socket oder Arbeits-Image fehlt")
    config = make_config(images=frozenset({image}), labels=TEST_LABELS)
    engine = PodmanEngine(config, socket_path)
    harness = Harness(tmp_path, config=config, engine=engine)
    yield harness
    _cleanup_own_objects()


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 240))


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def test_work_container_end_to_end(podman_harness):
    h = podman_harness

    async def scenario():
        state = await h.start(session="pt1", workspace="pw1", cpus=1.0, memory_mb=256)
        assert state["state"] == "running", state
        assert state["limits"]["memory_mb"] == 256 and state["limits"]["cpus"] == 1.0
        name = template.container_name("pt1")
        info = await h.engine.inspect_container(name)
        assert info["Config"]["Labels"]["multigpt.test"] == "1"
        assert (
            template.check_inspect(
                info, h.manager.sessions["pt1"].limits, h.manager.sessions["pt1"].preview_port
            )
            == []
        )

        # Dateien, git und Python im Container
        text, _ = await h.call("write", sid="pt1", path="main.py", content="print(6*7)\n")
        assert "angelegt" in text and h.bundles["pw1"]
        text, structured = await h.call("bash", sid="pt1", command="python3 main.py")
        assert "42" in text and structured["exit_code"] == 0
        text, _ = await h.call("bash", sid="pt1", command="id -u; git log --oneline | wc -l")
        assert text.splitlines()[0] == "1000"

        # Netz aus: keine Route, kein DNS
        text, structured = await h.call(
            "bash", sid="pt1",
            command="timeout 5 bash -c 'echo > /dev/tcp/1.1.1.1/443' && echo OFFEN || echo ZU; "
                    "getent hosts example.com || echo KEIN-DNS",
        )  # fmt: skip
        assert "ZU" in text and "OFFEN" not in text and "KEIN-DNS" in text

        # Keine Geheimnisse des Rechners: Umgebung und Dateisystem
        text, _ = await h.call("bash", sid="pt1", command="env | cut -d= -f1 | sort | tr '\\n' ' '")
        for secret in ("SSH_AUTH_SOCK", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"):
            assert secret not in text
        text, _ = await h.call(
            "bash", sid="pt1", command="ls /home /root 2>&1 | head; touch /etc/x 2>&1 | head -1"
        )
        assert "Read-only" in text

        # Pfad hinaus wird abgelehnt
        with pytest.raises(Exception, match="/workspace"):
            await h.call("read", sid="pt1", path="/etc/passwd")
        await h.call("bash", sid="pt1", command="ln -s /etc/passwd leak")
        with pytest.raises(Exception, match="Symlink"):
            await h.call("read", sid="pt1", path="leak")

        # Vorschau an 127.0.0.1 mit Token, ohne Verzeichnislisten
        await h.call("write", sid="pt1", path="site/index.html", content="<h1>Hi</h1>")
        _, info = await h.call("serve", sid="pt1", path="site")
        for _ in range(50):
            status, body = await asyncio.to_thread(_get, info["url"])
            if status == 200:
                break
            await asyncio.sleep(0.2)
        assert status == 200 and body == b"<h1>Hi</h1>"
        base = f"http://127.0.0.1:{info['port']}"
        assert (await asyncio.to_thread(_get, base + "/index.html"))[0] == 404
        assert (await asyncio.to_thread(_get, info["url"] + "../main.py"))[0] == 404

        await h.manager.stop("pt1")
        names = [c.name for c in await h.engine.list_containers()]
        assert name not in names

        # Neuer Container lädt den Stand aus dem Bundle
        state = await h.start(session="pt2", workspace="pw1")
        assert state["state"] == "running", state
        text, _ = await h.call("bash", sid="pt2", command="python3 main.py; git log --oneline")
        assert "42" in text and "write main.py" in text
        await h.manager.stop("pt2")
        await h.engine.aclose()

    run(scenario())


def test_allowlist_relay_only_for_helper_and_credentials_never_in_work_container(
    podman_harness,
):
    h = podman_harness
    secret = "Geheim-Zugang-" + RUN_ID[:8]

    async def scenario():
        state = await h.start(
            session="pt3", workspace="pw3", network="allowlist",
            allow=[{"host": "192.0.2.10", "port": 22}],
        )  # fmt: skip
        assert state["state"] == "running", state
        session = h.manager.sessions["pt3"]
        relay = session.relay_ip
        assert relay and relay[0].isdigit()
        # Arbeitscontainer: weder Relay noch draußen erreichbar
        text, _ = await h.call(
            "bash", sid="pt3",
            command=f"timeout 5 bash -c 'echo > /dev/tcp/{relay}/20000' && echo RELAY-OK; "
                    "timeout 5 bash -c 'echo > /dev/tcp/1.1.1.1/443' && echo OFFEN || echo ZU",
        )  # fmt: skip
        assert "RELAY-OK" not in text and "OFFEN" not in text
        # Hilfscontainer: nur das Relay, nur der freigegebene Port
        helper = await h.manager.open_helper(session)
        try:
            res = await h.engine.exec(
                helper,
                ["bash", "-c",
                 f"timeout 5 bash -c 'echo > /dev/tcp/{relay}/20000' && echo RELAY-OK; "
                 f"timeout 5 bash -c 'echo > /dev/tcp/{relay}/20001' && echo ANDERER; "
                 "timeout 5 bash -c 'echo > /dev/tcp/1.1.1.1/443' && echo OFFEN || echo ZU"],
                timeout=30, workdir="/tmp",
            )  # fmt: skip
            out = res.stdout.decode()
            assert "RELAY-OK" in out and "ANDERER" not in out and "OFFEN" not in out
        finally:
            await h.manager.close_helper(helper)
        # ssh mit Passwort: Zugang während und nach dem Aufruf nicht im Arbeitscontainer
        host = {
            "name": "testgeraet", "address": "192.0.2.10", "port": 22, "user": "admin",
            "host_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKey",
            "secret_type": "password", "secret": secret,
        }  # fmt: skip
        box = Toolbox(h.manager, session)
        call = asyncio.create_task(box.call("ssh", {"host": host, "command": "uptime"}))
        # Sucht den Zugang in Dateien und in allen Prozessen des Arbeitscontainers;
        # der Suchbegriff steht nur rückwärts im eigenen Aufruf.
        probe = [
            "python3", "-c",
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
        ]  # fmt: skip
        leaks = []
        for _ in range(5):
            await asyncio.sleep(1)
            res = await h.engine.exec(session.container, probe, timeout=30)
            leaks.append(res.stdout.decode().strip())
        text, _ = await call
        res = await h.engine.exec(session.container, probe, timeout=30)
        leaks.append(res.stdout.decode().strip())
        assert leaks == ["0"] * 6, leaks
        assert secret not in text
        tools_left = [
            c.name for c in await h.engine.list_containers()
            if c.labels.get(template.LABEL_ROLE) == "tool"
        ]  # fmt: skip
        assert tools_left == []
        await h.manager.stop("pt3")
        await h.engine.aclose()

    run(scenario())


def test_foreign_objects_are_never_removed(podman_harness):
    h = podman_harness

    async def scenario():
        from runner_agent.engine import EngineError

        with pytest.raises(EngineError):
            await h.engine.remove_container("n8n")
        with pytest.raises(EngineError):
            await h.engine.remove_network("podman")
        await h.engine.aclose()

    run(scenario())


def test_local_scratch_with_podman(tmp_path):
    socket_path, image = _socket(), _work_image()
    if not socket_path or not image:
        pytest.skip("Podman-Socket oder Arbeits-Image fehlt")
    root = tmp_path / "MultiGPT" / "Scratch"
    root.mkdir(parents=True)
    config = make_config(
        images=frozenset({image}),
        labels=TEST_LABELS,
        scratch_dir=str(root),
        scratch_host_dir=str(root),
    )
    h = Harness(tmp_path, config=config, engine=PodmanEngine(config, socket_path))
    ws_uuid = "0b5f2c1e-7d4a-4c1b-9a8e-3f2d1c0b9a87"

    async def scenario():
        state = await h.start(
            session="pt4", workspace="pw4", scratch_mode="local", workspace_uuid=ws_uuid
        )
        assert state["state"] == "running", state
        text, _ = await h.call(
            "bash", sid="pt4",
            command="id -u; echo daten > /scratch/out.txt; printf '#!/bin/sh\\necho X' > "
                    "/scratch/r.sh; chmod +x /scratch/r.sh; /scratch/r.sh || echo NOEXEC",
        )  # fmt: skip
        assert text.splitlines()[0] == "1000" and "NOEXEC" in text
        assert (root / ws_uuid / "out.txt").read_text() == "daten\n"
        assert (root / ws_uuid / "out.txt").stat().st_uid == os.getuid()
        _, info = await h.call("publish", sid="pt4", path="/scratch/out.txt", transfer="t1")
        assert info["size"] == 6
        await h.manager.stop("pt4")
        await h.engine.aclose()

    try:
        run(scenario())
    finally:
        _cleanup_own_objects()
