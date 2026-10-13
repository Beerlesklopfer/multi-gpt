"""Podman-Engine über die libpod-REST-API am Unix-Socket (M14-08).

Eigener kleiner Client statt eines SDK: JSON-Aufrufe mit ``httpx`` (``uds=``),
``exec`` mit übernommener Verbindung (Hijack) über einen rohen Unix-Socket, weil
``httpx`` keine bidirektionalen Ströme kann. Jeder Körper geht vorher durch die
feste Vorlage; gelöscht werden nur Objekte mit dem Label ``multigpt.runner=1``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import struct
from urllib.parse import quote

import httpx

from runner_agent import template
from runner_agent.engine import ContainerInfo, Engine, EngineError, EngineInfo, ExecResult, Pty

API = "/v5.0.0/libpod"


def _message(response: httpx.Response) -> str:
    try:
        data = response.json()
        text = data.get("message") or data.get("cause") or ""
    except ValueError:
        text = response.text
    return f"Podman {response.status_code}: {str(text)[:300]}"


class _Stream:
    """Übernommene HTTP-Verbindung eines ``exec`` (roh, bidirektional)."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, tty: bool):
        self.reader = reader
        self.writer = writer
        self.tty = tty
        self._buffer = b""

    async def read_frame(self) -> tuple[int, bytes]:
        """(Strom, Daten); Strom 1 = stdout, 2 = stderr; (0, b"") am Ende."""
        if self.tty:
            data = await self.reader.read(65536)
            return (1, data) if data else (0, b"")
        try:
            header = await self.reader.readexactly(8)
        except asyncio.IncompleteReadError:
            return 0, b""
        kind, size = header[0], struct.unpack(">I", header[4:])[0]
        try:
            data = await self.reader.readexactly(size)
        except asyncio.IncompleteReadError as exc:
            data = exc.partial
        return (kind if kind in (1, 2) else 1), data

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.writer.close()


class _PodmanPty(Pty):
    def __init__(self, engine: PodmanEngine, exec_id: str, stream: _Stream):
        self.engine = engine
        self.exec_id = exec_id
        self.stream = stream

    async def read(self) -> bytes:
        _kind, data = await self.stream.read_frame()
        return data

    async def write(self, data: bytes) -> None:
        self.stream.writer.write(data)
        with contextlib.suppress(ConnectionError):
            await self.stream.writer.drain()

    async def resize(self, cols: int, rows: int) -> None:
        await self.engine._request(
            "POST", f"/exec/{self.exec_id}/resize", params={"h": rows, "w": cols}, ok=(200, 201)
        )

    async def wait(self) -> int | None:
        return await self.engine._exit_code(self.exec_id)

    async def close(self) -> None:
        self.stream.close()


class PodmanEngine(Engine):
    def __init__(self, config, socket_path: str | None = None):
        super().__init__(config)
        self.socket_path = socket_path or config.podman_socket
        self.client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=self.socket_path),
            base_url="http://podman" + API,
            timeout=httpx.Timeout(60.0, connect=10.0),
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _request(self, method: str, path: str, *, ok=(200,), **kwargs) -> httpx.Response:
        try:
            response = await self.client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise EngineError(f"Podman nicht erreichbar: {exc.__class__.__name__}") from None
        if response.status_code not in ok:
            raise EngineError(_message(response))
        return response

    # --- allgemeine Aufrufe --------------------------------------------------------

    async def info(self) -> EngineInfo:
        data = (await self._request("GET", "/info")).json()
        host = data.get("host") or {}
        version = (data.get("version") or {}).get("Version") or "?"
        rootless = bool((host.get("security") or {}).get("rootless"))
        return EngineInfo("podman", str(version)[:64], rootless)

    async def create_network(self, body: dict) -> None:
        template.check_network_body(body)
        await self._request("POST", "/networks/create", json=body)

    async def remove_network(self, name: str) -> None:
        if not template.NETWORK_RE.match(name):
            raise EngineError("Fremdes Netz wird nicht angefasst.")
        response = await self._request("GET", f"/networks/{quote(name)}/json", ok=(200, 404))
        if response.status_code == 404:
            return
        labels = response.json().get("labels") or {}
        if labels.get(template.LABEL_RUNNER) != "1":
            raise EngineError("Fremdes Netz wird nicht angefasst.")
        await self._request("DELETE", f"/networks/{quote(name)}", ok=(200, 404))

    async def create_container(self, body: dict) -> None:
        template.check_body(body, self.config)
        await self._request("POST", "/containers/create", json=body, ok=(201,))

    async def start_container(self, name: str) -> None:
        await self._request("POST", f"/containers/{quote(name)}/start", ok=(204, 304))

    async def remove_container(self, name: str) -> None:
        if not template.NAME_RE.match(name):
            raise EngineError("Fremder Container wird nicht angefasst.")
        response = await self._request("GET", f"/containers/{quote(name)}/json", ok=(200, 404))
        if response.status_code == 404:
            return
        labels = (response.json().get("Config") or {}).get("Labels") or {}
        if labels.get(template.LABEL_RUNNER) != "1":
            raise EngineError("Fremder Container wird nicht angefasst.")
        await self._request(
            "DELETE",
            f"/containers/{quote(name)}",
            params={"force": "true", "v": "true", "timeout": 2},
            ok=(200, 204, 404),
        )

    async def inspect_container(self, name: str) -> dict:
        return (await self._request("GET", f"/containers/{quote(name)}/json")).json()

    async def list_containers(self) -> list[ContainerInfo]:
        filters = json.dumps({"label": [f"{template.LABEL_RUNNER}=1"]})
        data = (
            await self._request(
                "GET", "/containers/json", params={"all": "true", "filters": filters}
            )
        ).json()
        out = []
        for item in data or []:
            names = item.get("Names") or []
            labels = item.get("Labels") or {}
            if not names or labels.get(template.LABEL_RUNNER) != "1":
                continue
            out.append(ContainerInfo(names[0].lstrip("/"), labels, item.get("State") == "running"))
        return out

    # --- exec ----------------------------------------------------------------------

    async def _exec_create(
        self, name: str, argv: list[str], *, stdin: bool, tty: bool, workdir: str
    ) -> str:
        body = {
            "AttachStdin": stdin,
            "AttachStdout": True,
            "AttachStderr": True,
            "Cmd": argv,
            "Tty": tty,
            "WorkingDir": workdir,
            "User": template.USER,
            "Privileged": False,
        }
        response = await self._request(
            "POST", f"/containers/{quote(name)}/exec", json=body, ok=(201,)
        )
        return response.json()["Id"]

    async def _hijack(self, exec_id: str, tty: bool, cols: int = 80, rows: int = 24) -> _Stream:
        body = {"Detach": False, "Tty": tty}
        if tty:
            body.update({"h": rows, "w": cols})
        payload = json.dumps(body).encode()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(self.socket_path, limit=1 << 20), 10
            )
        except (OSError, TimeoutError):
            raise EngineError("Podman nicht erreichbar.") from None
        request = (
            f"POST {API}/exec/{exec_id}/start HTTP/1.1\r\n"
            "Host: podman\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Connection: Upgrade\r\n"
            "Upgrade: tcp\r\n\r\n"
        ).encode() + payload
        writer.write(request)
        await writer.drain()
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            writer.close()
            raise EngineError("exec: keine Antwort von Podman.") from None
        status_line = head.split(b"\r\n", 1)[0].decode("latin-1")
        parts = status_line.split(" ", 2)
        if len(parts) < 2 or parts[1] not in ("101", "200"):
            writer.close()
            raise EngineError(f"exec: {status_line[:200]}")
        return _Stream(reader, writer, tty)

    async def _exit_code(self, exec_id: str) -> int | None:
        for _ in range(50):
            data = (await self._request("GET", f"/exec/{exec_id}/json")).json()
            if not data.get("Running"):
                return data.get("ExitCode")
            await asyncio.sleep(0.1)
        return None

    async def exec(
        self,
        name: str,
        argv: list[str],
        *,
        stdin: bytes | None = None,
        timeout: float = 60,
        max_output: int = 64 * 1024,
        workdir: str = "/workspace",
    ) -> ExecResult:
        exec_id = await self._exec_create(
            name, argv, stdin=stdin is not None, tty=False, workdir=workdir
        )
        stream = await self._hijack(exec_id, tty=False)
        out, err, truncated = bytearray(), bytearray(), False

        async def pump():
            nonlocal truncated
            if stdin:
                stream.writer.write(stdin)
                await stream.writer.drain()
            while True:
                kind, data = await stream.read_frame()
                if kind == 0:
                    return
                target = out if kind == 1 else err
                room = max_output - len(target)
                if len(data) > room:
                    truncated = True
                target.extend(data[: max(room, 0)])

        try:
            await asyncio.wait_for(pump(), timeout)
        except TimeoutError:
            stream.close()
            return ExecResult(None, bytes(out), bytes(err), truncated, timed_out=True)
        except (ConnectionError, OSError) as exc:
            stream.close()
            raise EngineError(f"exec abgebrochen: {exc.__class__.__name__}") from None
        stream.close()
        code = await self._exit_code(exec_id)
        return ExecResult(code, bytes(out), bytes(err), truncated)

    async def exec_detached(self, name: str, argv: list[str], workdir: str = "/workspace") -> None:
        exec_id = await self._exec_create(name, argv, stdin=False, tty=False, workdir=workdir)
        await self._request(
            "POST", f"/exec/{exec_id}/start", json={"Detach": True, "Tty": False}, ok=(200, 204)
        )

    async def exec_pty(
        self,
        name: str,
        argv: list[str],
        cols: int = 80,
        rows: int = 24,
        workdir: str = "/workspace",
    ) -> Pty:
        exec_id = await self._exec_create(name, argv, stdin=True, tty=True, workdir=workdir)
        stream = await self._hijack(exec_id, tty=True, cols=cols, rows=rows)
        return _PodmanPty(self, exec_id, stream)
