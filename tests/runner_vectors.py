"""Gemeinsame Testvektoren für das Rahmenprotokoll (Python ↔ Rust, rs-e2e).

Orakel ist ``multigpt/runner/protocol.py``. Dieses Modul beschreibt die Fälle
und schreibt sie nach ``runner-rs/testdata/frames/``::

    python -m tests.runner_vectors          # Dateien neu schreiben

Format je Datei (``valid/<name>.json`` bzw. ``invalid/<name>.json``)::

    {"sender": "agent" | "server" | null,   # null = ohne Richtungsprüfung
     "frame": {...}                          # Rahmen als JSON-Objekt (kompakt serialisieren)
       oder "text": "..."                    # roher Text (kaputtes JSON usw.)
     "pad_total_bytes": N,                   # optional: Feld message.params.pad mit 'a'
                                             # auffüllen, bis der kompakte Text N Byte hat
     "canonical": {...},                     # nur valid, nur wenn ≠ frame: normalisierter
                                             # Rahmen ohne null-Felder (fehlt: = frame)
     "close_code": 4400 | 4413,              # nur invalid
     "note": "..."}                          # Zweck des Falls

Rust (``runner-rs``) muss genau dieselben Rahmen annehmen bzw. ablehnen, und
der angenommene Rahmen muss (ohne ``null``-Felder, Zahlen numerisch verglichen)
``canonical`` entsprechen. ``tests/test_runner_vectors.py`` prüft die Python-Seite
und dass die Dateien zu diesem Modul passen.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from multigpt.runner import protocol as P
from tests.test_runner_protocol import EXAMPLES, IMAGE, TOKEN

ROOT = Path(__file__).resolve().parent.parent
FRAMES_DIR = ROOT / "runner-rs" / "testdata" / "frames"

# Fälle, die Python (noch) anders entscheidet als vorgesehen. Rust soll das
# vorgesehene Verhalten zeigen; der Python-Test führt sie als xfail(strict), bis
# protocol.py angepasst ist (gemeldet auf dem Bus).
KNOWN_PYTHON_DEVIATIONS: dict[str, str] = {
    # Behoben von trust (fullmatch, doppelte Schlüssel, Surrogate) – derzeit keine.
}


def _frame(kind: str, **fields) -> dict:
    return {"type": kind, **fields}


def _example(kind: str, **changes) -> dict:
    sender, fields = EXAMPLES[kind]
    data = {"type": kind, **copy.deepcopy(fields)}
    for key, value in changes.items():
        if value is _DROP:
            data.pop(key, None)
        else:
            data[key] = value
    return data


_DROP = object()

# Ist-Grenzen ohne Netzart (für session_state.limits)
_LIMITS = {"cpus": 2, "memory_mb": 2048, "pids": 256, "tmpfs_mb": 512}
PACKAGE_HOSTS = [
    "deb.debian.org",
    "security.debian.org",
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
    "github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
    "index.crates.io",
    "static.crates.io",
]


def _packages(**changes) -> dict:
    """``session_start`` im Netzmodus „packages“ (ohne allow)."""
    base = {
        "network": "packages",
        "allow": _DROP,
        "package_hosts": list(PACKAGE_HOSTS),
        "rootfs_mb": 4096,
    }
    return _example("session_start", **{**base, **changes})


def _strip(value, key: str | None = None):
    """``None`` aus normalisierten Rahmen entfernen (nicht in MCP-Nachrichten)."""
    if isinstance(value, dict) and key != "message":
        return {k: _strip(v, k) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_strip(v) for v in value]
    return value


def valid_cases() -> list[tuple[str, str | None, dict, str]]:
    """(Name, Absender, Rahmen, Zweck)."""
    cases = []
    for kind, (sender, fields) in sorted(EXAMPLES.items()):
        cases.append((f"example_{kind}", sender, {"type": kind, **fields}, "Beispiel je Typ"))
        cases.append((f"nosender_{kind}", None, {"type": kind, **fields}, "ohne Richtungsprüfung"))
        # Nur Pflichtfelder (sofern die Regeln über mehrere Felder das zulassen)
        spec = P.FRAMES[kind][1]
        minimal = {"type": kind, **{k: v for k, v in fields.items() if spec[k][1]}}
        try:
            P.validate(minimal, sender)
        except P.ProtocolError:
            pass
        else:
            cases.append((f"minimal_{kind}", sender, minimal, "nur Pflichtfelder"))
    s = P.AGENT
    srv = P.SERVER
    cases += [
        (
            "mcp_from_agent_result",
            s,
            _frame(
                "mcp",
                session="s1",
                pipe="p1",
                message={"jsonrpc": "2.0", "id": 1, "result": {"tools": []}},
            ),
            "MCP-Antwort",
        ),
        (
            "mcp_error_response",
            s,
            _frame(
                "mcp",
                session="s1",
                pipe="p1",
                message={"jsonrpc": "2.0", "id": "a", "error": {"code": -32601, "message": "x"}},
            ),
            "MCP-Fehler mit String-ID",
        ),
        (
            "mcp_notification",
            srv,
            _frame(
                "mcp",
                session="s1",
                pipe="p1",
                message={"jsonrpc": "2.0", "method": "notifications/initialized"},
            ),
            "Benachrichtigung ohne id",
        ),
        (
            "mcp_id_null",
            s,
            _frame(
                "mcp",
                session="s1",
                pipe="p1",
                message={"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "x"}},
            ),
            "id null bleibt in der MCP-Nachricht erhalten",
        ),
        (
            "mcp_id_negative",
            srv,
            _frame(
                "mcp",
                session="s1",
                pipe="p1",
                message={"jsonrpc": "2.0", "id": -5, "method": "ping"},
            ),
            "negative Ganzzahl-ID",
        ),
        ("optional_null", s, _example("hello", sessions=None), "optionales Feld als null"),
        (
            "optional_nested_null",
            s,
            _frame(
                "heartbeat",
                sessions=[
                    {"session": "s1", "workspace": None, "state": "running", "preview_port": None}
                ],
            ),
            "verschachtelt optional null",
        ),
        ("cpus_float", srv, _example("session_start", cpus=1.5), "Zahl mit Nachkommastellen"),
        ("cpus_int", srv, _example("session_start", cpus=2), "Zahl als Ganzzahl"),
        ("cpus_min", srv, _example("session_start", cpus=0.1), "Untergrenze"),
        ("cpus_max", srv, _example("session_start", cpus=64), "Obergrenze"),
        ("heartbeat_seconds_min", srv, _example("welcome", heartbeat_seconds=5), "Grenze"),
        ("heartbeat_seconds_max", srv, _example("welcome", heartbeat_seconds=600), "Grenze"),
        ("runner_i64_max", srv, _example("welcome", runner=2**63 - 1), "größte Runner-ID"),
        ("pty_seq_max", s, _example("pty_data", seq=2**53), "seq bis 2^53"),
        ("pty_exit_code_min", s, _example("pty_exit", code=-255), "negativer Exit-Code"),
        ("pty_exit_no_code", s, _example("pty_exit", code=_DROP), "ohne Code"),
        (
            "hostname_253_umlauts",
            s,
            _example("hello", hostname="ä" * 253),
            "Länge in Zeichen, nicht Byte (506 Byte UTF-8)",
        ),
        (
            "agent_version_64_emoji",
            s,
            _example("hello", agent_version="😀" * 64),
            "Länge in Unicode-Zeichen (Astralebene zählt 1)",
        ),
        ("id_max_len", srv, _frame("session_stop", session="a" * 64, reason="user"), "64 Zeichen"),
        (
            "id_with_punct",
            srv,
            _frame("session_stop", session="a1_.:-z", reason="user"),
            "erlaubte Sonderzeichen",
        ),
        (
            "error_message_2000",
            s,
            _frame("error", code="internal", message="ü" * 2000),
            "MAX_TEXT in Zeichen",
        ),
        (
            "b64_empty_last_chunk",
            s,
            _example("bundle_chunk", data="", size=0, sha256=hashlib.sha256(b"").hexdigest()),
            "leeres Bundle",
        ),
        (
            "b64_noncanonical_bits",
            srv,
            _example("pty_input", data="YR=="),
            "Python dekodiert nicht-kanonische Restbits (YR== = 'a')",
        ),
        (
            "b64_pty_max",
            srv,
            _example("pty_input", data=P.b64encode(b"x" * P.PTY_BYTES)),
            "genau PTY_BYTES",
        ),
        (
            "chunk_not_last",
            s,
            _example("bundle_chunk", last=False, size=_DROP, sha256=_DROP),
            "Zwischenstück ohne size/sha256",
        ),
        (
            "preview_stopped",
            s,
            _frame("preview", session="s1", running=False),
            "Vorschau aus ohne port/token",
        ),
        (
            "preview_response_later_chunk",
            s,
            _frame(
                "preview_response",
                session="s1",
                request="r1",
                seq=1,
                data=P.b64encode(b"x"),
                last=True,
            ),
            "Folgestück ohne status",
        ),
        ("allow_with_allowlist", srv, _example("session_start"), "allow nur mit allowlist"),
        (
            "network_none_empty_allow",
            srv,
            _example("session_start", network="none", allow=[]),
            "leere Liste allow bei network none",
        ),
        ("headers_empty", srv, _example("preview_request", headers={}), "leere Header"),
        (
            "headers_50",
            srv,
            _example("preview_request", headers={f"X-H{i}": "v" for i in range(50)}),
            "50 Header",
        ),
        (
            "image_with_port",
            srv,
            _example("welcome", images=["localhost:5000/mgpt/work:dev@sha256:" + "d" * 64]),
            "Registry mit Port",
        ),
        ("commit_sha256", srv, _example("bundle_ack", head="e" * 64), "SHA-256-Commit"),
        (
            "scratch_container",
            srv,
            _example("session_start", scratch_mode="container", workspace_uuid=_DROP),
            "Scratchpad im Container",
        ),
        # Paketquellen (Netzmodus „packages“, pkg-server)
        ("packages_session_start", srv, _packages(), "Paketquellen: Hosts und Schicht"),
        (
            "packages_hosts_50",
            srv,
            _packages(package_hosts=[f"h{i}.example.org" for i in range(50)]),
            "50 Paket-Hosts",
        ),
        (
            "packages_host_forms",
            srv,
            _packages(package_hosts=["a.io", "x-1.y-2.example", "1a.example.com"]),
            "Ziffern und Bindestriche in Labels, TLD aus Buchstaben",
        ),
        ("packages_rootfs_min", srv, _packages(rootfs_mb=64), "Untergrenze rootfs_mb"),
        ("packages_rootfs_max", srv, _packages(rootfs_mb=1048576), "Obergrenze rootfs_mb"),
        (
            "packages_null_allow",
            srv,
            _packages(allow=None),
            "allow null bei packages",
        ),
        (
            "limits_packages",
            s,
            _example(
                "session_state",
                limits={**_LIMITS, "network": "packages", "rootfs_mb": 4096},
            ),
            "Ist-Grenzen im Modus packages",
        ),
        (
            "heartbeat_rootfs_used",
            s,
            _frame(
                "heartbeat",
                sessions=[
                    {"session": "s1", "state": "running", "rootfs_used_mb": 0},
                    {"session": "s2", "state": "running", "rootfs_used_mb": 1048576},
                ],
            ),
            "gemessene Schicht (Grenzen)",
        ),
        (
            "frame_exactly_max",
            srv,
            _frame(
                "mcp",
                session="s1",
                pipe="p1",
                message={"jsonrpc": "2.0", "method": "x", "params": {"pad": ""}},
            ),
            "Rahmen genau MAX_FRAME_BYTES",
        ),
    ]
    return cases


def invalid_cases() -> list[tuple[str, str | None, dict | str, str]]:
    """(Name, Absender, Rahmen oder roher Text, Zweck)."""
    s, srv = P.AGENT, P.SERVER
    cases = [
        ("not_json", None, "{type:hello}", "kein JSON"),
        ("empty_text", None, "", "leerer Text"),
        ("json_array", None, "[]", "kein Objekt"),
        ("json_null", None, "null", "kein Objekt"),
        ("json_string", None, '"hello"', "kein Objekt"),
        (
            "nan_number",
            srv,
            json.dumps(_example("session_start")).replace('"cpus": 2', '"cpus": NaN'),
            "NaN ist kein JSON",
        ),
        (
            "infinity_number",
            srv,
            json.dumps(_example("session_start")).replace('"cpus": 2', '"cpus": Infinity'),
            "Infinity ist kein JSON",
        ),
        ("type_missing", None, {"session": "s1"}, "ohne type"),
        ("type_unknown", None, {"type": "shell", "cmd": "id"}, "unbekannter Typ"),
        ("type_not_string", None, {"type": 1}, "type keine Zeichenkette"),
        ("type_case", None, _example("heartbeat") | {"type": "Heartbeat"}, "Groß/klein"),
        ("unknown_field", s, _example("heartbeat") | {"extra": 1}, "unbekanntes Feld"),
        (
            "unknown_nested_field",
            s,
            _frame("heartbeat", sessions=[{"session": "s1", "state": "running", "pid": 1}]),
            "unbekanntes verschachteltes Feld",
        ),
        ("required_missing", srv, _example("session_start", image=_DROP), "Pflichtfeld fehlt"),
        ("required_null", srv, _example("session_start", image=None), "Pflichtfeld null"),
        ("wrong_direction_hello", srv, _example("hello"), "hello kommt nur vom Agent"),
        ("wrong_direction_session_start", s, _example("session_start"), "nur vom Server"),
        ("wrong_direction_file_chunk", srv, _example("file_chunk"), "nur vom Agent"),
        ("wrong_direction_close", s, _example("close"), "close nur vom Server"),
        ("bool_as_int", srv, _example("welcome", heartbeat_seconds=True), "bool ist keine Zahl"),
        ("bool_as_num", srv, _example("session_start", cpus=True), "bool ist keine Zahl"),
        (
            "float_as_int",
            srv,
            _example("welcome", heartbeat_seconds=30.0),
            "1.0 ist keine Ganzzahl",
        ),
        ("string_as_int", srv, _example("welcome", heartbeat_seconds="30"), "Text statt Zahl"),
        ("int_as_bool", srv, _example("bundle_ack", ok=1), "1 ist kein bool"),
        ("heartbeat_seconds_low", srv, _example("welcome", heartbeat_seconds=4), "Grenze"),
        ("heartbeat_seconds_high", srv, _example("welcome", heartbeat_seconds=601), "Grenze"),
        ("runner_zero", srv, _example("welcome", runner=0), "Untergrenze 1"),
        ("runner_i64_overflow", srv, _example("welcome", runner=2**63), "über i64"),
        ("pty_seq_overflow", s, _example("pty_data", seq=2**53 + 1), "über 2^53"),
        ("negative_seq", s, _example("bundle_chunk", seq=-1), "negativ"),
        ("pty_exit_code_low", s, _example("pty_exit", code=-256), "Grenze"),
        ("cpus_low", srv, _example("session_start", cpus=0.05), "Grenze"),
        ("cpus_high", srv, _example("session_start", cpus=64.5), "Grenze"),
        (
            "max_frame_bytes_high",
            srv,
            _example("welcome", max_frame_bytes=P.MAX_FRAME_BYTES + 1),
            "Grenze",
        ),
        ("preview_port_low", s, _example("preview", port=1023), "nur 1024–65535"),
        ("preview_port_high", s, _example("preview", port=65536), "nur 1024–65535"),
        ("hostname_254_umlauts", s, _example("hello", hostname="ä" * 254), "254 Zeichen"),
        ("agent_version_65", s, _example("hello", agent_version="v" * 65), "65 Zeichen"),
        (
            "error_message_2001",
            s,
            _frame("error", code="internal", message="x" * 2001),
            "über MAX_TEXT",
        ),
        ("id_too_long", srv, _frame("session_stop", session="a" * 65, reason="user"), "65"),
        ("id_leading_dash", srv, _frame("session_stop", session="-a", reason="user"), "Format"),
        ("id_space", srv, _frame("session_stop", session="a b", reason="user"), "Format"),
        ("id_umlaut", srv, _frame("session_stop", session="sä", reason="user"), "nur ASCII"),
        ("id_slash", srv, _frame("session_stop", session="a/../b", reason="user"), "Pfad"),
        (
            "id_trailing_newline",
            srv,
            _frame("session_stop", session="s1\n", reason="user"),
            "kein \\n am Ende",
        ),
        (
            "commit_trailing_newline",
            srv,
            _example("bundle_ack", head="c" * 40 + "\n"),
            "kein \\n am Ende",
        ),
        ("token_trailing_newline", s, _example("hello", token=TOKEN + "\n"), "kein \\n am Ende"),
        (
            "image_trailing_newline",
            srv,
            _example("welcome", images=[IMAGE + "\n"]),
            "kein \\n am Ende",
        ),
        (
            "header_name_trailing_newline",
            srv,
            _example("preview_request", headers={"Accept\n": "x"}),
            "kein \\n am Header-Namen",
        ),
        (
            "header_value_newline",
            srv,
            _example("preview_request", headers={"Accept": "a\r\nSet-Cookie: x"}),
            "Header-Injection",
        ),
        (
            "header_value_long",
            srv,
            _example("preview_request", headers={"A": "x" * 4097}),
            "über 4096",
        ),
        ("header_name_space", srv, _example("preview_request", headers={"X Y": "1"}), "Format"),
        (
            "headers_51",
            srv,
            _example("preview_request", headers={f"X-H{i}": "v" for i in range(51)}),
            "über 50",
        ),
        (
            "header_value_number",
            srv,
            _example("preview_request", headers={"A": 1}),
            "Wert kein Text",
        ),
        ("enum_unknown", srv, _frame("session_stop", session="s1", reason="bored"), "Aufzählung"),
        (
            "engine_unknown",
            s,
            _example("hello", engine={"name": "lxc", "version": "1", "rootless": True}),
            "Aufzählung verschachtelt",
        ),
        ("method_post", srv, _example("preview_request", method="POST"), "nur GET/HEAD"),
        (
            "image_without_digest",
            srv,
            _example("session_start", image="ghcr.io/x/y:latest"),
            "Image nur mit Digest",
        ),
        (
            "image_uppercase",
            srv,
            _example("session_start", image="GHCR.io/x@sha256:" + "a" * 64),
            "Format",
        ),
        ("sha_uppercase", srv, _example("bundle_ack", head="C" * 40), "nur klein hex"),
        ("commit_41", srv, _example("bundle_ack", head="c" * 41), "Länge"),
        (
            "uuid_uppercase",
            srv,
            _example("session_start", workspace_uuid="6F1C2A8E-5B7D-4C3E-9F10-2A3B4C5D6E7F"),
            "kanonisch klein",
        ),
        ("mime_bad", s, _example("file_chunk", mime="image"), "MIME-Format"),
        ("token_short", s, _example("hello", token="x" * 15), "Token zu kurz"),
        ("token_bad_char", s, _example("hello", token="x" * 20 + "!"), "Token-Zeichen"),
        (
            "allow_without_allowlist",
            srv,
            _example("session_start", network="none"),
            "allow nur mit allowlist",
        ),
        (
            "allow_too_many",
            srv,
            _example("session_start", allow=[{"host": "h", "port": 1}] * 51),
            "über 50",
        ),
        (
            "allow_port_zero",
            srv,
            _example("session_start", allow=[{"host": "h", "port": 0}]),
            "Port 0",
        ),
        (
            "allow_host_underscore",
            srv,
            _example("session_start", allow=[{"host": "a_b", "port": 22}]),
            "Host-Format",
        ),
        # Paketquellen (Netzmodus „packages“, pkg-server)
        ("packages_without_hosts", srv, _packages(package_hosts=_DROP), "Pflicht bei packages"),
        ("packages_empty_hosts", srv, _packages(package_hosts=[]), "nicht leer"),
        ("packages_without_rootfs", srv, _packages(rootfs_mb=_DROP), "Pflicht bei packages"),
        (
            "packages_with_allow",
            srv,
            _packages(allow=[{"host": "nas.local", "port": 22}]),
            "allow nur mit allowlist",
        ),
        (
            "hosts_without_packages",
            srv,
            _example("session_start", package_hosts=["deb.debian.org"]),
            "package_hosts nur mit packages",
        ),
        (
            "rootfs_without_packages",
            srv,
            _example("session_start", network="none", allow=_DROP, rootfs_mb=4096),
            "rootfs_mb nur mit packages",
        ),
        ("packages_rootfs_low", srv, _packages(rootfs_mb=63), "Grenze"),
        ("packages_rootfs_high", srv, _packages(rootfs_mb=1048577), "Grenze"),
        (
            "packages_hosts_51",
            srv,
            _packages(package_hosts=[f"h{i}.example.org" for i in range(51)]),
            "höchstens 50",
        ),
        (
            "packages_host_too_long",
            srv,
            _packages(package_hosts=[".".join(["a" * 60] * 4) + ".example.org"]),
            "über 253 Zeichen",
        ),
        (
            "packages_host_ip",
            srv,
            _packages(package_hosts=["192.168.1.10"]),
            "keine IP",
        ),
        (
            "packages_host_port",
            srv,
            _packages(package_hosts=["deb.debian.org:80"]),
            "kein Port",
        ),
        (
            "packages_host_upper",
            srv,
            _packages(package_hosts=["Deb.Debian.org"]),
            "nur Kleinbuchstaben",
        ),
        (
            "packages_host_wildcard",
            srv,
            _packages(package_hosts=["*.debian.org"]),
            "kein Joker",
        ),
        (
            "packages_host_single_label",
            srv,
            _packages(package_hosts=["localhost"]),
            "mindestens zwei Labels",
        ),
        (
            "packages_host_trailing_dot",
            srv,
            _packages(package_hosts=["deb.debian.org."]),
            "kein Punkt am Ende",
        ),
        (
            "packages_host_numeric_tld",
            srv,
            _packages(package_hosts=["a.b.123"]),
            "TLD nur Buchstaben",
        ),
        (
            "packages_host_trailing_newline",
            srv,
            _packages(package_hosts=["deb.debian.org\n"]),
            "kein Zeilenumbruch",
        ),
        (
            "packages_host_leading_hyphen",
            srv,
            _packages(package_hosts=["-a.example.org"]),
            "Label beginnt mit Bindestrich",
        ),
        (
            "packages_host_underscore",
            srv,
            _packages(package_hosts=["a_b.example.org"]),
            "kein Unterstrich",
        ),
        (
            "packages_host_long_label",
            srv,
            _packages(
                package_hosts=[
                    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.org"
                ]
            ),
            "Label über 63 Zeichen",
        ),
        (
            "packages_host_empty",
            srv,
            _packages(package_hosts=[""]),
            "leer",
        ),
        (
            "limits_rootfs_low",
            s,
            _example("session_state", limits={**_LIMITS, "network": "packages", "rootfs_mb": 63}),
            "Grenze",
        ),
        (
            "limits_network_unknown",
            s,
            _example("session_state", limits={**_LIMITS, "network": "proxy"}),
            "unbekannte Netzart",
        ),
        (
            "heartbeat_rootfs_negative",
            s,
            _frame(
                "heartbeat", sessions=[{"session": "s1", "state": "running", "rootfs_used_mb": -1}]
            ),
            "Grenze",
        ),
        (
            "heartbeat_rootfs_high",
            s,
            _frame(
                "heartbeat",
                sessions=[{"session": "s1", "state": "running", "rootfs_used_mb": 1048577}],
            ),
            "Grenze",
        ),
        ("list_not_list", s, _frame("heartbeat", sessions={"session": "s1"}), "Liste erwartet"),
        ("object_not_object", s, _example("hello", engine="podman"), "Objekt erwartet"),
        ("last_chunk_without_size", s, _example("bundle_chunk", size=_DROP), "letztes Stück"),
        ("last_chunk_without_sha", s, _example("file_chunk", sha256=_DROP), "letztes Stück"),
        ("preview_running_without_token", s, _example("preview", token=_DROP), "port+token"),
        (
            "preview_response_first_without_status",
            s,
            _example("preview_response", status=_DROP),
            "status im ersten Stück",
        ),
        ("b64_bad_padding", srv, _example("pty_input", data="YQ"), "Padding fehlt"),
        ("b64_excess_padding", srv, _example("pty_input", data="YWJj===="), "zu viel Padding"),
        ("b64_data_after_padding", srv, _example("pty_input", data="YQ==YQ=="), "Daten danach"),
        ("b64_newline", srv, _example("pty_input", data="YWJj\n"), "Zeilenumbruch"),
        ("b64_space", srv, _example("pty_input", data="YW Jj"), "Leerzeichen"),
        ("b64_urlsafe", srv, _example("pty_input", data="-_-_"), "URL-Alphabet"),
        (
            "b64_pty_over",
            srv,
            _example("pty_input", data=P.b64encode(b"x" * (P.PTY_BYTES + 1))),
            "über PTY_BYTES",
        ),
        ("b64_not_string", srv, _example("pty_input", data=[1, 2]), "kein Text"),
        (
            "jsonrpc_version",
            srv,
            _frame(
                "mcp", session="s1", pipe="p1", message={"jsonrpc": "1.0", "id": 1, "method": "x"}
            ),
            "jsonrpc 2.0",
        ),
        (
            "jsonrpc_id_bool",
            srv,
            _frame(
                "mcp",
                session="s1",
                pipe="p1",
                message={"jsonrpc": "2.0", "id": True, "method": "x"},
            ),
            "id bool",
        ),
        (
            "jsonrpc_id_float",
            srv,
            _frame(
                "mcp", session="s1", pipe="p1", message={"jsonrpc": "2.0", "id": 1.5, "method": "x"}
            ),
            "id Bruch",
        ),
        (
            "jsonrpc_id_float_integral",
            srv,
            _frame(
                "mcp", session="s1", pipe="p1", message={"jsonrpc": "2.0", "id": 1.0, "method": "x"}
            ),
            "id 1.0 ist keine Ganzzahl",
        ),
        (
            "jsonrpc_id_object",
            srv,
            _frame(
                "mcp", session="s1", pipe="p1", message={"jsonrpc": "2.0", "id": {}, "method": "x"}
            ),
            "id Objekt",
        ),
        (
            "jsonrpc_method_empty",
            srv,
            _frame(
                "mcp", session="s1", pipe="p1", message={"jsonrpc": "2.0", "id": 1, "method": ""}
            ),
            "method leer",
        ),
        (
            "jsonrpc_method_number",
            srv,
            _frame(
                "mcp", session="s1", pipe="p1", message={"jsonrpc": "2.0", "id": 1, "method": 5}
            ),
            "method Zahl",
        ),
        (
            "jsonrpc_nothing",
            srv,
            _frame("mcp", session="s1", pipe="p1", message={"jsonrpc": "2.0", "id": 1}),
            "weder method noch result/error",
        ),
        (
            "jsonrpc_not_object",
            srv,
            _frame("mcp", session="s1", pipe="p1", message=[{"jsonrpc": "2.0"}]),
            "Batch nicht erlaubt",
        ),
        (
            "duplicate_key",
            srv,
            '{"type":"session_stop","session":"s1","reason":"bored","reason":"user"}',
            "doppelte Schlüssel ablehnen (Parser-Differenz)",
        ),
        (
            "lone_surrogate",
            s,
            json.dumps(_example("hello")).replace('"laptop"', '"\\ud800"'),
            "einzelnes Surrogat",
        ),
        (
            "frame_over_max",
            srv,
            _frame(
                "mcp",
                session="s1",
                pipe="p1",
                message={"jsonrpc": "2.0", "method": "x", "params": {"pad": ""}},
            ),
            "Rahmen über MAX_FRAME_BYTES",
        ),
    ]
    return cases


PADDED = {"frame_exactly_max": P.MAX_FRAME_BYTES, "frame_over_max": P.MAX_FRAME_BYTES + 1}


def compact(data) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def padded_text(frame: dict, total: int) -> str:
    """Feld message.params.pad mit 'a' auffüllen, bis der kompakte Text ``total`` Byte hat."""
    base = len(compact(frame).encode())
    data = copy.deepcopy(frame)
    data["message"]["params"]["pad"] = "a" * (total - base)
    text = compact(data)
    assert len(text.encode()) == total
    return text


def case_text(entry: dict) -> str:
    """Den zu prüfenden Text eines Vektors bauen (wie es auch der Rust-Test tun muss)."""
    if "text" in entry:
        return entry["text"]
    if "pad_total_bytes" in entry:
        return padded_text(entry["frame"], entry["pad_total_bytes"])
    return compact(entry["frame"])


def build() -> dict[str, dict]:
    """Alle Vektoren als {relativer Pfad: Inhalt}; prüft dabei gegen protocol.py."""
    out: dict[str, dict] = {}
    for name, sender, frame, note in valid_cases():
        entry = {"sender": sender, "frame": frame, "note": note}
        if name in PADDED:
            entry["pad_total_bytes"] = PADDED[name]
        parsed = P.parse(case_text(entry), sender)  # muss gültig sein
        canonical = _strip(parsed)
        if name not in PADDED and canonical != frame:
            entry["canonical"] = canonical
        assert f"valid/{name}.json" not in out, name
        out[f"valid/{name}.json"] = entry
    for name, sender, frame, note in invalid_cases():
        entry = {"sender": sender, "note": note}
        if isinstance(frame, str):
            entry["text"] = frame
        else:
            entry["frame"] = frame
        if name in PADDED:
            entry["pad_total_bytes"] = PADDED[name]
        entry["close_code"] = P.CLOSE_TOO_BIG if name in PADDED else P.CLOSE_PROTOCOL
        assert f"invalid/{name}.json" not in out, name
        out[f"invalid/{name}.json"] = entry
    return out


def render(entry: dict) -> str:
    return json.dumps(entry, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def write(root: Path = FRAMES_DIR) -> int:
    for sub in ("valid", "invalid"):
        (root / sub).mkdir(parents=True, exist_ok=True)
        for old in (root / sub).glob("*.json"):
            old.unlink()
    files = build()
    for rel, entry in files.items():
        (root / rel).write_text(render(entry), encoding="utf-8")
    return len(files)


if __name__ == "__main__":
    print(f"{write()} Vektoren nach {FRAMES_DIR}")
