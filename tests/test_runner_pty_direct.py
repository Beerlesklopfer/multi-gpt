"""Terminal im Browser über den direkten WebSocket (``runner/pty_direct.py``):
Origin-Prüfung, Anmeldung über die Django-Sitzung, Rechte wie beim alten Weg,
Rückfall bei Runner an einem anderen Prozess, Rahmen in beide Richtungen ohne
``RunnerMessage``, Grenzen, Audit – und die Latenz (Echo) im Vergleich zum alten
Weg über ``RunnerMessage`` + NOTIFY.
"""

from __future__ import annotations

import concurrent.futures
import json
import statistics
import threading
import time
import uuid

import pytest
from django.conf import settings as django_settings
from django.db import connections
from django.test import Client
from starlette.websockets import WebSocketDisconnect

from multigpt.accounts.models import Role
from multigpt.runner import delivery, pty_direct
from multigpt.runner import protocol as P
from multigpt.runner.models import Runner, RunnerAudit, RunnerMessage, RunnerSession
from tests.runner_gateway_support import (
    _NONCES,
    PATH,
    gateway_client,
    hello,
    make_token,
    make_user,
    open_frame,
    receive,
    send,
    wait_for,
)
from tests.test_runner_gateway_delivery import make_session

pytestmark = pytest.mark.django_db(transaction=True, serialized_rollback=True)

ORIGIN = "http://testserver"
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="browser")


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
    token_obj, secret = token
    with client.websocket_connect(PATH) as ws:
        hello(ws, secret)
        yield ws, Runner.objects.get(token=token_obj)


def cookie_for(user) -> str:
    web = Client()
    web.force_login(user)
    return web.cookies[django_settings.SESSION_COOKIE_NAME].value


def headers(cookie: str | None, origin: str | None = ORIGIN) -> dict:
    result = {}
    if origin is not None:
        result["origin"] = origin
    if cookie is not None:
        result["cookie"] = f"{django_settings.SESSION_COOKIE_NAME}={cookie}"
    return result


def url(session, **query) -> str:
    text = f"/runner/pty/{session.uuid}"
    if query:
        text += "?" + "&".join(f"{k}={v}" for k, v in query.items())
    return text


def browser_receive(ws, timeout=5.0) -> dict:
    message = _pool.submit(ws.receive).result(timeout)
    if message["type"] == "websocket.close":
        raise WebSocketDisconnect(message.get("code"))
    return json.loads(message["text"])


def browser_close_code(ws, timeout=5.0) -> int:
    deadline = time.monotonic() + timeout
    while True:
        message = _pool.submit(ws.receive).result(max(0.05, deadline - time.monotonic()))
        if message["type"] == "websocket.close":
            return message.get("code")


def refused(client, path, hdrs) -> tuple[dict | None, int]:
    """(letzter failure-Rahmen, Close-Code) einer abgelehnten Verbindung."""
    with client.websocket_connect(path, headers=hdrs) as ws:
        frame = None
        while True:
            message = _pool.submit(ws.receive).result(5)
            if message["type"] == "websocket.close":
                return frame, message.get("code")
            frame = json.loads(message["text"])


# --- Origin ------------------------------------------------------------------------


def test_origin_allowed(settings):
    settings.ALLOWED_HOSTS = ["multigpt.example.lan", "192.168.24.250"]
    settings.CSRF_TRUSTED_ORIGINS = ["http://192.168.24.250:8000"]
    ok = pty_direct.origin_allowed
    assert ok("https://multigpt.example.lan", "multigpt.example.lan")
    assert ok("https://MultiGPT.example.lan/", "multigpt.example.lan")
    # Entwicklung: Seite auf :8000, Dienst auf :8001 – über CSRF_TRUSTED_ORIGINS.
    assert ok("http://192.168.24.250:8000", "192.168.24.250:8001")
    # Hinter nginx kommt Host ohne Port an: derselbe Name genügt.
    assert ok("https://multigpt.example.lan:8443", "multigpt.example.lan")
    assert not ok("http://192.168.24.251:8000", "192.168.24.250:8001")
    assert not ok("https://evil.example", "multigpt.example.lan")
    assert not ok("https://evil.example", "evil.example")  # nicht in ALLOWED_HOSTS
    assert not ok(None, "multigpt.example.lan")
    assert not ok("null", "multigpt.example.lan")
    assert not ok("file://multigpt.example.lan", "multigpt.example.lan")


def test_foreign_or_missing_origin_rejected_before_handshake(client, anna, online):
    _ws, runner = online
    session = make_session(anna, runner)
    cookie = cookie_for(anna)
    for origin in (None, "https://evil.example"):
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(url(session), headers=headers(cookie, origin)):
                pass
        assert exc.value.code == pty_direct.CLOSE_FORBIDDEN


# --- Anmeldung und Rechte -------------------------------------------------------------


def test_requires_login(client, anna, online):
    _ws, runner = online
    session = make_session(anna, runner)
    frame, code = refused(client, url(session), headers(None))
    assert code == pty_direct.CLOSE_UNAUTHORIZED and frame["type"] == "failure"
    frame, code = refused(client, url(session), headers("ungueltig"))
    assert code == pty_direct.CLOSE_UNAUTHORIZED


def test_foreign_session_is_not_found(client, anna, online):
    _ws, runner = online
    session = make_session(anna, runner)
    bert = make_user("bert")
    _frame, code = refused(client, url(session), headers(cookie_for(bert)))
    assert code == pty_direct.CLOSE_NOT_FOUND
    _frame, code = refused(client, f"/runner/pty/{uuid.uuid4()}", headers(cookie_for(anna)))
    assert code == pty_direct.CLOSE_NOT_FOUND


def test_session_must_run_and_right_needed(client, anna, online):
    _ws, runner = online
    stopped = make_session(anna, runner, status=RunnerSession.Status.STOPPED)
    _frame, code = refused(client, url(stopped), headers(cookie_for(anna)))
    assert code == pty_direct.CLOSE_NOT_RUNNING
    session = make_session(anna, runner)
    Role.objects.filter(pk=anna.role_id).update(can_use_runner=False)
    _frame, code = refused(client, url(session), headers(cookie_for(anna)))
    assert code == pty_direct.CLOSE_FORBIDDEN


def test_runner_elsewhere_asks_for_fallback(client, anna, token):
    """Runner online, aber an keinem Prozess dieses Dienstes → alter Weg."""
    from django.utils import timezone

    token_obj, _secret = token
    runner = Runner.objects.create(
        token=token_obj, status=Runner.Status.ONLINE, last_seen=timezone.now()
    )
    session = make_session(anna, runner)
    frame, code = refused(client, url(session), headers(cookie_for(anna)))
    assert code == pty_direct.CLOSE_ELSEWHERE and frame["fallback"] is True
    Runner.objects.filter(pk=runner.pk).update(status=Runner.Status.OFFLINE)
    frame, code = refused(client, url(session), headers(cookie_for(anna)))
    assert code == pty_direct.CLOSE_ELSEWHERE and frame["fallback"] is False


# --- Ablauf --------------------------------------------------------------------------


def test_round_trip_without_queue(client, anna, online):
    agent, runner = online
    session = make_session(anna, runner)
    sid = str(session.uuid)
    with client.websocket_connect(
        url(session, cols=999, rows=30), headers=headers(cookie_for(anna))
    ) as browser:
        opened = receive(agent)
        assert opened["type"] == "pty_open" and opened["cols"] == 500 and opened["rows"] == 30
        pty = opened["pty"]
        assert browser_receive(browser) == {"type": "open", "pty": pty}

        send(agent, "pty_data", session=sid, pty=pty, seq=0, data=P.b64encode(b"$ "))
        assert P.b64decode(browser_receive(browser)["data"]) == b"$ "

        browser.send_text(json.dumps({"type": "input", "data": "ls\r"}))
        frame = receive(agent)
        assert frame["type"] == "pty_input" and P.b64decode(frame["data"]) == b"ls\r"
        assert frame["pty"] == pty and frame["session"] == sid

        browser.send_text(json.dumps({"type": "resize", "cols": 3, "rows": 1000}))
        frame = receive(agent)
        assert (frame["type"], frame["cols"], frame["rows"]) == ("pty_resize", 10, 200)

        browser.send_text("kein json")  # wird ignoriert
        send(agent, "pty_exit", session=sid, pty=pty, code=3)
        assert browser_receive(browser) == {"type": "exit", "code": 3}
        assert browser_close_code(browser) == pty_direct.CLOSE_NORMAL
    # Kein Umweg über die Warteschlange.
    assert not RunnerMessage.objects.filter(kind__startswith="pty").exists()
    wait_for(lambda: RunnerAudit.objects.filter(session=session, tool="terminal").count() == 2)
    messages = list(
        RunnerAudit.objects.filter(session=session, tool="terminal")
        .order_by("pk")
        .values_list("message", flat=True)
    )
    assert messages == ["Terminal geöffnet (Browser)", "Terminal geschlossen (Browser)"]
    # pty_exit kam vom Agent: kein pty_close hinterher.
    assert not wait_for_frame(agent, "pty_close", 0.3)


def wait_for_frame(agent, kind, timeout):
    try:
        deadline = time.monotonic() + timeout
        while True:
            frame = receive(agent, max(0.05, deadline - time.monotonic()))
            if frame["type"] == kind:
                return frame
    except AssertionError:
        return None


def test_browser_close_sends_pty_close(client, anna, online):
    agent, runner = online
    session = make_session(anna, runner)
    with client.websocket_connect(url(session), headers=headers(cookie_for(anna))) as browser:
        pty = receive(agent)["pty"]
        browser_receive(browser)
        browser.send_text(json.dumps({"type": "close"}))
        assert browser_receive(browser)["type"] == "end"
    frame = wait_for_frame(agent, "pty_close", 5)
    assert frame is not None and frame["pty"] == pty


def test_agent_error_ends_terminal(client, anna, online):
    agent, runner = online
    session = make_session(anna, runner)
    with client.websocket_connect(url(session), headers=headers(cookie_for(anna))) as browser:
        pty = receive(agent)["pty"]
        browser_receive(browser)
        send(agent, "error", session=str(session.uuid), ref=pty, code="engine", message="kaputt")
        frame = browser_receive(browser)
        assert frame == {"type": "failure", "message": "kaputt"}


def test_runner_disconnect_reaches_browser(client, anna, token):
    token_obj, secret = token
    with client.websocket_connect(PATH) as agent:
        hello(agent, secret)
        runner = Runner.objects.get(token=token_obj)
        session = make_session(anna, runner)
        browser_ctx = client.websocket_connect(url(session), headers=headers(cookie_for(anna)))
        browser = browser_ctx.__enter__()
        receive(agent)
        browser_receive(browser)
    try:
        frame = browser_receive(browser)
        assert frame["type"] == "failure" and "nicht verbunden" in frame["message"]
        assert browser_close_code(browser) == pty_direct.CLOSE_ELSEWHERE
    finally:
        browser_ctx.__exit__(None, None, None)


def test_input_too_large_and_rate_limit(client, anna, online, monkeypatch):
    agent, runner = online
    session = make_session(anna, runner)
    cookie = cookie_for(anna)
    with client.websocket_connect(url(session), headers=headers(cookie)) as browser:
        receive(agent)
        browser_receive(browser)
        browser.send_text(json.dumps({"type": "input", "data": "x" * (P.PTY_BYTES + 1)}))
        assert browser_close_code(browser) == pty_direct.CLOSE_TOO_BIG
    monkeypatch.setattr(pty_direct, "RATE_BURST", 3.0)
    monkeypatch.setattr(pty_direct, "RATE_PER_SECOND", 0.001)
    with client.websocket_connect(url(session), headers=headers(cookie)) as browser:
        browser_receive(browser)
        for _ in range(5):
            browser.send_text(json.dumps({"type": "input", "data": "a"}))
        assert browser_close_code(browser) == pty_direct.CLOSE_RATE_LIMIT


def test_limit_per_user(client, anna, online, monkeypatch):
    agent, runner = online
    session = make_session(anna, runner)
    monkeypatch.setattr(pty_direct, "PER_USER", 1)
    cookie = cookie_for(anna)
    with client.websocket_connect(url(session), headers=headers(cookie)) as first:
        receive(agent)
        browser_receive(first)
        _frame, code = refused(client, url(session), headers(cookie))
        assert code == pty_direct.CLOSE_RATE_LIMIT


# --- Latenz --------------------------------------------------------------------------


def _echo_agent(agent, session_id: str):
    """Fake-Agent: jede Eingabe sofort als Ausgabe zurück (wie das Echo der Shell).

    Liest blockierend (ohne Zeitgrenze, sonst bliebe ein wartender Leser übrig, der
    spätere Rahmen schluckt) bis zum Schließen der Verbindung."""
    nonce = _NONCES[agent]
    while True:
        try:
            message = agent.receive()
        except Exception:  # Verbindung am Testende geschlossen
            return
        if message["type"] == "websocket.close":
            return
        frame = open_frame(message["text"], nonce)
        if frame["type"] == "pty_input":
            send(agent, "pty_data", session=session_id, pty=frame["pty"], seq=0, data=frame["data"])


def test_echo_latency_direct_vs_queue(client, anna, online):
    agent, runner = online
    session = make_session(anna, runner)
    sid = str(session.uuid)
    worker = threading.Thread(target=_echo_agent, args=(agent, sid), daemon=True)
    rounds = 40
    # Neuer Weg: Browser-WebSocket.
    with client.websocket_connect(url(session), headers=headers(cookie_for(anna))) as browser:
        pty = receive(agent)["pty"]
        assert browser_receive(browser)["pty"] == pty
        worker.start()
        direct = []
        for i in range(rounds):
            started = time.perf_counter()
            browser.send_text(json.dumps({"type": "input", "data": f"k{i}"}))
            frame = browser_receive(browser)
            direct.append((time.perf_counter() - started) * 1000)
            assert P.b64decode(frame["data"]) == f"k{i}".encode()

    # Alter Weg: RunnerMessage + NOTIFY hin, Antwortkanal + NOTIFY zurück
    # (ohne HTTP und SSE, also eher zu günstig gemessen).
    def old_path():
        try:
            times = []
            with delivery.subscribe("pty", "alt1") as sub:
                delivery.send(
                    runner.pk,
                    {"type": "pty_open", "session": sid, "pty": "alt1", "cols": 80, "rows": 24},
                    session=session,
                )
                for i in range(rounds):
                    started = time.perf_counter()
                    delivery.send(
                        runner.pk,
                        {
                            "type": "pty_input",
                            "session": sid,
                            "pty": "alt1",
                            "data": P.b64encode(f"q{i}".encode()),
                        },
                        session=session,
                    )
                    while sub.get(timeout=5)["type"] != "pty_data":
                        pass
                    times.append((time.perf_counter() - started) * 1000)
            return times
        finally:
            connections.close_all()

    queue = _pool.submit(old_path).result(60)
    med_direct, med_queue = statistics.median(direct), statistics.median(queue)
    print(
        f"\nEcho-Latenz (ms) direkt: Median {med_direct:.1f}, max {max(direct):.1f}; "
        f"Warteschlange: Median {med_queue:.1f}, max {max(queue):.1f}"
    )
    assert med_direct < 50
    assert med_direct < med_queue


def test_ws_url_in_session_api(anna, token, settings):
    from django.urls import reverse

    token_obj, _secret = token
    runner = Runner.objects.create(token=token_obj, status=Runner.Status.OFFLINE)
    session = make_session(anna, runner)
    web = Client()
    web.force_login(anna)
    api = reverse("runner:session_api", args=[session.uuid])
    settings.RUNNER_BROWSER_WS_URL = ""  # unabhängig von der .env der Entwicklung
    assert web.get(api).json()["pty_ws_url"] == f"/runner/pty/{session.uuid}"
    settings.RUNNER_BROWSER_WS_URL = "http://192.168.24.250:8001/"
    assert (
        web.get(api).json()["pty_ws_url"] == f"http://192.168.24.250:8001/runner/pty/{session.uuid}"
    )
    page = web.get(reverse("runner:session", args=[session.uuid])).content.decode()
    assert f'data-pty-ws-url="http://192.168.24.250:8001/runner/pty/{session.uuid}"' in page
    settings.RUNNER_BROWSER_WS_URL = "off"
    assert web.get(api).json()["pty_ws_url"] == ""


def test_recheck_ends_after_logout_and_stop(client, anna, online, monkeypatch):
    from django.contrib.sessions.models import Session

    agent, runner = online
    session = make_session(anna, runner)
    monkeypatch.setattr(pty_direct, "RECHECK_SECONDS", 0.2)
    with client.websocket_connect(url(session), headers=headers(cookie_for(anna))) as browser:
        receive(agent)
        browser_receive(browser)
        Session.objects.all().delete()  # abgemeldet
        assert browser_receive(browser) == {"type": "end", "message": pty_direct.MSG_LOGIN}
        assert browser_close_code(browser) == pty_direct.CLOSE_UNAUTHORIZED
    with client.websocket_connect(url(session), headers=headers(cookie_for(anna))) as browser:
        browser_receive(browser)
        RunnerSession.objects.filter(pk=session.pk).update(status=RunnerSession.Status.STOPPED)
        assert browser_receive(browser)["type"] == "end"
        assert browser_close_code(browser) == pty_direct.CLOSE_NOT_RUNNING
