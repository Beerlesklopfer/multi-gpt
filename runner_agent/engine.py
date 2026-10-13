"""Container-Engine des Agents: Schnittstelle und ``FakeEngine`` für Tests.

Die echte Engine (Podman-REST über den Unix-Socket) steht in ``podman.py``. Jede
Engine prüft Container- und Netzkörper vor dem Anlegen mit der festen Vorlage
(``template.check_body``/``check_network_body``) und fasst nur Objekte mit dem
Label ``multigpt.runner=1`` an.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import pty
import shutil
import signal
import struct
import sys
import tempfile
import termios
from dataclasses import dataclass, field

from runner_agent import template

HELPERS = {
    "mgpt-fs": "mgpt_fs.py",
    "mgpt-serve": "mgpt_serve.py",
    "mgpt-relay": "mgpt_relay.py",
}


class EngineError(Exception):
    """Fehler der Container-Engine (deutsche Meldung, ohne Geheimnisse)."""


@dataclass
class ExecResult:
    code: int | None
    stdout: bytes = b""
    stderr: bytes = b""
    truncated: bool = False
    timed_out: bool = False


@dataclass
class EngineInfo:
    name: str
    version: str
    rootless: bool


@dataclass
class ContainerInfo:
    name: str
    labels: dict = field(default_factory=dict)
    running: bool = False


class Pty:
    """Laufender Prozess in einem Pseudo-Terminal des Containers."""

    async def read(self) -> bytes:
        """Nächste Ausgabe; ``b""`` am Ende."""
        raise NotImplementedError

    async def write(self, data: bytes) -> None:
        raise NotImplementedError

    async def resize(self, cols: int, rows: int) -> None:
        raise NotImplementedError

    async def wait(self) -> int | None:
        """Exit-Code nach dem Ende."""
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


class Engine:
    """Schnittstelle (alle Methoden async)."""

    def __init__(self, config):
        self.config = config

    async def info(self) -> EngineInfo:
        raise NotImplementedError

    async def create_network(self, body: dict) -> None:
        raise NotImplementedError

    async def remove_network(self, name: str) -> None:
        raise NotImplementedError

    async def create_container(self, body: dict) -> None:
        raise NotImplementedError

    async def start_container(self, name: str) -> None:
        raise NotImplementedError

    async def remove_container(self, name: str) -> None:
        raise NotImplementedError

    async def inspect_container(self, name: str) -> dict:
        raise NotImplementedError

    async def list_containers(self) -> list[ContainerInfo]:
        """Alle Container mit dem Label ``multigpt.runner=1``."""
        raise NotImplementedError

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
        raise NotImplementedError

    async def exec_detached(self, name: str, argv: list[str], workdir: str = "/workspace") -> None:
        raise NotImplementedError

    async def exec_pty(
        self,
        name: str,
        argv: list[str],
        cols: int = 80,
        rows: int = 24,
        workdir: str = "/workspace",
    ) -> Pty:
        raise NotImplementedError


# --- FakeEngine ------------------------------------------------------------------


class _LocalPty(Pty):
    def __init__(self, proc: asyncio.subprocess.Process, master: int):
        self.proc = proc
        self.master = master
        self._closed = False

    async def read(self) -> bytes:
        loop = asyncio.get_running_loop()
        while True:
            try:
                return os.read(self.master, 65536)
            except BlockingIOError:
                fut = loop.create_future()
                loop.add_reader(self.master, fut.set_result, None)
                try:
                    await fut
                finally:
                    loop.remove_reader(self.master)
            except OSError:  # EIO: Kindprozess beendet
                return b""

    async def write(self, data: bytes) -> None:
        with contextlib.suppress(OSError):
            os.write(self.master, data)

    async def resize(self, cols: int, rows: int) -> None:
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    async def wait(self) -> int | None:
        return await self.proc.wait()

    async def close(self) -> None:
        if self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGKILL)
            await self.proc.wait()
        if not self._closed:
            self._closed = True
            os.close(self.master)


class FakeEngine(Engine):
    """Engine ohne Container für Tests.

    Jeder „Container“ ist ein Verzeichnis: ``exec`` führt den Befehl als lokalen
    Prozess aus, mit ``/workspace`` = eigenes Temp-Verzeichnis (über
    ``MGPT_WORKSPACE``). Die Hilfsprogramme ``mgpt-fs``/``mgpt-serve`` laufen aus
    diesem Paket. Alle Körper werden wie bei Podman gegen die Vorlage geprüft und
    in ``created`` festgehalten; ``inspect`` leitet die Ist-Werte daraus ab
    (``tamper`` verfälscht sie für Tests).
    """

    def __init__(self, config, root: str | None = None):
        super().__init__(config)
        self.root = root or tempfile.mkdtemp(prefix="mgpt-fake-")
        self.containers: dict[str, dict] = {}
        self.networks: dict[str, dict] = {}
        self.created: list[dict] = []
        self.removed: list[str] = []
        self.tamper: dict = {}  # Name → Änderungen an HostConfig
        self.fail_start: set[str] = set()
        self.busy_ports: set[int] = set()
        # Test: Programm (z. B. ssh) durch ein Shell-Skript ersetzen; argv bleibt in calls.
        self.fake_commands: dict[str, str] = {}
        self.calls: list[tuple[str, list[str]]] = []

    async def info(self) -> EngineInfo:
        return EngineInfo("fake", "0", True)

    async def create_network(self, body: dict) -> None:
        template.check_network_body(body)
        if body["name"] in self.networks:
            raise EngineError("Netz existiert schon.")
        self.networks[body["name"]] = body

    async def remove_network(self, name: str) -> None:
        self.networks.pop(name, None)

    async def create_container(self, body: dict) -> None:
        template.check_body(body, self.config)
        name = body["name"]
        if name in self.containers:
            raise EngineError("Container existiert schon.")
        for net in body["Networks"]:
            if net not in self.networks:
                raise EngineError(f"Netz fehlt: {net}")
        base = os.path.join(self.root, name)
        os.makedirs(os.path.join(base, "workspace"))
        os.makedirs(os.path.join(base, "tmp"))
        scratch = None
        for mount in body["mounts"]:
            if mount["destination"] == "/scratch":
                # Bind: im Test ist die Host-Sicht gleich der Sicht des Agents.
                scratch = mount["source"] if mount["type"] == "bind" else base + "/scratch"
                os.makedirs(scratch, exist_ok=True)
        self.containers[name] = {"body": body, "running": False, "dir": base, "scratch": scratch}
        self.created.append(body)

    async def start_container(self, name: str) -> None:
        box = self._get(name)
        if name in self.fail_start:
            raise EngineError("Start fehlgeschlagen.")
        for port in box["body"].get("portmappings", []):
            if port["host_port"] in self.busy_ports:
                raise EngineError("address already in use")
        box["running"] = True

    async def remove_container(self, name: str) -> None:
        box = self.containers.pop(name, None)
        if box is not None:
            self.removed.append(name)
            shutil.rmtree(box["dir"], ignore_errors=True)

    async def inspect_container(self, name: str) -> dict:
        box = self._get(name)
        body = box["body"]
        res = body["resource_limits"]
        tmpfs = {
            m["destination"]: ",".join(o for o in m["options"] if o != "U")
            for m in body["mounts"]
            if m["type"] == "tmpfs"
        }
        mounts = [
            {
                "Type": "bind",
                "Source": m["source"],
                "Destination": m["destination"],
                "Options": [o for o in m["options"] if o != "rw"],
            }
            for m in body["mounts"]
            if m["type"] == "bind"
        ]
        host = {
            "CpuQuota": res["cpu"]["quota"],
            "CpuPeriod": res["cpu"]["period"],
            "Memory": res["memory"]["limit"],
            "MemorySwap": res["memory"]["swap"],
            "PidsLimit": res["pids"]["limit"],
            "Tmpfs": tmpfs,
            "ReadonlyRootfs": body["read_only_filesystem"],
            "Privileged": body["privileged"],
            "CapAdd": [],
            "SecurityOpt": ["no-new-privileges"] if body["no_new_privileges"] else [],
            "NetworkMode": "bridge",
            "PidMode": "private",
            "IpcMode": "private",
            "UTSMode": "private",
            "Binds": [],
            "Devices": [],
            "PortBindings": {
                f"{p['container_port']}/tcp": [
                    {"HostIp": p["host_ip"], "HostPort": str(p["host_port"])}
                ]
                for p in body.get("portmappings", [])
            },
        }
        host.update(self.tamper.get(name, {}))
        return {
            "Name": name,
            "Config": {"User": body["user"], "Labels": body["labels"]},
            "HostConfig": host,
            "Mounts": mounts,
            "State": {
                "Running": box["running"],
                "Status": "running" if box["running"] else "created",
            },
            "NetworkSettings": {"Networks": {n: {} for n in body["Networks"]}},
        }

    async def list_containers(self) -> list[ContainerInfo]:
        return [
            ContainerInfo(name, dict(box["body"]["labels"]), box["running"])
            for name, box in self.containers.items()
        ]

    def _get(self, name: str) -> dict:
        try:
            return self.containers[name]
        except KeyError:
            raise EngineError(f"Container nicht gefunden: {name}") from None

    def workspace(self, name: str) -> str:
        return os.path.join(self._get(name)["dir"], "workspace")

    def _local(self, name: str, argv: list[str], workdir: str):
        box = self._get(name)
        if not box["running"]:
            raise EngineError("Container läuft nicht.")
        ws = os.path.join(box["dir"], "workspace")
        tmp = os.path.join(box["dir"], "tmp")
        # Zugangsdateien (ssh) im tmp des „Containers“, nie im /tmp des Rechners.
        tmp_dir = os.path.join(box["dir"], "tmp")
        argv = [arg.replace("/tmp/.mgpt-ssh", tmp_dir + "/.mgpt-ssh") for arg in argv]
        self.calls.append((name, list(argv)))
        for token in argv:
            if token in self.fake_commands:
                argv = ["sh", "-c", self.fake_commands[token]]
                break
        helper = HELPERS.get(argv[0])
        if helper:
            argv = [sys.executable, os.path.join(os.path.dirname(__file__), helper), *argv[1:]]
        env = {
            **template.ENV,
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": tmp,
            "MGPT_WORKSPACE": ws,
            "MGPT_TMP": tmp,
            "MGPT_SCRATCH": box["scratch"] or os.path.join(box["dir"], "kein-scratch"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
        cwd = ws if workdir.startswith("/workspace") else tmp
        return argv, env, cwd

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
        argv, env, cwd = self._local(name, argv, workdir)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
            start_new_session=True,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
            return ExecResult(None, timed_out=True)
        truncated = len(out) > max_output or len(err) > max_output
        return ExecResult(proc.returncode, out[:max_output], err[:max_output], truncated)

    async def exec_detached(self, name: str, argv: list[str], workdir: str = "/workspace") -> None:
        # Nur festhalten: Hintergrunddienste (Vorschau) laufen im Test nicht wirklich.
        self._local(name, argv, workdir)
        self._get(name).setdefault("detached", []).append(list(argv))

    async def exec_pty(
        self,
        name: str,
        argv: list[str],
        cols: int = 80,
        rows: int = 24,
        workdir: str = "/workspace",
    ) -> Pty:
        argv, env, cwd = self._local(name, argv, workdir)
        master, slave = pty.openpty()
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        os.set_blocking(master, False)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            cwd=cwd,
            start_new_session=True,
        )
        os.close(slave)
        return _LocalPty(proc, master)

    async def shutdown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
