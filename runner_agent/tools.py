"""Werkzeuge im Arbeitscontainer und MCP-Server je pipe (M14-09).

Für MultiGPT ist der Runner ein MCP-Server (Transport ``runner``). Je ``pipe``
läuft hier ein Low-Level-``Server`` des MCP-SDK über Memory-Streams; die
JSON-RPC-Nachrichten kommen und gehen als Rahmen ``mcp``. Der Server bietet dem
Modell seinen eigenen Katalog an (``multigpt/runner/tools.py``); ``tools/list``
dient nur dem Abgleich.

Alle Werkzeuge laufen im Container der Sitzung: Dateien über ``mgpt-fs`` (Pfade
nur unter ``/workspace``, kein Symlink hinaus), Befehle in einem PTY mit
Zeitlimit und Ausgabegrenze. Nach Werkzeugen, die Dateien ändern können, folgt
ein Commit mit neuem Bundle (8r).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.shared._context_streams import create_context_streams
from mcp.shared.message import SessionMessage

from runner_agent import VERSION, protocol, template
from runner_agent.engine import EngineError
from runner_agent.session import Session, SessionError, SessionManager, clean_output

logger = logging.getLogger("runner_agent")

BASH_TIMEOUT_DEFAULT = 120
BASH_TIMEOUT_MAX = 600
PUBLISH_MAX_BYTES = 20 * 1024 * 1024
TELNET_QUIET = 1.0  # Sekunden ohne Ausgabe = Antwort vollständig
TELNET_MAX_WAIT = 20
SSH_DIR = "/tmp/.mgpt-ssh"
KEY_LINE_RE = re.compile(r"^[a-z0-9@.-]+ [A-Za-z0-9+/=]+$")


def _schema(properties: dict, required=()) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


_PATH = {"type": "string"}
_HOST = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "address": {"type": "string"},
        "port": {"type": "integer"},
        "user": {"type": "string"},
        "host_key": {"type": "string"},
        "secret_type": {"type": "string", "enum": ["key", "password", "none"]},
        "secret": {"type": "string"},
    },
    "required": ["name", "address", "port"],
}

TOOLS = [
    types.Tool(
        name="read",
        description="Datei lesen (mit Zeilennummern).",
        input_schema=_schema(
            {"path": _PATH, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, ["path"]
        ),
    ),
    types.Tool(
        name="write",
        description="Datei anlegen oder überschreiben.",
        input_schema=_schema({"path": _PATH, "content": {"type": "string"}}, ["path", "content"]),
    ),
    types.Tool(
        name="edit",
        description="Eindeutige Textstelle ersetzen.",
        input_schema=_schema(
            {
                "path": _PATH,
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean"},
            },
            ["path", "old_string", "new_string"],
        ),
    ),
    types.Tool(
        name="glob",
        description="Dateien nach Muster finden.",
        input_schema=_schema({"pattern": {"type": "string"}, "path": _PATH}, ["pattern"]),
    ),
    types.Tool(
        name="grep",
        description="Volltextsuche mit regulärem Ausdruck.",
        input_schema=_schema(
            {
                "pattern": {"type": "string"},
                "path": _PATH,
                "glob": {"type": "string"},
                "ignore_case": {"type": "boolean"},
                "max_results": {"type": "integer"},
            },
            ["pattern"],
        ),
    ),
    types.Tool(
        name="bash",
        description="Shell-Befehl im PTY ausführen (Zeitlimit, Ausgabe gekürzt).",
        input_schema=_schema(
            {"command": {"type": "string"}, "timeout_s": {"type": "integer"}}, ["command"]
        ),
    ),
    types.Tool(
        name="serve",
        description="Vorschau-Server für ein Verzeichnis starten.",
        input_schema=_schema({"path": _PATH}),
    ),
    types.Tool(
        name="publish",
        description="Datei aus /scratch oder /workspace als Anhang an MultiGPT schicken.",
        input_schema=_schema(
            {
                "path": _PATH,
                "name": {"type": "string"},
                "transfer": {"type": "string"},
                "max_bytes": {"type": "integer"},
            },
            ["path", "transfer"],
        ),
    ),
    types.Tool(
        name="ssh",
        description="Befehl per SSH auf einem freigegebenen Host (Zugang vom Server).",
        input_schema=_schema({"host": _HOST, "command": {"type": "string"}}, ["host", "command"]),
    ),
    types.Tool(
        name="telnet",
        description="Eingabe per Telnet an einen freigegebenen Host (Zugang vom Server).",
        input_schema=_schema({"host": _HOST, "input": {"type": "string"}}, ["host", "input"]),
    ),
]
TOOL_NAMES = frozenset(tool.name for tool in TOOLS)
CHANGING = frozenset({"write", "edit", "bash"})


class ToolError(Exception):
    pass


def _str(args: dict, key: str, *, required: bool = True, empty: bool = False) -> str | None:
    value = args.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or (not empty and value == ""):
        raise ToolError(f"Argument „{key}“ fehlt oder ist kein Text.")
    return value


def _int(args: dict, key: str, low: int, high: int) -> int | None:
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ToolError(f"Argument „{key}“ muss eine Zahl zwischen {low} und {high} sein.")
    return value


def _summary(name: str, args: dict) -> str:
    """Commit-Nachricht (kurz, ohne Inhalte)."""
    if name in ("write", "edit"):
        return f"{name} {args.get('path', '')}"[:200]
    if name == "bash":
        command = " ".join(str(args.get("command", "")).split())
        return f"bash: {command[:150]}"
    return name


class Toolbox:
    """Werkzeuge einer Sitzung."""

    def __init__(self, manager: SessionManager, session: Session):
        self.manager = manager
        self.session = session

    async def call(self, name: str, args: dict) -> tuple[str, dict | None]:
        """Werkzeug ausführen; liefert (Text, structuredContent)."""
        if name not in TOOL_NAMES:
            raise ToolError(f"Unbekanntes Werkzeug: {name}")
        if not isinstance(args, dict):
            raise ToolError("Argumente müssen ein Objekt sein.")
        session = self.session
        async with session.lock:
            self.manager.touch(session)
            if name in ("write", "edit") and self.manager.scratch_full(session):
                raise ToolError(
                    f"Scratchpad über der Grenze von {session.scratch.size_mb} MB: Schreiben "
                    "gesperrt. Bitte zuerst Dateien in /scratch löschen (bash: rm)."
                )
            try:
                text, structured = await getattr(self, f"tool_{name}")(args)
            finally:
                self.manager.touch(session)
            if name in CHANGING:
                text += await self.manager.save(session, _summary(name, args))
                text += self.manager.scratch_note(session)
        return text, structured

    # --- Dateien ---------------------------------------------------------------------

    async def tool_read(self, args):
        result = await self.manager.fs(
            self.session,
            "read",
            path=_str(args, "path"),
            offset=_int(args, "offset", 1, 10**9),
            limit=_int(args, "limit", 1, 100_000),
        )
        text = result["content"] or "(leere Datei bzw. keine Zeilen in diesem Bereich)"
        if result["to"] < result["lines"] or result["from"] > 1:
            text += f"\n[Zeilen {result['from']}–{result['to']} von {result['lines']}]"
        if result.get("truncated"):
            text += "\n[Datei nur teilweise gelesen (über 2 MB).]"
        return text, None

    async def tool_write(self, args):
        result = await self.manager.fs(
            self.session,
            "write",
            path=_str(args, "path"),
            content=_str(args, "content", empty=True),
        )
        verb = "angelegt" if result["created"] else "überschrieben"
        return f"{result['path']} {verb} ({result['bytes']} Byte).", None

    async def tool_edit(self, args):
        result = await self.manager.fs(
            self.session,
            "edit",
            path=_str(args, "path"),
            old_string=_str(args, "old_string"),
            new_string=_str(args, "new_string", empty=True),
            replace_all=bool(args.get("replace_all")),
        )
        return f"{result['path']}: {result['replacements']} Stelle(n) ersetzt.", None

    async def tool_glob(self, args):
        result = await self.manager.fs(
            self.session,
            "glob",
            pattern=_str(args, "pattern"),
            path=_str(args, "path", required=False),
        )
        if not result["paths"]:
            return "Keine Treffer.", None
        text = "\n".join(result["paths"])
        if result["total"] > len(result["paths"]):
            text += f"\n[{result['total']} Treffer, gezeigt {len(result['paths'])}]"
        return text, None

    async def tool_grep(self, args):
        result = await self.manager.fs(
            self.session,
            "grep",
            pattern=_str(args, "pattern"),
            path=_str(args, "path", required=False),
            glob=_str(args, "glob", required=False),
            ignore_case=bool(args.get("ignore_case")),
            max_results=_int(args, "max_results", 1, 1000),
        )
        if not result["matches"]:
            return "Keine Treffer.", None
        text = "\n".join(result["matches"])
        if result["total"] > len(result["matches"]):
            text += f"\n[{result['total']} Treffer, gezeigt {len(result['matches'])}]"
        return text, None

    # --- Befehle ---------------------------------------------------------------------

    async def run_pty(
        self,
        argv: list[str],
        timeout: int,
        label: str,
        env_prefix: list[str] | None = None,
        container: str | None = None,
    ) -> tuple[str, int | None, bool]:
        """Befehl im PTY ausführen (Arbeits- oder Hilfscontainer), streamen, gekürzt liefern."""
        session = self.session
        session.run_counter += 1
        pty_id = f"{label}-{session.run_counter}"
        wrapped = ["timeout", "-k", "5", str(timeout), *(env_prefix or []), *argv]
        handle = await self.manager.engine.exec_pty(
            container or session.container,
            wrapped,
            120,
            40,
            workdir="/tmp" if container else "/workspace",
        )
        session.ptys[pty_id] = handle
        collected = bytearray()
        try:
            code, truncated = await asyncio.wait_for(
                self.manager.pump(session, pty_id, handle, collected, session.output_bytes),
                timeout + 15,
            )
        except TimeoutError:
            code, truncated = 124, False
        finally:
            with contextlib.suppress(Exception):
                await handle.close()
        return clean_output(bytes(collected)), code, truncated

    async def tool_bash(self, args):
        command = _str(args, "command")
        limit = min(BASH_TIMEOUT_MAX, self.session.command_seconds)
        timeout = _int(args, "timeout_s", 1, BASH_TIMEOUT_MAX) or BASH_TIMEOUT_DEFAULT
        timeout = min(timeout, limit)
        limit = self.manager.scratch_limit(self.session)
        if limit is not None:
            # Lokales Scratchpad ohne Quota: Dateigröße je Prozess auf den Rest begrenzen
            # (über der Grenze 0 – Löschen geht weiter, Schreiben nicht).
            used = self.manager.scratch_usage(self.session) or 0
            # Nicht über die harte Grenze der Vorlage (fsize = tmpfs-Größe) hinaus.
            rest = min(max(0, limit - used), self.session.limits.tmpfs_mb << 20)
            command = f"ulimit -f {rest // 1024}; {command}"
        output, code, truncated = await self.run_pty(
            ["bash", "--noprofile", "--norc", "-c", command], timeout, "run"
        )
        text = output.rstrip("\n") or "(keine Ausgabe)"
        if truncated:
            text += f"\n[Ausgabe nach {self.session.output_bytes} Byte gekürzt]"
        if code == 124:
            text += f"\n[Zeitlimit von {timeout} s erreicht, Befehl abgebrochen]"
        text += f"\n[Exit-Code {code if code is not None else 'unbekannt'}]"
        return text, {"exit_code": code, "truncated": truncated}

    async def tool_serve(self, args):
        info = await self.manager.start_preview(self.session, _str(args, "path", required=False))
        text = (
            f"Vorschau läuft: {info['url']}\n"
            "Nur statische Dateien; Verzeichnisse liefern index.html, keine Listen."
        )
        return text, info

    async def tool_publish(self, args):
        """Datei per ``file_chunk`` an den Server (Übertragungs-ID gibt der Server vor)."""
        transfer = _str(args, "transfer")
        if not protocol.ID_RE.match(transfer):
            raise ToolError("Übertragungs-ID ungültig.")
        max_bytes = _int(args, "max_bytes", 1, protocol.MAX_BUNDLE_BYTES) or PUBLISH_MAX_BYTES
        meta, data = await self.manager._fs(
            self.session, "file_out", path=_str(args, "path"), max_bytes=max_bytes
        )
        name = _str(args, "name", required=False) or meta["name"]
        name = name.replace("/", "_").replace("\\", "_").strip()[:255] or "datei"
        mime = meta.get("mime") if protocol.MIME_RE.match(meta.get("mime") or "") else None
        parts = protocol.split_bundle(data, self.manager.chunk_bytes)
        for seq, part, last in parts:
            await self.manager.send(
                "file_chunk",
                session=self.session.id,
                transfer=transfer,
                seq=seq,
                data=protocol.b64encode(part),
                last=last,
                size=len(data) if last else None,
                sha256=meta["sha256"] if last else None,
                name=name if last else None,
                mime=mime if last else None,
            )
        info = {"transfer": transfer, "size": len(data), "sha256": meta["sha256"], "name": name}
        return f"{meta['path']} als „{name}“ übertragen ({len(data)} Byte).", info

    # --- ssh und telnet (8v, 8w) ----------------------------------------------------------

    def _target(self, args) -> tuple[dict, int]:
        host = args.get("host")
        if not isinstance(host, dict):
            raise ToolError("Host fehlt (der Server setzt den Zugang ein).")
        address, port = host.get("address"), host.get("port")
        if not isinstance(address, str) or not isinstance(port, int):
            raise ToolError("Host ohne Adresse oder Port.")
        if self.session.limits.network != "allowlist" or not self.session.relay_ip:
            raise ToolError("Für diese Sitzung ist kein Netz zu freigegebenen Hosts eingerichtet.")
        try:
            index = self.session.relay_targets.index((address, port))
        except ValueError:
            raise ToolError("Host steht nicht in der Positivliste dieser Sitzung.") from None
        return host, template.RELAY_BASE_PORT + index

    async def _put_secret_files(self, helper: str, files: dict[str, str]) -> None:
        """Zugang nur im Hilfscontainer ablegen (dessen /tmp, 0600), nie im Arbeitscontainer."""
        script = ["umask 077", f"mkdir -p {SSH_DIR}"]
        payload = b""
        for name, content in files.items():
            data = content.encode()
            script.append(f"head -c {len(data)} > {SSH_DIR}/{name}")
            payload += data
        if "askpass" in files:
            script.append(f"chmod 700 {SSH_DIR}/askpass")
        result = await self.manager.engine.exec(
            helper, ["sh", "-c", " && ".join(script)], stdin=payload, timeout=30, workdir="/tmp"
        )
        if result.code != 0:
            raise ToolError("Zugang konnte im Hilfscontainer nicht abgelegt werden.")

    async def tool_ssh(self, args):
        host, relay_port = self._target(args)
        command = _str(args, "command")
        host_key = str(host.get("host_key") or "").strip()
        if not KEY_LINE_RE.match(host_key):
            raise ToolError("Host-Schlüssel fehlt; ohne festgehaltenen Schlüssel kein SSH (8v).")
        user = str(host.get("user") or "")
        if not re.match(r"^[A-Za-z0-9._-]{1,64}$", user):
            raise ToolError("Benutzername ungültig.")
        secret_type = host.get("secret_type") or "none"
        secret = host.get("secret") or ""
        files = {"known_hosts": f"mgpt-host {host_key}\n"}
        options = [
            "-F", "/dev/null",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={SSH_DIR}/known_hosts",
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "HostKeyAlias=mgpt-host",
            "-o", "UpdateHostKeys=no",
            "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=15",
            "-o", "LogLevel=ERROR",
            "-o", "ForwardAgent=no",
            "-o", "ForwardX11=no",
            "-o", "ClearAllForwardings=yes",
            "-o", "IdentitiesOnly=yes",
            "-o", "NumberOfPasswordPrompts=1",
        ]  # fmt: skip
        env = []
        if secret_type == "key":
            files["key"] = secret if secret.endswith("\n") else secret + "\n"
            options += ["-o", f"IdentityFile={SSH_DIR}/key", "-o", "PasswordAuthentication=no",
                        "-o", "KbdInteractiveAuthentication=no", "-o", "BatchMode=yes"]  # fmt: skip
        elif secret_type == "password":
            files["pw"] = secret
            files["askpass"] = f"#!/bin/sh\ncat {SSH_DIR}/pw\n"
            options += ["-o", "PreferredAuthentications=password,keyboard-interactive",
                        "-o", "PubkeyAuthentication=no"]  # fmt: skip
            env = ["env", f"SSH_ASKPASS={SSH_DIR}/askpass", "SSH_ASKPASS_REQUIRE=force",
                   "DISPLAY=none"]  # fmt: skip
        else:
            options += ["-o", "BatchMode=yes"]
        helper = await self.manager.open_helper(self.session)
        try:
            await self._put_secret_files(helper, files)
            argv = [
                "ssh", *options, "-p", str(relay_port), "-l", user, self.session.relay_ip,
                "--", command,
            ]  # fmt: skip
            timeout = min(self.session.command_seconds, BASH_TIMEOUT_MAX)
            output, code, truncated = await self.run_pty(
                argv, timeout, "ssh", env, container=helper
            )
        finally:
            await self.manager.close_helper(helper)
        if secret:
            output = output.replace(secret, "****")
        text = output.rstrip("\n") or "(keine Ausgabe)"
        if truncated:
            text += f"\n[Ausgabe nach {self.session.output_bytes} Byte gekürzt]"
        if code == 255:
            text += "\n[SSH-Verbindung fehlgeschlagen (Host-Schlüssel, Zugang oder Netz)]"
        text += f"\n[Exit-Code {code if code is not None else 'unbekannt'}]"
        return text, {"exit_code": code}

    async def tool_telnet(self, args):
        host, relay_port = self._target(args)
        data = _str(args, "input", empty=True)
        name = str(host.get("name") or host["address"])
        conn = self.session.telnet.get(name)
        if conn is None or conn.closed:
            conn = TelnetConnection(self, relay_port)
            await conn.open()
            self.session.telnet[name] = conn
            output = await conn.login(host)
        else:
            output = ""
        output += await conn.send(data)
        text = clean_output(output.encode()).strip("\n") or "(keine Ausgabe)"
        if conn.closed:
            text += "\n[Telnet-Verbindung beendet]"
        return text, None


class TelnetConnection:
    """Offene Telnet-Sitzung im PTY des Containers (bleibt bis Ende/Leerlauf offen)."""

    def __init__(self, box: Toolbox, relay_port: int):
        self.box = box
        self.relay_port = relay_port
        self.handle = None
        self.pty_id = ""
        self.buffer = bytearray()
        self.event = asyncio.Event()
        self.closed = False
        self.task: asyncio.Task | None = None
        self.helper: str | None = None

    async def open(self) -> None:
        """Telnet im eigenen Hilfscontainer (nur am Relay-Netz), nie im Arbeitscontainer."""
        session = self.box.session
        session.run_counter += 1
        self.pty_id = f"telnet-{session.run_counter}"
        self.helper = await self.box.manager.open_helper(session)
        try:
            self.handle = await self.box.manager.engine.exec_pty(
                self.helper, ["telnet", session.relay_ip, str(self.relay_port)], 120, 40,
                workdir="/tmp",
            )  # fmt: skip
        except Exception:
            await self.box.manager.close_helper(self.helper)
            raise
        session.ptys[self.pty_id] = self.handle
        self.task = asyncio.create_task(self._reader())

    async def _reader(self) -> None:
        manager, session = self.box.manager, self.box.session
        seq = 0
        try:
            while True:
                data = await self.handle.read()
                if not data:
                    break
                self.buffer.extend(data)
                self.event.set()
                await manager.send(
                    "pty_data",
                    session=session.id,
                    pty=self.pty_id,
                    seq=seq,
                    data=protocol.b64encode(data[: protocol.PTY_BYTES]),
                )
                seq += 1
        finally:
            self.closed = True
            self.event.set()
            session.ptys.pop(self.pty_id, None)
            if self.helper is not None:
                await manager.close_helper(self.helper)
            with contextlib.suppress(Exception):
                await manager.send("pty_exit", session=session.id, pty=self.pty_id)

    async def _collect(self, until: re.Pattern | None = None, max_wait: float = TELNET_MAX_WAIT):
        """Ausgabe sammeln, bis ``until`` passt bzw. TELNET_QUIET lang Ruhe ist."""
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline and not self.closed:
            self.event.clear()
            if until is not None and until.search(self.buffer.decode("utf-8", "replace")):
                break
            try:
                await asyncio.wait_for(self.event.wait(), TELNET_QUIET)
            except TimeoutError:
                if until is None:
                    break
        out = self.buffer.decode("utf-8", errors="replace")
        self.buffer.clear()
        limit = self.box.session.output_bytes
        return out[-limit:]

    async def login(self, host: dict) -> str:
        user, secret = host.get("user") or "", host.get("secret") or ""
        if not user:
            return await self._collect()
        out = await self._collect(re.compile(r"(login|user(name)?)\s*:\s*$", re.I | re.M), 15)
        await self.handle.write(user.encode() + b"\r\n")
        if host.get("secret_type") == "password" and secret:
            out += await self._collect(re.compile(r"pass(word)?\s*:\s*$", re.I | re.M), 15)
            await self.handle.write(secret.encode() + b"\r\n")
            # Passwort-Echo nie zurückgeben
            out += (await self._collect()).replace(secret, "****")
        return out

    async def send(self, data: str) -> str:
        if self.closed:
            return ""
        await self.handle.write(data.encode() + b"\r\n")
        return await self._collect()

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
        if self.handle is not None:
            with contextlib.suppress(Exception):
                await self.handle.close()
        if self.helper is not None:
            await self.box.manager.close_helper(self.helper)
        self.closed = True


# --- MCP je pipe ---------------------------------------------------------------------------


def build_server(box: Toolbox) -> Server:
    async def list_tools(ctx, params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=TOOLS)

    async def call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        started = time.monotonic()
        try:
            text, structured = await box.call(params.name, dict(params.arguments or {}))
            error = False
        except (ToolError, SessionError) as exc:
            text, structured, error = str(getattr(exc, "message", None) or exc), None, True
        except EngineError as exc:
            text, structured, error = f"Container-Fehler: {exc}", None, True
        logger.info(
            "Werkzeug %s in Sitzung %s: %s (%.1f s)",
            params.name,
            box.session.id,
            "Fehler" if error else "ok",
            time.monotonic() - started,
        )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)],
            structured_content=structured,
            is_error=error,
        )

    return Server(
        "multigpt-runner",
        version=VERSION,
        instructions="Arbeitsumgebung im Container; Dateien unter /workspace.",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


class McpPipe:
    """Eine MCP-Verbindung (pipe) einer Sitzung über den Rückkanal."""

    def __init__(self, manager: SessionManager, session: Session, pipe: str):
        self.manager = manager
        self.session = session
        self.pipe = pipe
        self.server = build_server(Toolbox(manager, session))
        self._in_send, self._in_recv = create_context_streams[SessionMessage | Exception](32)
        self._out_send, self._out_recv = create_context_streams[SessionMessage](32)
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(self._writer)
                await self.server.run(
                    self._in_recv,
                    self._out_send,
                    self.server.create_initialization_options(),
                )
                tg.cancel_scope.cancel()
        except Exception:  # noqa: BLE001 - pipe endet, Agent läuft weiter
            logger.exception("MCP-pipe %s beendet", self.pipe)
        finally:
            self.session.pipes.pop(self.pipe, None)

    async def _writer(self) -> None:
        async with self._out_recv:
            async for message in self._out_recv:
                data = json.loads(
                    message.message.model_dump_json(by_alias=True, exclude_unset=True)
                )
                try:
                    await self.manager.send(
                        "mcp", session=self.session.id, pipe=self.pipe, message=data
                    )
                except protocol.ProtocolError as exc:
                    logger.warning("MCP-Antwort nicht sendbar: %s", exc.message)

    async def feed(self, message: dict) -> None:
        try:
            parsed = types.jsonrpc_message_adapter.validate_python(message, by_name=False)
        except Exception as exc:  # noqa: BLE001 - als Fehler an den Server
            await self._in_send.send(exc)
            return
        await self._in_send.send(SessionMessage(parsed))

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._in_send.aclose()
        if self.task is not None:
            self.task.cancel()


async def on_mcp(manager: SessionManager, frame: dict) -> None:
    session = manager.get(frame["session"])
    pipe = session.pipes.get(frame["pipe"])
    if pipe is None:
        pipe = McpPipe(manager, session, frame["pipe"])
        session.pipes[frame["pipe"]] = pipe
        pipe.start()
    manager.touch(session)
    await pipe.feed(frame["message"])
