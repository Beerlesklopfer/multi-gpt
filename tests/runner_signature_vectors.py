"""Gemeinsame Testvektoren der Server-Signaturen (Protokoll 2, Python ↔ Rust, rs-e2e).

Orakel ist ``protocol.wrap_signed``/``protocol.open_signed``. Der Schlüssel ist
ein fester TEST-Schlüssel (Seed öffentlich, nur für Tests, nie in einer
Installation). Ed25519 signiert deterministisch, die Dateien sind also stabil::

    python -m tests.runner_signature_vectors     # runner-rs/testdata/signatures/ neu schreiben

Format je Datei::

    {"public_key": "<Base64, 32 Byte>",   # Installationsschlüssel aus MGPT_SERVER_KEY_FILE
     "key_id": "<16 hex>",                # Kennung dieses Schlüssels
     "nonce": "<64 hex>",                 # nonce aus dem eigenen hello
     "text": "<empfangener Textrahmen>",
     "expect": "ok" | "reject",
     "inner": {...},                      # nur ok: innerer Rahmen ohne null-Felder
     "close_code": 4498 | 4400,           # nur reject
     "note": "..."}

Ablauf wie im Agent: ``parse(text, SERVER)`` (Umschlag), dann ``open_signed``
(Typ ``signed``, Schlüsselkennung, Signatur über
``b"multigpt-runner-frame/2\\n" + nonce + b"\\n" + payload``, innerer Rahmen mit
``parse(payload, SERVER)``, kein Umschlag im Umschlag).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from multigpt.runner import protocol as P

ROOT = Path(__file__).resolve().parent.parent
SIG_DIR = ROOT / "runner-rs" / "testdata" / "signatures"

# Feste Test-Schlüssel (öffentlich bekannt, nur Tests).
TEST_SEED = bytes(range(32))
OTHER_SEED = bytes(range(32, 64))
NONCE = "a1" * 32
OTHER_NONCE = "b2" * 32
IMAGE = "ghcr.io/beerlesklopfer/multigpt-runner-work:1@sha256:" + "a" * 64


def _key(seed: bytes) -> tuple[Ed25519PrivateKey, bytes]:
    private = Ed25519PrivateKey.from_private_bytes(seed)
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private, public


PRIVATE, PUBLIC = _key(TEST_SEED)
OTHER_PRIVATE, OTHER_PUBLIC = _key(OTHER_SEED)
KEY_ID = P.key_id(PUBLIC)
OTHER_KEY_ID = P.key_id(OTHER_PUBLIC)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _compact(data) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _envelope(payload: bytes, *, nonce=NONCE, private=PRIVATE, key=KEY_ID, sig=None) -> str:
    if sig is None:
        sig = private.sign(P.signed_message(nonce, payload))
    return _compact({"type": "signed", "key": key, "payload": _b64(payload), "sig": _b64(sig)})


WELCOME = {
    "type": "welcome",
    "runner": 7,
    "protocol": P.PROTOCOL_VERSION,
    "server_version": "0.4",
    "heartbeat_seconds": 30,
    "images": [IMAGE],
    "max_frame_bytes": P.MAX_FRAME_BYTES,
    "chunk_bytes": P.CHUNK_BYTES,
    "services": ["read", "write", "bash"],
    "catalog": "valid",
}
CLOSE = {"type": "close", "reason": "revoked", "message": "Token widerrufen – bitte neu koppeln"}
MCP = {
    "type": "mcp",
    "session": "s1",
    "pipe": "p1",
    "message": {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "write", "arguments": {"path": "ä/ö.txt", "content": "😀\n"}},
    },
}
STOP = {"type": "session_stop", "session": "s1", "reason": "quota"}
HELLO = {
    "type": "hello",
    "token": "mgrt_abcd1234_" + "x" * 43,
    "protocol": P.PROTOCOL_VERSION,
    "agent_version": "0.4.0",
    "engine": {"name": "podman", "version": "5.4.2", "rootless": True},
    "hostname": "laptop",
    "nonce": NONCE,
}


def _payload(frame: dict) -> bytes:
    return _compact(frame).encode("utf-8")


def cases():
    """(Name, Text, Erwartung, Close-Code, Zweck)."""
    ok = P.CLOSE_SIGNATURE
    for name, frame in (
        ("welcome", WELCOME),
        ("close", CLOSE),
        ("mcp_unicode", MCP),
        ("session_stop", STOP),
    ):
        yield f"ok_{name}", _envelope(_payload(frame)), "ok", None, "gültig signiert"
    welcome = _payload(WELCOME)
    tampered = _payload({**WELCOME, "services": ["read", "write", "bash", "setup_host"]})
    good_sig = PRIVATE.sign(P.signed_message(NONCE, welcome))
    yield "tampered_payload", _envelope(tampered, sig=good_sig), "reject", ok, "payload verändert"
    yield "wrong_nonce", _envelope(welcome, nonce=OTHER_NONCE), "reject", ok, "andere Verbindung"
    yield "wrong_key_id", _envelope(welcome, key=OTHER_KEY_ID), "reject", ok, "fremde Kennung"
    yield (
        "foreign_key",
        _envelope(welcome, private=OTHER_PRIVATE),
        "reject",
        ok,
        "fremder Schlüssel",
    )
    yield (
        "foreign_key_own_id",
        _envelope(welcome, private=OTHER_PRIVATE, key=OTHER_KEY_ID),
        "reject",
        ok,
        "fremder Schlüssel mit passender eigener Kennung",
    )
    yield "sig_zero", _envelope(welcome, sig=bytes(64)), "reject", ok, "Nullsignatur"
    yield (
        "sig_without_context",
        _envelope(welcome, sig=PRIVATE.sign(welcome)),
        "reject",
        ok,
        "Signatur ohne Kontext/nonce",
    )
    yield (
        "sig_63_bytes",
        _envelope(welcome, sig=good_sig[:63]),
        "reject",
        P.CLOSE_PROTOCOL,
        "Signatur zu kurz (Schema)",
    )
    yield "unsigned_welcome", _compact(WELCOME), "reject", ok, "Rahmen ohne Umschlag"
    yield "unsigned_close", _compact(CLOSE), "reject", ok, "auch close nur signiert"
    nested = _envelope(welcome).encode("utf-8")
    yield "nested_envelope", _envelope(nested), "reject", ok, "Umschlag im Umschlag"
    bad_inner = _payload({**STOP, "extra": 1})
    yield "inner_unknown_field", _envelope(bad_inner), "reject", P.CLOSE_PROTOCOL, "innen ungültig"
    yield (
        "inner_agent_frame",
        _envelope(_payload(HELLO)),
        "reject",
        P.CLOSE_PROTOCOL,
        "innen falsche Richtung",
    )
    yield (
        "inner_not_utf8",
        _envelope(b'{"type":"close","reason":"revoked","message":"\xff"}'),
        "reject",
        P.CLOSE_PROTOCOL,
        "innen kein UTF-8",
    )
    yield "inner_not_json", _envelope(b"welcome"), "reject", P.CLOSE_PROTOCOL, "innen kein JSON"
    env = json.loads(_envelope(welcome))
    yield (
        "envelope_extra_field",
        _compact({**env, "alg": "none"}),
        "reject",
        P.CLOSE_PROTOCOL,
        "Umschlag mit fremdem Feld",
    )
    yield (
        "envelope_key_upper",
        _compact({**env, "key": KEY_ID.upper()}),
        "reject",
        P.CLOSE_PROTOCOL,
        "Kennung nur klein hex",
    )
    yield (
        "envelope_payload_not_b64",
        _compact({**env, "payload": "e30"}),
        "reject",
        P.CLOSE_PROTOCOL,
        "payload kein Base64",
    )


def evaluate(entry: dict):
    """(Ergebnis, innerer Rahmen bzw. Close-Code) nach Python."""
    public = base64.b64decode(entry["public_key"])
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    def verify(message: bytes, sig: bytes) -> bool:
        try:
            Ed25519PublicKey.from_public_bytes(public).verify(sig, message)
        except InvalidSignature:
            return False
        return True

    try:
        outer = P.parse(entry["text"], P.SERVER)
        inner = P.open_signed(outer, entry["nonce"], entry["key_id"], verify)
    except P.ProtocolError as exc:
        return "reject", exc.close_code
    return "ok", {k: v for k, v in inner.items() if v is not None}


def build() -> dict[str, dict]:
    out = {}
    for name, text, expect, code, note in cases():
        entry = {
            "public_key": _b64(PUBLIC),
            "key_id": KEY_ID,
            "nonce": NONCE,
            "text": text,
            "expect": expect,
            "note": note,
        }
        if expect == "ok":
            result, inner = evaluate(entry)
            assert result == "ok", (name, inner)
            entry["inner"] = inner
        else:
            entry["close_code"] = code
        assert f"{name}.json" not in out
        out[f"{name}.json"] = entry
    return out


def render(entry: dict) -> str:
    return json.dumps(entry, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def write(root: Path = SIG_DIR) -> int:
    root.mkdir(parents=True, exist_ok=True)
    for old in root.glob("*.json"):
        old.unlink()
    files = build()
    for name, entry in files.items():
        (root / name).write_text(render(entry), encoding="utf-8")
    return len(files)


if __name__ == "__main__":
    print(f"{write()} Signatur-Vektoren nach {SIG_DIR}")
