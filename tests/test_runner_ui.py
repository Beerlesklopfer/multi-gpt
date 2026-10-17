"""Runner-Oberfläche (M14-11/12, 8p/8t): Live-Status, Audit je Sitzung, Chat-Bereich,
Terminal (PTY über MultiGPT) und Vorschau – mit gefälschter Zustellung (``delivery``)."""

import base64
import hashlib
import re
import uuid
from pathlib import Path

import pytest
from django.conf import settings as django_settings
from django.urls import reverse
from django.utils import timezone

from multigpt.chat.models import AIModel, Conversation, Provider
from multigpt.runner import audit, control, delivery, tokens, views_live
from multigpt.runner.mcp_server import ensure_mcp_server
from multigpt.runner.models import (
    Runner,
    RunnerAudit,
    RunnerSession,
    RunnerWorkspace,
)
from tests.node_support import make_user
from tests.runner_fakes import ALL_SERVICES

pytestmark = pytest.mark.django_db

VENDOR = Path(django_settings.BASE_DIR) / "multigpt/chat/static/chat/vendor/xterm"


@pytest.fixture(autouse=True)
def _no_notify(monkeypatch):
    monkeypatch.setattr(control, "notify_ctl", lambda kind, **data: None)


class FakeSub:
    def __init__(self, frames):
        self.frames = list(frames)
        self.closed = False

    def get(self, timeout=None):
        if not self.frames:
            raise delivery.RunnerTimeout()
        frame = self.frames.pop(0)
        if isinstance(frame, Exception):
            raise frame
        return frame

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


@pytest.fixture
def fake_delivery(monkeypatch):
    """Gesendete Rahmen sammeln; Antworten je Abonnement vorgeben (``replies``)."""
    state = {"sent": [], "replies": [], "subs": [], "online": True}

    def send(runner_id, frame, *, session=None, reply=True):
        state["sent"].append({"runner": runner_id, "frame": frame, "reply": reply})
        return len(state["sent"])

    def subscribe(kind, key):
        sub = FakeSub(state["replies"](kind, key) if callable(state["replies"]) else [])
        state["subs"].append((kind, key, sub))
        return sub

    monkeypatch.setattr(delivery, "send", send)
    monkeypatch.setattr(delivery, "subscribe", subscribe)
    monkeypatch.setattr(delivery, "runner_online", lambda runner_id: state["online"])
    return state


@pytest.fixture
def anna():
    return make_user("anna")


def make_runner(user, *, online=True):
    created = tokens.create_token(user, "Laptop")
    return Runner.objects.create(
        token=created.token,
        name="pc-anna",
        status=Runner.Status.ONLINE if online else Runner.Status.OFFLINE,
        last_seen=timezone.now(),
        engine_version="5.4",
        catalog_state="valid",
        services=ALL_SERVICES,
    )


def make_session(user, runner, *, status=RunnerSession.Status.RUNNING, preview=True, **extra):
    workspace = RunnerWorkspace.objects.create(owner=user, name=f"ws-{uuid.uuid4().hex[:6]}")
    return RunnerSession.objects.create(
        owner=user,
        runner=runner,
        workspace=workspace,
        status=status,
        preview_port=24080 if preview else None,
        **extra,
    )


@pytest.fixture
def setup(anna):
    runner = make_runner(anna)
    session = make_session(anna, runner)
    return runner, session


# --- xterm.js eingebunden (kein CDN) ----------------------------------------------------


def test_xterm_is_vendored_with_checksums():
    readme = (VENDOR.parent / "README.md").read_text()
    for name in ("xterm.js", "addon-fit.js", "xterm.css"):
        data = (VENDOR / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() in readme, name
    assert "@xterm/xterm" in readme and "6.0.0" in readme
    assert (VENDOR / "LICENSE").exists()


def test_templates_use_no_cdn_and_no_inline_scripts():
    root = Path(django_settings.BASE_DIR) / "multigpt"
    files = [
        root / "runner/templates/runner/session.html",
        root / "runner/templates/runner/_live_status.html",
        root / "runner/templates/runner/_chat_panel.html",
        root / "runner/templates/runner/_chat_workbench.html",
        root / "runner/templates/runner/_terminal.html",
    ]
    for path in files:
        text = path.read_text()
        assert "<script>" not in text and "<script " not in text.replace("<script src", ""), path
        assert not re.search(r"https?://(cdn|unpkg|jsdelivr)", text), path
        assert not re.search(r"\son[a-z]+=", text), path  # keine Inline-Handler


# --- Live-Status ------------------------------------------------------------------------


def test_status_api_lists_own_runner_sessions_and_runtime(client, anna, setup):
    runner, session = setup
    client.force_login(anna)
    response = client.get(reverse("runner:status"))
    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    data = response.json()
    assert data["poll_seconds"] == 5
    assert data["runners"][0]["online"] is True
    assert data["sessions"][0]["uuid"] == str(session.uuid)
    assert data["sessions"][0]["active"] is True
    assert data["runtime"]["limit_minutes"] == 600  # Startwert Erwachsene
    assert data["workspaces"][0]["name"] == session.workspace.name


def test_status_marks_stale_runner_offline(client, anna, setup):
    runner, _ = setup
    Runner.objects.filter(pk=runner.pk).update(
        last_seen=timezone.now() - delivery.stale_after() * 2
    )
    client.force_login(anna)
    assert client.get(reverse("runner:status")).json()["runners"][0]["online"] is False


def test_runner_page_has_live_status(client, anna, setup):
    client.force_login(anna)
    html = client.get(reverse("runner:index")).content.decode()
    assert 'id="runner-live"' in html
    assert "runner_status.js" in html
    assert reverse("runner:status") in html
    assert "von 600 Minuten" in html
    assert "<script>" not in html


def test_session_page_shows_audit_without_output(client, anna, setup):
    runner, session = setup
    audit.record(
        RunnerAudit.Kind.TOOL,
        session=session,
        runner=runner,
        tool="bash",
        arguments={"command": "ls -la", "password": "geheim-123"},
        exit_code=0,
        duration_ms=12,
        message="ok",
    )
    client.force_login(anna)
    response = client.get(reverse("runner:session", args=[session.uuid]))
    assert response.status_code == 200
    html = response.content.decode()
    assert "ls -la" in html
    assert "geheim-123" not in html  # Geheimnisse entfernt (audit.redact)
    assert "Terminal öffnen" in html
    assert "chat/vendor/xterm/xterm.js" in html  # nur als Pfad für das Nachladen
    assert '<script src="/static/chat/vendor/xterm' not in html


def test_session_api(client, anna, setup):
    _, session = setup
    client.force_login(anna)
    data = client.get(reverse("runner:session_api", args=[session.uuid])).json()
    assert data["running"] is True
    assert data["preview_url"] == (f"/runner/vorschau/{session.uuid}/{session.preview_token}/")
    assert data["pty_url"].endswith("/pty/")


def test_no_preview_url_without_preview_server(client, anna):
    runner = make_runner(anna)
    session = make_session(anna, runner, preview=False)
    client.force_login(anna)
    data = client.get(reverse("runner:session_api", args=[session.uuid])).json()
    assert data["preview_url"] is None


# --- Chat ---------------------------------------------------------------------------------


@pytest.fixture
def ai_model():
    provider = Provider.objects.create(name="Testanbieter", kind=Provider.Kind.OPENAI_COMPAT)
    return AIModel.objects.create(provider=provider, model_id="m-1", display_name="Modell Eins")


def test_chat_shows_runner_panel_only_with_right_and_server(client, anna, ai_model):
    conv = Conversation.objects.create(user=anna, title="Code")
    client.force_login(anna)
    url = reverse("chat:conversation", args=[conv.pk])
    assert 'id="runner-picker"' not in client.get(url).content.decode()  # noch nie gekoppelt
    ws = RunnerWorkspace.objects.create(owner=anna, name="Spiel")
    server = ensure_mcp_server(anna)
    html = client.get(url).content.decode()
    assert 'id="runner-picker"' in html
    assert f'data-runner-server="{server.pk}"' in html
    assert f'<option value="{ws.pk}">Spiel</option>' in html
    assert 'id="runner-workbench"' in html
    assert 'sandbox="allow-scripts"' in html
    assert "allow-same-origin" not in html
    assert "runner_chat.js" in html
    assert "<script>" not in html


def test_teen_sees_no_runner_panel(client, ai_model):
    tim = make_user("tim", "teen")
    ensure_mcp_server(tim)
    conv = Conversation.objects.create(user=tim, title="x")
    client.force_login(tim)
    html = client.get(reverse("chat:conversation", args=[conv.pk])).content.decode()
    assert 'id="runner-picker"' not in html


# --- Terminal -----------------------------------------------------------------------------


def _read_stream(response, limit=20):
    chunks = []
    for chunk in response.streaming_content:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        if len(chunks) >= limit:
            break
    response.close()
    return "".join(chunks)


def test_pty_stream_subscribes_before_open_and_closes(client, anna, setup, fake_delivery):
    runner, session = setup
    output = base64.b64encode(b"$ ").decode()

    def replies(kind, key):
        assert kind == "pty"
        # Abonnement besteht, bevor pty_open gesendet wird.
        assert not any(s["frame"]["type"] == "pty_open" for s in fake_delivery["sent"])
        return [
            {
                "type": "pty_data",
                "session": str(session.uuid),
                "pty": key,
                "seq": 0,
                "data": output,
            },
            {"type": "pty_exit", "session": str(session.uuid), "pty": key, "code": 0},
        ]

    fake_delivery["replies"] = replies
    client.force_login(anna)
    response = client.get(
        reverse("runner:pty_stream", args=[session.uuid]), {"cols": "100", "rows": "30"}
    )
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/event-stream")
    body = _read_stream(response)
    assert "event: open" in body and "event: data" in body and output in body
    assert "event: exit" in body
    frame = fake_delivery["sent"][0]["frame"]
    assert frame["type"] == "pty_open" and frame["cols"] == 100 and frame["rows"] == 30
    assert frame["session"] == str(session.uuid)
    assert fake_delivery["subs"][0][2].closed


def test_pty_input_sends_without_reply_channel(client, anna, setup, fake_delivery):
    _, session = setup
    client.force_login(anna)
    response = client.post(
        reverse("runner:pty_input", args=[session.uuid, "abc123"]),
        {"data": "ls\r"},
        content_type="application/json",
    )
    assert response.status_code == 200
    sent = fake_delivery["sent"][0]
    assert sent["reply"] is False
    assert sent["frame"]["type"] == "pty_input"
    assert base64.b64decode(sent["frame"]["data"]) == b"ls\r"


def test_pty_resize_clamps(client, anna, setup, fake_delivery):
    _, session = setup
    client.force_login(anna)
    client.post(
        reverse("runner:pty_resize", args=[session.uuid, "abc123"]),
        {"cols": 9999, "rows": 1},
        content_type="application/json",
    )
    frame = fake_delivery["sent"][0]["frame"]
    assert (frame["cols"], frame["rows"]) == (500, 4)


def test_pty_needs_running_session(client, anna, fake_delivery):
    runner = make_runner(anna)
    session = make_session(anna, runner, status=RunnerSession.Status.STOPPED)
    client.force_login(anna)
    assert client.get(reverse("runner:pty_stream", args=[session.uuid])).status_code == 409
    assert fake_delivery["sent"] == []


def test_pty_offline_runner(client, anna, setup, fake_delivery):
    _, session = setup
    fake_delivery["online"] = False
    client.force_login(anna)
    assert client.get(reverse("runner:pty_stream", args=[session.uuid])).status_code == 503


# --- Vorschau -----------------------------------------------------------------------------


def _preview_replies(body=b"<h1>Hallo</h1>", status=200, headers=None):
    def replies(kind, key):
        assert kind == "request"
        return [
            {
                "type": "preview_response",
                "request": key,
                "seq": 0,
                "status": status,
                "headers": headers or {"Content-Type": "text/html"},
                "data": base64.b64encode(body).decode(),
                "last": True,
            }
        ]

    return replies


def test_preview_delivers_with_strict_headers(client, setup, fake_delivery):
    _, session = setup
    fake_delivery["replies"] = _preview_replies(
        headers={
            "Content-Type": "text/html",
            "Set-Cookie": "x=1",
            "Access-Control-Allow-Origin": "*",
        }
    )
    # Ohne Anmeldung: Zugriff nur über das Token im Pfad.
    response = client.get(f"/runner/vorschau/{session.uuid}/{session.preview_token}/")
    assert response.status_code == 200
    assert response.content == b"<h1>Hallo</h1>"
    csp = response["Content-Security-Policy"]
    assert csp.startswith("sandbox allow-scripts;")
    assert "allow-same-origin" not in csp
    assert "connect-src 'none'" in csp and "form-action 'none'" in csp
    assert "frame-ancestors 'self'" in csp
    assert response["Referrer-Policy"] == "no-referrer"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["Cache-Control"] == "no-store"
    assert "Set-Cookie" not in response and "Access-Control-Allow-Origin" not in response
    frame = fake_delivery["sent"][0]["frame"]
    assert frame["type"] == "preview_request" and frame["path"] == "/index.html"
    assert session.preview_token not in frame["path"]


def test_preview_subpath_and_type(client, setup, fake_delivery):
    _, session = setup
    fake_delivery["replies"] = _preview_replies(b"body{}", headers={"Content-Type": "text/html"})
    response = client.get(f"/runner/vorschau/{session.uuid}/{session.preview_token}/css/a.css")
    assert response["Content-Type"].startswith("text/css")  # nach Endung, nicht nach Agent
    assert fake_delivery["sent"][0]["frame"]["path"] == "/css/a.css"


@pytest.mark.parametrize(
    "path, expected",
    [
        ("", "/index.html"),
        ("a/", "/a/index.html"),
        ("a/./b.js", "/a/b.js"),
        ("../etc/passwd", None),
        ("a/%2e%2e/b", None),
        (".git/config", None),
        ("a/.env", None),
        ("a\\b", None),
        ("a%00b", None),
    ],
)
def test_clean_preview_path(path, expected):
    assert views_live.clean_preview_path(path) == expected
