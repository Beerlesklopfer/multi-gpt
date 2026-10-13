"""Runner-Agent: Verbindung, Backoff (gefälschte Uhr), TLS-Pinning, Rahmen über den Draht.

Die Gegenseite ist ein kleiner ``websockets``-Server im Prozess, der je
Verbindung ein Drehbuch abspielt. Der Test gegen die echte ASGI-App des
Runner-Dienstes steht in ``test_runner_agent_gateway.py``.
"""

from __future__ import annotations

import asyncio
import datetime
import ssl

import pytest
from websockets.asyncio.server import serve

from runner_agent import protocol, tls
from runner_agent.agent import CLOSE_TLS, Agent
from runner_agent.backoff import Backoff
from runner_agent.engine import FakeEngine
from tests.test_runner_agent import IMAGE, make_config

TOKEN = "mgrt_" + "a" * 43


def welcome_text(**kw):
    values = dict(
        runner=7,
        protocol=1,
        server_version="test",
        heartbeat_seconds=30,
        images=[IMAGE],
        max_frame_bytes=protocol.MAX_FRAME_BYTES,
        chunk_bytes=protocol.CHUNK_BYTES,
    )
    values.update(kw)
    return protocol.encode("welcome", protocol.SERVER, **values)


class FakeServer:
    """WebSocket-Server, der je Verbindung ``scripts[n]`` ausführt."""

    def __init__(self, scripts, ssl_context=None):
        self.scripts = list(scripts)
        self.ssl_context = ssl_context
        self.hellos: list[dict] = []
        self.connections = 0
        self.received: list[dict] = []
        self.headers: list = []

    async def handler(self, ws):
        index = self.connections
        self.connections += 1
        self.headers.append(ws.request.headers)
        script = self.scripts[min(index, len(self.scripts) - 1)]
        await script(self, ws)

    async def hello(self, ws):
        frame = protocol.parse(await asyncio.wait_for(ws.recv(), 5), protocol.AGENT)
        assert frame["type"] == "hello"
        self.hellos.append(frame)
        return frame

    async def recv(self, ws, kind):
        while True:
            frame = protocol.parse(await asyncio.wait_for(ws.recv(), 10), protocol.AGENT)
            self.received.append(frame)
            if frame["type"] == kind:
                return frame

    async def __aenter__(self):
        self._server = await serve(self.handler, "127.0.0.1", 0, ssl=self.ssl_context)
        await self._server.__aenter__()
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        await self._server.__aexit__(*exc)


def agent_for(tmp_path, port, *, scheme="ws", delays=None, **config):
    token_file = tmp_path / "token"
    if not token_file.exists():
        token_file.write_text(TOKEN)
    cfg = make_config(
        url=f"{scheme}://127.0.0.1:{port}/runner/ws",
        token_file=str(token_file),
        insecure_dev=scheme == "ws",
        hostname="pc",
        **config,
    )
    sleeps = delays if delays is not None else []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        await asyncio.sleep(0)

    agent = Agent(
        cfg,
        FakeEngine(cfg, root=str(tmp_path / "engine")),
        sleep=fake_sleep,
        backoff=Backoff(base=1, factor=2, maximum=60, jitter=0.2, rand=lambda: 0.5),
    )
    return agent, sleeps


async def until(predicate, timeout=10):
    for _ in range(int(timeout / 0.02)):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("Bedingung nicht erreicht")


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 60))


# --- Backoff und Close-Codes -----------------------------------------------------------


def test_reconnect_with_backoff_and_exit_after_4401(tmp_path):
    async def close_1011(server, ws):
        await server.hello(ws)
        await ws.close(1011)

    async def drop(server, ws):
        await server.hello(ws)
        ws.transport.abort()

    async def welcome_then_restart(server, ws):
        await server.hello(ws)
        await ws.send(welcome_text())
        await ws.close(1012)

    async def bad_token(server, ws):
        await server.hello(ws)
        await ws.close(4401, "Token ungültig")

    async def accept(server, ws):
        await server.hello(ws)
        await ws.send(welcome_text())
        await ws.wait_closed()

    async def scenario():
        async with FakeServer([close_1011, drop, welcome_then_restart, bad_token, accept]) as srv:
            agent, sleeps = agent_for(tmp_path, srv.port)
            task = asyncio.create_task(agent.run())
            await until(lambda: srv.connections == 4 and agent.last_close == 4401)
            # Backoff: 1 s, 2 s, nach welcome zurückgesetzt wieder 1 s; nach 4401 kein Versuch
            assert agent.delays == [1.0, 2.0, 1.0]
            await asyncio.sleep(0.2)
            assert srv.connections == 4 and srv.hellos[0]["token"] == TOKEN
            # Der Agent endet (Exit-Code 3 in main), statt es erneut zu versuchen
            assert await asyncio.wait_for(task, 5) == 4401

    run(scenario())


def test_other_codes_wait_longest_and_hello_has_no_origin(tmp_path):
    async def protocol_error(server, ws):
        hello = await server.hello(ws)
        assert hello["engine"] == {"name": "fake", "version": "0", "rootless": True}
        assert hello["agent_version"] and hello["hostname"] == "pc"
        await ws.close(4400)

    async def scenario():
        async with FakeServer([protocol_error]) as srv:
            agent, _ = agent_for(tmp_path, srv.port)
            task = asyncio.create_task(agent.run())
            await until(lambda: len(agent.delays) >= 2)
            agent.stop()
            await asyncio.wait_for(task, 5)
            assert agent.delays[:2] == [60.0, 60.0]
            assert "Origin" not in srv.headers[0]
            assert TOKEN not in str(srv.headers[0])

    run(scenario())


def test_unreachable_server_backs_off(tmp_path):
    async def scenario():
        agent, _ = agent_for(tmp_path, 1)  # Port 1: Verbindung abgelehnt
        task = asyncio.create_task(agent.run())
        await until(lambda: len(agent.delays) >= 3)
        agent.stop()
        await asyncio.wait_for(task, 5)
        assert agent.delays[:3] == [1.0, 2.0, 4.0]

    run(scenario())


# --- TLS mit Fingerprint ------------------------------------------------------------------


def _self_signed(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "snakeoil")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_file, key_file)
    return ctx, tls.fingerprint(cert.public_bytes(serialization.Encoding.DER))


@pytest.mark.parametrize("pin_ok", [True, False])
def test_tls_pinning(tmp_path, pin_ok):
    server_ctx, fingerprint = _self_signed(tmp_path)

    async def accept(server, ws):
        await server.hello(ws)
        await ws.send(welcome_text())
        await ws.wait_closed()

    async def scenario():
        async with FakeServer([accept], ssl_context=server_ctx) as srv:
            pin = fingerprint if pin_ok else "0" * 64
            agent, _ = agent_for(tmp_path, srv.port, scheme="wss", ca_sha256=pin)
            if pin_ok:
                task = asyncio.create_task(agent.run())
                await until(lambda: agent.welcome is not None)
                agent.stop()
                await asyncio.wait_for(task, 5)
            else:
                code = await agent.run_once()
                assert code == CLOSE_TLS
                await asyncio.sleep(0.1)
                assert srv.hellos == []  # Token hat den Rechner nicht verlassen

    run(scenario())


def test_self_signed_without_pin_is_refused(tmp_path):
    server_ctx, _ = _self_signed(tmp_path)

    async def accept(server, ws):
        await server.hello(ws)

    async def scenario():
        async with FakeServer([accept], ssl_context=server_ctx) as srv:
            agent, _ = agent_for(tmp_path, srv.port, scheme="wss")
            assert await agent.run_once() == protocol.CLOSE_ABNORMAL
            assert srv.hellos == []

    run(scenario())


# --- Rahmen über den Draht -----------------------------------------------------------------


def test_session_and_mcp_over_websocket(tmp_path):
    results = {}

    async def full(server, ws):
        hello = await server.hello(ws)
        assert hello["sessions"] == []
        await ws.send(welcome_text())
        await ws.send(
            protocol.encode(
                "session_start",
                protocol.SERVER,
                session="11",
                workspace="5",
                image=IMAGE,
                cpus=1,
                memory_mb=256,
                pids=128,
                tmpfs_mb=32,
                network="none",
                command_seconds=30,
                output_bytes=8192,
                idle_minutes=15,
            )
        )
        state = await server.recv(ws, "session_state")
        results["state"] = state
        await ws.send(
            protocol.encode(
                "mcp",
                protocol.SERVER,
                session="11",
                pipe="pipe-1",
                message={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "1"},
                    },
                },
            )
        )
        await server.recv(ws, "mcp")
        await ws.send(
            protocol.encode(
                "mcp",
                protocol.SERVER,
                session="11",
                pipe="pipe-1",
                message={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "write", "arguments": {"path": "a.txt", "content": "x"}},
                },
            )
        )
        chunk = await server.recv(ws, "bundle_chunk")
        assert chunk["last"] and chunk["message"] == "write a.txt"
        await ws.send(
            protocol.encode(
                "bundle_ack",
                protocol.SERVER,
                session="11",
                transfer=chunk["transfer"],
                ok=True,
                head=chunk["head"],
            )
        )
        reply = await server.recv(ws, "mcp")
        results["reply"] = reply["message"]
        await ws.send(
            protocol.encode("mcp", protocol.SERVER, session="99", pipe="p", message={
                "jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        )  # fmt: skip
        results["error"] = await server.recv(ws, "error")
        await ws.send(protocol.encode("session_stop", protocol.SERVER, session="11", reason="user"))
        results["stopped"] = await server.recv(ws, "session_state")
        await ws.close(1000)

    async def scenario():
        async with FakeServer([full]) as srv:
            agent, _ = agent_for(tmp_path, srv.port)
            code = await agent.run_once()
            assert code == 1000

    run(scenario())
    assert results["state"]["state"] == "running"
    assert results["state"]["limits"]["memory_mb"] == 256
    assert results["reply"]["result"]["content"][0]["text"] == "a.txt angelegt (1 Byte)."
    assert results["error"]["code"] == "unknown_session" and results["error"]["ref"] == "p"
    assert results["stopped"]["state"] == "stopped"


def test_frames_before_welcome_are_ignored_and_invalid_frames_answered(tmp_path):
    results = {}

    async def script(server, ws):
        await server.hello(ws)
        await ws.send(protocol.encode("session_stop", protocol.SERVER, session="1", reason="user"))
        await ws.send('{"type":"welcome","evil":1}')
        results["error"] = await server.recv(ws, "error")
        await ws.close(1000)

    async def scenario():
        async with FakeServer([script]) as srv:
            agent, _ = agent_for(tmp_path, srv.port)
            assert await agent.run_once() == 1000

    run(scenario())
    assert results["error"]["code"] == "invalid_frame"
