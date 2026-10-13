"""Einstellungen des Agents aus der Umgebung (Quadlet-Datei der Seite „Runner“)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from runner_agent import protocol

FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")


class ConfigError(Exception):
    pass


@dataclass
class Config:
    url: str  # wss://host/runner/ws
    token_file: str = "/run/secrets/token"
    ca_sha256: str | None = None  # festgehaltener Fingerprint (snakeoil, Frage 8i)
    podman_socket: str = "/run/podman.sock"
    images: frozenset[str] = field(default_factory=frozenset)  # lokale Positivliste
    insecure_dev: bool = False  # nur Entwicklung: ws:// erlaubt
    hostname: str = ""
    max_cpus: float = 8.0  # lokale Obergrenzen, egal was der Server schickt
    max_memory_mb: int = 16384
    max_pids: int = 4096
    max_tmpfs_mb: int = 8192
    # Lokales Scratchpad: Ordner ~/MultiGPT/Scratch des Rechners – im Agent-Container
    # eingebunden unter scratch_dir, für Podman (Host-Sicht) unter scratch_host_dir.
    scratch_dir: str | None = None
    scratch_host_dir: str | None = None
    labels: dict = field(default_factory=dict)  # zusätzliche Labels (Tests: multigpt.test=1)

    def read_token(self) -> str:
        """Token aus der Secret-Datei (nie aus der Umgebung, nie geloggt)."""
        try:
            with open(self.token_file, encoding="utf-8") as fh:
                token = fh.read().strip()
        except OSError as exc:
            raise ConfigError(
                f"Token-Datei nicht lesbar: {self.token_file} ({exc.strerror})"
            ) from None
        if not protocol.TOKEN_RE.match(token):
            raise ConfigError("Token-Datei enthält keinen gültigen Token.")
        return token


def normalize_url(raw: str, insecure_dev: bool = False) -> str:
    """``https://host`` → ``wss://host/runner/ws``; ``ws://`` nur mit insecure_dev."""
    parts = urlsplit(raw.strip())
    scheme = {"https": "wss", "http": "ws"}.get(parts.scheme, parts.scheme)
    if scheme not in ("wss", "ws"):
        raise ConfigError("MGPT_URL muss mit https:// oder wss:// beginnen.")
    if scheme == "ws" and not insecure_dev:
        raise ConfigError("Unverschlüsselte Verbindung nur mit MGPT_INSECURE_DEV=1.")
    if not parts.hostname:
        raise ConfigError("MGPT_URL ohne Rechnernamen.")
    path = parts.path.rstrip("/")
    if not path.endswith("/runner/ws"):
        path += "/runner/ws"
    return urlunsplit((scheme, parts.netloc, path, "", ""))


def normalize_fingerprint(raw: str | None) -> str | None:
    if not raw:
        return None
    value = raw.strip().lower().replace(":", "")
    if value.startswith("sha256="):
        value = value[7:]
    if not FINGERPRINT_RE.match(value):
        raise ConfigError("MGPT_CA_SHA256 ist kein SHA-256-Fingerprint.")
    return value


def _host_dir(raw: str | None) -> str | None:
    if not raw:
        return None
    path = os.path.normpath(raw)
    if not os.path.isabs(path) or not path.endswith("/MultiGPT/Scratch"):
        raise ConfigError("MGPT_SCRATCH_HOST_DIR muss auf …/MultiGPT/Scratch zeigen.")
    return path


def from_env(env: dict[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    insecure = env.get("MGPT_INSECURE_DEV", "") == "1"
    if not env.get("MGPT_URL"):
        raise ConfigError("MGPT_URL fehlt.")
    images = frozenset(env.get("MGPT_IMAGES", "").split())
    for image in images:
        if not protocol.IMAGE_RE.match(image):
            raise ConfigError(f"MGPT_IMAGES: Image ohne Digest: {image}")
    return Config(
        url=normalize_url(env["MGPT_URL"], insecure),
        token_file=env.get("MGPT_TOKEN_FILE") or "/run/secrets/token",
        ca_sha256=normalize_fingerprint(env.get("MGPT_CA_SHA256")),
        podman_socket=env.get("MGPT_PODMAN_SOCKET") or "/run/podman.sock",
        images=images,
        insecure_dev=insecure,
        scratch_dir=env.get("MGPT_SCRATCH_DIR") or None,
        scratch_host_dir=_host_dir(env.get("MGPT_SCRATCH_HOST_DIR")),
        hostname=(env.get("MGPT_HOSTNAME") or os.uname().nodename)[:253],
    )
