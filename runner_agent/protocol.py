"""Rahmenprotokoll zwischen Runner-Agent und Runner-Dienst (M14-03).

Diese Datei hat bewusst **keine Django-Importe** und nur die Standardbibliothek:
Sie wird unverändert in das Agent-Paket ``runner_agent/`` kopiert, ein Test
prüft die Gleichheit. Änderungen nur nach Absprache (Sprach-Bus) und in beiden
Kopien.

**Rahmen:** Jeder WebSocket-Textrahmen ist genau ein JSON-Objekt mit dem Feld
``type`` und den Feldern seines Typs. Unbekannte Typen und unbekannte Felder
werden abgewiesen, ebenso Rahmen in der falschen Richtung (``parse`` mit
``sender``). Binärrahmen gibt es nicht; Binärdaten (Bundle-Stücke, PTY-Strom,
Vorschau-Antworten) stehen als Base64 im Feld ``data``. Optionale Felder dürfen
fehlen oder ``null`` sein; ``parse`` ergänzt fehlende optionale Felder mit
``None`` bzw. ihrem Standardwert, Empfänger dürfen also ``frame["feld"]`` lesen.

**Ablauf:**

1. Der Agent verbindet sich (ohne ``Origin``-Header) und sendet innerhalb von
   ``HELLO_TIMEOUT`` Sekunden ``hello`` mit Token. Der Server antwortet mit
   ``welcome`` oder schließt mit einem Close-Code (``CLOSE_*``).
2. Der Agent sendet alle ``heartbeat_seconds`` einen ``heartbeat`` mit dem
   Zustand seiner Sitzungen.
3. ``session_start`` (Server) → der Agent holt bei Bedarf das Bundle
   (``bundle_get`` → ``bundle_chunk`` …), startet den Arbeitscontainer und
   meldet ``session_state`` mit den tatsächlichen Grenzen.
4. Werkzeuge laufen als MCP (JSON-RPC 2.0) im Rahmen ``mcp``; je ``pipe`` eine
   MCP-Verbindung. Neue Stände lädt der Agent mit ``bundle_chunk`` hoch, der
   Server bestätigt mit ``bundle_ack``.
5. Terminal: ``pty_open``/``pty_input``/``pty_resize``/``pty_close`` (Server),
   ``pty_data``/``pty_exit`` (Agent). Vorschau: ``preview`` (Agent meldet Port
   und Token), ``preview_request``/``preview_response`` über den Rückkanal.
6. ``session_stop`` (Server) beendet eine Sitzung; ``close`` kündigt das Ende
   der Verbindung mit Grund an.

Bundle-Übertragung (beide Richtungen; ``file_chunk`` für ``publish`` ebenso, nur
Agent → Server): Stücke ``bundle_chunk`` mit fortlaufender
``seq`` ab 0, höchstens ``CHUNK_BYTES`` Rohdaten je Stück. Das letzte Stück hat
``last = true`` und trägt ``size`` und ``sha256`` (hex) des ganzen Bundles. Der
Empfänger prüft Reihenfolge, Größe und Prüfsumme und antwortet mit
``bundle_ack`` (``ok``, bei Fehler ``error``). Eine ``transfer``-ID trennt
gleichzeitige Übertragungen.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
from collections.abc import Callable
from typing import Any

PROTOCOL_VERSION = 1

# --- Größen und Zeiten -------------------------------------------------------

# Ein Rahmen (JSON-Text, UTF-8). Liegt unter ws_max_size (8 MB) von uvicorn.
MAX_FRAME_BYTES = 4 * 1024 * 1024
# Rohdaten je Bundle-Stück (Base64 macht daraus ~1,34 MB).
CHUNK_BYTES = 1024 * 1024
# Obergrenze eines Bundles im Protokoll; die tatsächliche Grenze setzt der Server
# je Rolle (Standard 50 MB, Entscheidung 8s).
MAX_BUNDLE_BYTES = 1024 * 1024 * 1024
# Rohdaten je PTY-Rahmen bzw. Vorschau-Stück.
PTY_BYTES = 64 * 1024
PREVIEW_BYTES = 1024 * 1024
MAX_TEXT = 2000  # Meldungen, Fehlertexte
MAX_ID = 64

HELLO_TIMEOUT = 10  # Sekunden bis zum ersten Rahmen ``hello``
HEARTBEAT_SECONDS = 30
PREVIEW_PORTS = range(18080, 18090)  # Entscheidung 8x

# --- Close-Codes ---------------------------------------------------------------

CLOSE_NORMAL = 1000
CLOSE_GOING_AWAY = 1001
CLOSE_ABNORMAL = 1006  # kein Close-Rahmen (Netz weg)
CLOSE_SERVER_ERROR = 1011
CLOSE_RESTART = 1012
CLOSE_TRY_AGAIN = 1013
CLOSE_PROTOCOL = 4400  # ungültiger Rahmen bzw. falsche Reihenfolge
CLOSE_TOKEN = 4401  # Token ungültig, abgelaufen oder widerrufen
CLOSE_QUOTA = 4402  # Kontingent erschöpft und Stopp nicht bestätigt
CLOSE_FORBIDDEN = 4403  # Konto ohne Recht bzw. inaktiv, Origin gesetzt
CLOSE_HELLO_TIMEOUT = 4408  # kein ``hello`` innerhalb HELLO_TIMEOUT
CLOSE_REPLACED = 4409  # neue Verbindung desselben Tokens hat diese ersetzt
CLOSE_TOO_BIG = 4413  # Rahmen über MAX_FRAME_BYTES
CLOSE_VERSION = 4426  # Protokollversion nicht unterstützt
CLOSE_RATE_LIMIT = 4429  # zu viele Rahmen bzw. Anmeldeversuche

CLOSE_REASONS = {
    CLOSE_PROTOCOL: "Protokollfehler",
    CLOSE_TOKEN: "Token ungültig",
    CLOSE_QUOTA: "Kontingent erschöpft",
    CLOSE_FORBIDDEN: "Kein Recht",
    CLOSE_HELLO_TIMEOUT: "Keine Anmeldung",
    CLOSE_REPLACED: "Ersetzt durch neue Verbindung",
    CLOSE_TOO_BIG: "Rahmen zu groß",
    CLOSE_VERSION: "Protokollversion nicht unterstützt",
    CLOSE_RATE_LIMIT: "Zu viele Anfragen",
}

# Nach diesen Codes verbindet sich der Agent nicht neu (Mensch muss handeln).
NO_RECONNECT = frozenset({CLOSE_TOKEN, CLOSE_FORBIDDEN, CLOSE_REPLACED, CLOSE_VERSION})
# Nach diesen Codes neu verbinden mit Backoff; alle übrigen ebenfalls, aber mit
# der längsten Wartezeit.
RECONNECT = frozenset(
    {CLOSE_ABNORMAL, CLOSE_SERVER_ERROR, CLOSE_RESTART, CLOSE_TRY_AGAIN, CLOSE_GOING_AWAY}
)


def should_reconnect(code: int | None) -> bool:
    """Darf der Agent nach diesem Close-Code neu verbinden?"""
    return code not in NO_RECONNECT


# --- Aufzählungen ----------------------------------------------------------------

AGENT = "agent"
SERVER = "server"

ENGINES = ("podman", "docker", "fake")
SESSION_STATES = ("starting", "running", "stopping", "stopped", "failed", "unknown")
STOP_REASONS = ("user", "idle", "quota", "admin", "revoked", "mismatch", "replaced", "error")
CLOSE_FRAME_REASONS = ("revoked", "rights", "quota", "replaced", "shutdown", "protocol")
NETWORK_MODES = ("none", "allowlist", "default")
SCRATCH_MODES = ("container", "local")
ERROR_CODES = (
    "invalid_frame",  # Rahmen verletzt das Schema
    "unknown_session",
    "unknown_pipe",
    "not_allowed",  # Vorlage/Rechte verbieten den Befehl
    "busy",  # Sitzung bzw. Arbeitsstand belegt
    "limit",  # Größen- bzw. Ratenbegrenzung
    "bundle",  # Bundle fehlerhaft (Prüfsumme, Reihenfolge, Größe)
    "engine",  # Fehler der Container-Engine
    "offline",  # Runner nicht verbunden
    "timeout",
    "internal",
)
HTTP_METHODS = ("GET", "HEAD")


class ProtocolError(ValueError):
    """Rahmen ungültig. ``close_code`` ist der passende Close-Code."""

    def __init__(self, message: str, close_code: int = CLOSE_PROTOCOL):
        super().__init__(message)
        self.message = message
        self.close_code = close_code


# --- Feldprüfungen -------------------------------------------------------------

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")  # SHA-1 oder SHA-256
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
# Image nur mit Digest: name[:tag]@sha256:<64 hex>
IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._/:-]{0,254}@sha256:[0-9a-f]{64}$")
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:-]{0,252}$")
HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MIME_RE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")

Check = Callable[[Any, str], Any]


def _fail(name: str, text: str) -> ProtocolError:
    return ProtocolError(f"Feld {name}: {text}")


def _str(max_len: int = MAX_TEXT, pattern: re.Pattern | None = None) -> Check:
    def check(value, name):
        if not isinstance(value, str):
            raise _fail(name, "Text erwartet")
        if len(value) > max_len:
            raise _fail(name, f"länger als {max_len} Zeichen")
        if pattern is not None and not pattern.match(value):
            raise _fail(name, "ungültiges Format")
        return value

    return check


def _int(lo: int, hi: int) -> Check:
    def check(value, name):
        if isinstance(value, bool) or not isinstance(value, int):
            raise _fail(name, "Ganzzahl erwartet")
        if not lo <= value <= hi:
            raise _fail(name, f"außerhalb {lo}…{hi}")
        return value

    return check


def _num(lo: float, hi: float) -> Check:
    def check(value, name):
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise _fail(name, "Zahl erwartet")
        if not math.isfinite(value) or not lo <= value <= hi:
            raise _fail(name, f"außerhalb {lo}…{hi}")
        return value

    return check


def _bool(value, name):
    if not isinstance(value, bool):
        raise _fail(name, "true/false erwartet")
    return value


def _enum(choices: tuple[str, ...]) -> Check:
    def check(value, name):
        if value not in choices:
            raise _fail(name, f"erlaubt: {', '.join(choices)}")
        return value

    return check


def _b64(max_raw: int) -> Check:
    limit = 4 * ((max_raw + 2) // 3)

    def check(value, name):
        if not isinstance(value, str):
            raise _fail(name, "Base64-Text erwartet")
        if len(value) > limit:
            raise _fail(name, f"mehr als {max_raw} Byte")
        try:
            raw = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            raise _fail(name, "kein gültiges Base64") from None
        if len(raw) > max_raw:
            raise _fail(name, f"mehr als {max_raw} Byte")
        return value

    return check


def _list(item: Check, max_items: int) -> Check:
    def check(value, name):
        if not isinstance(value, list):
            raise _fail(name, "Liste erwartet")
        if len(value) > max_items:
            raise _fail(name, f"mehr als {max_items} Einträge")
        return [item(entry, f"{name}[{i}]") for i, entry in enumerate(value)]

    return check


def _object(fields: dict[str, tuple[Check, bool]]) -> Check:
    def check(value, name):
        if not isinstance(value, dict):
            raise _fail(name, "Objekt erwartet")
        return _fields(value, fields, name)

    return check


def _headers(value, name):
    if not isinstance(value, dict):
        raise _fail(name, "Objekt erwartet")
    if len(value) > 50:
        raise _fail(name, "mehr als 50 Einträge")
    for key, item in value.items():
        if not HEADER_NAME_RE.match(key):
            raise _fail(name, "ungültiger Header-Name")
        if not isinstance(item, str) or len(item) > 4096 or "\r" in item or "\n" in item:
            raise _fail(f"{name}.{key}", "ungültiger Wert")
    return value


def _jsonrpc(value, name):
    """MCP-Nachricht: JSON-RPC-2.0-Objekt (Anfrage, Benachrichtigung, Antwort)."""
    if not isinstance(value, dict):
        raise _fail(name, "JSON-RPC-Objekt erwartet")
    if value.get("jsonrpc") != "2.0":
        raise _fail(name, "jsonrpc muss 2.0 sein")
    if "method" in value:
        if not isinstance(value["method"], str) or not value["method"]:
            raise _fail(name, "method ungültig")
    elif "result" not in value and "error" not in value:
        raise _fail(name, "weder method noch result/error")
    if "id" in value and not (
        value["id"] is None
        or isinstance(value["id"], str)
        or (isinstance(value["id"], int) and not isinstance(value["id"], bool))
    ):
        raise _fail(name, "id ungültig")
    return value


def _fields(data: dict, spec: dict[str, tuple[Check, bool]], where: str) -> dict:
    unknown = set(data) - set(spec)
    if unknown:
        raise ProtocolError(f"{where}: unbekannte Felder {', '.join(sorted(unknown))}")
    out = {}
    for name, (check, required) in spec.items():
        value = data.get(name)
        if value is None:
            if required:
                raise _fail(f"{where}.{name}", "fehlt")
            out[name] = None
            continue
        out[name] = check(value, f"{where}.{name}")
    return out


# --- Bausteine -------------------------------------------------------------------

_id = _str(MAX_ID, ID_RE)
_sha256 = _str(64, SHA256_RE)
_text = _str(MAX_TEXT)

ENGINE = _object(
    {
        "name": (_enum(ENGINES), True),
        "version": (_str(64), True),
        "rootless": (_bool, True),
    }
)
SESSION_INFO = _object(
    {
        "session": (_id, True),
        "workspace": (_id, False),
        "state": (_enum(SESSION_STATES), True),
        "preview_port": (_int(PREVIEW_PORTS.start, PREVIEW_PORTS.stop - 1), False),
    }
)
ALLOW = _object(
    {
        "host": (_str(253, HOST_RE), True),
        "port": (_int(1, 65535), True),
    }
)
LIMITS = _object(
    {
        "cpus": (_num(0.1, 64), True),
        "memory_mb": (_int(64, 262144), True),
        "pids": (_int(16, 65536), True),
        "tmpfs_mb": (_int(16, 65536), True),
        "network": (_enum(NETWORK_MODES), True),
    }
)
BUNDLE_INFO = _object(
    {
        "size": (_int(1, MAX_BUNDLE_BYTES), True),
        "sha256": (_sha256, True),
        "head": (_str(64, COMMIT_RE), False),
    }
)

# Jeder Typ: (Absender, Felder {Name: (Prüfung, Pflicht)}).
FRAMES: dict[str, tuple[frozenset[str], dict[str, tuple[Check, bool]]]] = {
    # Anmeldung
    "hello": (
        frozenset({AGENT}),
        {
            "token": (_str(128, TOKEN_RE), True),
            "protocol": (_int(1, 1000), True),
            "agent_version": (_str(64), True),
            "engine": (ENGINE, True),
            "hostname": (_str(253), True),
            "sessions": (_list(SESSION_INFO, 100), False),
        },
    ),
    "welcome": (
        frozenset({SERVER}),
        {
            "runner": (_int(1, 2**63 - 1), True),
            "protocol": (_int(1, 1000), True),
            "server_version": (_str(64), True),
            "heartbeat_seconds": (_int(5, 600), True),
            "images": (_list(_str(320, IMAGE_RE), 20), True),
            "max_frame_bytes": (_int(1024, MAX_FRAME_BYTES), True),
            "chunk_bytes": (_int(1024, CHUNK_BYTES), True),
        },
    ),
    "heartbeat": (
        frozenset({AGENT}),
        {"sessions": (_list(SESSION_INFO, 100), True)},
    ),
    # Sitzungen
    "session_start": (
        frozenset({SERVER}),
        {
            "session": (_id, True),
            "workspace": (_id, True),
            "image": (_str(320, IMAGE_RE), True),
            "cpus": (_num(0.1, 64), True),
            "memory_mb": (_int(64, 262144), True),
            "pids": (_int(16, 65536), True),
            "tmpfs_mb": (_int(16, 65536), True),
            "network": (_enum(NETWORK_MODES), True),
            "allow": (_list(ALLOW, 50), False),
            "command_seconds": (_int(1, 86400), True),  # Zeitlimit je Befehl
            "output_bytes": (_int(1024, 16 * 1024 * 1024), True),  # Ausgabe je Befehl
            "idle_minutes": (_int(1, 1440), True),
            "bundle": (BUNDLE_INFO, False),  # None: leerer Arbeitsstand (git init)
            # Scratchpad (/scratch): tmpfs im Container oder lokaler Ordner des Rechners
            # ~/MultiGPT/Scratch/<workspace_uuid>/ (None = container).
            "scratch_mode": (_enum(SCRATCH_MODES), False),
            "scratch_mb": (_int(1, 1024 * 1024), False),
            "workspace_uuid": (_str(36, UUID_RE), False),
        },
    ),
    "session_state": (
        frozenset({AGENT}),
        {
            "session": (_id, True),
            "state": (_enum(SESSION_STATES), True),
            "limits": (LIMITS, False),  # tatsächliche Werte aus inspect
            "preview_port": (_int(PREVIEW_PORTS.start, PREVIEW_PORTS.stop - 1), False),
            "error": (_text, False),
        },
    ),
    "session_stop": (
        frozenset({SERVER}),
        {
            "session": (_id, True),
            "reason": (_enum(STOP_REASONS), True),
        },
    ),
    # MCP (JSON-RPC 2.0) je pipe, in beide Richtungen
    "mcp": (
        frozenset({AGENT, SERVER}),
        {
            "session": (_id, True),
            "pipe": (_id, True),
            "message": (_jsonrpc, True),
        },
    ),
    # Bundle-Übertragung
    "bundle_get": (
        frozenset({AGENT}),
        {
            "session": (_id, True),
            "transfer": (_id, True),
        },
    ),
    "bundle_chunk": (
        frozenset({AGENT, SERVER}),
        {
            "session": (_id, True),
            "transfer": (_id, True),
            "seq": (_int(0, MAX_BUNDLE_BYTES // 1024), True),
            "data": (_b64(CHUNK_BYTES), True),
            "last": (_bool, True),
            "size": (_int(0, MAX_BUNDLE_BYTES), False),  # Pflicht im letzten Stück
            "sha256": (_sha256, False),  # Pflicht im letzten Stück
            "head": (_str(64, COMMIT_RE), False),  # Commit (Upload vom Agent)
            "message": (_text, False),  # Commit-Nachricht (Upload vom Agent)
        },
    ),
    # Datei aus dem Container (Werkzeug publish): Stücke wie beim Bundle, nur
    # Agent → Server; ``transfer`` gibt der Server im Werkzeugaufruf vor.
    "file_chunk": (
        frozenset({AGENT}),
        {
            "session": (_id, True),
            "transfer": (_id, True),
            "seq": (_int(0, MAX_BUNDLE_BYTES // 1024), True),
            "data": (_b64(CHUNK_BYTES), True),
            "last": (_bool, True),
            "size": (_int(0, MAX_BUNDLE_BYTES), False),  # Pflicht im letzten Stück
            "sha256": (_sha256, False),  # Pflicht im letzten Stück
            "name": (_str(255), False),  # Dateiname (ohne Pfad), im letzten Stück
            "mime": (_str(100, MIME_RE), False),
        },
    ),
    "bundle_ack": (
        frozenset({AGENT, SERVER}),
        {
            "session": (_id, True),
            "transfer": (_id, True),
            "ok": (_bool, True),
            "head": (_str(64, COMMIT_RE), False),
            "error": (_text, False),
        },
    ),
    # Terminal (PTY)
    "pty_open": (
        frozenset({SERVER}),
        {
            "session": (_id, True),
            "pty": (_id, True),
            "cols": (_int(10, 1000), True),
            "rows": (_int(5, 500), True),
        },
    ),
    "pty_input": (
        frozenset({SERVER}),
        {
            "session": (_id, True),
            "pty": (_id, True),
            "data": (_b64(PTY_BYTES), True),
        },
    ),
    "pty_resize": (
        frozenset({SERVER}),
        {
            "session": (_id, True),
            "pty": (_id, True),
            "cols": (_int(10, 1000), True),
            "rows": (_int(5, 500), True),
        },
    ),
    "pty_close": (
        frozenset({SERVER}),
        {
            "session": (_id, True),
            "pty": (_id, True),
        },
    ),
    "pty_data": (
        frozenset({AGENT}),
        {
            "session": (_id, True),
            "pty": (_id, True),
            "seq": (_int(0, 2**53), True),
            "data": (_b64(PTY_BYTES), True),
        },
    ),
    "pty_exit": (
        frozenset({AGENT}),
        {
            "session": (_id, True),
            "pty": (_id, True),
            "code": (_int(-255, 255), False),
        },
    ),
    # Vorschau (8p/8x)
    "preview": (
        frozenset({AGENT}),
        {
            "session": (_id, True),
            "running": (_bool, True),
            "port": (_int(PREVIEW_PORTS.start, PREVIEW_PORTS.stop - 1), False),
            "token": (_str(128, TOKEN_RE), False),
        },
    ),
    "preview_request": (
        frozenset({SERVER}),
        {
            "session": (_id, True),
            "request": (_id, True),
            "method": (_enum(HTTP_METHODS), True),
            "path": (_str(2048), True),
            "headers": (_headers, False),
        },
    ),
    "preview_response": (
        frozenset({AGENT}),
        {
            "session": (_id, True),
            "request": (_id, True),
            "seq": (_int(0, 100000), True),
            "status": (_int(100, 599), False),  # Pflicht im ersten Stück
            "headers": (_headers, False),
            "data": (_b64(PREVIEW_BYTES), False),
            "last": (_bool, True),
        },
    ),
    # Allgemein
    "error": (
        frozenset({AGENT, SERVER}),
        {
            "code": (_enum(ERROR_CODES), True),
            "message": (_text, True),
            "session": (_id, False),
            "ref": (_id, False),  # pipe, transfer, pty bzw. request
        },
    ),
    "close": (
        frozenset({SERVER}),
        {
            "reason": (_enum(CLOSE_FRAME_REASONS), True),
            "message": (_text, False),
        },
    ),
}

FRAME_TYPES = frozenset(FRAMES)
# Pflichtfelder im letzten Bundle-Stück
_LAST_CHUNK = ("size", "sha256")


def _semantic(frame: dict) -> None:
    """Prüfungen über mehrere Felder."""
    kind = frame["type"]
    if kind in ("bundle_chunk", "file_chunk") and frame["last"]:
        for name in _LAST_CHUNK:
            if frame[name] is None:
                raise _fail(f"{kind}.{name}", "fehlt im letzten Stück")
    elif kind == "session_start" and frame["network"] != "allowlist" and frame["allow"]:
        raise _fail("session_start.allow", "nur mit network=allowlist")
    elif kind == "preview_response" and frame["seq"] == 0 and frame["status"] is None:
        raise _fail("preview_response.status", "fehlt im ersten Stück")
    elif kind == "preview" and frame["running"] and None in (frame["port"], frame["token"]):
        raise _fail("preview", "port und token nötig, wenn die Vorschau läuft")


def validate(data: Any, sender: str | None = None) -> dict:
    """Rahmen (bereits als Python-Objekt) prüfen und normalisiert zurückgeben.

    ``sender`` (``AGENT``/``SERVER``) prüft zusätzlich die Richtung.
    """
    if not isinstance(data, dict):
        raise ProtocolError("Rahmen muss ein JSON-Objekt sein")
    kind = data.get("type")
    if not isinstance(kind, str) or kind not in FRAMES:
        raise ProtocolError("unbekannter Rahmentyp")
    senders, spec = FRAMES[kind]
    if sender is not None and sender not in senders:
        raise ProtocolError(f"Rahmen {kind} ist von {sender} nicht erlaubt")
    body = {key: value for key, value in data.items() if key != "type"}
    frame = {"type": kind, **_fields(body, spec, kind)}
    _semantic(frame)
    return frame


def parse(text: str | bytes, sender: str | None = None) -> dict:
    """Empfangenen Textrahmen prüfen; wirft ``ProtocolError``."""
    size = len(text.encode() if isinstance(text, str) else text)
    if size > MAX_FRAME_BYTES:
        raise ProtocolError("Rahmen zu groß", CLOSE_TOO_BIG)
    try:
        data = json.loads(text)
    except (ValueError, UnicodeDecodeError):
        raise ProtocolError("kein gültiges JSON") from None
    return validate(data, sender)


def encode(kind: str, sender: str | None = None, **fields: Any) -> str:
    """Rahmen bauen, prüfen und als JSON-Text liefern (``None``-Felder entfallen)."""
    data = {"type": kind, **{key: value for key, value in fields.items() if value is not None}}
    validate(data, sender)
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if len(text.encode()) > MAX_FRAME_BYTES:
        raise ProtocolError("Rahmen zu groß", CLOSE_TOO_BIG)
    return text


# --- Hilfen für Binärdaten ------------------------------------------------------


def b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64decode(text: str) -> bytes:
    return base64.b64decode(text, validate=True)


def split_bundle(data: bytes, chunk_bytes: int = CHUNK_BYTES) -> list[tuple[int, bytes, bool]]:
    """Bundle in Stücke teilen: Liste von (seq, Rohdaten, last)."""
    if not data:
        return [(0, b"", True)]
    parts = [data[i : i + chunk_bytes] for i in range(0, len(data), chunk_bytes)]
    return [(seq, part, seq == len(parts) - 1) for seq, part in enumerate(parts)]


class BundleAssembler:
    """Empfangene ``bundle_chunk``- bzw. ``file_chunk``-Rahmen einer Übertragung zusammensetzen.

    ``add(frame)`` liefert ``None``, solange Stücke fehlen, und am Ende die
    geprüften Bytes. Fehler (Reihenfolge, Größe, Prüfsumme) als ``ProtocolError``
    mit deutscher Meldung (für ``bundle_ack.error``).
    """

    def __init__(self, transfer: str, max_bytes: int = MAX_BUNDLE_BYTES):
        self.transfer = transfer
        self.max_bytes = min(max_bytes, MAX_BUNDLE_BYTES)
        self.next_seq = 0
        self.size = 0
        self._hash = hashlib.sha256()
        self._parts: list[bytes] = []

    def add(self, frame: dict) -> bytes | None:
        if frame["transfer"] != self.transfer:
            raise ProtocolError("Stück gehört zu einer anderen Übertragung")
        if frame["seq"] != self.next_seq:
            raise ProtocolError("Stücke in falscher Reihenfolge")
        part = b64decode(frame["data"])
        self.size += len(part)
        if self.size > self.max_bytes:
            raise ProtocolError(f"Bundle größer als {self.max_bytes} Byte", CLOSE_TOO_BIG)
        self._hash.update(part)
        self._parts.append(part)
        self.next_seq += 1
        if not frame["last"]:
            return None
        if frame["size"] != self.size:
            raise ProtocolError("Größe des Bundles stimmt nicht")
        if frame["sha256"] != self._hash.hexdigest():
            raise ProtocolError("Prüfsumme des Bundles stimmt nicht")
        return b"".join(self._parts)
