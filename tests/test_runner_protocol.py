"""Rahmenprotokoll Runner-Agent ↔ Runner-Dienst (M14-03, ``runner/protocol.py``):
gültige und ungültige Rahmen, unbekannte Felder und Typen, Richtung, Größen,
Bundle-Stücke mit Prüfsumme und Gleichheit der Kopie im Agent-Paket."""

import hashlib
import json
from pathlib import Path

import pytest

from multigpt.runner import protocol as P

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "mgrt_abcd1234_" + "x" * 43
IMAGE = "ghcr.io/beerlesklopfer/multigpt-runner-work:0.5@sha256:" + "a" * 64
SHA = "b" * 64
COMMIT = "c" * 40

# Ein gültiges Beispiel je Rahmentyp: (Absender, Felder).
EXAMPLES = {
    "hello": (
        P.AGENT,
        {
            "token": TOKEN,
            "protocol": 1,
            "agent_version": "0.5.0",
            "engine": {"name": "podman", "version": "5.4.2", "rootless": True},
            "hostname": "laptop",
            "sessions": [{"session": "s1", "workspace": "3", "state": "running"}],
        },
    ),
    "welcome": (
        P.SERVER,
        {
            "runner": 7,
            "protocol": 1,
            "server_version": "0.5",
            "heartbeat_seconds": 30,
            "images": [IMAGE],
            "max_frame_bytes": P.MAX_FRAME_BYTES,
            "chunk_bytes": P.CHUNK_BYTES,
        },
    ),
    "heartbeat": (P.AGENT, {"sessions": [{"session": "s1", "state": "running"}]}),
    "session_start": (
        P.SERVER,
        {
            "session": "0b9a5d1e-3c1f-4f7a-9a37-6c2a0e1d2f30",
            "workspace": "3",
            "image": IMAGE,
            "cpus": 2,
            "memory_mb": 2048,
            "pids": 256,
            "tmpfs_mb": 512,
            "network": "allowlist",
            "allow": [{"host": "nas.local", "port": 22}],
            "command_seconds": 120,
            "output_bytes": 65536,
            "idle_minutes": 15,
            "bundle": {"size": 1234, "sha256": SHA, "head": COMMIT},
            "scratch_mode": "local",
            "scratch_mb": 1024,
            "workspace_uuid": "6f1c2a8e-5b7d-4c3e-9f10-2a3b4c5d6e7f",
        },
    ),
    "session_state": (
        P.AGENT,
        {
            "session": "s1",
            "state": "running",
            "limits": {
                "cpus": 2,
                "memory_mb": 2048,
                "pids": 256,
                "tmpfs_mb": 512,
                "network": "none",
            },
            "preview_port": 18080,
        },
    ),
    "session_stop": (P.SERVER, {"session": "s1", "reason": "idle"}),
    "mcp": (
        P.SERVER,
        {
            "session": "s1",
            "pipe": "p1",
            "message": {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        },
    ),
    "bundle_get": (P.AGENT, {"session": "s1", "transfer": "t1"}),
    "bundle_chunk": (
        P.AGENT,
        {
            "session": "s1",
            "transfer": "t1",
            "seq": 0,
            "data": P.b64encode(b"abc"),
            "last": True,
            "size": 3,
            "sha256": hashlib.sha256(b"abc").hexdigest(),
            "head": COMMIT,
            "message": "Datei geändert",
        },
    ),
    "file_chunk": (
        P.AGENT,
        {
            "session": "s1",
            "transfer": "pub-1",
            "seq": 0,
            "data": P.b64encode(b"PNG"),
            "last": True,
            "size": 3,
            "sha256": hashlib.sha256(b"PNG").hexdigest(),
            "name": "bild.png",
            "mime": "image/png",
        },
    ),
    "bundle_ack": (P.SERVER, {"session": "s1", "transfer": "t1", "ok": True, "head": COMMIT}),
    "pty_open": (P.SERVER, {"session": "s1", "pty": "t1", "cols": 80, "rows": 24}),
    "pty_input": (P.SERVER, {"session": "s1", "pty": "t1", "data": P.b64encode(b"ls\r")}),
    "pty_resize": (P.SERVER, {"session": "s1", "pty": "t1", "cols": 120, "rows": 40}),
    "pty_close": (P.SERVER, {"session": "s1", "pty": "t1"}),
    "pty_data": (
        P.AGENT,
        {"session": "s1", "pty": "t1", "seq": 0, "data": P.b64encode(b"\x1b[0m$ ")},
    ),
    "pty_exit": (P.AGENT, {"session": "s1", "pty": "t1", "code": 0}),
    "preview": (P.AGENT, {"session": "s1", "running": True, "port": 18081, "token": "z" * 32}),
    "preview_request": (
        P.SERVER,
        {
            "session": "s1",
            "request": "r1",
            "method": "GET",
            "path": "/index.html",
            "headers": {"Accept": "text/html"},
        },
    ),
    "preview_response": (
        P.AGENT,
        {
            "session": "s1",
            "request": "r1",
            "seq": 0,
            "status": 200,
            "headers": {"Content-Type": "text/html"},
            "data": P.b64encode(b"<p>Hallo</p>"),
            "last": True,
        },
    ),
    "error": (
        P.AGENT,
        {"code": "unknown_session", "message": "Sitzung unbekannt", "session": "s1"},
    ),
    "close": (P.SERVER, {"reason": "revoked", "message": "Token widerrufen"}),
}


def test_every_frame_type_has_an_example():
    assert set(EXAMPLES) == P.FRAME_TYPES


@pytest.mark.parametrize("kind", sorted(EXAMPLES))
def test_valid_frames_roundtrip(kind):
    sender, fields = EXAMPLES[kind]
    text = P.encode(kind, sender, **fields)
    frame = P.parse(text, sender)
    assert frame["type"] == kind
    for name, value in fields.items():
        assert _strip(frame[name]) == value


def _strip(value):
    """Verschachtelte optionale Felder kommen als None zurück – zum Vergleich weglassen."""
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_strip(v) for v in value]
    return value


def test_optional_fields_are_none_after_parse():
    frame = P.parse(json.dumps({"type": "session_stop", "session": "s1", "reason": "user"}))
    assert frame == {"type": "session_stop", "session": "s1", "reason": "user"}
    frame = P.parse(json.dumps({"type": "pty_exit", "session": "s1", "pty": "t1"}), P.AGENT)
    assert frame["code"] is None


@pytest.mark.parametrize(
    "data, text",
    [
        ({"type": "nope"}, "unbekannter Rahmentyp"),
        ({"session": "s1"}, "unbekannter Rahmentyp"),
        (
            {"type": "session_stop", "session": "s1", "reason": "user", "extra": 1},
            "unbekannte Felder",
        ),
        ({"type": "session_stop", "session": "s1"}, "reason: fehlt"),
        ({"type": "session_stop", "session": "s1", "reason": "keiner"}, "erlaubt"),
        ({"type": "session_stop", "session": "../etc", "reason": "user"}, "ungültiges Format"),
        ({"type": "session_stop", "session": "x" * 65, "reason": "user"}, "länger"),
        ({"type": "pty_resize", "session": "s", "pty": "p", "cols": True, "rows": 5}, "Ganzzahl"),
        ({"type": "pty_resize", "session": "s", "pty": "p", "cols": 5, "rows": 5}, "außerhalb"),
        ({"type": "pty_input", "session": "s", "pty": "p", "data": "%%%"}, "Base64"),
        ({"type": "mcp", "session": "s", "pipe": "p", "message": {"id": 1}}, "jsonrpc"),
        (
            {"type": "mcp", "session": "s", "pipe": "p", "message": {"jsonrpc": "2.0", "id": 1}},
            "weder",
        ),
    ],
)
def test_invalid_frames_are_rejected(data, text):
    with pytest.raises(P.ProtocolError, match=text) as exc:
        P.validate(data)
    assert exc.value.close_code == P.CLOSE_PROTOCOL


def test_direction_is_checked():
    sender, fields = EXAMPLES["session_start"]
    text = json.dumps({"type": "session_start", **fields})
    assert P.parse(text, P.SERVER)["type"] == "session_start"
    with pytest.raises(P.ProtocolError, match="nicht erlaubt"):
        P.parse(text, P.AGENT)
    with pytest.raises(P.ProtocolError, match="nicht erlaubt"):
        P.encode("hello", P.SERVER, **EXAMPLES["hello"][1])


def test_image_needs_digest_and_allow_needs_allowlist():
    _sender, fields = EXAMPLES["session_start"]
    with pytest.raises(P.ProtocolError, match="image"):
        P.validate({"type": "session_start", **fields, "image": "debian:latest"})
    with pytest.raises(P.ProtocolError, match="allowlist"):
        P.validate({"type": "session_start", **fields, "network": "none"})
    frame = P.validate({"type": "session_start", **fields, "network": "none", "allow": []})
    assert frame["allow"] == []


def test_semantic_rules():
    with pytest.raises(P.ProtocolError, match="file_chunk.size"):
        P.validate(
            {
                "type": "file_chunk",
                "session": "s",
                "transfer": "t",
                "seq": 0,
                "data": "",
                "last": True,
            }
        )
    with pytest.raises(P.ProtocolError, match="nicht erlaubt"):
        P.validate(
            {
                "type": "file_chunk",
                "session": "s",
                "transfer": "t",
                "seq": 0,
                "data": "",
                "last": False,
            },
            P.SERVER,
        )
    with pytest.raises(P.ProtocolError, match="letzten Stück"):
        P.validate(
            {
                "type": "bundle_chunk",
                "session": "s",
                "transfer": "t",
                "seq": 0,
                "data": "",
                "last": True,
            }
        )
    with pytest.raises(P.ProtocolError, match="ersten Stück"):
        P.validate(
            {"type": "preview_response", "session": "s", "request": "r", "seq": 0, "last": True}
        )
    with pytest.raises(P.ProtocolError, match="port und token"):
        P.validate({"type": "preview", "session": "s", "running": True, "port": 18080})
    with pytest.raises(P.ProtocolError, match="außerhalb"):
        P.validate(
            {"type": "preview", "session": "s", "running": True, "port": 8080, "token": "z" * 32}
        )


def test_size_limits():
    big = "x" * (P.MAX_FRAME_BYTES + 1)
    with pytest.raises(P.ProtocolError) as exc:
        P.parse(big)
    assert exc.value.close_code == P.CLOSE_TOO_BIG
    chunk = P.b64encode(b"\0" * (P.CHUNK_BYTES + 1))
    with pytest.raises(P.ProtocolError, match="mehr als"):
        P.validate(
            {
                "type": "bundle_chunk",
                "session": "s",
                "transfer": "t",
                "seq": 0,
                "data": chunk,
                "last": False,
            }
        )
    with pytest.raises(P.ProtocolError, match="kein gültiges JSON"):
        P.parse("{nope")
    with pytest.raises(P.ProtocolError, match="JSON-Objekt"):
        P.parse("[1, 2]")


def test_bundle_split_and_assemble():
    data = bytes(range(256)) * 9000  # gut 2 MB, drei Stücke
    digest = hashlib.sha256(data).hexdigest()
    parts = P.split_bundle(data)
    assert [seq for seq, _part, _last in parts] == [0, 1, 2]
    assert [last for _seq, _part, last in parts] == [False, False, True]
    assembler = P.BundleAssembler("t1")
    result = None
    for seq, part, last in parts:
        fields = {
            "session": "s",
            "transfer": "t1",
            "seq": seq,
            "data": P.b64encode(part),
            "last": last,
        }
        if last:
            fields.update(size=len(data), sha256=digest)
        result = assembler.add(P.parse(P.encode("bundle_chunk", P.AGENT, **fields), P.AGENT))
        assert (result is None) == (not last)
    assert result == data


def _chunk(seq, data, last, **extra):
    return P.validate(
        {
            "type": "bundle_chunk",
            "session": "s",
            "transfer": "t1",
            "seq": seq,
            "data": P.b64encode(data),
            "last": last,
            **extra,
        }
    )


def test_bundle_assembler_rejects_bad_input():
    with pytest.raises(P.ProtocolError, match="Reihenfolge"):
        P.BundleAssembler("t1").add(_chunk(1, b"a", False))
    with pytest.raises(P.ProtocolError, match="Prüfsumme"):
        P.BundleAssembler("t1").add(_chunk(0, b"abc", True, size=3, sha256=SHA))
    with pytest.raises(P.ProtocolError, match="Größe"):
        P.BundleAssembler("t1").add(
            _chunk(0, b"abc", True, size=4, sha256=hashlib.sha256(b"abc").hexdigest())
        )
    with pytest.raises(P.ProtocolError, match="größer") as exc:
        P.BundleAssembler("t1", max_bytes=2).add(_chunk(0, b"abc", False))
    assert exc.value.close_code == P.CLOSE_TOO_BIG
    with pytest.raises(P.ProtocolError, match="andere"):
        P.BundleAssembler("t2").add(_chunk(0, b"abc", False))


def test_close_codes_and_reconnect():
    assert (P.CLOSE_TOKEN, P.CLOSE_FORBIDDEN) == (4401, 4403)
    for code in (4401, 4403, 4409, 4426):
        assert not P.should_reconnect(code)
    for code in (1006, 1011, 1012, 1013, 4402, 4408, 4429, None):
        assert P.should_reconnect(code)


def test_protocol_has_no_django_imports():
    source = (ROOT / "multigpt/runner/protocol.py").read_text()
    assert "django" not in source.lower().replace("django-importe", "")
    imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    assert all("multigpt" not in line and "django" not in line for line in imports)


def test_agent_copy_is_identical():
    copy = ROOT / "runner_agent" / "protocol.py"
    if not copy.exists():
        pytest.skip("runner_agent/protocol.py fehlt noch")
    assert copy.read_bytes() == (ROOT / "multigpt/runner/protocol.py").read_bytes(), (
        "runner_agent/protocol.py weicht ab – Kopie aktualisieren "
        "(cp multigpt/runner/protocol.py runner_agent/)"
    )
