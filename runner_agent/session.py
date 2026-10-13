"""Sitzungen: Arbeitscontainer, Netz, Arbeitsstand (git-Bundle), Terminal, Vorschau.

Eine Sitzung ist ein wegwerfbarer Arbeitscontainer mit tmpfs ``/workspace``.
Beim Start lädt der Agent das Bundle des Arbeitsstands vom Server und klont es
dort hinein; nach jeder Dateiänderung committet er und lädt ein neues Bundle
hoch (Entscheidungen vom 2026-10-13, 8r). Endet die Sitzung, bleibt auf dem
Rechner nichts übrig.

**Netz (8w, Pflicht 4 aus dem Gutachten):** Jeder Arbeitscontainer hängt an einem
eigenen Podman-Netz. Bei „aus“ und „Positivliste“ ist es intern (kein Gateway,
keine Route, kein DNS); nur so lässt sich der Vorschau-Port an 127.0.0.1
veröffentlichen, nach außen gibt es keinen Weg. Für die Positivliste startet der
Agent zusätzlich ein Relay (``mgpt-relay``) an einem eigenen internen Relay-Netz
und an einem Ausgang; es leitet nur zu den freigegebenen Zielen weiter. Der
Arbeitscontainer hängt nicht am Relay-Netz: ssh und telnet laufen in einem
kurzlebigen Hilfscontainer je Aufruf, der als einziger dort hängt und den Zugang
in seinem eigenen /tmp bekommt. So sieht das Modell weder Adresse noch Zugang.
Fällt das Relay aus, bleibt das Netz zu.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import secrets
import stat
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from runner_agent import protocol, template
from runner_agent.engine import Engine, EngineError, Pty

logger = logging.getLogger("runner_agent")

BUNDLE_TIMEOUT = 120  # Sekunden für Download bzw. Bestätigung eines Uploads
FS_TIMEOUT = 120
ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)|\x1b[@-Z\\-_]")

Send = Callable[..., Awaitable[None]]


class SessionError(Exception):
    """Fehler mit deutscher Meldung und Fehlercode des Protokolls."""

    def __init__(self, message: str, code: str = "internal"):
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass
class Session:
    id: str
    workspace: str
    spec: dict  # Werte aus session_start (ohne bundle)
    limits: template.Limits
    container: str
    state: str = "starting"
    preview_port: int | None = None
    preview_token: str | None = None
    preview_dir: str | None = None
    relay_ip: str | None = None
    relay_targets: list[tuple[str, int]] = field(default_factory=list)
    head: str | None = None
    last_activity: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    ptys: dict[str, Pty] = field(default_factory=dict)
    pty_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    pipes: dict = field(default_factory=dict)
    telnet: dict = field(default_factory=dict)
    run_counter: int = 0
    scratch: template.Scratch | None = None
    image: str | None = None
    scratch_used: int = 0
    scratch_local: str | None = None  # Ordner in Sicht des Agents (lokales Scratchpad)

    def info(self) -> dict:
        data = {"session": self.id, "workspace": self.workspace, "state": self.state}
        if self.preview_port is not None:
            data["preview_port"] = self.preview_port
        return data

    @property
    def command_seconds(self) -> int:
        return int(self.spec.get("command_seconds") or 120)

    @property
    def output_bytes(self) -> int:
        return int(self.spec.get("output_bytes") or 64 * 1024)


def clean_output(data: bytes) -> str:
    """PTY-Ausgabe für das Modell: ohne Steuerzeichen, Zeilenenden vereinheitlicht."""
    text = ANSI_RE.sub(b"", data).replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return text.decode("utf-8", errors="replace")


class SessionManager:
    """Alle Sitzungen dieses Agents.

    ``send(type, **felder)`` schickt einen Rahmen an den Server. ``server_images``
    ist die Image-Liste aus ``welcome``. ``clock`` (monoton) ist im Test ersetzbar.
    """

    def __init__(self, engine: Engine, config, send: Send, clock=time.monotonic):
        self.engine = engine
        self.config = config
        self.send = send
        self.clock = clock
        self.server_images: list[str] | None = None
        self.chunk_bytes = protocol.CHUNK_BYTES
        self.sessions: dict[str, Session] = {}
        self._downloads: dict[str, tuple[protocol.BundleAssembler, asyncio.Future]] = {}
        self._uploads: dict[str, asyncio.Future] = {}
        self._starting: set[str] = set()

    # --- Abgleich -------------------------------------------------------------------

    async def adopt(self) -> None:
        """Laufende Arbeitscontainer (z. B. nach Neustart des Agents) übernehmen.

        Container ohne lesbares Soll oder ohne laufenden Zustand werden entfernt;
        der Server gleicht beim ``hello`` ab und stoppt, was er nicht kennt.
        """
        try:
            containers = await self.engine.list_containers()
        except EngineError as exc:
            logger.warning("Abgleich nicht möglich: %s", exc)
            return
        for box in containers:
            labels = box.labels
            if labels.get(template.LABEL_ROLE) != "work":
                continue
            sid = labels.get(template.LABEL_SESSION, "")
            if sid in self.sessions:
                continue
            try:
                spec = json.loads(labels.get(template.LABEL_SPEC) or "")
                limits = template.Limits(**spec["limits"])
                scratch_spec = spec.get("scratch") or ["container", 1024, None, None]
                if not box.running or box.name != template.container_name(sid):
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                await self._cleanup(sid)
                continue
            port = labels.get(template.LABEL_PREVIEW)
            session = Session(
                id=sid,
                workspace=labels.get(template.LABEL_WORKSPACE, ""),
                spec=spec,
                limits=limits,
                container=box.name,
                state="running",
                preview_port=int(port) if port and port.isdigit() else None,
                last_activity=self.clock(),
                scratch=template.Scratch(*scratch_spec[:3]),
                scratch_local=scratch_spec[3],
                image=spec.get("image"),
            )
            if limits.network == "allowlist":
                session.relay_targets = [tuple(item) for item in spec.get("allow") or []]
                with contextlib.suppress(EngineError):
                    info = await self.engine.inspect_container(
                        template.container_name(sid, "relay")
                    )
                    nets = (info.get("NetworkSettings") or {}).get("Networks") or {}
                    net = nets.get(template.network_name(sid, "rel")) or {}
                    session.relay_ip = net.get("IPAddress") or None
            self.sessions[sid] = session

    def infos(self) -> list[dict]:
        return [session.info() for session in self.sessions.values()]

    def get(self, sid: str) -> Session:
        session = self.sessions.get(sid)
        if session is None or session.state != "running":
            raise SessionError("Sitzung unbekannt oder nicht gestartet.", "unknown_session")
        return session

    def touch(self, session: Session) -> None:
        session.last_activity = self.clock()

    # --- Start und Stopp ---------------------------------------------------------------

    async def start(self, frame: dict) -> None:
        sid = frame["session"]
        existing = self.sessions.get(sid)
        if existing is not None or sid in self._starting:
            if existing is not None:
                await self._report(existing)
            return
        self._starting.add(sid)
        try:
            session = await self._start(frame)
        except (SessionError, template.TemplateError, EngineError) as exc:
            message = getattr(exc, "message", None) or str(exc)
            logger.warning("Sitzung %s nicht gestartet: %s", sid, message)
            await self._cleanup(sid)
            self.sessions.pop(sid, None)
            await self.send("session_state", session=sid, state="failed", error=message[:1000])
            return
        finally:
            self._starting.discard(sid)
        await self._report(session)

    async def _report(self, session: Session) -> None:
        await self.send(
            "session_state",
            session=session.id,
            state=session.state,
            limits=session.limits.as_dict() if session.state == "running" else None,
            preview_port=session.preview_port,
        )

    async def _start(self, frame: dict) -> Session:
        sid, workspace = frame["session"], frame["workspace"]
        limits = template.Limits(
            cpus=float(frame["cpus"]),
            memory_mb=frame["memory_mb"],
            pids=frame["pids"],
            tmpfs_mb=frame["tmpfs_mb"],
            network=frame["network"],
        )
        template.check_image(frame["image"], self.server_images, self.config.images)
        template.check_limits(limits, self.config)
        for other in self.sessions.values():
            if other.workspace == workspace:
                raise SessionError("Arbeitsstand ist schon in einer Sitzung geöffnet.", "busy")
        allow = [(entry["host"], entry["port"]) for entry in frame.get("allow") or []]
        if limits.network == "allowlist" and not allow:
            raise SessionError("Positivliste ohne Einträge.", "not_allowed")
        scratch, scratch_local = self._scratch(frame)
        spec = {
            "image": frame["image"],
            "scratch": [scratch.mode, scratch.size_mb, scratch.source, scratch_local],
            "limits": limits.as_dict(),
            "command_seconds": frame["command_seconds"],
            "output_bytes": frame["output_bytes"],
            "idle_minutes": frame["idle_minutes"],
            "allow": [list(item) for item in allow],
        }
        session = Session(
            id=sid,
            workspace=workspace,
            spec=spec,
            limits=limits,
            container=template.container_name(sid),
            relay_targets=allow,
            last_activity=self.clock(),
            scratch=scratch,
            scratch_local=scratch_local,
            image=frame["image"],
        )
        self.sessions[sid] = session
        await self._create_network(sid, limits.network)
        if limits.network == "allowlist":
            session.relay_ip = await self._start_relay(sid, frame["image"], allow)
        await self._start_container(session, frame["image"], spec)
        info = await self.engine.inspect_container(session.container)
        problems = template.check_inspect(
            info, limits, session.preview_port, scratch.source if scratch.mode == "local" else None
        )
        if problems:
            raise SessionError("Abweichende Grenzen: " + "; ".join(problems), "not_allowed")
        session.limits = template.actual_limits(info, limits.network)
        await self._load_workspace(session, frame.get("bundle"))
        session.state = "running"
        return session

    def _labels(self, labels: dict) -> dict:
        """Zusätzliche Labels aus der Konfiguration; eigene Labels gehen vor."""
        return {**{str(k): str(v) for k, v in self.config.labels.items()}, **labels}

    async def _create_network(self, sid: str, mode: str) -> None:
        labels = self._labels({template.LABEL_SESSION: sid})
        if mode == "default":
            labels["multigpt.network"] = "default"
        await self.engine.create_network(
            template.network_body(
                template.network_name(sid, "net"), internal=mode != "default", labels=labels
            )
        )
        if mode == "allowlist":
            await self.engine.create_network(
                template.network_body(
                    template.network_name(sid, "rel"), internal=True, labels=labels
                )
            )
            await self.engine.create_network(
                template.network_body(
                    template.network_name(sid, "out"), internal=False, labels=labels
                )
            )

    async def _start_relay(self, sid: str, image: str, allow: list[tuple[str, int]]) -> str:
        body = template.relay_body(sid, image, allow)
        body["labels"] = self._labels(body["labels"])
        await self.engine.create_container(body)
        await self.engine.start_container(body["name"])
        info = await self.engine.inspect_container(body["name"])
        networks = (info.get("NetworkSettings") or {}).get("Networks") or {}
        net = networks.get(template.network_name(sid, "rel")) or {}
        address = net.get("IPAddress") or "relay"
        return address

    def _scratch(self, frame: dict) -> tuple[template.Scratch, str | None]:
        """Scratchpad der Sitzung: tmpfs oder ~/MultiGPT/Scratch/<uuid>/ des Rechners.

        Der Server nennt nur die UUID; den Ordner legt der Agent selbst an und prüft
        ihn (kein Symlink, direkt unter dem Scratch-Ordner).
        """
        mode = frame.get("scratch_mode") or "container"
        size_mb = int(frame.get("scratch_mb") or 1024)
        if mode != "local":
            return template.Scratch("container", min(size_mb, self.config.max_tmpfs_mb)), None
        uuid = frame.get("workspace_uuid")
        root, host = self.config.scratch_dir, self.config.scratch_host_dir
        if not (root and host):
            raise SessionError(
                "Lokales Scratchpad ist auf diesem Rechner nicht eingerichtet.", "not_allowed"
            )
        if not uuid or not template.UUID_RE.match(uuid):
            raise SessionError("Lokales Scratchpad ohne Arbeitsstand-UUID.", "not_allowed")
        try:
            info = os.lstat(root)
            if not stat.S_ISDIR(info.st_mode):
                raise SessionError("Scratch-Ordner ist kein Verzeichnis.", "not_allowed")
            target = os.path.join(root, uuid)
            try:
                os.mkdir(target, 0o700)
            except FileExistsError:
                pass
            if not stat.S_ISDIR(os.lstat(target).st_mode):
                raise SessionError("Scratch-Ordner ist kein Verzeichnis (Symlink?).", "not_allowed")
        except OSError as exc:
            raise SessionError(
                f"Scratch-Ordner nicht nutzbar ({exc.strerror}).", "engine"
            ) from None
        return template.Scratch("local", size_mb, f"{host}/{uuid}"), target

    def scratch_usage(self, session: Session) -> int | None:
        """Belegung des lokalen Scratchpads in Byte (wie ``du``); None ohne lokales."""
        if not session.scratch_local or session.scratch is None:
            return None
        total = 0
        for dirpath, _dirs, files in os.walk(session.scratch_local, followlinks=False):
            for name in files:
                with contextlib.suppress(OSError):
                    total += os.lstat(os.path.join(dirpath, name)).st_blocks * 512
        session.scratch_used = total
        return total

    def scratch_limit(self, session: Session) -> int | None:
        if not session.scratch_local or session.scratch is None:
            return None
        return session.scratch.size_mb << 20

    def scratch_full(self, session: Session) -> bool:
        """Lokales Scratchpad über der Grenze? (Ein Bind-Mount hat keine Quota.)"""
        used, limit = self.scratch_usage(session), self.scratch_limit(session)
        return used is not None and used >= limit

    def scratch_note(self, session: Session) -> str:
        """Hinweis, wenn das lokale Scratchpad über seiner Grenze liegt."""
        if not self.scratch_full(session):
            return ""
        return (
            f"\n[Scratchpad belegt {session.scratch_used >> 20} MB, Grenze "
            f"{session.scratch.size_mb} MB: Schreiben ist gesperrt, bis Dateien in /scratch "
            "gelöscht sind (bash: rm).]"
        )

    def _free_ports(self) -> list[int]:
        used = {s.preview_port for s in self.sessions.values() if s.preview_port}
        return [port for port in protocol.PREVIEW_PORTS if port not in used]

    async def _start_container(self, session: Session, image: str, spec: dict) -> None:
        net = template.network_name(session.id, "net")
        last_error = None
        for port in self._free_ports():
            body = template.work_body(
                session.id, session.workspace, image, session.limits, net, port, spec,
                session.scratch,
            )  # fmt: skip
            body["labels"] = self._labels(body["labels"])
            await self.engine.create_container(body)
            try:
                await self.engine.start_container(session.container)
            except EngineError as exc:
                last_error = exc
                await self.engine.remove_container(session.container)
                if "address already in use" in str(exc) or "bind" in str(exc):
                    continue
                raise
            session.preview_port = port
            return
        raise SessionError(
            f"Kein freier Vorschau-Port in 18080–18089 ({last_error or 'alle belegt'}).", "limit"
        )

    async def stop(self, sid: str, reason: str = "user") -> None:
        session = self.sessions.pop(sid, None)
        if session is not None:
            session.state = "stopping"
            await self.close_session_io(session)
        await self._cleanup(sid)
        await self.send("session_state", session=sid, state="stopped")

    async def close_session_io(self, session: Session) -> None:
        for task in list(session.pty_tasks.values()):
            task.cancel()
        for handle in list(session.ptys.values()):
            with contextlib.suppress(Exception):
                await handle.close()
        for conn in list(session.telnet.values()):
            with contextlib.suppress(Exception):
                await conn.close()
        for pipe in list(session.pipes.values()):
            with contextlib.suppress(Exception):
                await pipe.close()
        session.ptys.clear()
        session.pty_tasks.clear()
        session.telnet.clear()
        session.pipes.clear()

    async def open_helper(self, session: Session) -> str:
        """Hilfscontainer für einen ssh/telnet-Aufruf starten (nur am Relay-Netz)."""
        if not session.image:
            raise SessionError("Image der Sitzung unbekannt.", "internal")
        call = secrets.token_hex(8)
        body = template.tool_body(session.id, call, session.image)
        body["labels"] = self._labels(body["labels"])
        await self.engine.create_container(body)
        try:
            await self.engine.start_container(body["name"])
        except EngineError:
            await self.close_helper(body["name"])
            raise
        return body["name"]

    async def close_helper(self, name: str) -> None:
        with contextlib.suppress(EngineError):
            await self.engine.remove_container(name)

    async def _cleanup(self, sid: str) -> None:
        names = [template.container_name(sid), template.container_name(sid, "relay")]
        with contextlib.suppress(EngineError):
            for box in await self.engine.list_containers():
                if (
                    box.labels.get(template.LABEL_ROLE) == "tool"
                    and box.labels.get(template.LABEL_SESSION) == sid
                ):
                    names.append(box.name)
        for name in names:
            with contextlib.suppress(EngineError):
                await self.engine.remove_container(name)
        for kind in ("net", "rel", "out"):
            with contextlib.suppress(EngineError):
                await self.engine.remove_network(template.network_name(sid, kind))

    async def stop_all(self) -> None:
        for sid in list(self.sessions):
            with contextlib.suppress(Exception):
                await self.stop(sid, "replaced")

    async def check_idle(self) -> None:
        """Sitzungen ohne Aktivität seit ``idle_minutes`` stoppen (8k, Absicherung)."""
        now = self.clock()
        for session in list(self.sessions.values()):
            # Belegung des lokalen Scratchpads regelmäßig nachmessen.
            with contextlib.suppress(OSError):
                await asyncio.to_thread(self.scratch_usage, session)
            idle = int(session.spec.get("idle_minutes") or 15) * 60
            busy = session.lock.locked() or session.ptys
            if session.state == "running" and not busy and now - session.last_activity > idle:
                await self.stop(session.id, "idle")

    # --- mgpt-fs -----------------------------------------------------------------------

    async def fs(self, session: Session, op: str, payload: bytes = b"", **fields) -> dict:
        """``mgpt-fs`` im Container aufrufen; liefert die Antwort (ohne Rohdaten)."""
        limit = self.scratch_limit(session)
        if op in ("write", "edit") and limit is not None:
            fields["scratch_limit"] = limit  # mgpt-fs prüft vor dem Schreiben nach /scratch
        result, _data = await self._fs(session, op, payload, **fields)
        return result

    async def _fs(self, session: Session, op: str, payload: bytes = b"", **fields):
        request = json.dumps({"op": op, **fields}, ensure_ascii=False).encode() + b"\n" + payload
        max_output = protocol.MAX_BUNDLE_BYTES if op == "bundle_out" else 32 * 1024 * 1024
        res = await self.engine.exec(
            session.container,
            ["mgpt-fs"],
            stdin=request,
            timeout=FS_TIMEOUT,
            max_output=max_output,
        )
        if res.timed_out:
            raise SessionError("Zeitlimit im Container überschritten.", "timeout")
        line, _, rest = res.stdout.partition(b"\n")
        try:
            result = json.loads(line)
        except ValueError:
            err = res.stderr.decode("utf-8", errors="replace").strip()[-300:]
            raise SessionError(f"mgpt-fs antwortet nicht ({err or res.code}).", "engine") from None
        if not result.get("ok"):
            raise SessionError(str(result.get("error") or "Fehler in mgpt-fs."), "not_allowed")
        return result, rest

    # --- Arbeitsstand (git-Bundle) ------------------------------------------------------

    async def _load_workspace(self, session: Session, bundle: dict | None) -> None:
        if bundle is None:
            await self.fs(session, "init")
            session.head = None
            return
        data = await self.download(session, bundle)
        result = await self.fs(session, "bundle_in", data, size=len(data))
        session.head = result.get("head")

    async def download(self, session: Session, bundle: dict) -> bytes:
        transfer = "dl-" + secrets.token_hex(8)
        future = asyncio.get_running_loop().create_future()
        max_bytes = min(int(bundle["size"]), protocol.MAX_BUNDLE_BYTES)
        self._downloads[transfer] = (protocol.BundleAssembler(transfer, max_bytes), future)
        try:
            await self.send("bundle_get", session=session.id, transfer=transfer)
            data = await asyncio.wait_for(future, BUNDLE_TIMEOUT)
        except TimeoutError:
            raise SessionError("Arbeitsstand kam nicht rechtzeitig an.", "timeout") from None
        finally:
            self._downloads.pop(transfer, None)
        if len(data) != bundle["size"]:
            raise SessionError("Arbeitsstand hat die falsche Größe.", "bundle")
        return data

    async def on_bundle_chunk(self, frame: dict) -> None:
        entry = self._downloads.get(frame["transfer"])
        if entry is None:
            await self.send(
                "error",
                code="bundle",
                message="Unbekannte Übertragung.",
                session=frame["session"],
                ref=frame["transfer"],
            )
            return
        assembler, future = entry
        try:
            data = assembler.add(frame)
        except protocol.ProtocolError as exc:
            await self.send(
                "bundle_ack",
                session=frame["session"],
                transfer=frame["transfer"],
                ok=False,
                error=exc.message,
            )
            if not future.done():
                future.set_exception(SessionError(exc.message, "bundle"))
            return
        if data is not None:
            await self.send(
                "bundle_ack", session=frame["session"], transfer=frame["transfer"], ok=True
            )
            if not future.done():
                future.set_result(data)

    def on_bundle_ack(self, frame: dict) -> None:
        future = self._uploads.get(frame["transfer"])
        if future is not None and not future.done():
            future.set_result(frame)

    async def save(self, session: Session, message: str) -> str:
        """Nach einer Änderung: commit und neues Bundle hochladen (8r).

        Liefert einen kurzen Hinweis für das Werkzeugergebnis ("" ohne Änderung).
        """
        result = await self.fs(session, "commit", message=message)
        if not result.get("changed"):
            return ""
        meta, data = await self._fs(session, "bundle_out")
        head = meta.get("head")
        if not data:
            return ""
        transfer = "up-" + secrets.token_hex(8)
        future = asyncio.get_running_loop().create_future()
        self._uploads[transfer] = future
        try:
            for seq, part, last in protocol.split_bundle(data, self.chunk_bytes):
                await self.send(
                    "bundle_chunk",
                    session=session.id,
                    transfer=transfer,
                    seq=seq,
                    data=protocol.b64encode(part),
                    last=last,
                    size=len(data) if last else None,
                    sha256=meta.get("sha256") if last else None,
                    head=head if last else None,
                    message=message[: protocol.MAX_TEXT] if last else None,
                )
            ack = await asyncio.wait_for(future, BUNDLE_TIMEOUT)
        except TimeoutError:
            return "\n[Arbeitsstand: Server hat das Speichern nicht bestätigt.]"
        finally:
            self._uploads.pop(transfer, None)
        if not ack.get("ok"):
            return f"\n[Arbeitsstand nicht gespeichert: {ack.get('error') or 'abgelehnt'}]"
        session.head = ack.get("head") or head
        return ""

    # --- Terminal ------------------------------------------------------------------------

    async def pump(
        self,
        session: Session,
        pty_id: str,
        handle: Pty,
        collect: bytearray | None = None,
        limit: int = 0,
    ) -> tuple[int | None, bool]:
        """PTY-Ausgabe als ``pty_data`` streamen (und für das Modell sammeln).

        Liefert (Exit-Code, gekürzt).
        """
        seq, truncated = 0, False
        try:
            while True:
                data = await handle.read()
                if not data:
                    break
                self.touch(session)
                if collect is not None:
                    room = limit - len(collect)
                    if len(data) > room:
                        truncated = True
                    collect.extend(data[: max(room, 0)])
                for start in range(0, len(data), protocol.PTY_BYTES):
                    await self.send(
                        "pty_data",
                        session=session.id,
                        pty=pty_id,
                        seq=seq,
                        data=protocol.b64encode(data[start : start + protocol.PTY_BYTES]),
                    )
                    seq += 1
            code = await handle.wait()
        finally:
            session.ptys.pop(pty_id, None)
        await self.send("pty_exit", session=session.id, pty=pty_id, code=_exit(code))
        return code, truncated

    async def pty_open(self, frame: dict) -> None:
        session = self.get(frame["session"])
        pty_id = frame["pty"]
        if pty_id in session.ptys:
            raise SessionError("Terminal existiert schon.", "busy")
        handle = await self.engine.exec_pty(
            session.container,
            ["bash", "--noprofile", "--norc", "-i"],
            frame["cols"],
            frame["rows"],
        )
        session.ptys[pty_id] = handle
        self.touch(session)

        async def run():
            try:
                await self.pump(session, pty_id, handle)
            finally:
                session.pty_tasks.pop(pty_id, None)
                with contextlib.suppress(Exception):
                    await handle.close()

        session.pty_tasks[pty_id] = asyncio.create_task(run())

    async def pty_input(self, frame: dict) -> None:
        session = self.get(frame["session"])
        handle = session.ptys.get(frame["pty"])
        if handle is None:
            raise SessionError("Terminal unbekannt.", "unknown_session")
        self.touch(session)
        await handle.write(protocol.b64decode(frame["data"]))

    async def pty_resize(self, frame: dict) -> None:
        session = self.get(frame["session"])
        handle = session.ptys.get(frame["pty"])
        if handle is not None:
            await handle.resize(frame["cols"], frame["rows"])

    async def pty_close(self, frame: dict) -> None:
        session = self.get(frame["session"])
        handle = session.ptys.get(frame["pty"])
        if handle is not None:
            with contextlib.suppress(Exception):
                await handle.write(b"\x04")
                await handle.close()

    # --- Vorschau ------------------------------------------------------------------------

    async def start_preview(self, session: Session, path: str | None) -> dict:
        """Vorschau-Server im Container starten (8p/8x); liefert url, port, token."""
        directory = path or "."
        result = await self.fs(session, "glob", pattern="*", path=directory)
        del result  # nur Prüfung: Verzeichnis liegt unter /workspace
        if session.preview_port is None:
            raise SessionError("Für diese Sitzung ist kein Vorschau-Port frei.", "limit")
        token = session.preview_token or secrets.token_urlsafe(24)
        await self.engine.exec_detached(
            session.container,
            ["mgpt-serve", "--token", token, "--dir", directory, "--port", "8080"],
        )
        session.preview_token = token
        session.preview_dir = directory
        await self.send(
            "preview", session=session.id, running=True, port=session.preview_port, token=token
        )
        url = f"http://127.0.0.1:{session.preview_port}/{token}/"
        return {"url": url, "port": session.preview_port, "token": token}

    async def preview_request(self, frame: dict) -> None:
        """Vorschau über den Rückkanal (8p): Datei per mgpt-fs, nie Verzeichnislisten."""
        sid, request = frame["session"], frame["request"]
        session = self.sessions.get(sid)
        status, body, ctype = 404, b"", "text/plain; charset=utf-8"
        if session is not None and session.state == "running" and session.preview_dir:
            path = frame["path"]
            prefix = f"/{session.preview_token}/"
            if path.startswith(prefix):
                path = path[len(prefix) - 1 :]
            try:
                result = await self.fs(session, "fetch", dir=session.preview_dir, path=path)
            except SessionError:
                result = {"status": 500}
            status = int(result.get("status") or 500)
            if status == 200:
                body = base64.b64decode(result.get("data") or "")
                ctype = result.get("content_type") or "application/octet-stream"
            self.touch(session)
        if status != 200:
            body = f"{status}\n".encode()
        headers = {
            "Content-Type": ctype,
            "Content-Security-Policy": "sandbox allow-scripts allow-forms allow-modals; "
            "default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob:; connect-src 'none'",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        }
        if frame["method"] == "HEAD":
            headers["Content-Length"] = str(len(body))
            body = b""
        parts = [
            body[i : i + protocol.PREVIEW_BYTES]
            for i in range(0, len(body), protocol.PREVIEW_BYTES)
        ]
        parts = parts or [b""]
        for seq, part in enumerate(parts):
            await self.send(
                "preview_response",
                session=sid,
                request=request,
                seq=seq,
                status=status if seq == 0 else None,
                headers=headers if seq == 0 else None,
                data=protocol.b64encode(part) if part else None,
                last=seq == len(parts) - 1,
            )


def _exit(code: int | None) -> int | None:
    if code is None:
        return None
    return max(-255, min(255, int(code)))
