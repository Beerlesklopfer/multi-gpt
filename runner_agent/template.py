"""Feste Vorlage für Container (Entscheidung 8g, M14-09).

Der Podman-Socket ist so mächtig wie der Nutzer des Rechners. Der Agent baut die
Container-Optionen deshalb selbst aus einem festen Schema (``work_body``,
``relay_body``); der Server liefert nur Werte, nie Optionen. Vor jedem Anlegen
prüft ``check_body`` den fertigen libpod-Körper noch einmal gegen die Vorlage –
auch ein Fehler im Agent kann so keine Mounts, Privilegien oder Host-Namespaces
durchreichen. Nach dem Start vergleicht ``check_inspect`` die tatsächlichen Werte
aus ``inspect`` mit dem Soll.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from runner_agent import protocol

LABEL_RUNNER = "multigpt.runner"
LABEL_SESSION = "multigpt.session"
LABEL_WORKSPACE = "multigpt.workspace"
LABEL_ROLE = "multigpt.role"  # work | relay
LABEL_SPEC = "multigpt.spec"  # Sollwerte der Sitzung (JSON), für den Abgleich
LABEL_PREVIEW = "multigpt.preview_port"
LABEL_CALL = "multigpt.call"  # Hilfscontainer: Kennung des Aufrufs

USER = "1000:1000"
PREVIEW_CONTAINER_PORT = 8080
RELAY_BASE_PORT = 20000
CPU_PERIOD = 100_000
TMP_MB = 256  # /tmp (tmpfs), zusätzlich zu /workspace
NOFILE = 4096
ENV = {
    "HOME": "/tmp",
    "PATH": "/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin",
    "LANG": "C.UTF-8",
    "TERM": "xterm-256color",
}
NAME_RE = re.compile(r"^mgpt-(ses|rly|tol)-[0-9a-f]{16}$")
NETWORK_RE = re.compile(r"^mgpt-(net|out|rel)-[0-9a-f]{16}$")
TMPFS_OPTION_RE = re.compile(r"^(size=\d+[kmg]?|mode=[0-7]{3,4}|nosuid|nodev|noexec|U)$")
RELAY_TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:-]{0,252}:\d{1,5}$")

ALLOWED_KEYS = frozenset(
    {
        "name",
        "image",
        "command",
        "labels",
        "user",
        "hostname",
        "read_only_filesystem",
        "cap_drop",
        "no_new_privileges",
        "privileged",
        "init",
        "netns",
        "Networks",
        "mounts",
        "resource_limits",
        "r_limits",
        "env",
        "env_host",
        "httpproxy",
        "portmappings",
        "ipcns",
        "pidns",
        "utsns",
        "cgroupns",
        "image_volume_mode",
        "work_dir",
        "stop_timeout",
        "use_image_hosts",
        "use_image_resolve_conf",
        "userns",
    }
)
# Diese Werte sind fest; jede Abweichung wird abgelehnt.
FIXED = {
    "user": USER,
    "read_only_filesystem": True,
    "cap_drop": ["ALL"],
    "no_new_privileges": True,
    "privileged": False,
    "init": True,
    "env_host": False,
    "httpproxy": False,
    "ipcns": {"nsmode": "private"},
    "pidns": {"nsmode": "private"},
    "utsns": {"nsmode": "private"},
    "cgroupns": {"nsmode": "private"},
    "image_volume_mode": "ignore",
    "use_image_hosts": True,
}
LABEL_NETWORK = "multigpt.network"  # none | allowlist | default


KEEP_ID = {"nsmode": "keep-id", "value": "uid=1000,gid=1000"}
BIND_OPTIONS = ["rbind", "rw", "noexec", "nosuid", "nodev"]
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@dataclass(frozen=True)
class Scratch:
    """Scratchpad ``/scratch``: tmpfs (``container``) oder Ordner des Rechners (``local``)."""

    mode: str
    size_mb: int
    source: str | None = None  # Host-Pfad bei local

    def mount(self) -> dict:
        if self.mode == "local":
            return {
                "destination": "/scratch",
                "type": "bind",
                "source": self.source,
                "options": list(BIND_OPTIONS),
            }
        mount = _tmpfs("/scratch", self.size_mb, "0700", True)
        mount["options"].append("noexec")
        return mount


class TemplateError(Exception):
    """Befehl verletzt die feste Vorlage (wird nie ausgeführt)."""


@dataclass(frozen=True)
class Limits:
    cpus: float
    memory_mb: int
    pids: int
    tmpfs_mb: int
    network: str

    def as_dict(self) -> dict:
        return {
            "cpus": self.cpus,
            "memory_mb": self.memory_mb,
            "pids": self.pids,
            "tmpfs_mb": self.tmpfs_mb,
            "network": self.network,
        }


def short_id(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def container_name(session: str, role: str = "work", call: str | None = None) -> str:
    """Name je Rolle: Arbeitscontainer, Relay, Hilfscontainer je ssh/telnet-Aufruf."""
    if role == "tool":
        return f"mgpt-tol-{short_id(f'{session}#{call}')}"
    return f"mgpt-{'ses' if role == 'work' else 'rly'}-{short_id(session)}"


def network_name(session: str, kind: str = "net") -> str:
    return f"mgpt-{kind}-{short_id(session)}"


def check_image(image: str, server_images, local_images) -> None:
    """Image nur mit Digest, aus der Liste des Servers UND der lokalen Positivliste."""
    if not isinstance(image, str) or not protocol.IMAGE_RE.match(image):
        raise TemplateError("Image ohne Digest.")
    if image not in set(local_images):
        raise TemplateError("Image steht nicht in der lokalen Positivliste (MGPT_IMAGES).")
    if server_images is not None and image not in set(server_images):
        raise TemplateError("Image steht nicht in der Liste des Servers.")


def check_limits(limits: Limits, config) -> None:
    """Werte des Servers gegen die lokalen Obergrenzen des Agents prüfen."""
    if not 0.1 <= limits.cpus <= config.max_cpus:
        raise TemplateError(f"CPU-Grenze außerhalb 0,1…{config.max_cpus}.")
    if not 64 <= limits.memory_mb <= config.max_memory_mb:
        raise TemplateError(f"RAM-Grenze außerhalb 64…{config.max_memory_mb} MB.")
    if not 16 <= limits.pids <= config.max_pids:
        raise TemplateError(f"Prozessgrenze außerhalb 16…{config.max_pids}.")
    if not 16 <= limits.tmpfs_mb <= config.max_tmpfs_mb:
        raise TemplateError(f"tmpfs-Grenze außerhalb 16…{config.max_tmpfs_mb} MB.")
    if limits.network not in protocol.NETWORK_MODES:
        raise TemplateError("Unbekannte Netzart.")


def _tmpfs(destination: str, size_mb: int, mode: str, chown: bool) -> dict:
    options = [f"size={size_mb}m", f"mode={mode}", "nosuid", "nodev"]
    if chown:
        options.append("U")
    return {"destination": destination, "type": "tmpfs", "source": "tmpfs", "options": options}


def _base(name: str, image: str, labels: dict, limits: Limits) -> dict:
    memory = limits.memory_mb * 1024 * 1024
    return {
        "name": name,
        "image": image,
        "labels": labels,
        "hostname": "workspace",
        **json.loads(json.dumps(FIXED)),
        "resource_limits": {
            "cpu": {"quota": int(round(limits.cpus * CPU_PERIOD)), "period": CPU_PERIOD},
            "memory": {"limit": memory, "swap": memory},
            "pids": {"limit": limits.pids},
        },
        "r_limits": [
            {"type": "nofile", "soft": NOFILE, "hard": NOFILE},
            {"type": "fsize", "soft": limits.tmpfs_mb << 20, "hard": limits.tmpfs_mb << 20},
            {"type": "core", "soft": 0, "hard": 0},
        ],
        "env": dict(ENV),
        "stop_timeout": 5,
    }


def work_body(
    session: str,
    workspace: str,
    image: str,
    limits: Limits,
    network: str,
    preview_port: int | None,
    spec: dict,
    scratch: Scratch | None = None,
) -> dict:
    """Arbeitscontainer: nur tmpfs /workspace und /tmp, Netz nur über ``network``.

    ``network`` ist das Podman-Netz der Sitzung (intern ohne Gateway bei „aus“
    und Positivliste, mit Gateway bei „default“).
    """
    labels = {
        LABEL_RUNNER: "1",
        LABEL_ROLE: "work",
        LABEL_SESSION: session,
        LABEL_WORKSPACE: workspace,
        LABEL_SPEC: json.dumps(spec, separators=(",", ":"), sort_keys=True),
        LABEL_NETWORK: limits.network,
    }
    body = _base(container_name(session), image, labels, limits)
    body.update(
        {
            # Ohne Netz keine DNS-Server des Rechners im Container (8w).
            "use_image_resolve_conf": limits.network != "default",
            "command": ["sleep", "infinity"],
            "work_dir": "/workspace",
            "netns": {"nsmode": "bridge"},
            "Networks": {network: {}},
            "mounts": [
                _tmpfs("/workspace", limits.tmpfs_mb, "0700", True),
                _tmpfs("/tmp", TMP_MB, "1777", False),
            ],
        }
    )
    if scratch is not None:
        body["mounts"].append(scratch.mount())
        if scratch.mode == "local":
            # Host-Nutzer = Nutzer 1000 im Container: Dateien im Ordner gehören ihm.
            body["userns"] = dict(KEEP_ID)
    if preview_port is not None:
        labels[LABEL_PREVIEW] = str(preview_port)
        body["portmappings"] = [
            {
                "host_ip": "127.0.0.1",
                "host_port": preview_port,
                "container_port": PREVIEW_CONTAINER_PORT,
                "protocol": "tcp",
            }
        ]
    return body


def relay_body(session: str, image: str, targets: list[tuple[str, int]]) -> dict:
    """Relay-Container der Positivliste (8w): Relay-Netz (nur Hilfscontainer) + Ausgang."""
    labels = {LABEL_RUNNER: "1", LABEL_ROLE: "relay", LABEL_SESSION: session}
    limits = Limits(cpus=0.5, memory_mb=128, pids=64, tmpfs_mb=16, network="allowlist")
    body = _base(container_name(session, "relay"), image, labels, limits)
    body.update(
        {
            "command": [
                "mgpt-relay",
                "--base",
                str(RELAY_BASE_PORT),
                *[f"{host}:{port}" for host, port in targets],
            ],
            "work_dir": "/",
            "use_image_resolve_conf": False,
            "netns": {"nsmode": "bridge"},
            "Networks": {network_name(session, "rel"): {}, network_name(session, "out"): {}},
            "mounts": [_tmpfs("/tmp", 16, "1777", False)],
        }
    )
    return body


def tool_body(session: str, call: str, image: str) -> dict:
    """Hilfscontainer für einen ssh/telnet-Aufruf (Grundsatz: Modell sieht nie den Zugang).

    Läuft nur am Relay-Netz der Sitzung; der Arbeitscontainer hängt dort nicht. Der
    Zugang liegt nur in seinem /tmp, der Container wird nach dem Aufruf entfernt.
    """
    labels = {LABEL_RUNNER: "1", LABEL_ROLE: "tool", LABEL_SESSION: session, LABEL_CALL: call}
    limits = Limits(cpus=0.5, memory_mb=128, pids=64, tmpfs_mb=16, network="allowlist")
    body = _base(container_name(session, "tool", call), image, labels, limits)
    body.update(
        {
            "command": ["sleep", "infinity"],
            "work_dir": "/tmp",
            "use_image_resolve_conf": True,
            "netns": {"nsmode": "bridge"},
            "Networks": {network_name(session, "rel"): {}},
            "mounts": [_tmpfs("/tmp", 16, "0700", True)],
        }
    )
    return body


# --- Prüfung -------------------------------------------------------------------


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise TemplateError(message)


def _check_mounts(mounts, role: str, config) -> bool:
    """Nur tmpfs; einzige Ausnahme ist der Scratch-Ordner des Rechners. Liefert True bei Bind."""
    allowed = {"/workspace", "/tmp", "/scratch"} if role == "work" else {"/tmp"}
    _expect(isinstance(mounts, list) and mounts, "mounts fehlen.")
    seen, bind = set(), False
    for mount in mounts:
        _expect(isinstance(mount, dict), "Mount ungültig.")
        _expect(
            set(mount) == {"destination", "type", "source", "options"}, "Mount mit fremden Feldern."
        )
        dest = mount["destination"]
        _expect(dest in allowed, f"Mount-Ziel nicht erlaubt: {dest}")
        _expect(dest not in seen, "Mount-Ziel doppelt.")
        seen.add(dest)
        if mount["type"] == "bind":
            _expect(dest == "/scratch", "Bind-Mount nur für /scratch.")
            _check_scratch_source(mount["source"], config)
            _expect(mount["options"] == BIND_OPTIONS, "Bind-Mount ohne noexec/nosuid/nodev.")
            bind = True
            continue
        _expect(mount["type"] == "tmpfs" and mount["source"] == "tmpfs", "Nur tmpfs erlaubt.")
        options = mount["options"]
        _expect(isinstance(options, list), "Mount-Optionen ungültig.")
        for option in options:
            _expect(
                isinstance(option, str) and TMPFS_OPTION_RE.match(option) is not None,
                f"Mount-Option nicht erlaubt: {option}",
            )
        _expect("nosuid" in options and "nodev" in options, "tmpfs ohne nosuid/nodev.")
        _expect(any(o.startswith("size=") for o in options), "tmpfs ohne Größe.")
    return bind


def _check_scratch_source(source, config) -> None:
    host = getattr(config, "scratch_host_dir", None)
    _expect(bool(host), "Lokales Scratchpad ist nicht eingerichtet.")
    _expect(isinstance(source, str), "Scratch-Pfad ungültig.")
    parent, _, name = source.rpartition("/")
    _expect(parent == host and UUID_RE.match(name) is not None, "Scratch-Pfad nicht erlaubt.")


def _check_resources(body: dict, config) -> None:
    res = body.get("resource_limits")
    _expect(
        isinstance(res, dict) and set(res) == {"cpu", "memory", "pids"}, "cgroup-Grenzen fehlen."
    )
    cpu, mem, pids = res["cpu"], res["memory"], res["pids"]
    _expect(set(cpu) == {"quota", "period"} and cpu["period"] == CPU_PERIOD, "CPU-Grenze ungültig.")
    _expect(0 < cpu["quota"] <= config.max_cpus * CPU_PERIOD, "CPU-Grenze zu hoch.")
    _expect(set(mem) == {"limit", "swap"}, "RAM-Grenze ungültig.")
    _expect(0 < mem["limit"] <= config.max_memory_mb << 20, "RAM-Grenze zu hoch.")
    _expect(mem["swap"] == mem["limit"], "Swap muss gleich RAM sein.")
    _expect(set(pids) == {"limit"} and 0 < pids["limit"] <= config.max_pids, "pids ungültig.")
    rlimits = body.get("r_limits")
    _expect(isinstance(rlimits, list), "ulimits fehlen.")
    kinds = set()
    for item in rlimits:
        _expect(set(item) == {"type", "soft", "hard"}, "ulimit ungültig.")
        _expect(item["type"] in ("nofile", "fsize", "core", "nproc"), "ulimit nicht erlaubt.")
        _expect(0 <= item["soft"] <= item["hard"], "ulimit ungültig.")
        kinds.add(item["type"])
    _expect({"nofile", "fsize"} <= kinds, "ulimit nofile/fsize fehlt.")


def check_body(body: dict, config) -> str:
    """libpod-Körper gegen die Vorlage prüfen; liefert die Rolle (work/relay)."""
    _expect(isinstance(body, dict), "Körper ungültig.")
    unknown = set(body) - ALLOWED_KEYS
    _expect(not unknown, f"Option nicht erlaubt: {', '.join(sorted(unknown))}")
    for key, value in FIXED.items():
        _expect(body.get(key) == value, f"Option {key} weicht von der Vorlage ab.")
    labels = body.get("labels")
    _expect(isinstance(labels, dict) and labels.get(LABEL_RUNNER) == "1", "Label fehlt.")
    _expect(all(isinstance(v, str) for v in labels.values()), "Label ungültig.")
    role = labels.get(LABEL_ROLE)
    _expect(role in ("work", "relay", "tool"), "Rolle unbekannt.")
    name = body.get("name")
    _expect(isinstance(name, str) and NAME_RE.match(name) is not None, "Name ungültig.")
    _expect(
        name == container_name(labels.get(LABEL_SESSION, ""), role, labels.get(LABEL_CALL)),
        "Name passt nicht zur Sitzung.",
    )
    check_image(body.get("image"), None, config.images)
    _expect(body.get("env") == ENV, "Umgebung weicht von der Vorlage ab.")
    resolv = body.get("use_image_resolve_conf")
    _expect(isinstance(resolv, bool), "use_image_resolve_conf fehlt.")
    if role == "work" and labels.get(LABEL_NETWORK) != "default":
        _expect(resolv is True, "Ohne Netz keine DNS-Server des Rechners.")
    _expect(body.get("netns") == {"nsmode": "bridge"}, "Netz-Namespace nicht erlaubt.")
    networks = body.get("Networks")
    _expect(isinstance(networks, dict) and networks, "Netz fehlt.")
    for net, opts in networks.items():
        _expect(NETWORK_RE.match(net) is not None and opts == {}, f"Netz nicht erlaubt: {net}")
    bind = _check_mounts(body.get("mounts"), role, config)
    if bind:
        _expect(body.get("userns") == KEEP_ID, "Lokales Scratchpad nur mit userns keep-id.")
    else:
        _expect("userns" not in body, "userns nur beim lokalen Scratchpad.")
    _check_resources(body, config)
    ports = body.get("portmappings", [])
    if role == "work":
        _expect(body.get("command") == ["sleep", "infinity"], "Befehl weicht ab.")
        _expect(len(networks) == 1, "Arbeitscontainer nur an einem Netz.")
        _expect(
            next(iter(networks)).startswith("mgpt-net-"), "Arbeitscontainer nur am Sitzungsnetz."
        )
        _expect(isinstance(ports, list) and len(ports) <= 1, "Höchstens ein Port.")
        for port in ports:
            _expect(
                port
                == {
                    "host_ip": "127.0.0.1",
                    "host_port": port.get("host_port"),
                    "container_port": PREVIEW_CONTAINER_PORT,
                    "protocol": "tcp",
                },
                "Port nur 8080 an 127.0.0.1.",
            )
            _expect(port["host_port"] in protocol.PREVIEW_PORTS, "Port außerhalb 18080–18089.")
    elif role == "tool":
        _expect(body.get("command") == ["sleep", "infinity"], "Befehl weicht ab.")
        _expect(not ports, "Hilfscontainer ohne Ports.")
        _expect(
            set(networks) == {network_name(labels[LABEL_SESSION], "rel")},
            "Hilfscontainer nur am Relay-Netz.",
        )
    else:
        command = body.get("command")
        _expect(
            isinstance(command, list)
            and command[:3] == ["mgpt-relay", "--base", str(RELAY_BASE_PORT)]
            and 4 <= len(command) <= 53
            and all(isinstance(t, str) and RELAY_TARGET_RE.match(t) for t in command[3:]),
            "Relay-Befehl weicht ab.",
        )
        _expect(not ports, "Relay ohne Ports.")
        _expect(
            set(networks)
            == {
                network_name(labels[LABEL_SESSION], "rel"),
                network_name(labels[LABEL_SESSION], "out"),
            },
            "Relay nur an Relay-Netz und Ausgang.",
        )
    return role


def network_body(name: str, *, internal: bool, labels: dict) -> dict:
    return {
        "name": name,
        "driver": "bridge",
        "internal": internal,
        "dns_enabled": False,
        "labels": {LABEL_RUNNER: "1", **labels},
    }


def check_network_body(body: dict) -> None:
    _expect(
        set(body) == {"name", "driver", "internal", "dns_enabled", "labels"},
        "Netzoptionen ungültig.",
    )
    _expect(NETWORK_RE.match(body["name"]) is not None, "Netzname ungültig.")
    _expect(body["driver"] == "bridge" and body["dns_enabled"] is False, "Netzart ungültig.")
    _expect(body["labels"].get(LABEL_RUNNER) == "1", "Label fehlt.")
    if body["name"].startswith("mgpt-rel-"):
        _expect(body["internal"] is True, "Relay-Netz muss intern sein.")
    if body["name"].startswith("mgpt-net-") and body["labels"].get("multigpt.network") != "default":
        _expect(body["internal"] is True, "Sitzungsnetz muss intern sein.")


# --- Ist-Werte -------------------------------------------------------------------


def _tmpfs_mb(options: str) -> int | None:
    for part in options.split(","):
        if part.startswith("size="):
            value = part[5:]
            unit = value[-1].lower() if value[-1].isalpha() else ""
            number = int(value[:-1] if unit else value)
            return {"k": number // 1024, "m": number, "g": number * 1024}.get(unit, number >> 20)
    return None


def actual_limits(info: dict, network: str) -> Limits:
    """Tatsächliche Grenzen aus ``inspect`` (libpod-Format)."""
    host = info.get("HostConfig") or {}
    quota, period = host.get("CpuQuota") or 0, host.get("CpuPeriod") or 0
    nano = host.get("NanoCpus") or 0
    cpus = round(quota / period, 3) if quota and period else round(nano / 1e9, 3)
    tmpfs = (host.get("Tmpfs") or {}).get("/workspace", "")
    return Limits(
        cpus=cpus,
        memory_mb=int(host.get("Memory") or 0) >> 20,
        pids=int(host.get("PidsLimit") or 0),
        tmpfs_mb=_tmpfs_mb(tmpfs) or 0,
        network=network,
    )


def check_inspect(
    info: dict, expected: Limits, preview_port: int | None, scratch_source: str | None = None
) -> list[str]:
    """Abweichungen der Ist-Werte von Soll und Vorlage (leer = in Ordnung)."""
    problems = []
    host = info.get("HostConfig") or {}
    actual = actual_limits(info, expected.network)
    if abs(actual.cpus - expected.cpus) > 0.01:
        problems.append(f"CPU {actual.cpus} statt {expected.cpus}")
    if actual.memory_mb != expected.memory_mb:
        problems.append(f"RAM {actual.memory_mb} MB statt {expected.memory_mb} MB")
    if int(host.get("MemorySwap") or 0) != int(host.get("Memory") or -1):
        problems.append("Swap ungleich RAM")
    if actual.pids != expected.pids:
        problems.append(f"pids {actual.pids} statt {expected.pids}")
    if actual.tmpfs_mb != expected.tmpfs_mb:
        problems.append(f"tmpfs {actual.tmpfs_mb} MB statt {expected.tmpfs_mb} MB")
    if host.get("Privileged"):
        problems.append("privilegiert")
    if not host.get("ReadonlyRootfs"):
        problems.append("Wurzeldateisystem beschreibbar")
    if host.get("CapAdd"):
        problems.append("zusätzliche Capabilities")
    if "no-new-privileges" not in (host.get("SecurityOpt") or []):
        problems.append("no-new-privileges fehlt")
    for key in ("PidMode", "IpcMode", "UTSMode", "NetworkMode"):
        if str(host.get(key) or "").startswith("host"):
            problems.append(f"Host-Namespace {key}")
    if host.get("Devices"):
        problems.append("Geräte eingebunden")
    for mount in info.get("Mounts") or []:
        options = mount.get("Options") or []
        if not (
            scratch_source
            and mount.get("Type") == "bind"
            and mount.get("Source") == scratch_source
            and mount.get("Destination") == "/scratch"
            and {"noexec", "nosuid", "nodev"} <= set(options)
        ):
            problems.append(f"Mount {mount.get('Destination')} nicht erlaubt")
    for bind in host.get("Binds") or []:
        if not (scratch_source and bind.startswith(f"{scratch_source}:/scratch:")):
            problems.append("Bind-Mount nicht erlaubt")
    if set(host.get("Tmpfs") or {}) - {"/workspace", "/tmp", "/scratch"}:
        problems.append("fremdes tmpfs")
    bindings = host.get("PortBindings") or {}
    for port, entries in bindings.items():
        for entry in entries or []:
            if (
                port != f"{PREVIEW_CONTAINER_PORT}/tcp"
                or entry.get("HostIp") != "127.0.0.1"
                or str(entry.get("HostPort")) != str(preview_port)
            ):
                problems.append(f"Port {port} weicht ab")
    if (info.get("Config") or {}).get("User") != USER:
        problems.append("Nutzer weicht ab")
    return problems
