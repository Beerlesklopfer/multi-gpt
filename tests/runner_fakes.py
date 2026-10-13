"""Fake-Runner für die Tests des Transports ``runner`` (M14-06), ohne Container.

:class:`FakeLink` ersetzt die Zustellung von ``gateway`` (RunnerMessage +
LISTEN/NOTIFY): Je pipe läuft im MCP-Loop ein MCP-Server des SDK über
Memory-Streams; Nachrichten gehen dabei wie auf dem Draht als JSON-``dict``
hin und her. Der Server bildet die Werkzeuge des Agents auf einem
Datei-``dict`` nach.
"""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import re

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.shared.message import SessionMessage
from mcp_types import BlobResourceContents, EmbeddedResource, jsonrpc_message_adapter

from multigpt.runner import tools as runner_tools
from multigpt.runner.mcp_transport import RunnerOffline

# Alle Dienste eines gültig signierten Katalogs (Runner.services im Test)
ALL_SERVICES = [t.name for t in runner_tools.CATALOG.values() if not t.server_side]


class FakeWorkspace:
    """Dateien und Protokoll eines Fake-Containers (für Prüfungen im Test)."""

    def __init__(self):
        self.files: dict[str, str] = {}
        self.blobs: dict[str, bytes] = {}  # Binärdateien (z. B. in /scratch)
        self.calls: list[tuple[str, dict]] = []
        self.remote: list[dict] = []  # setup_host/telnet-Aufrufe mit Zugang (nur im Test lesen)
        self.fail_setup = False


def _norm(path: str) -> str:
    path = (path or ".").removeprefix("/workspace").removeprefix("/scratch").lstrip("/")
    return path or "."


def build_server(ws: FakeWorkspace) -> MCPServer:
    server = MCPServer("fake-runner")

    @server.tool()
    def read(path: str, offset: int = 1, limit: int = 2000) -> str:
        ws.calls.append(("read", {"path": path}))
        text = ws.files.get(_norm(path))
        if text is None:
            raise ValueError(f"Datei nicht gefunden: {path}")
        lines = text.splitlines()[offset - 1 : offset - 1 + limit]
        return "\n".join(f"{offset + i:6}\t{line}" for i, line in enumerate(lines))

    @server.tool()
    def write(path: str, content: str) -> str:
        ws.calls.append(("write", {"path": path}))
        ws.files[_norm(path)] = content
        return f"{path} geschrieben ({len(content)} Zeichen)."

    @server.tool()
    def edit(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
        ws.calls.append(("edit", {"path": path}))
        text = ws.files.get(_norm(path), "")
        count = text.count(old_string)
        if count == 0 or (count > 1 and not replace_all):
            raise ValueError("Textstelle nicht eindeutig gefunden.")
        ws.files[_norm(path)] = text.replace(old_string, new_string)
        return "geändert"

    @server.tool()
    def glob(pattern: str, path: str = ".") -> str:
        ws.calls.append(("glob", {"pattern": pattern}))
        return "\n".join(sorted(p for p in ws.files if fnmatch.fnmatch(p, pattern)))

    @server.tool()
    def grep(pattern: str, path: str = ".", glob: str = "", ignore_case: bool = False,
             max_results: int = 100) -> str:  # fmt: skip
        ws.calls.append(("grep", {"pattern": pattern}))
        flags = re.IGNORECASE if ignore_case else 0
        hits = [
            f"{name}:{n}:{line}"
            for name, text in sorted(ws.files.items())
            for n, line in enumerate(text.splitlines(), 1)
            if re.search(pattern, line, flags)
        ]
        return "\n".join(hits[:max_results])

    @server.tool()
    def bash(command: str, timeout_s: int = 120) -> str:
        ws.calls.append(("bash", {"command": command, "timeout_s": timeout_s}))
        if command.startswith("echo "):
            return command[5:]
        return f"$ {command}\n(exit 0)"

    @server.tool()
    def serve(path: str = ".") -> dict:
        ws.calls.append(("serve", {"path": path}))
        return {"url": "http://127.0.0.1:18080/tok123/", "port": 18080, "token": "tok123"}

    @server.tool()
    def publish(path: str, transfer: str, max_bytes: int, name: str = ""):
        """Datei als eingebettete Ressource (statt file_chunk-Rahmen)."""
        ws.calls.append(("publish", {"path": path, "transfer": transfer, "max_bytes": max_bytes}))
        key = _norm(path)
        if key not in ws.blobs and key not in ws.files:
            raise ValueError(f"Datei nicht gefunden: {path}")
        data = ws.blobs.get(key) or ws.files[key].encode()
        if len(data) > max_bytes:
            raise ValueError("zu groß")
        return [
            EmbeddedResource(
                type="resource",
                resource=BlobResourceContents(
                    uri=f"file:///scratch/{key}",
                    mime_type="application/octet-stream",
                    blob=base64.b64encode(data).decode(),
                ),
            )
        ]

    @server.tool()
    def setup_host(host: dict, pairing: dict) -> str:
        ws.calls.append(("setup_host", {"host": host.get("name")}))
        ws.remote.append({"tool": "setup_host", "host": dict(host), "pairing": dict(pairing)})
        if ws.fail_setup:
            raise ValueError("Einrichtung gescheitert")
        return f"{host.get('name')}: eingerichtet"

    @server.tool()
    def telnet(input: str, host: dict) -> str:  # noqa: A002 - Name aus dem Schema
        ws.calls.append(("telnet", {"input": input, "host": host.get("name")}))
        ws.remote.append({"tool": "telnet", "input": input, "host": dict(host)})
        return f"{host.get('name')}: ok"

    return server


class FakeLink:
    """Zustellung im Prozess: je pipe ein MCP-Server über Memory-Streams.

    ``online`` False -> ``send`` meldet ``RunnerOffline`` wie die echte Zustellung.
    """

    def __init__(self, workspace: FakeWorkspace | None = None):
        self.workspace = workspace or FakeWorkspace()
        self.online = True
        self.opened: list[tuple[int, str, int | None]] = []
        self.closed: list[str] = []
        self.sent: list[tuple[int, str, dict]] = []
        self._pipes: dict[str, dict] = {}

    async def open(self, runner_id, pipe, session_id):
        if not self.online:
            raise RunnerOffline("Runner nicht verbunden.")
        self.opened.append((runner_id, pipe, session_id))
        to_server_send, to_server_recv = anyio.create_memory_object_stream[SessionMessage](32)
        to_client_send, to_client_recv = anyio.create_memory_object_stream[SessionMessage](32)
        lowlevel = build_server(self.workspace)._lowlevel_server

        async def run():
            await lowlevel.run(
                to_server_recv, to_client_send, lowlevel.create_initialization_options()
            )

        task = asyncio.get_running_loop().create_task(run())
        self._pipes[pipe] = {
            "to_server": to_server_send,
            "from_server": to_client_recv,
            "task": task,
        }

    async def send(self, runner_id, pipe, message):
        if not self.online or pipe not in self._pipes:
            raise RunnerOffline("Runner nicht verbunden.")
        self.sent.append((runner_id, pipe, message))
        parsed = jsonrpc_message_adapter.validate_python(message, by_name=False)
        await self._pipes[pipe]["to_server"].send(SessionMessage(parsed))

    async def receive(self, pipe):
        entry = self._pipes.get(pipe)
        if entry is None:
            return None
        try:
            item = await entry["from_server"].receive()
        except (anyio.EndOfStream, anyio.ClosedResourceError):
            return None
        return item.message.model_dump(mode="json", by_alias=True, exclude_unset=True)

    async def close(self, pipe):
        self.closed.append(pipe)
        entry = self._pipes.pop(pipe, None)
        if entry is None:
            return
        await entry["to_server"].aclose()
        entry["task"].cancel()
