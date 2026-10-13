"""Gleichwertigkeit Python-/Rust-Agent gegen den echten Runner-Dienst (rs-e2e).

Jeder Test läuft für beide Agenten (``agent_variant``) als eigener Prozess gegen
die ASGI-App unter uvicorn im Testprozess (Test-Datenbank, echte Commits).
Geprüft wird nur, was von außen sichtbar ist: Exit-Code, Zustand des Runners
in der Datenbank und Audit. Die Engine ist die FakeEngine (keine Container);
die Abläufe mit echtem Podman stehen in ``test_runner_parity_podman.py``.
"""

from __future__ import annotations

import time

import pytest

from multigpt.accounts.models import Role
from multigpt.runner import control, gateway, store, tokens, trust
from multigpt.runner.models import Runner, RunnerAudit
from tests.runner_gateway_support import make_token, make_user
from tests.runner_parity_support import (
    VARIANTS,
    AgentProcess,
    GatewayServer,
    agent_env,
    catalog_env,
    make_release_key,
    require_variant,
    wait_for,
)

pytestmark = pytest.mark.django_db(transaction=True, serialized_rollback=True)

EXIT_CONFIG = 2
EXIT_NO_RETRY = 3


@pytest.fixture(params=VARIANTS)
def agent_variant(request):
    require_variant(request.param)
    return request.param


@pytest.fixture
def server(monkeypatch):
    # Kurzer Heartbeat (Untergrenze des Protokolls), damit der Test ihn sieht.
    monkeypatch.setattr(gateway, "HEARTBEAT_SECONDS", 5)
    srv = GatewayServer()
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture
def agents():
    started: list[AgentProcess] = []
    yield started
    for agent in started:
        agent.stop()


def launch(agents, variant, tmp_path, env) -> AgentProcess:
    agent = AgentProcess(variant, env, tmp_path / f"agent-{len(agents)}.log")
    agents.append(agent)
    return agent


def _runner(token):
    return Runner.objects.filter(token=token).first()


def _online(token):
    runner = _runner(token)
    return runner is not None and runner.status == Runner.Status.ONLINE


def _connects(token) -> int:
    runner = _runner(token)
    if runner is None:
        return 0
    return RunnerAudit.objects.filter(kind=RunnerAudit.Kind.CONNECT, runner=runner).count()


def test_login_heartbeat_and_revocation_without_retry(
    agent_variant, server, agents, tmp_path, monkeypatch
):
    beats = []
    original = store.heartbeat

    def counting(*args, **kwargs):
        beats.append(time.monotonic())
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "heartbeat", counting)
    token, secret = make_token(make_user("anna"))
    agent = launch(agents, agent_variant, tmp_path, agent_env(tmp_path, server.url, secret))
    wait_for(lambda: _online(token), 30, "Anmeldung", agent)
    runner = _runner(token)
    assert runner.name == "paritaet"
    assert runner.engine == "fake"
    assert runner.agent_version
    # Heartbeat im Intervall aus welcome (5 s): mindestens zwei binnen 14 s
    wait_for(lambda: len(beats) >= 2, 14, "zwei Heartbeats", agent)
    assert 3.5 <= beats[1] - beats[0] <= 8, beats
    # Widerruf: 4401, der Agent endet mit Exit-Code 3 ohne neuen Versuch
    assert tokens.revoke(token)
    assert agent.wait(15) == EXIT_NO_RETRY, agent.log()
    assert _connects(token) <= 1
    wait_for(lambda: not _online(token), 10, "Runner offline nach Widerruf")


def test_wrong_token_exits_without_retry(agent_variant, server, agents, tmp_path):
    make_user("bert")
    agent = launch(
        agents, agent_variant, tmp_path, agent_env(tmp_path, server.url, "mgrt_" + "x" * 43)
    )
    assert agent.wait(20) == EXIT_NO_RETRY, agent.log()
    failed = RunnerAudit.objects.filter(kind=RunnerAudit.Kind.AUTH_FAILED).count()
    assert failed == 1  # genau ein Versuch
    assert not Runner.objects.exists()


def test_role_without_right_exits_4403(agent_variant, server, agents, tmp_path):
    tim = make_user("tim")
    _, secret = make_token(tim)
    tim.role = Role.objects.get(key="teen")
    tim.save()
    agent = launch(agents, agent_variant, tmp_path, agent_env(tmp_path, server.url, secret))
    assert agent.wait(20) == EXIT_NO_RETRY, agent.log()


def test_right_lost_while_connected_exits(agent_variant, server, agents, tmp_path):
    anna = make_user("anna")
    token, secret = make_token(anna)
    agent = launch(agents, agent_variant, tmp_path, agent_env(tmp_path, server.url, secret))
    wait_for(lambda: _online(token), 30, "Anmeldung", agent)
    anna.role = Role.objects.get(key="teen")
    anna.save()
    control.rights_changed(anna.pk)
    assert agent.wait(20) == EXIT_NO_RETRY, agent.log()


def test_reconnects_after_server_restart(agent_variant, server, agents, tmp_path):
    token, secret = make_token(make_user("anna"))
    agent = launch(agents, agent_variant, tmp_path, agent_env(tmp_path, server.url, secret))
    wait_for(lambda: _online(token), 30, "Anmeldung", agent)
    port = server.port
    first = _runner(token).connected_at
    server.stop()
    wait_for(lambda: not _online(token), 20, "Runner offline nach Neustart")
    time.sleep(1)
    restarted = GatewayServer(port=port)
    restarted.start()
    try:
        # Backoff 1 s × 2 … – nach wenigen Sekunden wieder da, Agent läuft weiter
        wait_for(lambda: _online(token), 40, "erneute Anmeldung", agent)
        assert _runner(token).connected_at > first
        assert agent.returncode is None
    finally:
        restarted.stop()


@pytest.mark.parametrize(
    "case",
    [
        "no_url",
        "plain_ws_without_dev",
        "ftp_url",
        "token_file_missing",
        "token_invalid",
        "images_empty",
        "image_without_digest",
        "fingerprint_bad",
        "server_key_missing",
        "server_key_invalid",
    ],
)
def test_configuration_errors_exit_2(agent_variant, agents, tmp_path, case):
    url = "http://127.0.0.1:9/runner/ws"
    env = agent_env(tmp_path, url, "mgrt_" + "y" * 43)
    if case == "no_url":
        env.pop("MGPT_URL")
    elif case == "plain_ws_without_dev":
        env.pop("MGPT_INSECURE_DEV")
        env.pop("MGPT_ENGINE")
    elif case == "ftp_url":
        env["MGPT_URL"] = "ftp://127.0.0.1/runner/ws"
    elif case == "token_file_missing":
        env["MGPT_TOKEN_FILE"] = str(tmp_path / "fehlt")
    elif case == "token_invalid":
        (tmp_path / "token").write_text("kein token!")
    elif case == "images_empty":
        env["MGPT_IMAGES"] = ""
    elif case == "image_without_digest":
        env["MGPT_IMAGES"] = "ghcr.io/x/y:latest"
    elif case == "fingerprint_bad":
        env["MGPT_CA_SHA256"] = "zz"
    elif case == "server_key_missing":
        env["MGPT_SERVER_KEY_FILE"] = str(tmp_path / "fehlt.pub")
    elif case == "server_key_invalid":
        (tmp_path / "server.pub").chmod(0o644)
        (tmp_path / "server.pub").write_text("kein Schlüssel\n")
    agent = launch(agents, agent_variant, tmp_path, env)
    assert agent.wait(15) == EXIT_CONFIG, agent.log()


def test_token_never_in_log(agent_variant, server, agents, tmp_path):
    token, secret = make_token(make_user("anna"))
    agent = launch(agents, agent_variant, tmp_path, agent_env(tmp_path, server.url, secret))
    wait_for(lambda: _online(token), 30, "Anmeldung", agent)
    assert tokens.revoke(token)
    agent.wait(15)
    assert secret not in agent.log()


def test_foreign_server_key_rejects_signed_frames(agent_variant, server, agents, tmp_path):
    """Signiert der Server mit einem anderen Schlüssel, lehnt der Agent jeden Rahmen ab
    (schon ``welcome``), schließt mit 4498 und endet ohne Neuversuch."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    token, secret = make_token(make_user("anna"))
    env = agent_env(tmp_path, server.url, secret)
    foreign = Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    key_file = tmp_path / "server.pub"
    key_file.chmod(0o644)
    key_file.write_text(trust.public_text(foreign) + "\n")
    agent = launch(agents, agent_variant, tmp_path, env)
    assert agent.wait(20) == EXIT_NO_RETRY, agent.log()
    assert _connects(token) <= 1


# --- TLS mit festgehaltenem Fingerprint (8i) ------------------------------------------


@pytest.fixture
def tls_server(tmp_path_factory, monkeypatch):
    from tests.test_runner_agent_connection import _self_signed

    monkeypatch.setattr(gateway, "HEARTBEAT_SECONDS", 5)
    certdir = tmp_path_factory.mktemp("cert")
    _, fingerprint = _self_signed(certdir)
    srv = GatewayServer(
        ssl_certfile=str(certdir / "cert.pem"), ssl_keyfile=str(certdir / "key.pem")
    )
    srv.start()
    yield srv, fingerprint
    srv.stop()


def test_tls_with_pinned_fingerprint(agent_variant, tls_server, agents, tmp_path):
    srv, fingerprint = tls_server
    token, secret = make_token(make_user("anna"))
    pin = ":".join(fingerprint[i : i + 2] for i in range(0, 64, 2)).upper()  # Schreibweise frei
    # MGPT_INSECURE_DEV erlaubt nur zusätzlich ws:// (und die FakeEngine); bei https
    # prüft der Agent TLS trotzdem gegen den festgehaltenen Fingerprint.
    agent = launch(
        agents, agent_variant, tmp_path, agent_env(tmp_path, srv.url, secret, MGPT_CA_SHA256=pin)
    )
    wait_for(lambda: _online(token), 30, "Anmeldung über TLS mit Pin", agent)


@pytest.mark.parametrize("pin", ["wrong", "none"])
def test_tls_wrong_or_missing_pin_never_sends_token(
    agent_variant, tls_server, agents, tmp_path, pin
):
    srv, fingerprint = tls_server
    token, secret = make_token(make_user("anna"))
    env = agent_env(tmp_path, srv.url, secret)
    if pin == "wrong":
        env["MGPT_CA_SHA256"] = "0" * 64
    agent = launch(agents, agent_variant, tmp_path, env)
    agent.wait_started()
    time.sleep(6)
    # Kein hello beim Server: weder Anmeldung noch abgelehnter Versuch
    assert not Runner.objects.filter(token=token).exists()
    assert not RunnerAudit.objects.filter(kind=RunnerAudit.Kind.AUTH_FAILED).exists()
    # Der Agent versucht es später erneut (kein Exit-Code wie bei 4401)
    assert agent.returncode is None, agent.log()


# --- Signierter Dienstkatalog (Release-Schlüssel) ------------------------------------


@pytest.fixture
def release_key(settings, tmp_path):
    return make_release_key(settings, tmp_path)


@pytest.mark.parametrize("variant_catalog", ["signed", "tampered", "unsigned"])
def test_catalog_signature_decides_services(
    agent_variant, server, agents, tmp_path, release_key, variant_catalog
):
    private, key_id = release_key
    token, secret = make_token(make_user("anna"))
    extra = {}
    if variant_catalog != "unsigned":
        extra = catalog_env(tmp_path, private, key_id, tamper=variant_catalog == "tampered")
    agent = launch(
        agents, agent_variant, tmp_path, agent_env(tmp_path, server.url, secret, **extra)
    )
    wait_for(lambda: _online(token), 30, "Anmeldung", agent)
    runner = _runner(token)
    if variant_catalog == "signed":
        assert runner.catalog_state == "valid", runner.catalog_state
        assert {"read", "write", "bash"} <= set(runner.services), runner.services
        assert "ssh" not in runner.services
    else:
        assert runner.catalog_state == ("invalid" if variant_catalog == "tampered" else "unsigned")
        assert runner.services == []
        wait_for(
            lambda: RunnerAudit.objects.filter(kind=RunnerAudit.Kind.TRUST).exists(),
            10,
            "Audit-Eintrag zum Katalog",
        )
