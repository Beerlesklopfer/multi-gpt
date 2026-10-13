"""Sicherheit der Runner-Oberfläche (M14-14): fremdes Konto, Verwalter ohne Inhalte,
Token im Vorschaupfad, CSP. Ohne echten Runner (``delivery`` gefälscht)."""

import base64

import pytest
from django.urls import reverse

from multigpt.chat.models import Conversation
from multigpt.runner import audit, control, delivery
from multigpt.runner.models import RunnerAudit, RunnerSession
from tests.node_support import make_user
from tests.test_runner_ui import FakeSub, make_runner, make_session

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _no_notify(monkeypatch):
    monkeypatch.setattr(control, "notify_ctl", lambda kind, **data: None)


@pytest.fixture
def sent(monkeypatch):
    frames = []

    def send(runner_id, frame, *, session=None, reply=True):
        frames.append(frame)
        return len(frames)

    def subscribe(kind, key):
        return FakeSub(
            [
                {
                    "type": "preview_response",
                    "request": key,
                    "seq": 0,
                    "status": 200,
                    "headers": {"Content-Type": "text/html"},
                    "data": base64.b64encode(b"<p>ok</p>").decode(),
                    "last": True,
                }
            ]
            if kind == "request"
            else []
        )

    monkeypatch.setattr(delivery, "send", send)
    monkeypatch.setattr(delivery, "subscribe", subscribe)
    monkeypatch.setattr(delivery, "runner_online", lambda runner_id: True)
    return frames


@pytest.fixture
def anna_session():
    anna = make_user("anna")
    runner = make_runner(anna)
    session = make_session(anna, runner)
    conv = Conversation.objects.create(user=anna, title="Annas Projekt")
    RunnerSession.objects.filter(pk=session.pk).update(conversation=conv)
    audit.record(
        RunnerAudit.Kind.TOOL,
        session=session,
        runner=runner,
        tool="write",
        arguments={"path": "geheimplan.txt"},
        message="Datei geheimplan.txt geschrieben",
    )
    return anna, session


# --- Fremdes Konto und Verwalter ----------------------------------------------------------


@pytest.mark.parametrize("role", ["adult", "admin"])
def test_foreign_session_is_404_everywhere(client, anna_session, sent, role):
    _, session = anna_session
    other = make_user("ben", role, is_staff=role == "admin")
    client.force_login(other)
    for name in ("runner:session", "runner:session_api", "runner:pty_stream"):
        assert client.get(reverse(name, args=[session.uuid])).status_code == 404, name
    for name in ("runner:pty_input", "runner:pty_resize", "runner:pty_close"):
        response = client.post(
            reverse(name, args=[session.uuid, "abc"]),
            {"data": "rm -rf /", "cols": 80, "rows": 24},
            content_type="application/json",
        )
        assert response.status_code == 404, name
    assert sent == []  # nichts an den Runner


@pytest.mark.parametrize("role", ["adult", "admin"])
def test_status_shows_only_own_data(client, anna_session, role):
    _, session = anna_session
    other = make_user("ben", role)
    client.force_login(other)
    data = client.get(reverse("runner:status")).json()
    assert data["runners"] == [] and data["sessions"] == [] and data["workspaces"] == []
    assert str(session.uuid) not in client.get(reverse("runner:index")).content.decode()


def test_admin_sees_only_metadata(client, anna_session):
    _, session = anna_session
    ben = make_user("ben", "admin", is_staff=True)
    client.force_login(ben)
    entry = RunnerAudit.objects.get(tool="write")
    pages = [
        reverse("admin:runner_runnersession_change", args=[session.pk]),
        reverse("admin:runner_runneraudit_change", args=[entry.pk]),
        reverse("admin:runner_runneraudit_changelist"),
        reverse("admin:runner_runnersession_changelist"),
    ]
    for url in pages:
        response = client.get(url)
        assert response.status_code == 200, url
        html = response.content.decode()
        assert "geheimplan" not in html, url  # weder Argumente noch Meldung
        assert session.preview_token not in html, url
    # Keine Warteschlange mit Nutzlasten im Admin.
    with pytest.raises(Exception):  # noqa: B017 – NoReverseMatch
        reverse("admin:runner_runnermessage_changelist")


def test_admin_cannot_open_preview_without_token(client, anna_session, sent):
    _, session = anna_session
    ben = make_user("ben", "admin", is_staff=True)
    client.force_login(ben)
    response = client.get(f"/runner/vorschau/{session.uuid}/falsch/")
    assert response.status_code == 404
    assert sent == []


# --- Token im Vorschaupfad -----------------------------------------------------------------


def test_preview_requires_exact_token(client, anna_session, sent):
    _, session = anna_session
    token = session.preview_token
    assert len(token) >= 40  # 32 Byte Zufall, base64url
    for bad in ("x", token[:-1], token + "x", token.upper() if token.upper() != token else "Y"):
        response = client.get(f"/runner/vorschau/{session.uuid}/{bad}/")
        assert response.status_code == 404, bad
        assert response["Content-Security-Policy"].startswith("sandbox")
    assert sent == []
    assert client.get(f"/runner/vorschau/{session.uuid}/{token}/").status_code == 200


def test_token_of_other_session_does_not_work(client, anna_session, sent):
    anna, session = anna_session
    other = make_session(anna, session.runner)
    response = client.get(f"/runner/vorschau/{other.uuid}/{session.preview_token}/")
    assert response.status_code == 404
    assert sent == []


def test_preview_ends_with_session(client, anna_session, sent):
    _, session = anna_session
    RunnerSession.objects.filter(pk=session.pk).update(status=RunnerSession.Status.STOPPED)
    response = client.get(f"/runner/vorschau/{session.uuid}/{session.preview_token}/")
    assert response.status_code == 404


def test_preview_blocked_after_revoke_or_right_lost(client, anna_session, sent):
    anna, session = anna_session
    from multigpt.runner import tokens

    tokens.revoke(session.runner.token)
    response = client.get(f"/runner/vorschau/{session.uuid}/{session.preview_token}/")
    assert response.status_code == 404
    assert sent == []


def test_preview_path_traversal_never_reaches_runner(client, anna_session, sent):
    _, session = anna_session
    base = f"/runner/vorschau/{session.uuid}/{session.preview_token}/"
    for path in ("..%2f..%2fetc/passwd", ".git/config", "a/%2e%2e/%2e%2e/x", "a%5cb"):
        assert client.get(base + path).status_code == 404, path
    assert sent == []


def test_preview_token_not_in_audit_or_status(client, anna_session):
    anna, session = anna_session
    client.force_login(anna)
    status = client.get(reverse("runner:status")).content.decode()
    assert session.preview_token not in status
    page = client.get(reverse("runner:session", args=[session.uuid])).content.decode()
    # Die Seite zeigt den Link nur dem Besitzer, nie im Audit.
    assert not RunnerAudit.objects.filter(arguments__contains=session.preview_token).exists()
    assert page.count(session.preview_token) <= 1


# --- CSP der Vorschau ----------------------------------------------------------------------


def test_preview_csp_blocks_network_and_same_origin(client, anna_session, sent):
    _, session = anna_session
    response = client.get(f"/runner/vorschau/{session.uuid}/{session.preview_token}/")
    csp = {
        part.strip().split(" ", 1)[0]: part.strip()
        for part in response["Content-Security-Policy"].split(";")
    }
    assert csp["sandbox"] == "sandbox allow-scripts"  # kein allow-same-origin, keine Formulare
    assert csp["default-src"] == "default-src 'none'"
    assert csp["connect-src"] == "connect-src 'none'"
    assert csp["form-action"] == "form-action 'none'"
    assert csp["base-uri"] == "base-uri 'none'"
    assert "frame-ancestors 'self'" in csp["frame-ancestors"]
    assert response["X-Frame-Options"] == "SAMEORIGIN"
    assert response["Cross-Origin-Resource-Policy"] == "same-origin"


def test_preview_unknown_type_is_not_html(client, anna_session, monkeypatch, sent):
    _, session = anna_session
    response = client.get(f"/runner/vorschau/{session.uuid}/{session.preview_token}/datei.bin")
    # Agent behauptet text/html – ohne passende Endung wird nichts als HTML ausgeliefert.
    assert response["Content-Type"] == "application/octet-stream"
    assert response["X-Content-Type-Options"] == "nosniff"


def test_preview_does_not_set_cookies_or_use_session(client, anna_session, sent):
    _, session = anna_session
    response = client.get(f"/runner/vorschau/{session.uuid}/{session.preview_token}/")
    assert not response.cookies
    assert "Vary" not in response or "Cookie" not in response["Vary"]
