"""Runner-Agent gegen die echte ASGI-App des Runner-Dienstes (uvicorn, Zufallsport).

Der Agent verbindet sich wie im Betrieb über ``websockets`` (hier ``ws://`` mit
``MGPT_INSECURE_DEV``), die App prüft Token und Recht in der Test-Datenbank.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
import uvicorn

from multigpt.runner import control, delivery, gateway
from multigpt.runner.models import Runner
from runner_agent.agent import Agent
from runner_agent.backoff import Backoff
from runner_agent.engine import FakeEngine
from tests.runner_gateway_support import make_token, make_user
from tests.test_runner_agent import make_config

pytestmark = pytest.mark.django_db(transaction=True, serialized_rollback=True)


@pytest.fixture
def server_port():
    config = uvicorn.Config(
        gateway.build_app(),
        host="127.0.0.1",
        port=0,
        ws="websockets",
        lifespan="on",
        log_level="warning",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn startet nicht"
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield port
    server.should_exit = True
    thread.join(10)
    delivery.shutdown()


def make_agent(tmp_path, port, secret, delays):
    token_file = tmp_path / "token"
    token_file.write_text(secret)
    cfg = make_config(
        url=f"ws://127.0.0.1:{port}/runner/ws",
        token_file=str(token_file),
        insecure_dev=True,
        hostname="laptop",
    )

    async def fake_sleep(seconds):
        delays.append(seconds)
        await asyncio.sleep(0)

    return Agent(
        cfg,
        FakeEngine(cfg, root=str(tmp_path / "engine")),
        sleep=fake_sleep,
        backoff=Backoff(rand=lambda: 0.5),
    )


async def until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while not await asyncio.to_thread(predicate):
        assert time.monotonic() < deadline, "Bedingung nicht erreicht"
        await asyncio.sleep(0.05)


def test_agent_logs_in_and_revocation_stops_reconnects(server_port, tmp_path):
    token_obj, secret = make_token(make_user("anna"))
    delays: list[float] = []
    agent = make_agent(tmp_path, server_port, secret, delays)

    def online():
        runner = Runner.objects.filter(token=token_obj).first()
        return runner is not None and runner.status == Runner.Status.ONLINE

    async def scenario():
        task = asyncio.create_task(agent.run())
        await until(lambda: agent.welcome is not None)
        await until(online)
        runner = await asyncio.to_thread(Runner.objects.get, token=token_obj)
        assert agent.welcome["runner"] == runner.pk
        # Widerruf: Server schließt mit 4401, der Agent versucht es nicht erneut
        await asyncio.to_thread(control.revoke_token, token_obj.pk)
        # Der Agent endet ohne neuen Versuch
        assert await asyncio.wait_for(task, 10) == 4401
        assert agent.delays == []

    asyncio.run(asyncio.wait_for(scenario(), 60))


def test_wrong_token_closes_4401_without_retry(server_port, tmp_path):
    make_user("bert")
    delays: list[float] = []
    agent = make_agent(tmp_path, server_port, "mgrt_" + "x" * 43, delays)

    async def scenario():
        code = await agent.run_once()
        assert code == 4401

    asyncio.run(asyncio.wait_for(scenario(), 30))


def test_role_without_right_closes_4403(server_port, tmp_path):
    tim = make_user("tim")
    _, secret = make_token(tim)
    from multigpt.accounts.models import Role

    tim.role = Role.objects.get(key="teen")
    tim.save()
    agent = make_agent(tmp_path, server_port, secret, [])

    async def scenario():
        assert await agent.run_once() == 4403

    asyncio.run(asyncio.wait_for(scenario(), 30))
