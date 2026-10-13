"""Zustellung über LISTEN/NOTIFY (M14-05, ``runner/delivery.py``, ``listener.py``):
Rahmen an den Runner über eine zweite Verbindung und einen zweiten Prozess,
Antworten an den richtigen Abnehmer (pipe, session, pty), verlorene
Benachrichtigung beim Heartbeat, Runner offline → sofortiger Fehler, Trennen →
Fehler an wartende Abnehmer, async ``Pipe``, Steuerkanal (Stopp, Kontingent),
Bundles in Stücken in beide Richtungen und Abgleich der Ist-Grenzen.
"""

import asyncio
import concurrent.futures
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from django.db import connection, connections
from django.utils import timezone

from multigpt.runner import bundles, control, delivery, gateway
from multigpt.runner import protocol as P
from multigpt.runner.models import (
    Runner,
    RunnerAudit,
    RunnerMessage,
    RunnerSession,
    RunnerWorkspace,
)
from tests.runner_gateway_support import (
    PATH,
    expect_close,
    gateway_client,
    hello,
    make_token,
    make_user,
    receive,
    receive_until,
    send,
    wait_for,
)

pytestmark = pytest.mark.django_db(transaction=True, serialized_rollback=True)

ROOT = Path(__file__).resolve().parent.parent
_threads = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="web")


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


@pytest.fixture
def online(client, token):
    """Verbundener FakeRunner: (ws, runner)."""
    token_obj, secret = token
    with client.websocket_connect(PATH) as ws:
        hello(ws, secret)
        yield ws, Runner.objects.get(token=token_obj)


def in_thread(func, *args, **kwargs):
    """Wie ein anderer Request-Thread (eigene DB-Verbindung, danach geschlossen)."""

    def run():
        try:
            return func(*args, **kwargs)
        finally:
            connections.close_all()

    return _threads.submit(run)


def make_session(anna, runner, *, bundle=b"", **extra):
    workspace = RunnerWorkspace.objects.create(
        owner=anna,
        name=f"ws-{uuid.uuid4().hex[:6]}",
        bundle=bundle or None,
        size=len(bundle),
        sha256=hashlib.sha256(bundle).hexdigest() if bundle else "",
        head_commit="c" * 40 if bundle else "",
    )
    extra.setdefault("status", RunnerSession.Status.RUNNING)
    session = RunnerSession.objects.create(owner=anna, workspace=workspace, runner=runner, **extra)
    RunnerWorkspace.objects.filter(pk=workspace.pk).update(active_session=session)
    return session


def mcp_frame(session, pipe, message):
    return {"type": "mcp", "session": str(session), "pipe": pipe, "message": message}


def test_send_to_offline_runner_fails_immediately(token):
    token_obj, _secret = token
    runner = Runner.objects.create(token=token_obj, status=Runner.Status.OFFLINE)
    started = time.monotonic()
    with pytest.raises(delivery.RunnerOffline, match="nicht verbunden"):
        delivery.send(runner.pk, {"type": "session_stop", "session": "s1", "reason": "user"})
    # Online gemeldet, aber ohne Lebenszeichen seit 3 Heartbeats: ebenso.
    Runner.objects.filter(pk=runner.pk).update(
        status=Runner.Status.ONLINE, last_seen=timezone.now() - timedelta(minutes=5)
    )
    with pytest.raises(delivery.RunnerOffline):
        delivery.send(runner.pk, {"type": "session_stop", "session": "s1", "reason": "user"})
    assert time.monotonic() - started < 1
    assert not RunnerMessage.objects.exists()


def test_invalid_or_gateway_frames_are_refused(online):
    _ws, runner = online
    with pytest.raises(P.ProtocolError):
        delivery.send(runner.pk, {"type": "session_stop", "session": "s1"})
    with pytest.raises(ValueError, match="nur der Runner-Dienst"):
        delivery.send(runner.pk, {"type": "close", "reason": "revoked"})


def test_mcp_roundtrip_over_second_connection(online, anna):
    ws, runner = online
    session = make_session(anna, runner)
    pipe = str(uuid.uuid4())
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    with delivery.subscribe("pipe", pipe) as sub:
        started = time.monotonic()
        delivery.send(runner.pk, mcp_frame(session.uuid, pipe, request))
        frame = receive(ws)
        assert frame["type"] == "mcp" and frame["message"] == request
        assert time.monotonic() - started < 1  # NOTIFY, nicht das Nachholen
        answer = {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}
        send(ws, "mcp", session=str(session.uuid), pipe=pipe, message=answer)
        reply = sub.get(timeout=5)
        assert reply["message"] == answer
    assert not RunnerMessage.objects.exists()


def test_request_waits_for_matching_answer(online, anna):
    ws, runner = online
    session = make_session(anna, runner, status=RunnerSession.Status.STARTING)
    frame = {
        "type": "session_stop",
        "session": str(session.uuid),
        "reason": "user",
    }
    future = in_thread(
        delivery.request,
        runner.pk,
        frame,
        timeout=5,
        accept=lambda answer: answer["state"] == "stopped",
    )
    assert receive(ws)["type"] == "session_stop"
    send(ws, "session_state", session=str(session.uuid), state="stopping")
    send(ws, "session_state", session=str(session.uuid), state="stopped")
    answer = future.result(timeout=5)
    assert answer["state"] == "stopped"
    session.refresh_from_db()
    assert session.status == RunnerSession.Status.STOPPED and session.ended is not None


def test_request_times_out(online):
    _ws, runner = online
    with pytest.raises(delivery.RunnerTimeout):
        delivery.request(
            runner.pk, {"type": "session_stop", "session": "s9", "reason": "user"}, timeout=0.3
        )


def test_lost_notification_is_fetched_on_heartbeat(online):
    ws, runner = online
    # Zeile ohne NOTIFY (Benachrichtigung verloren).
    RunnerMessage.objects.create(
        runner=runner,
        direction=RunnerMessage.Direction.TO_RUNNER,
        kind="session_stop",
        payload={"type": "session_stop", "session": "s7", "reason": "idle"},
    )
    time.sleep(0.3)
    send(ws, "heartbeat", sessions=[])
    assert receive_until(ws, "session_stop")["session"] == "s7"


def test_disconnect_fails_waiting_subscribers(client, token, anna):
    token_obj, secret = token
    pipe = str(uuid.uuid4())
    with delivery.subscribe("pipe", pipe) as sub:
        with client.websocket_connect(PATH) as ws:
            hello(ws, secret)
            runner = Runner.objects.get(token=token_obj)
            session = make_session(anna, runner)
            delivery.send(
                runner.pk,
                mcp_frame(session.uuid, pipe, {"jsonrpc": "2.0", "id": 5, "method": "ping"}),
            )
            assert receive(ws)["type"] == "mcp"
        started = time.monotonic()
        with pytest.raises(delivery.RunnerOffline):
            sub.get(timeout=5)
        assert time.monotonic() - started < 2
    wait_for(lambda: Runner.objects.get(pk=runner.pk).status == Runner.Status.OFFLINE)
    session.refresh_from_db()
    assert session.status == RunnerSession.Status.UNKNOWN


def test_async_pipe(online, anna):
    ws, runner = online
    session = make_session(anna, runner)

    async def scenario():
        pipe = delivery.Pipe(runner.pk, session.uuid)
        await pipe.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        answer = await pipe.receive(timeout=5)
        with pytest.raises(delivery.RunnerTimeout):
            await pipe.receive(timeout=0.2)
        await pipe.aclose()
        return pipe.pipe, answer

    future = _threads.submit(asyncio.run, scenario())
    frame = receive(ws)
    assert frame["message"]["method"] == "initialize"
    send(
        ws,
        "mcp",
        session=frame["session"],
        pipe=frame["pipe"],
        message={"jsonrpc": "2.0", "id": 1, "result": {"ok": True}},
    )
    pipe, answer = future.result(timeout=10)
    assert pipe == frame["pipe"]
    assert answer == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}


def test_async_pipe_reports_offline(client, token, anna):
    token_obj, secret = token
    with client.websocket_connect(PATH) as ws:
        hello(ws, secret)
        runner = Runner.objects.get(token=token_obj)
        session = make_session(anna, runner)

        async def scenario():
            pipe = delivery.Pipe(runner.pk, session.uuid)
            await pipe.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})
            try:
                await pipe.receive(timeout=5)
            finally:
                await pipe.aclose()

        future = _threads.submit(asyncio.run, scenario())
        assert receive(ws)["type"] == "mcp"
    with pytest.raises(delivery.RunnerOffline):
        future.result(timeout=10)


def test_roundtrip_from_another_process(online, anna):
    """Ein zweiter Prozess (wie ein anderer gunicorn-Worker) fragt und bekommt die Antwort."""
    ws, runner = online
    session = make_session(anna, runner)
    code = (
        "import json, sys, django; django.setup()\n"
        "from django.db import connection\n"
        "connection.settings_dict['NAME'] = sys.argv[1]\n"
        "from multigpt.runner import delivery\n"
        "frame = {'type': 'mcp', 'session': sys.argv[3], 'pipe': sys.argv[4],\n"
        "         'message': {'jsonrpc': '2.0', 'id': 9, 'method': 'tools/list'}}\n"
        "answer = delivery.request(int(sys.argv[2]), frame, timeout=20)\n"
        "print(json.dumps(answer['message']))\n"
        "delivery.shutdown()\n"
    )
    pipe = str(uuid.uuid4())
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": "multigpt.settings"}
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            code,
            connection.settings_dict["NAME"],
            str(runner.pk),
            str(session.uuid),
            pipe,
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        frame = receive(ws, timeout=30)
        assert frame["pipe"] == pipe
        send(
            ws,
            "mcp",
            session=str(session.uuid),
            pipe=pipe,
            message={"jsonrpc": "2.0", "id": 9, "result": {"tools": []}},
        )
        out, err = proc.communicate(timeout=30)
        assert proc.returncode == 0, err.decode()[-2000:]
        assert json.loads(out) == {"jsonrpc": "2.0", "id": 9, "result": {"tools": []}}
    finally:
        if proc.poll() is None:
            proc.kill()


def test_stop_session_via_ctl(online, anna):
    ws, runner = online
    session = make_session(anna, runner)
    control.stop_session(session.uuid, "idle")
    assert receive(ws) == {"type": "session_stop", "session": str(session.uuid), "reason": "idle"}
    with pytest.raises(ValueError):
        control.stop_session(session.uuid, "keiner")


def test_quota_stop_and_disconnect_without_confirmation(online, anna, monkeypatch):
    monkeypatch.setattr(gateway, "QUOTA_GRACE_SECONDS", 0.5)
    ws, runner = online
    first = make_session(anna, runner)
    second = make_session(anna, runner)
    control.stop_user_sessions(anna.pk)
    stops = {receive(ws)["session"], receive(ws)["session"]}
    assert stops == {str(first.uuid), str(second.uuid)}
    first.refresh_from_db()
    assert first.quota_stop_sent is not None
    # Nur eine Sitzung bestätigt: nach der Frist wird getrennt.
    send(ws, "session_state", session=str(first.uuid), state="stopped")
    assert receive_until(ws, "close")["reason"] == "quota"
    assert expect_close(ws) == P.CLOSE_QUOTA


def test_quota_stop_confirmed_keeps_connection(online, anna, monkeypatch):
    monkeypatch.setattr(gateway, "QUOTA_GRACE_SECONDS", 0.3)
    ws, runner = online
    session = make_session(anna, runner)
    control.stop_user_sessions(anna.pk)
    assert receive(ws)["reason"] == "quota"
    send(ws, "session_state", session=str(session.uuid), state="stopped")
    time.sleep(0.6)
    # Weiter verbunden: ein Rahmen über die Warteschlange kommt an.
    delivery.send(
        runner.pk, {"type": "session_stop", "session": "s1", "reason": "user"}, reply=False
    )
    assert receive(ws)["session"] == "s1"
    session.refresh_from_db()
    assert session.status == RunnerSession.Status.STOPPED


def test_session_state_with_wrong_limits_stops_session(online, anna):
    ws, runner = online
    requested = {"cpus": 1, "memory_mb": 1024, "pids": 256, "tmpfs_mb": 512, "network": False}
    session = make_session(anna, runner, limits_requested=requested)
    with delivery.subscribe("session", str(session.uuid)) as sub:
        delivery.send(
            runner.pk, {"type": "session_stop", "session": str(session.uuid), "reason": "user"}
        )
        receive(ws)  # Route der Sitzung ist jetzt dieser Prozess
        actual = {**requested, "cpus": 2, "network": "none"}
        send(ws, "session_state", session=str(session.uuid), state="running", limits=actual)
        assert receive(ws) == {
            "type": "session_stop",
            "session": str(session.uuid),
            "reason": "mismatch",
        }
        assert sub.get(timeout=5)["state"] == "running"
    session.refresh_from_db()
    assert session.status == RunnerSession.Status.STOPPING
    assert session.limits_actual["cpus"] == 2
    audit = RunnerAudit.objects.get(kind=RunnerAudit.Kind.LIMITS)
    assert audit.session_id == session.pk and "cpus" in audit.message


def test_bundle_download_in_chunks(online, anna):
    ws, runner = online
    data = os.urandom(P.CHUNK_BYTES * 2 + 1234)
    session = make_session(anna, runner, bundle=data)
    send(ws, "bundle_get", session=str(session.uuid), transfer="t1")
    assembler = P.BundleAssembler("t1")
    result = None
    while result is None:
        frame = receive(ws)
        assert frame["type"] == "bundle_chunk"
        result = assembler.add(frame)
    assert result == data
    assert frame["head"] == "c" * 40


def test_bundle_download_refused_for_foreign_session(online, anna):
    ws, _runner = online
    session = make_session(anna, None, bundle=b"x" * 10)  # keinem Runner zugeordnet
    send(ws, "bundle_get", session=str(session.uuid), transfer="t1")
    frame = receive(ws)
    assert frame["type"] == "bundle_ack" and frame["ok"] is False


def _upload(ws, session, data, transfer="u1", sha=None):
    parts = P.split_bundle(data, 1000)
    for seq, part, last in parts:
        fields = {
            "session": str(session.uuid),
            "transfer": transfer,
            "seq": seq,
            "data": P.b64encode(part),
            "last": last,
        }
        if last:
            fields.update(
                size=len(data),
                sha256=sha or hashlib.sha256(data).hexdigest(),
                head="d" * 40,
                message="Datei geändert",
            )
        send(ws, "bundle_chunk", **fields)
    return receive_until(ws, "bundle_ack")


def test_bundle_upload_goes_to_accept(online, anna, monkeypatch):
    ws, runner = online
    session = make_session(anna, runner)
    calls = []

    def accept(workspace_id, data, sha256, *, session_id=None, head="", message=""):
        calls.append((workspace_id, data, sha256, session_id, head, message))
        return bundles.AcceptResult(True, head=head, size=len(data), message="")

    monkeypatch.setattr(bundles, "accept", accept)
    data = os.urandom(3500)
    ack = _upload(ws, session, data)
    assert ack["ok"] is True and ack["head"] == "d" * 40
    assert calls == [
        (
            session.workspace_id,
            data,
            hashlib.sha256(data).hexdigest(),
            session.pk,
            "d" * 40,
            "Datei geändert",
        )
    ]
    # Falsche Prüfsumme: abgelehnt, ohne die Annahme zu fragen.
    ack = _upload(ws, session, data, transfer="u2", sha="0" * 64)
    assert ack["ok"] is False and "Prüfsumme" in ack["error"]
    assert len(calls) == 1


def test_bundle_upload_respects_role_limit(online, anna, monkeypatch):
    ws, runner = online
    session = make_session(anna, runner)
    monkeypatch.setattr("multigpt.runner.store.bundle_limit", lambda runner_id, session_id: 2000)
    ack = _upload(ws, session, os.urandom(3500))
    assert ack["ok"] is False and "größer" in ack["error"]


def test_pty_and_preview_are_routed(online, anna):
    ws, runner = online
    session = make_session(anna, runner)
    sid = str(session.uuid)
    with (
        delivery.subscribe("pty", "term1") as pty,
        delivery.subscribe("session", sid) as events,
    ):
        delivery.send(
            runner.pk, {"type": "pty_open", "session": sid, "pty": "term1", "cols": 80, "rows": 24}
        )
        assert receive(ws)["type"] == "pty_open"
        send(ws, "pty_data", session=sid, pty="term1", seq=0, data=P.b64encode(b"$ "))
        assert P.b64decode(pty.get(timeout=5)["data"]) == b"$ "
        send(ws, "pty_exit", session=sid, pty="term1", code=0)
        assert pty.get(timeout=5)["type"] == "pty_exit"

        delivery.send(runner.pk, {"type": "session_stop", "session": sid, "reason": "user"})
        receive(ws)
        send(ws, "preview", session=sid, running=True, port=18083, token="t" * 32)
        assert events.get(timeout=5)["port"] == 18083
    session.refresh_from_db()
    assert session.preview_port == 18083
    assert session.preview_token != "t" * 32  # Token des Agents bleibt beim Agent


def test_cleanup_removes_orphaned_messages(token):
    token_obj, _secret = token
    runner = Runner.objects.create(token=token_obj)
    old = RunnerMessage.objects.create(
        runner=runner,
        direction=RunnerMessage.Direction.TO_SERVER,
        kind="mcp",
        payload={},
        created=timezone.now() - timedelta(minutes=11),
    )
    fresh = RunnerMessage.objects.create(
        runner=runner, direction=RunnerMessage.Direction.TO_SERVER, kind="mcp", payload={}
    )
    assert delivery.cleanup() == 1
    assert list(RunnerMessage.objects.values_list("pk", flat=True)) == [fresh.pk]
    assert old.pk != fresh.pk


def test_agent_pty_without_open_goes_to_session(online, anna):
    ws, runner = online
    session = make_session(anna, runner)
    sid = str(session.uuid)
    with delivery.subscribe("session", sid) as events:
        delivery.send(runner.pk, {"type": "session_stop", "session": sid, "reason": "user"})
        receive(ws)
        send(ws, "pty_data", session=sid, pty="run-1", seq=0, data=P.b64encode(b"ok\n"))
        send(ws, "pty_exit", session=sid, pty="run-1", code=0)
        assert events.get(timeout=5)["pty"] == "run-1"
        assert events.get(timeout=5)["type"] == "pty_exit"


def test_file_chunks_go_to_transfer_subscriber(online, anna):
    ws, runner = online
    session = make_session(anna, runner)
    data = os.urandom(2500)
    with delivery.subscribe_transfer(runner.pk, "pub-1") as sub:
        # Wie im Werkzeug: danach der MCP-Aufruf publish (spätere Zeile, also
        # erst nach der Zuordnung beim Agent).
        call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {}}
        delivery.send(runner.pk, mcp_frame(session.uuid, str(uuid.uuid4()), call))
        assert receive(ws)["type"] == "mcp"
        assembler = P.BundleAssembler("pub-1", max_bytes=10_000)
        for seq, part, last in P.split_bundle(data, 1000):
            fields = {"session": str(session.uuid), "transfer": "pub-1", "seq": seq}
            fields.update(data=P.b64encode(part), last=last)
            if last:
                fields.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
                fields.update(name="bild.png", mime="image/png")
            send(ws, "file_chunk", **fields)
        result = None
        while result is None:
            frame = sub.get(timeout=5)
            result = assembler.add(frame)
        assert result == data and frame["name"] == "bild.png"
