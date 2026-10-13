"""Runner-Dienst (M14-04, ``runner/gateway.py``) mit FakeRunner: Anmeldung
(Token, Recht, Origin, Hello-Timeout, Version, Drosselung), Heartbeat und
Online-Status, Ersetzen alter Verbindungen, Widerruf über ``mgpt_runner_ctl``
in unter einer Sekunde, Ratenbegrenzung, Abgleich beim Wiederverbinden.
"""

import time
from datetime import timedelta

import pytest
from django.utils import timezone
from starlette.websockets import WebSocketDisconnect

from multigpt.runner import control, gateway, store
from multigpt.runner import protocol as P
from multigpt.runner.management.commands.runner_gateway import parse_bind, uvicorn_options
from multigpt.runner.models import Runner, RunnerAudit, RunnerSession, RunnerToken, RunnerWorkspace
from tests.runner_gateway_support import (
    PATH,
    expect_close,
    gateway_client,
    hello,
    hello_fields,
    make_token,
    make_user,
    receive,
    receive_until,
    send,
    wait_for,
)

pytestmark = pytest.mark.django_db(transaction=True, serialized_rollback=True)


@pytest.fixture
def anna():
    return make_user("anna")


@pytest.fixture
def token(anna):
    return make_token(anna)


@pytest.fixture
def client():
    with gateway_client() as client:
        yield client


def runner_of(token_obj) -> Runner:
    return Runner.objects.get(token=token_obj)


def test_hello_welcome_sets_runner_online(client, token, settings):
    settings.RUNNER_WORK_IMAGE = "ghcr.io/x/work:1@sha256:" + "a" * 64
    token_obj, secret = token
    with client.websocket_connect(PATH) as ws:
        welcome = hello(ws, secret, hostname="laptop-anna")
        runner = runner_of(token_obj)
        assert welcome["runner"] == runner.pk
        assert welcome["protocol"] == P.PROTOCOL_VERSION
        assert welcome["heartbeat_seconds"] == gateway.HEARTBEAT_SECONDS
        assert welcome["images"] == [settings.RUNNER_WORK_IMAGE]
        assert runner.status == Runner.Status.ONLINE
        assert runner.name == "laptop-anna"
        assert (runner.engine, runner.engine_version, runner.rootless) == ("podman", "5.4.2", True)
        assert RunnerAudit.objects.filter(kind=RunnerAudit.Kind.CONNECT, runner=runner).exists()
        token_obj.refresh_from_db()
        assert token_obj.last_used_at is not None
    wait_for(lambda: runner_of(token_obj).status == Runner.Status.OFFLINE)


def test_images_without_digest_are_not_offered(client, token, settings):
    settings.RUNNER_WORK_IMAGE = "ghcr.io/x/work:latest"
    with client.websocket_connect(PATH) as ws:
        assert hello(ws, token[1])["images"] == []


@pytest.mark.parametrize("variant", ["wrong", "unknown_prefix", "expired", "revoked", "inactive"])
def test_bad_token_closes_4401(client, anna, token, variant):
    token_obj, secret = token
    if variant == "wrong":
        secret = secret[:-4] + "AAAA"
    elif variant == "unknown_prefix":
        secret = "mgrt_zzzzzzzz_" + "x" * 43
    elif variant == "expired":
        RunnerToken.objects.filter(pk=token_obj.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
    elif variant == "revoked":
        RunnerToken.objects.filter(pk=token_obj.pk).update(revoked_at=timezone.now())
    elif variant == "inactive":
        anna.is_active = False
        anna.save()
    with client.websocket_connect(PATH) as ws:
        send(ws, "hello", **hello_fields(secret))
        assert expect_close(ws) == P.CLOSE_TOKEN
    assert RunnerAudit.objects.filter(kind=RunnerAudit.Kind.AUTH_FAILED).count() == 1
    assert not Runner.objects.exists()


def test_role_without_right_closes_4403(client):
    tim = make_user("tim")
    _token, secret = make_token(tim)
    tim.role = tim.role.__class__.objects.get(key="teen")
    tim.save()
    with client.websocket_connect(PATH) as ws:
        send(ws, "hello", **hello_fields(secret))
        assert expect_close(ws) == P.CLOSE_FORBIDDEN
    assert not Runner.objects.exists()


def test_origin_header_is_rejected_before_handshake(client, token):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(PATH, headers={"origin": "https://nas.example"}):
            pass
    assert exc.value.code == P.CLOSE_FORBIDDEN


def test_hello_timeout_closes_4408(client, monkeypatch):
    monkeypatch.setattr(gateway, "HELLO_TIMEOUT", 0.3)
    with client.websocket_connect(PATH) as ws:
        started = time.monotonic()
        assert expect_close(ws) == P.CLOSE_HELLO_TIMEOUT
        assert time.monotonic() - started < 3


@pytest.mark.parametrize(
    "text, code",
    [
        ("{kein json", P.CLOSE_PROTOCOL),
        ('{"type": "heartbeat", "sessions": []}', P.CLOSE_PROTOCOL),
        ('{"type": "welcome"}', P.CLOSE_PROTOCOL),
    ],
)
def test_invalid_first_frame_closes(client, text, code):
    with client.websocket_connect(PATH) as ws:
        ws.send_text(text)
        assert expect_close(ws) == code


def test_binary_frame_and_wrong_version(client, token):
    with client.websocket_connect(PATH) as ws:
        ws.send_bytes(b"\x00\x01")
        assert expect_close(ws) == P.CLOSE_PROTOCOL
    with client.websocket_connect(PATH) as ws:
        send(ws, "hello", **hello_fields(token[1], protocol=2))
        assert expect_close(ws) == P.CLOSE_VERSION


def test_failed_logins_are_throttled_per_ip(client, token, settings, monkeypatch):
    settings.RUNNER_FAILURE_LIMIT = 2
    monkeypatch.setattr(gateway, "_client_ip", lambda websocket: "192.0.2.7")
    for _ in range(2):
        with client.websocket_connect(PATH) as ws:
            send(ws, "hello", **hello_fields("mgrt_zzzzzzzz_" + "x" * 43))
            assert expect_close(ws) == P.CLOSE_TOKEN
    # Jetzt auch mit gültigem Token gesperrt, bis das Zeitfenster vorbei ist.
    with client.websocket_connect(PATH) as ws:
        assert expect_close(ws) == P.CLOSE_RATE_LIMIT
    assert set(RunnerAudit.objects.values_list("ip", flat=True)) == {"192.0.2.7"}


def test_heartbeat_updates_last_seen(client, token):
    token_obj, secret = token
    with client.websocket_connect(PATH) as ws:
        hello(ws, secret)
        old = timezone.now() - timedelta(minutes=5)
        Runner.objects.filter(token=token_obj).update(last_seen=old)
        send(ws, "heartbeat", sessions=[])
        wait_for(lambda: runner_of(token_obj).last_seen > old)


def test_heartbeat_checks_rights_freshly(client, token, anna):
    _token_obj, secret = token
    with client.websocket_connect(PATH) as ws:
        hello(ws, secret)
        # Ohne NOTIFY (z. B. Benachrichtigung verloren): der Heartbeat merkt es.
        anna.role = anna.role.__class__.objects.get(key="teen")
        anna.save()
        send(ws, "heartbeat", sessions=[])
        assert receive(ws)["type"] == "close"
        assert expect_close(ws) == P.CLOSE_FORBIDDEN


def test_revocation_disconnects_within_one_second(client, token):
    token_obj, secret = token
    with client.websocket_connect(PATH) as ws:
        hello(ws, secret)
        started = time.monotonic()
        control.revoke_token(token_obj.pk)
        frame = receive(ws, timeout=2)
        assert frame == {"type": "close", "reason": "revoked", "message": "Token widerrufen"}
        assert expect_close(ws, timeout=2) == P.CLOSE_TOKEN
        assert time.monotonic() - started < 1.0
    wait_for(lambda: runner_of(token_obj).status == Runner.Status.OFFLINE)


def test_rights_change_via_ctl_closes_4403(client, token, anna):
    _token_obj, secret = token
    with client.websocket_connect(PATH) as ws:
        hello(ws, secret)
        anna.role = anna.role.__class__.objects.get(key="teen")
        anna.save()
        control.rights_changed(anna.pk)
        assert receive_until(ws, "close")["reason"] == "rights"
        assert expect_close(ws) == P.CLOSE_FORBIDDEN


def test_new_connection_replaces_old_one(client, token):
    token_obj, secret = token
    with client.websocket_connect(PATH) as first:
        hello(first, secret)
        with client.websocket_connect(PATH) as second:
            hello(second, secret)
            assert receive_until(first, "close")["reason"] == "replaced"
            assert expect_close(first) == P.CLOSE_REPLACED
            # Die neue Verbindung bleibt, der Runner ist online.
            send(second, "heartbeat", sessions=[])
            time.sleep(0.2)
            assert runner_of(token_obj).status == Runner.Status.ONLINE
    assert Runner.objects.count() == 1


def test_rate_limit_per_token(client, token, settings):
    settings.RUNNER_RATE_BURST = 3
    settings.RUNNER_RATE_PER_SECOND = 0.01
    with client.websocket_connect(PATH) as ws:
        hello(ws, token[1])
        for _ in range(4):
            send(ws, "pty_exit", session="s1", pty="p1")
        assert expect_close(ws) == P.CLOSE_RATE_LIMIT


def test_idle_connection_is_closed(client, token, monkeypatch):
    monkeypatch.setattr(gateway, "idle_timeout", lambda: 0.3)
    with client.websocket_connect(PATH) as ws:
        hello(ws, token[1])
        assert expect_close(ws) == P.CLOSE_TRY_AGAIN


def _session(anna, runner, status=RunnerSession.Status.RUNNING, **extra):
    workspace = RunnerWorkspace.objects.create(
        owner=anna, name=f"ws{RunnerSession.objects.count()}"
    )
    session = RunnerSession.objects.create(
        owner=anna, workspace=workspace, runner=runner, status=status, **extra
    )
    RunnerWorkspace.objects.filter(pk=workspace.pk).update(active_session=session)
    return session


def test_disconnect_marks_sessions_unknown_and_hello_reconciles(client, token, anna):
    token_obj, secret = token
    with client.websocket_connect(PATH) as ws:
        hello(ws, secret)
        runner = runner_of(token_obj)
        kept = _session(anna, runner)
        gone = _session(anna, runner)
    wait_for(lambda: runner_of(token_obj).status == Runner.Status.OFFLINE)
    kept.refresh_from_db()
    assert kept.status == RunnerSession.Status.UNKNOWN

    stray = "0b9a5d1e-3c1f-4f7a-9a37-6c2a0e1d2f30"
    reported = [
        {"session": str(kept.uuid), "state": "running"},
        {"session": stray, "state": "running"},
    ]
    with client.websocket_connect(PATH) as ws:
        hello(ws, secret, sessions=reported)
        # Unbekannte Sitzung im Container: Stopp.
        assert receive(ws) == {"type": "session_stop", "session": stray, "reason": "error"}
        kept.refresh_from_db()
        gone.refresh_from_db()
        assert kept.status == RunnerSession.Status.RUNNING
        assert gone.status == RunnerSession.Status.STOPPED and gone.ended is not None
        assert not RunnerWorkspace.objects.get(pk=gone.workspace_id).active_session_id


def test_heartbeat_books_runtime_and_stops_unknown(client, token, anna, monkeypatch):
    calls = []
    from multigpt.billing import runtime

    monkeypatch.setattr(
        runtime, "record_heartbeat", lambda session, running, now=None: calls.append(running)
    )
    token_obj, secret = token
    with client.websocket_connect(PATH) as ws:
        hello(ws, secret)
        session = _session(anna, runner_of(token_obj))
        send(
            ws,
            "heartbeat",
            sessions=[
                {"session": str(session.uuid), "state": "running"},
                {"session": "fremd-1", "state": "running"},
            ],
        )
        assert receive(ws) == {"type": "session_stop", "session": "fremd-1", "reason": "error"}
        wait_for(lambda: calls == [True])


def test_session_of_other_runner_is_not_touched(client, token, anna):
    """Ein Agent kann über session_state keine fremde Sitzung ändern."""
    token_obj, secret = token
    bert = make_user("bert")
    _other_token, other_secret = make_token(bert)
    with client.websocket_connect(PATH) as ws:
        hello(ws, other_secret)
        foreign = _session(anna, None)
        send(ws, "session_state", session=str(foreign.uuid), state="stopped")
        send(ws, "session_state", session=str(foreign.uuid), state="running")
        assert receive(ws)["type"] == "session_stop"
    foreign.refresh_from_db()
    assert foreign.status == RunnerSession.Status.RUNNING and foreign.ended is None


def test_command_options():
    assert parse_bind("") == {"host": "127.0.0.1", "port": 8001}
    assert parse_bind("[::1]:9000") == {"host": "::1", "port": 9000}
    assert parse_bind("unix:/run/mgpt.sock") == {"uds": "/run/mgpt.sock"}
    options = uvicorn_options("0.0.0.0:8002")
    assert options["ws"] == "websockets-sansio" and options["workers"] == 1
    assert options["ws_max_size"] == 8 * 1024 * 1024
    assert options["server_header"] is False
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        parse_bind("nirgendwo")


def test_store_mismatch_uses_network_mode():
    assert (
        store._limits_mismatch({"cpus": 2, "network": False}, {"cpus": 2, "network": "none"}) == []
    )
    assert store._limits_mismatch(
        {"cpus": 2, "network": False}, {"cpus": 3, "network": "default"}
    ) == [
        "cpus",
        "network",
    ]
