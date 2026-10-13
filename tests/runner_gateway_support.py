"""Hilfen für die Tests des Runner-Dienstes (M14-04/05): FakeRunner gegen die
ASGI-App über ``starlette.testclient.TestClient.websocket_connect``.

Die App läuft im Portal-Thread des TestClient; ihre ORM-Zugriffe gehen über
``sync_to_async`` in einen eigenen Thread mit eigener Datenbankverbindung. Die
Tests brauchen deshalb echte Commits (``transaction=True``).
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import secrets
import time
import weakref

from starlette.testclient import TestClient

from multigpt.accounts.models import Role, User
from multigpt.runner import delivery, gateway, tokens, trust
from multigpt.runner import protocol as P

PATH = gateway.PATH
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="fake-runner")


class Disconnected(Exception):
    def __init__(self, code):
        super().__init__(f"Verbindung geschlossen mit {code}")
        self.code = code


def make_user(username, role_key="adult", **extra):
    return User.objects.create_user(
        username, password="Geheim-Test-1234", role=Role.objects.get(key=role_key), **extra
    )


def make_token(user, **extra):
    created = tokens.create_token(user, "Test", **extra)
    return created.token, created.secret


@contextlib.contextmanager
def gateway_client():
    """TestClient mit Lebenszyklus (LISTEN-Verbindung) – beim Ende alles schließen."""
    app = gateway.build_app()
    try:
        with TestClient(app) as client:
            yield client
    finally:
        # Beim Ende schließt die App die DB-Verbindung ihres ORM-Threads selbst.
        delivery.shutdown()


def receive_raw(ws, timeout=5.0) -> dict:
    future = _pool.submit(ws.receive)
    try:
        return future.result(timeout)
    except concurrent.futures.TimeoutError:
        raise AssertionError(f"Kein Rahmen innerhalb {timeout} s") from None


# nonce aus dem hello je Verbindung (für die Prüfung der Signaturen)
_NONCES: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def open_frame(text: str, nonce: str) -> dict:
    """Umschlag ``signed`` prüfen (Installationsschlüssel des Tests) und öffnen."""
    key = trust.load_server_key()
    outer = P.parse(text, P.SERVER)
    return P.open_signed(
        outer,
        nonce,
        key.key_id,
        lambda message, sig: trust.verify(key.public_raw, message, sig),
    )


def receive(ws, timeout=5.0) -> dict:
    """Nächsten Rahmen vom Server (Signatur und Schema geprüft); ``Disconnected``
    beim Schließen."""
    message = receive_raw(ws, timeout)
    if message["type"] == "websocket.close":
        raise Disconnected(message.get("code"))
    return open_frame(message["text"], _NONCES[ws])


def receive_until(ws, kind, timeout=5.0) -> dict:
    """Rahmen überspringen, bis einer vom Typ ``kind`` kommt."""
    deadline = time.monotonic() + timeout
    while True:
        frame = receive(ws, max(0.05, deadline - time.monotonic()))
        if frame["type"] == kind:
            return frame


def expect_close(ws, timeout=5.0) -> int:
    """Rahmen überspringen, bis geschlossen wird; liefert den Close-Code."""
    deadline = time.monotonic() + timeout
    while True:
        message = receive_raw(ws, max(0.05, deadline - time.monotonic()))
        if message["type"] == "websocket.close":
            return message.get("code")


def send(ws, kind, **fields) -> None:
    if kind == "hello" and fields.get("nonce"):
        _NONCES[ws] = fields["nonce"]
    ws.send_text(P.encode(kind, P.AGENT, **fields))


def hello_fields(secret, **extra) -> dict:
    fields = {
        "token": secret,
        "protocol": P.PROTOCOL_VERSION,
        "agent_version": "0.0-test",
        "engine": {"name": "podman", "version": "5.4.2", "rootless": True},
        "hostname": "testrechner",
        "nonce": secrets.token_hex(32),
    }
    fields.update(extra)
    return fields


def hello(ws, secret, **extra) -> dict:
    """``hello`` senden und ``welcome`` erwarten."""
    send(ws, "hello", **hello_fields(secret, **extra))
    frame = receive(ws)
    assert frame["type"] == "welcome", frame
    return frame


def wait_for(condition, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while True:
        value = condition()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError("Bedingung nicht erfüllt")
        time.sleep(interval)
