"""Runner-Agent: Sitzungen, Arbeitsstand, Werkzeuge und MCP mit der FakeEngine.

Die FakeEngine führt Befehle als lokale Prozesse in einem eigenen Verzeichnis
aus; ``send`` des Agents landet in einem Mitschnitt, der wie der Server auf
``bundle_get`` und hochgeladene Bundles antwortet.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time

import pytest

from runner_agent import protocol, template, tools
from runner_agent.engine import FakeEngine
from runner_agent.session import SessionManager
from tests.test_runner_agent import OTHER_IMAGE, make_config


class Harness:
    """Agent-Seite mit FakeEngine; spielt die Gegenseite für Bundles."""

    def __init__(self, tmp_path, *, config=None, clock=time.monotonic, engine=None):
        self.config = config or make_config()
        self.engine = engine or FakeEngine(self.config, root=str(tmp_path / "engine"))
        self.manager = SessionManager(self.engine, self.config, self.send, clock=clock)
        # Wie nach einem welcome mit gültig signiertem Katalog: alle Dienste frei.
        self.manager.services = frozenset(tools.TOOL_NAMES)
        self.manager.server_images = sorted(self.config.images)
        self.frames: list[dict] = []
        self.bundles: dict[str, bytes] = {}  # Arbeitsstand-Bundles „auf dem Server“
        self.uploads: dict[str, protocol.BundleAssembler] = {}
        self.reject_upload = False
        self.workspace_of: dict[str, str] = {}

    async def send(self, kind, **fields):
        frame = protocol.parse(protocol.encode(kind, protocol.AGENT, **fields), protocol.AGENT)
        self.frames.append(frame)
        if kind == "bundle_get":
            asyncio.get_running_loop().create_task(self._serve_bundle(frame))
        elif kind == "bundle_chunk":
            assembler = self.uploads.setdefault(
                frame["transfer"], protocol.BundleAssembler(frame["transfer"])
            )
            data = assembler.add(frame)
            if data is not None:
                ws = self.workspace_of[frame["session"]]
                if not self.reject_upload:
                    self.bundles[ws] = data
                ack = protocol.parse(
                    protocol.encode(
                        "bundle_ack",
                        protocol.SERVER,
                        session=frame["session"],
                        transfer=frame["transfer"],
                        ok=not self.reject_upload,
                        head=frame["head"] if not self.reject_upload else None,
                        error="zu groß" if self.reject_upload else None,
                    ),
                    protocol.SERVER,
                )
                asyncio.get_running_loop().call_soon(self.manager.on_bundle_ack, ack)

    async def _serve_bundle(self, frame):
        data = self.bundles[self.workspace_of[frame["session"]]]
        for seq, part, last in protocol.split_bundle(data, 1024):
            chunk = protocol.parse(
                protocol.encode(
                    "bundle_chunk",
                    protocol.SERVER,
                    session=frame["session"],
                    transfer=frame["transfer"],
                    seq=seq,
                    data=protocol.b64encode(part),
                    last=last,
                    size=len(data) if last else None,
                    sha256=hashlib.sha256(data).hexdigest() if last else None,
                ),
                protocol.SERVER,
            )
            await self.manager.on_bundle_chunk(chunk)

    def start_frame(self, session="s1", workspace="w1", **kw):
        values = dict(
            session=session,
            workspace=workspace,
            image=sorted(self.config.images)[0],
            cpus=1.0,
            memory_mb=512,
            pids=256,
            tmpfs_mb=64,
            network="none",
            command_seconds=60,
            output_bytes=4096,
            idle_minutes=15,
        )
        values.update(kw)
        if workspace in self.bundles and "bundle" not in kw:
            data = self.bundles[workspace]
            values["bundle"] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        self.workspace_of[session] = workspace
        return protocol.parse(protocol.encode("session_start", protocol.SERVER, **values))

    def of(self, kind):
        return [f for f in self.frames if f["type"] == kind]

    async def start(self, **kw):
        await self.manager.start(self.start_frame(**kw))
        return self.of("session_state")[-1]

    async def call(self, name, sid="s1", **args):
        box = tools.Toolbox(self.manager, self.manager.get(sid))
        return await box.call(name, args)


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 60))


@pytest.fixture
def harness(tmp_path):
    return Harness(tmp_path)


# --- Start und Stopp --------------------------------------------------------------------


def test_session_start_reports_actual_limits(harness):
    async def scenario():
        state = await harness.start()
        assert state["state"] == "running", state
        assert state["limits"] == {
            "cpus": 1.0,
            "memory_mb": 512,
            "pids": 256,
            "tmpfs_mb": 64,
            "network": "none",
        }
        assert state["preview_port"] == 18080
        net = template.network_name("s1")
        assert harness.engine.networks[net]["internal"] is True
        body = harness.engine.created[0]
        assert body["Networks"] == {net: {}}
        assert template.check_body(body, harness.config) == "work"
        assert harness.manager.infos() == [
            {"session": "s1", "workspace": "w1", "state": "running", "preview_port": 18080}
        ]
        # git init im leeren Arbeitsstand
        ws = harness.engine.workspace(template.container_name("s1"))
        assert os.path.isdir(os.path.join(ws, ".git"))
        await harness.manager.stop("s1", "user")
        assert harness.of("session_state")[-1] == {
            "type": "session_state",
            "session": "s1",
            "state": "stopped",
            "limits": None,
            "preview_port": None,
            "error": None,
        }
        assert harness.engine.containers == {} and harness.engine.networks == {}

    run(scenario())


@pytest.mark.parametrize(
    "change,needle",
    [
        ({"image": OTHER_IMAGE}, "Positivliste"),
        ({"cpus": 64.0}, "CPU"),
        ({"memory_mb": 262144}, "RAM"),
    ],
)
def test_session_start_rejects_foreign_image_and_limits(harness, change, needle):
    async def scenario():
        state = await harness.start(**change)
        assert state["state"] == "failed" and needle in state["error"]
        assert harness.engine.created == [] and harness.engine.networks == {}

    run(scenario())


def test_image_must_also_be_in_server_list(harness):
    harness.manager.server_images = [OTHER_IMAGE]

    async def scenario():
        state = await harness.start()
        assert state["state"] == "failed" and "Server" in state["error"]

    run(scenario())


def test_deviating_actual_limits_stop_the_container(harness):
    name = template.container_name("s1")
    harness.engine.tamper[name] = {"Memory": 4096 << 20, "MemorySwap": 4096 << 20}

    async def scenario():
        state = await harness.start()
        assert state["state"] == "failed"
        assert "Abweichende Grenzen" in state["error"] and "RAM" in state["error"]
        assert name in harness.engine.removed and harness.engine.containers == {}

    run(scenario())


def test_workspace_only_in_one_session_and_busy_port_is_skipped(harness):
    harness.engine.busy_ports.add(18080)

    async def scenario():
        assert (await harness.start())["preview_port"] == 18081
        state = await harness.start(session="s2", workspace="w1")
        assert state["state"] == "failed" and "schon in einer Sitzung" in state["error"]
        state = await harness.start(session="s3", workspace="w3")
        assert state["state"] == "running" and state["preview_port"] == 18082

    run(scenario())


def test_allowlist_starts_relay_outside_work_container(harness):
    async def scenario():
        state = await harness.start(
            network="allowlist", allow=[{"host": "192.168.1.20", "port": 22}]
        )
        assert state["state"] == "running" and state["limits"]["network"] == "allowlist"
        bodies = {b["labels"][template.LABEL_ROLE]: b for b in harness.engine.created}
        relay, work = bodies["relay"], bodies["work"]
        assert relay["command"][3:] == ["192.168.1.20:22"]
        # Relay nur am Relay-Netz und am Ausgang; der Arbeitscontainer erreicht es nicht
        assert set(relay["Networks"]) == {
            template.network_name("s1", "rel"),
            template.network_name("s1", "out"),
        }
        assert list(work["Networks"]) == [template.network_name("s1", "net")]
        assert harness.engine.networks[template.network_name("s1", "net")]["internal"] is True
        assert harness.engine.networks[template.network_name("s1", "rel")]["internal"] is True
        assert harness.engine.networks[template.network_name("s1", "out")]["internal"] is False
        await harness.manager.stop("s1")
        assert harness.engine.containers == {} and harness.engine.networks == {}

    run(scenario())


def test_adopt_running_containers_after_restart(tmp_path):
    first = Harness(tmp_path)

    async def scenario():
        await first.start()
        second = SessionManager(first.engine, first.config, first.send)
        await second.adopt()
        assert second.infos() == [
            {"session": "s1", "workspace": "w1", "state": "running", "preview_port": 18080}
        ]
        assert second.sessions["s1"].limits.memory_mb == 512

    run(scenario())


def test_idle_sessions_are_stopped(tmp_path):
    now = [1000.0]
    harness = Harness(tmp_path, clock=lambda: now[0])

    async def scenario():
        await harness.start(idle_minutes=1)
        await harness.manager.check_idle()
        assert "s1" in harness.manager.sessions
        now[0] += 61
        await harness.manager.check_idle()
        assert "s1" not in harness.manager.sessions
        assert harness.of("session_state")[-1]["state"] == "stopped"

    run(scenario())


# --- Werkzeuge und Arbeitsstand ---------------------------------------------------------


def test_file_tools_commit_and_upload_bundle(harness):
    async def scenario():
        await harness.start()
        text, _ = await harness.call("write", path="app.py", content="print('hallo')\n")
        assert text == "app.py angelegt (15 Byte)."
        upload = harness.of("bundle_chunk")
        assert upload[-1]["last"] and upload[-1]["message"] == "write app.py"
        assert len(upload[-1]["head"]) == 40
        assert "w1" in harness.bundles
        text, _ = await harness.call("read", path="app.py")
        assert text == "     1\tprint('hallo')"
        text, _ = await harness.call("edit", path="app.py", old_string="hallo", new_string="welt")
        assert "1 Stelle" in text
        assert len([f for f in harness.of("bundle_chunk") if f["last"]]) == 2
        text, _ = await harness.call("glob", pattern="*.py")
        assert text == "app.py"
        text, _ = await harness.call("grep", pattern="welt")
        assert text == "app.py:1:print('welt')"
        # Lesen ändert nichts: kein weiteres Bundle
        assert len([f for f in harness.of("bundle_chunk") if f["last"]]) == 2

    run(scenario())


def test_bundle_is_loaded_into_new_session(harness):
    async def scenario():
        await harness.start()
        await harness.call("write", path="notes/a.md", content="# A\n")
        await harness.manager.stop("s1")
        state = await harness.start(session="s2", workspace="w1")
        assert state["state"] == "running", state
        assert harness.of("bundle_get")[-1]["session"] == "s2"
        assert any(f["type"] == "bundle_ack" and f["ok"] for f in harness.frames)
        text, _ = await harness.call("read", sid="s2", path="notes/a.md")
        assert "# A" in text

    run(scenario())


def test_rejected_upload_is_reported(harness):
    harness.reject_upload = True

    async def scenario():
        await harness.start()
        text, _ = await harness.call("write", path="a.txt", content="x")
        assert "nicht gespeichert: zu groß" in text

    run(scenario())


def test_path_outside_workspace_is_a_tool_error(harness):
    async def scenario():
        await harness.start()
        with pytest.raises(Exception) as info:
            await harness.call("write", path="/etc/passwd", content="x")
        assert "/workspace" in str(info.value)

    run(scenario())


def test_bash_streams_pty_and_commits(harness):
    async def scenario():
        await harness.start()
        text, structured = await harness.call("bash", command="echo hallo > out.txt; echo fertig")
        assert "fertig" in text and "[Exit-Code 0]" in text
        assert structured == {"exit_code": 0, "truncated": False}
        data = [f for f in harness.of("pty_data") if f["pty"] == "run-1"]
        assert b"fertig" in b"".join(protocol.b64decode(f["data"]) for f in data)
        assert harness.of("pty_exit")[-1] == {
            "type": "pty_exit",
            "session": "s1",
            "pty": "run-1",
            "code": 0,
        }
        assert harness.of("bundle_chunk")[-1]["message"].startswith("bash: echo hallo")
        text, structured = await harness.call("bash", command="exit 3")
        assert structured["exit_code"] == 3

    run(scenario())


def test_bash_timeout_and_output_limit(harness):
    async def scenario():
        await harness.start(output_bytes=1024, command_seconds=2)
        text, structured = await harness.call("bash", command="sleep 30", timeout_s=600)
        assert "Zeitlimit von 2 s" in text and structured["exit_code"] == 124
        text, structured = await harness.call("bash", command="yes x | head -c 50000")
        assert structured["truncated"] is True and "gekürzt" in text
        assert len(text) < 2000

    run(scenario())


def test_terminal_from_browser(harness):
    async def scenario():
        await harness.start()
        server = dict(session="s1", pty="t1")
        open_ = protocol.parse(
            protocol.encode("pty_open", protocol.SERVER, cols=80, rows=24, **server)
        )
        await harness.manager.pty_open(open_)
        data = protocol.b64encode(b"echo $((6*7)); exit\n")
        await harness.manager.pty_input(
            protocol.parse(protocol.encode("pty_input", protocol.SERVER, data=data, **server))
        )
        for _ in range(100):
            if any(f["pty"] == "t1" for f in harness.of("pty_exit")):
                break
            await asyncio.sleep(0.05)
        out = b"".join(
            protocol.b64decode(f["data"]) for f in harness.of("pty_data") if f["pty"] == "t1"
        )
        assert b"42" in out

    run(scenario())


def test_serve_and_preview_over_back_channel(harness):
    async def scenario():
        await harness.start()
        await harness.call("write", path="site/index.html", content="<h1>Vorschau</h1>")
        text, info = await harness.call("serve", path="site")
        assert info["port"] == 18080 and info["url"].startswith("http://127.0.0.1:18080/")
        assert info["url"].endswith(f"/{info['token']}/")
        preview = harness.of("preview")[-1]
        assert preview["running"] and preview["token"] == info["token"]
        box = harness.engine.containers[template.container_name("s1")]
        assert box["detached"][-1][:3] == ["mgpt-serve", "--token", info["token"]]

        async def get(path, method="GET"):
            frame = protocol.parse(
                protocol.encode(
                    "preview_request",
                    protocol.SERVER,
                    session="s1",
                    request=f"r{len(harness.frames)}",
                    method=method,
                    path=path,
                )
            )
            await harness.manager.preview_request(frame)
            return harness.of("preview_response")[-1]

        resp = await get("/index.html")
        assert resp["status"] == 200 and resp["last"]
        assert protocol.b64decode(resp["data"]) == b"<h1>Vorschau</h1>"
        assert "sandbox" in resp["headers"]["Content-Security-Policy"]
        assert (await get("/"))["status"] == 200
        assert (await get("/../app.py"))["status"] == 404
        assert (await get("/fehlt.html"))["status"] == 404
        head = await get("/index.html", "HEAD")
        assert head["data"] is None and head["headers"]["Content-Length"] == "17"

    run(scenario())


def test_preview_without_serve_is_404(harness):
    async def scenario():
        await harness.start()
        frame = protocol.parse(
            protocol.encode(
                "preview_request", protocol.SERVER, session="s1", request="r1", method="GET",
                path="/index.html",
            )
        )  # fmt: skip
        await harness.manager.preview_request(frame)
        assert harness.of("preview_response")[-1]["status"] == 404

    run(scenario())


def test_ssh_and_telnet_need_allowlist_and_host_key(harness):
    host = {
        "name": "nas",
        "address": "192.168.1.20",
        "port": 22,
        "user": "admin",
        "host_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI",
        "secret_type": "password",
        "secret": "pw",
    }

    async def scenario():
        await harness.start()
        with pytest.raises(tools.ToolError, match="kein Netz"):
            await harness.call("ssh", host=host, command="uptime")
        await harness.start(
            session="s2", workspace="w2", network="allowlist",
            allow=[{"host": "192.168.1.30", "port": 22}],
        )  # fmt: skip
        with pytest.raises(tools.ToolError, match="Positivliste"):
            await harness.call("ssh", sid="s2", host=host, command="uptime")
        with pytest.raises(tools.ToolError, match="Positivliste"):
            await harness.call("telnet", sid="s2", host=dict(host, port=23), input="ls")
        allowed = dict(host, address="192.168.1.30", host_key="")
        with pytest.raises(tools.ToolError, match="Host-Schlüssel"):
            await harness.call("ssh", sid="s2", host=allowed, command="uptime")
        with pytest.raises(tools.ToolError, match="Host fehlt"):
            await harness.call("ssh", sid="s2", host="nas", command="uptime")

    run(scenario())


# --- MCP je pipe --------------------------------------------------------------------------


def test_mcp_pipe_lists_and_calls_tools(harness):
    async def scenario():
        await harness.start()

        async def rpc(message):
            frame = protocol.parse(
                protocol.encode("mcp", protocol.SERVER, session="s1", pipe="p1", message=message)
            )
            await tools.on_mcp(harness.manager, frame)

        async def reply(msg_id):
            for _ in range(200):
                for frame in harness.of("mcp"):
                    if frame["message"].get("id") == msg_id:
                        return frame["message"]
                await asyncio.sleep(0.02)
            raise AssertionError(f"keine Antwort auf {msg_id}")

        await rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            }
        )
        init = await reply(1)
        assert init["result"]["serverInfo"]["name"] == "multigpt-runner"
        await rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
        await rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {tool["name"] for tool in (await reply(2))["result"]["tools"]}
        assert names == {
            "read", "write", "edit", "glob", "grep", "bash", "serve", "publish", "ssh", "telnet"
        }  # fmt: skip
        await rpc(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "write", "arguments": {"path": "x.txt", "content": "1"}},
            }
        )
        result = (await reply(3))["result"]
        assert result["content"][0]["text"] == "x.txt angelegt (1 Byte)."
        assert not result.get("isError")
        await rpc(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "read", "arguments": {"path": "../../etc/passwd"}},
            }
        )
        result = (await reply(4))["result"]
        assert result["isError"] is True and "/workspace" in result["content"][0]["text"]
        await harness.manager.stop("s1")

    run(scenario())


# --- Scratchpad ------------------------------------------------------------------------

UUID = "0b5f2c1e-7d4a-4c1b-9a8e-3f2d1c0b9a87"


def test_container_scratch_and_publish(harness):
    async def scenario():
        state = await harness.start(scratch_mode="container", scratch_mb=32)
        assert state["state"] == "running"
        body = harness.engine.created[0]
        scratch = [m for m in body["mounts"] if m["destination"] == "/scratch"][0]
        assert scratch["type"] == "tmpfs" and "noexec" in scratch["options"]
        assert "size=32m" in scratch["options"] and "userns" not in body
        text, _ = await harness.call("write", path="/scratch/report.txt", content="Ergebnis\n")
        assert text.startswith("/scratch/report.txt angelegt")
        # /scratch gehört nicht in git: kein neues Bundle
        assert harness.of("bundle_chunk") == []
        text, _ = await harness.call("glob", pattern="*", path="/scratch")
        assert text == "/scratch/report.txt"
        text, info = await harness.call(
            "publish", path="/scratch/report.txt", transfer="tr-1", max_bytes=1000
        )
        assert info == {
            "transfer": "tr-1",
            "size": 9,
            "sha256": hashlib.sha256(b"Ergebnis\n").hexdigest(),
            "name": "report.txt",
        }
        chunks = harness.of("file_chunk")
        assert chunks[-1]["last"] and chunks[-1]["name"] == "report.txt"
        assert chunks[-1]["mime"] == "text/plain"
        assert b"".join(protocol.b64decode(c["data"]) for c in chunks) == b"Ergebnis\n"
        with pytest.raises(Exception, match="größer"):
            await harness.call("publish", path="/scratch/report.txt", transfer="tr-2", max_bytes=3)
        with pytest.raises(Exception, match="/scratch"):
            await harness.call("read", path="/scratch/../etc/passwd")

    run(scenario())


def _local_harness(tmp_path):
    scratch_root = tmp_path / "home" / "MultiGPT" / "Scratch"
    scratch_root.mkdir(parents=True)
    config = make_config(scratch_dir=str(scratch_root), scratch_host_dir=str(scratch_root))
    return Harness(tmp_path, config=config), scratch_root


def test_local_scratch_is_the_only_bind_mount(tmp_path):
    harness, root = _local_harness(tmp_path)

    async def scenario():
        state = await harness.start(scratch_mode="local", scratch_mb=1, workspace_uuid=UUID)
        assert state["state"] == "running", state
        body = harness.engine.created[0]
        binds = [m for m in body["mounts"] if m["type"] == "bind"]
        assert binds == [
            {
                "destination": "/scratch",
                "type": "bind",
                "source": f"{root}/{UUID}",
                "options": ["rbind", "rw", "noexec", "nosuid", "nodev"],
            }
        ]
        assert body["userns"] == {"nsmode": "keep-id", "value": "uid=1000,gid=1000"}
        assert (root / UUID).is_dir()
        await harness.call("write", path="/scratch/a.txt", content="x")
        assert (root / UUID / "a.txt").read_text() == "x"
        # Über der Grenze: Hinweis im Ergebnis
        # mgpt-fs prüft vor dem Schreiben: Datei über der Grenze wird abgelehnt
        with pytest.raises(Exception, match="Scratchpad voll"):
            await harness.call("write", path="/scratch/b.txt", content="y" * (2 << 20))
        # bash: Dateigröße je Prozess auf den Rest begrenzt (RLIMIT_FSIZE)
        text, structured = await harness.call(
            "bash",
            command='head -c 3000000 /dev/zero > "${MGPT_SCRATCH:-/scratch}/dd.bin"; echo rc=$?',
        )
        assert "rc=0" not in text
        assert (root / UUID / "dd.bin").stat().st_size <= 1 << 20
        (root / UUID / "dd.bin").unlink()
        # Über der Grenze (z. B. von außen befüllt): Hinweis, Schreibwerkzeuge gesperrt
        (root / UUID / "big.bin").write_bytes(os.urandom(2 << 20))
        text, _ = await harness.call("bash", command="true")
        assert "Scratchpad belegt 2 MB, Grenze 1 MB" in text
        with pytest.raises(tools.ToolError, match="Schreiben gesperrt"):
            await harness.call("write", path="notes.txt", content="x")
        await harness.call("bash", command='rm "${MGPT_SCRATCH:-/scratch}/big.bin"')
        text, _ = await harness.call("write", path="notes.txt", content="x")
        assert "angelegt" in text

    run(scenario())


def test_local_scratch_checks(tmp_path):
    harness, root = _local_harness(tmp_path)
    plain = Harness(tmp_path / "plain")

    async def scenario():
        state = await plain.start(scratch_mode="local", workspace_uuid=UUID)
        assert state["state"] == "failed" and "nicht eingerichtet" in state["error"]
        state = await harness.start(scratch_mode="local")
        assert state["state"] == "failed" and "UUID" in state["error"]
        (root / UUID).symlink_to(tmp_path)
        state = await harness.start(session="s2", scratch_mode="local", workspace_uuid=UUID)
        assert state["state"] == "failed" and "Symlink" in state["error"]

    (tmp_path / "plain").mkdir()
    run(scenario())


# --- ssh und telnet außerhalb des Arbeitscontainers ---------------------------------------

SECRET = "Geheim-Zugang-4711"
HOST = {
    "name": "nas",
    "address": "192.168.1.20",
    "port": 22,
    "user": "admin",
    "host_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHostKey",
    "secret_type": "password",
    "secret": SECRET,
}


def _secret_in_tree(path) -> list[str]:
    hits = []
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            full = os.path.join(dirpath, name)
            try:
                with open(full, "rb") as fh:
                    if SECRET.encode() in fh.read():
                        hits.append(full)
            except OSError:
                pass
    return hits


def test_ssh_runs_in_helper_container_with_credentials_only_there(harness):
    seen = {}

    async def scenario():
        await harness.start(network="allowlist", allow=[{"host": "192.168.1.20", "port": 22}])
        work = harness.engine.containers[template.container_name("s1")]["dir"]
        harness.engine.fake_commands["ssh"] = "sleep 0.5; echo remote-ok"
        call = asyncio.create_task(harness.call("ssh", host=HOST, command="uptime"))
        for _ in range(100):
            helpers = [
                n for n, b in harness.engine.containers.items()
                if b["body"]["labels"][template.LABEL_ROLE] == "tool"
            ]  # fmt: skip
            if helpers and _secret_in_tree(harness.engine.containers[helpers[0]]["dir"]):
                break
            await asyncio.sleep(0.02)
        seen["helper"] = helpers[0]
        seen["helper_has_secret"] = bool(
            _secret_in_tree(harness.engine.containers[helpers[0]]["dir"])
        )
        seen["work_during"] = _secret_in_tree(work)
        text, structured = await call
        seen["text"], seen["work_after"] = text, _secret_in_tree(work)
        body = [b for b in harness.engine.created if b["name"] == seen["helper"]][0]
        seen["networks"] = list(body["Networks"])
        seen["ssh_container"] = [n for n, argv in harness.engine.calls if "ssh" in argv]
        seen["remaining"] = [
            n for n, b in harness.engine.containers.items()
            if b["body"]["labels"][template.LABEL_ROLE] == "tool"
        ]  # fmt: skip

    run(scenario())
    assert seen["helper_has_secret"] and seen["work_during"] == [] and seen["work_after"] == []
    assert "remote-ok" in seen["text"] and SECRET not in seen["text"]
    assert seen["networks"] == [template.network_name("s1", "rel")]
    assert seen["ssh_container"] == [seen["helper"]]
    assert seen["remaining"] == []  # Hilfscontainer nach dem Aufruf entfernt
    for frame in harness.frames:  # Zugang nie im Rahmen an den Server
        assert SECRET not in json.dumps(frame)


def test_telnet_login_in_helper_container_and_password_masked(harness):
    script = (
        'printf "login: "; read u; printf "Password: "; stty -echo; read p; stty echo; '
        'echo; echo "welcome $u"; while read c; do echo "out:$c"; done'
    )

    async def scenario():
        await harness.start(network="allowlist", allow=[{"host": "192.168.1.20", "port": 23}])
        harness.engine.fake_commands["telnet"] = script
        work = harness.engine.containers[template.container_name("s1")]["dir"]
        host = dict(HOST, port=23)
        text, _ = await harness.call("telnet", host=host, input="show version")
        assert "welcome admin" in text and "out:show version" in text
        assert SECRET not in text
        assert _secret_in_tree(work) == []
        tool_boxes = [
            b for b in harness.engine.containers.values()
            if b["body"]["labels"][template.LABEL_ROLE] == "tool"
        ]  # fmt: skip
        assert len(tool_boxes) == 1  # bleibt für weitere Eingaben offen
        await harness.manager.stop("s1")
        assert harness.engine.containers == {}

    run(scenario())
