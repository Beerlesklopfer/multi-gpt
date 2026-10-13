"""Manipulationsschutz des Runners (M14, Rust-Agent, Entscheidungen vom 2026-10-13).

Authentifiziert wird ein Agent nur über seinen Kopplungs-Token. Die
Ed25519-Schlüssel hier schützen vor Manipulation:

**Installationsschlüssel** (je MultiGPT-Installation): Der postinst bzw.
``mgpt-ctl runner keygen`` erzeugt das Paar; der private Teil liegt nur auf dem
Server (``RUNNER_SERVER_KEY``, 0600, Dienstnutzer). Der Runner-Dienst signiert
damit **jeden** Rahmen an seine Runner (Umschlag ``signed``, siehe
``protocol.py``). Den öffentlichen Teil zeigt die Kopplungsseite; der Agent legt
ihn schreibgeschützt außerhalb des Containers ab und nimmt nur an, was dieser
Server signiert hat.

**Release-Schlüssel** (je Release-Lauf der CI, privater Teil verworfen): Mit
ihnen sind der Dienstkatalog (``catalog.json``) und die Prüfsummen der
Runner-Programme signiert. Der Server kennt die öffentlichen Schlüssel der
letzten drei Releases (``release_keys.json`` im Paket, ``RUNNER_DIST_DIR``).
Beim ``hello`` prüft :func:`check_catalog` den gemeldeten Katalog;
:func:`grant` bestimmt daraus die freigegebenen Dienste: bekannte Namen mit
Beschreibung und Einstufung des Servers (``tools.CATALOG``), neue Namen erst
nach Einstufung durch einen Verwalter (``RunnerService``). Unsigniert,
verändert oder mit unbekanntem Schlüssel: keine Dienste und ein Audit-Eintrag.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from django.conf import settings
from django.utils import timezone

from . import protocol

logger = logging.getLogger(__name__)

KEEP_RELEASES = 3  # wie APT_KEEP_RELEASES: Runner überstehen ein Server-Update
CATALOG_FORMAT = 1
MAX_DESCRIPTION = 2000
MAX_SCHEMA_BYTES = 32 * 1024
# Namen, die der Server bewusst nicht mehr anbietet (ssh im Alltag → setup_host).
RETIRED = frozenset({"ssh"})

ARCHES = ("amd64", "arm64")
ARCH_ALIASES = {"x86_64": "amd64", "aarch64": "arm64", "amd64": "amd64", "arm64": "arm64"}
BINARY_NAME = "multigpt-runner-linux-{arch}"
SUMS_NAME = "SHA256SUMS"

MSG_NO_KEY = (
    "Installationsschlüssel des Runners fehlt ({path}). Erzeugen mit: mgpt-ctl runner keygen"
)


class TrustError(Exception):
    """Schlüssel bzw. Katalog unbrauchbar; ``str()`` ist eine deutsche Meldung."""


# --- Hilfen -------------------------------------------------------------------------------


def _b64decode(text: str, size: int) -> bytes:
    try:
        raw = base64.b64decode((text or "").strip(), validate=True)
    except (binascii.Error, ValueError):
        raise TrustError("kein gültiges Base64") from None
    if len(raw) != size:
        raise TrustError(f"{size} Byte erwartet")
    return raw


def public_text(raw: bytes) -> str:
    """Öffentlicher Schlüssel als eine Zeile Base64 (32 Byte roh)."""
    return base64.b64encode(raw).decode("ascii")


def verify(public_raw: bytes, message: bytes, sig: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(public_raw).verify(sig, message)
    except (InvalidSignature, ValueError):
        return False
    return True


# --- Installationsschlüssel ---------------------------------------------------------------


@dataclass(frozen=True)
class ServerKey:
    private: Ed25519PrivateKey = field(repr=False)
    public_raw: bytes

    @property
    def key_id(self) -> str:
        return protocol.key_id(self.public_raw)

    @property
    def public_b64(self) -> str:
        return public_text(self.public_raw)

    def sign(self, message: bytes) -> bytes:
        return self.private.sign(message)


_lock = threading.Lock()
_cache: dict[str, tuple[float, ServerKey]] = {}


def server_key_path() -> Path:
    return Path(getattr(settings, "RUNNER_SERVER_KEY", "") or "server.key")


def _from_private(private: Ed25519PrivateKey) -> ServerKey:
    raw = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return ServerKey(private, raw)


def generate_server_key(path: Path | str | None = None, *, force: bool = False):
    """Paar erzeugen, falls es fehlt (bzw. mit ``force`` ersetzen).

    Liefert ``(neu, ServerKey)``. Die Datei (PKCS#8, PEM) entsteht mit 0600 im
    Verzeichnis mit 0700. Ein vorhandener Schlüssel bleibt ohne ``force``
    unverändert – ein neuer trennt alle gekoppelten Runner (neu koppeln).
    """
    path = Path(path) if path is not None else server_key_path()
    if path.exists() and not force:
        return False, load_server_key(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(pem)
            fh.flush()
            os.fsync(fh.fileno())
        if force:
            os.replace(tmp, path)
        else:
            os.link(tmp, path)  # scheitert, falls inzwischen jemand anderes schrieb
    finally:
        tmp.unlink(missing_ok=True)
    with _lock:
        _cache.pop(str(path), None)
    return True, load_server_key(path)


def load_server_key(path: Path | str | None = None) -> ServerKey:
    """Installationsschlüssel laden (zwischengespeichert, neu bei geänderter Datei)."""
    path = Path(path) if path is not None else server_key_path()
    try:
        stat = path.stat()
    except OSError:
        raise TrustError(MSG_NO_KEY.format(path=path)) from None
    with _lock:
        cached = _cache.get(str(path))
        if cached is not None and cached[0] == stat.st_mtime:
            return cached[1]
    if stat.st_mode & 0o077:
        logger.warning("Installationsschlüssel %s ist für andere lesbar (chmod 600).", path)
    try:
        private = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError):
        raise TrustError(f"Installationsschlüssel {path} ist nicht lesbar.") from None
    if not isinstance(private, Ed25519PrivateKey):
        raise TrustError(f"Installationsschlüssel {path} ist kein Ed25519-Schlüssel.")
    key = _from_private(private)
    with _lock:
        _cache[str(path)] = (stat.st_mtime, key)
    return key


def server_key_or_none() -> ServerKey | None:
    try:
        return load_server_key()
    except TrustError:
        return None


def public_key_text() -> str:
    """Öffentlicher Installationsschlüssel für die Kopplungsseite ('' ohne Schlüssel)."""
    key = server_key_or_none()
    return key.public_b64 if key is not None else ""


def sign_frame(key: ServerKey, frame: dict, nonce: str) -> str:
    """Rahmen des Servers prüfen, kodieren und im Umschlag ``signed`` liefern."""
    data = {k: v for k, v in frame.items() if k != "type"}
    text = protocol.encode(frame["type"], protocol.SERVER, **data)
    return protocol.wrap_signed(text, nonce, key.key_id, key.sign)


# --- Release-Schlüssel --------------------------------------------------------------------


@dataclass(frozen=True)
class ReleaseKey:
    release: str
    key_id: str
    public_raw: bytes


def dist_dir() -> Path:
    return Path(getattr(settings, "RUNNER_DIST_DIR", "") or "/usr/share/multi-gpt/runner")


def release_keys_path() -> Path:
    explicit = getattr(settings, "RUNNER_RELEASE_KEYS_FILE", "") or ""
    return Path(explicit) if explicit else dist_dir() / "release_keys.json"


def release_keys(path: Path | None = None) -> list[ReleaseKey]:
    """Öffentliche Release-Schlüssel (neueste zuerst, höchstens drei).

    Format (CI, rs-ci): ``{"version": 1, "keys": [{"release", "key_id",
    "public_key"}, …]}``. Fehlerhafte Einträge werden übersprungen.
    """
    path = path or release_keys_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        logger.warning("Release-Schlüssel %s nicht lesbar.", path)
        return []
    out = []
    entries = data.get("keys") if isinstance(data, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        try:
            raw = _b64decode(entry.get("public_key"), protocol.PUBLIC_KEY_BYTES)
        except (TrustError, AttributeError):
            logger.warning("Release-Schlüssel in %s übersprungen (ungültig).", path)
            continue
        kid = protocol.key_id(raw)
        if entry.get("key_id") not in (None, kid):
            logger.warning("Release-Schlüssel %s: key_id passt nicht.", entry.get("release"))
            continue
        out.append(ReleaseKey(str(entry.get("release") or "")[:50], kid, raw))
        if len(out) == KEEP_RELEASES:
            break
    return out


# --- Dienstkatalog ------------------------------------------------------------------------


@dataclass
class CatalogCheck:
    state: str  # protocol.CATALOG_STATES
    release: str = ""
    services: list[dict] = field(default_factory=list)
    message: str = ""

    @property
    def trusted(self) -> bool:
        return self.state in ("valid", "dev")


def parse_catalog(text: str) -> tuple[str, list[dict]]:
    """``catalog.json`` lesen: (Release, Dienste). ``TrustError`` bei Formfehlern."""
    try:
        data = json.loads(text)
    except ValueError:
        raise TrustError("Katalog ist kein JSON.") from None
    if not isinstance(data, dict) or data.get("format") != CATALOG_FORMAT:
        raise TrustError(f"Katalogformat {CATALOG_FORMAT} erwartet.")
    release = data.get("release")
    if not isinstance(release, str) or not 0 < len(release) <= 50:
        raise TrustError("Katalog ohne Release.")
    entries = data.get("services")
    if not isinstance(entries, list) or len(entries) > protocol.MAX_SERVICES:
        raise TrustError("Katalog: services muss eine Liste sein.")
    services, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise TrustError("Katalog: Dienst ist kein Objekt.")
        name, kind = entry.get("name"), entry.get("kind")
        if not isinstance(name, str) or not protocol.SERVICE_RE.match(name) or name in seen:
            raise TrustError("Katalog: Dienstname ungültig bzw. doppelt.")
        if kind not in protocol.SERVICE_KINDS:
            raise TrustError(f"Katalog: Art von {name} ungültig.")
        description = entry.get("description")
        if not isinstance(description, str) or len(description) > MAX_DESCRIPTION:
            raise TrustError(f"Katalog: Beschreibung von {name} ungültig.")
        schema = entry.get("input_schema")
        if not isinstance(schema, dict) or len(json.dumps(schema)) > MAX_SCHEMA_BYTES:
            raise TrustError(f"Katalog: Parameter von {name} ungültig.")
        seen.add(name)
        services.append(
            {"name": name, "kind": kind, "description": description, "input_schema": schema}
        )
    return release, services


def _accept_unsigned() -> bool:
    return bool(settings.DEBUG and getattr(settings, "RUNNER_ACCEPT_UNSIGNED", False))


def check_catalog(hello: dict, keys: list[ReleaseKey] | None = None) -> CatalogCheck:
    """Katalog aus ``hello`` gegen die Release-Schlüssel prüfen (ohne DB)."""
    text = hello.get("catalog")
    if not text:
        return CatalogCheck("missing", message="Der Runner meldet keinen Dienstkatalog.")
    if not hello.get("catalog_sig"):
        if _accept_unsigned():
            try:
                release, services = parse_catalog(text)
            except TrustError as exc:
                return CatalogCheck("invalid", message=str(exc))
            return CatalogCheck("dev", release, services, "Unsignierter Katalog (Entwicklung).")
        return CatalogCheck("unsigned", message="Dienstkatalog ohne Signatur.")
    keys = release_keys() if keys is None else keys
    key = next((k for k in keys if k.key_id == hello.get("catalog_key")), None)
    if key is None:
        return CatalogCheck(
            "unknown_key",
            message="Release-Schlüssel des Katalogs unbekannt – Runner bitte aktualisieren.",
        )
    try:
        sig = _b64decode(hello["catalog_sig"], protocol.SIGNATURE_BYTES)
    except TrustError:
        return CatalogCheck("invalid", message="Signatur des Katalogs unlesbar.")
    if not verify(key.public_raw, text.encode("utf-8"), sig):
        return CatalogCheck("invalid", message="Signatur des Katalogs ungültig (verändert?).")
    try:
        release, services = parse_catalog(text)
    except TrustError as exc:
        return CatalogCheck("invalid", message=str(exc))
    return CatalogCheck("valid", release, services, f"Katalog {release} gültig signiert.")


def grant(check: CatalogCheck, now=None) -> list[str]:
    """Freigegebene Dienste eines geprüften Katalogs (DB: neue Dienste vormerken).

    Bekannte Namen (``tools.CATALOG``, ohne Werkzeuge des Servers wie ``todo``)
    gelten mit der Einstufung des Servers; ``RETIRED`` nie; neue Namen nur,
    wenn ein Verwalter sie freigegeben hat (sonst als „nicht eingestuft“
    vorgemerkt).
    """
    from . import tools
    from .models import RunnerService

    if not check.trusted:
        return []
    now = now or timezone.now()
    granted = []
    for entry in check.services:
        name = entry["name"]
        known = tools.get(name)
        if known is not None:
            if not known.server_side:
                granted.append(name)
            continue
        if name in RETIRED:
            continue
        service, created = RunnerService.objects.get_or_create(
            name=name,
            defaults={
                "proposed_kind": entry["kind"],
                "description": entry["description"],
                "input_schema": entry["input_schema"],
                "release": check.release,
                "first_seen": now,
                "last_seen": now,
            },
        )
        if not created:
            changed = (
                service.description != entry["description"]
                or service.input_schema != entry["input_schema"]
            )
            RunnerService.objects.filter(pk=service.pk).update(
                proposed_kind=entry["kind"],
                description=entry["description"],
                input_schema=entry["input_schema"],
                release=check.release,
                last_seen=now,
            )
            if changed and service.status == RunnerService.Status.APPROVED:
                # Neue Beschreibung bzw. Parameter: erneut einstufen lassen.
                RunnerService.objects.filter(pk=service.pk).update(
                    status=RunnerService.Status.PENDING
                )
                logger.warning("Runner-Dienst %s geändert – Einstufung zurückgesetzt.", name)
                continue
        if service.offered:
            granted.append(name)
    return granted


def extra_tools(names) -> dict:
    """Freigegebene neue Dienste als ``tools.RunnerTool`` (Name → Werkzeug)."""
    from . import tools
    from .models import RunnerService

    wanted = [n for n in names or () if tools.get(n) is None]
    if not wanted:
        return {}
    out = {}
    for service in RunnerService.objects.filter(
        name__in=wanted, status=RunnerService.Status.APPROVED
    ).exclude(kind=""):
        out[service.name] = tools.RunnerTool(
            service.name,
            service.kind,
            service.description,
            service.input_schema or {"type": "object", "properties": {}},
        )
    return out


# --- Runner-Programme zum Herunterladen -------------------------------------------------


def public_url() -> str:
    """Adresse von MultiGPT für Runner (``https://host``, ohne / am Ende).

    ``RUNNER_PUBLIC_URL``, sonst der erste Eintrag aus ``CSRF_TRUSTED_ORIGINS``;
    die Kopplungsseite nimmt die Adresse der Anfrage.
    """
    explicit = (getattr(settings, "RUNNER_PUBLIC_URL", "") or "").strip()
    if not explicit:
        origins = list(getattr(settings, "CSRF_TRUSTED_ORIGINS", None) or [])
        explicit = next((o for o in origins if o.startswith("https://") and "*" not in o), "")
    return explicit.rstrip("/")


def normalize_arch(value: str) -> str | None:
    return ARCH_ALIASES.get((value or "").lower())


def binary_path(arch: str) -> Path:
    return dist_dir() / BINARY_NAME.format(arch=arch)


_SUM_RE = re.compile(r"^([0-9a-f]{64}) [ *](\S+)$")


@dataclass
class DistInfo:
    """Was im Download-Verzeichnis liegt; ``signed``: SHA256SUMS mit gültiger
    Signatur eines bekannten Release-Schlüssels (``release``)."""

    binaries: dict[str, dict] = field(default_factory=dict)  # arch -> {name, sha256, size}
    signed: bool = False
    release: str = ""


def dist_info() -> DistInfo:
    info = DistInfo()
    base = dist_dir()
    try:
        sums_raw = (base / SUMS_NAME).read_bytes()
    except OSError:
        return info
    sums = {}
    for line in sums_raw.decode("utf-8", "replace").splitlines():
        match = _SUM_RE.match(line.strip())
        if match:
            sums[match.group(2)] = match.group(1)
    try:
        sig = _b64decode((base / f"{SUMS_NAME}.sig").read_text(encoding="ascii"), 64)
    except (OSError, UnicodeDecodeError, TrustError):
        sig = None
    if sig is not None:
        for key in release_keys():
            if verify(key.public_raw, sums_raw, sig):
                info.signed, info.release = True, key.release
                break
    for arch in ARCHES:
        name = BINARY_NAME.format(arch=arch)
        path = base / name
        if name in sums and path.is_file():
            info.binaries[arch] = {
                "name": name,
                "sha256": sums[name],
                "size": path.stat().st_size,
            }
    return info


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
