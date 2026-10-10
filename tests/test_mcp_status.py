"""Online-Status und Werkzeugliste je MCP-Server (M4a-09, chat/mcp/status.py).

Die meisten Tests verbinden im Prozess (eigener Testserver mit annotations);
die stdio-Tests starten tests/mcp_test_server.py bzw. ein fehlendes Programm,
die HTTP-Tests nutzen einen lokalen Mini-Server mit festem Statuscode.
"""

import http.server
import json
import shlex
import socket
import sys
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from django.contrib.messages import get_messages
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from multigpt.accounts.models import Role, User
from multigpt.chat import tooling
from multigpt.chat.mcp import bridge
from multigpt.chat.mcp import client as mcp_client
from multigpt.chat.mcp import status as mcp_status
from multigpt.chat.models import AIModel, Conversation, McpServer, Provider
from multigpt.chat.providers import registry
from tests.test_tool_loop import ScriptedAdapter, answer, events_of

SERVER_SCRIPT = Path(__file__).with_name("mcp_test_server.py")
SECRET = "tok-sehr-geheim-4711"

pytestmark = pytest.mark.django_db


# --- Testserver mit annotations ------------------------------------------------

annotated = MCPServer("multigpt-annotated")


@annotated.tool(annotations=ToolAnnotations(readOnlyHint=True))
def lesen(pfad: str, zeilen: int = 10) -> str:
    """Liest eine Datei. <script>alert("x")</script>"""
    return pfad


@annotated.tool(annotations=ToolAnnotations(destructiveHint=True, readOnlyHint=False))
def loeschen(pfad: str) -> str:
    """Löscht eine Datei."""
    return pfad


@annotated.tool(description="Sehr lang. " * 200)
def neutral() -> str:
    return "ok"


@pytest.fixture(autouse=True)
def _fresh_bridge():
    bridge.shutdown()
    yield
    bridge.shutdown()


@pytest.fixture
def inproc(monkeypatch):
    """Verbindet im Prozess mit dem annotierten Testserver; zählt Verbindungen."""
    calls = []

    def factory(config):
        calls.append(config.pk)
        return Client(annotated, cache=None)

    monkeypatch.setattr(mcp_client, "_make_client", factory)
    return calls


@pytest.fixture
def no_connect(monkeypatch):
    def boom(config):  # pragma: no cover - darf nicht aufgerufen werden
        raise AssertionError("verbunden")

    monkeypatch.setattr(mcp_client, "_make_client", boom)


def _server(name="Test", **kwargs):
    defaults = {"transport": "stdio", "command": "unbenutzt", "timeout_seconds": 30}
    defaults.update(kwargs)
    return McpServer.objects.create(name=name, **defaults)


def _http(url, **kwargs):
    return _server(name="Web", transport="http", command="", url=url, credentials=SECRET, **kwargs)


# --- Lokaler HTTP-Server mit festem Status -------------------------------------


class _StatusHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - Name aus http.server
        code = int(self.path.strip("/").split("/")[0])
        self.send_response(code)
        self.send_header("Content-Length", "4")
        self.end_headers()
        self.wfile.write(b"nein")

    do_GET = do_POST  # noqa: N815

    def log_message(self, *args):
        pass


@pytest.fixture
def status_http():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StatusHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# --- online / offline mit Ursachen -------------------------------------------------


def test_online_stores_tools_with_annotations(inproc):
    server = _server()
    outcome = mcp_status.force_check(server)
    assert outcome.online and outcome.error == ""
    server.refresh_from_db()
    assert server.online and server.last_error == ""
    assert server.last_checked and server.last_online
    tools = {t["name"]: t for t in server.reported_tools}
    assert set(tools) == {"lesen", "loeschen", "neutral"}
    assert tools["lesen"]["annotations"] == {"readOnlyHint": True}
    assert tools["lesen"]["params"] == ["pfad", "zeilen"]
    assert tools["lesen"]["required"] == ["pfad"]
    assert tools["loeschen"]["annotations"] == {"destructiveHint": True, "readOnlyHint": False}
    assert "annotations" not in tools["neutral"]
    assert len(tools["neutral"]["description"]) <= mcp_status.DESCRIPTION_MAX
    # Nur Parameternamen, kein Eingabeschema.
    assert "properties" not in json.dumps(server.reported_tools)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (401, "Zugang abgelehnt (HTTP 401): Token prüfen."),
        (403, "Zugang abgelehnt (HTTP 403): Token prüfen."),
        (404, "Adresse nicht gefunden (HTTP 404): Pfad prüfen"),
        (500, "Serverfehler (HTTP 500)"),
    ],
)
def test_http_status_causes(status_http, code, expected):
    server = _http(f"{status_http}/{code}/mcp")
    outcome = mcp_status.force_check(server)
    server.refresh_from_db()
    assert not server.online and not outcome.online
    assert server.last_error.startswith(expected), server.last_error
    assert SECRET not in server.last_error


def test_http_refused():
    port = _free_port()
    server = _http(f"http://127.0.0.1:{port}/mcp")
    mcp_status.force_check(server)
    server.refresh_from_db()
    assert server.last_error.startswith(f"Verbindung abgelehnt: 127.0.0.1:{port}")
    assert SECRET not in server.last_error


def test_http_timeout():
    # Nimmt Verbindungen an (Backlog), antwortet aber nie.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(5)
    try:
        server = _http(f"http://127.0.0.1:{sock.getsockname()[1]}/mcp", timeout_seconds=1)
        mcp_status.force_check(server)
    finally:
        sock.close()
    server.refresh_from_db()
    assert (
        server.last_error
        == "Zeitüberschreitung: Der Server hat nicht innerhalb von 1 s geantwortet."
    )


def test_check_timeout_is_capped():
    assert mcp_status.check_timeout(McpServer(timeout_seconds=600)) == 10.0
    assert mcp_status.check_timeout(McpServer(timeout_seconds=3)) == 3.0


def test_stdio_program_not_found():
    server = _server(command="/nicht/vorhanden/mcp-server --flag geheim")
    mcp_status.force_check(server)
    server.refresh_from_db()
    assert server.last_error.startswith("Programm nicht gefunden: „/nicht/vorhanden/mcp-server“")
    assert "geheim" not in server.last_error


def test_stdio_program_exits_immediately():
    server = _server(command=shlex.join([sys.executable, "-c", "pass"]))
    mcp_status.force_check(server)
    server.refresh_from_db()
    assert server.last_error.startswith("Programm beendet sich sofort")


def test_stdio_real_server_online_and_closed_after_worker_check():
    server = _server(command=shlex.join([sys.executable, str(SERVER_SCRIPT)]))
    outcomes = mcp_status.check_due(close_after=True)
    assert [o.online for o in outcomes] == [True]
    server.refresh_from_db()
    assert {"echo", "add"} <= {t["name"] for t in server.reported_tools}

    async def stats():
        return mcp_client.connection_stats()

    assert server.pk not in bridge.run(stats(), timeout=5)  # Worker hält keinen Prozess offen


def test_setup_error_is_stored_without_secret():
    server = _server(command='python "offen', credentials=json.dumps({"env": {"K": SECRET}}))
    mcp_status.force_check(server)
    server.refresh_from_db()
    assert server.last_error.startswith("Falsch eingerichtet:")
    assert SECRET not in server.last_error


def test_inactive_server_is_never_checked(no_connect):
    server = _server(active=False)
    outcome = mcp_status.force_check(server)
    assert outcome.skipped
    server.refresh_from_db()
    assert server.last_checked is None
    assert not bridge.is_running()
    assert mcp_status.check_due() == []


def test_offline_keeps_last_reported_tools(inproc, monkeypatch):
    server = _server()
    mcp_status.force_check(server)
    monkeypatch.setattr(mcp_client, "_make_client", lambda config: (_ for _ in ()).throw(OSError()))
    bridge.shutdown()
    mcp_status.force_check(server)
    server.refresh_from_db()
    assert not server.online and server.last_error
    assert len(server.reported_tools) == 3


def test_status_change_logged_with_id_only(caplog):
    server = _server(name="Geheimname", command="/nicht/da")
    with caplog.at_level("INFO", logger="multigpt.chat.mcp.status"):
        mcp_status.force_check(server)
    assert f"MCP-Server {server.pk} ist jetzt offline" in caplog.text
    assert "Geheimname" not in caplog.text


# --- periodische Prüfung -----------------------------------------------------------


@pytest.fixture
def recorded(monkeypatch):
    seen = []

    def fake(servers, close_after):
        seen.extend(s.name for s in servers)
        return [mcp_status.CheckOutcome(s.pk, True) for s in servers]

    monkeypatch.setattr(mcp_status, "_run_checks", fake)
    return seen


def test_check_due_intervals(recorded):
    now = timezone.now()

    def make(name, online, ago, active=True):
        server = _server(name=name, active=active, online=online)
        McpServer.objects.filter(pk=server.pk).update(
            last_checked=None if ago is None else now - timedelta(seconds=ago)
        )

    make("neu", False, None)
    make("online-frisch", True, 120)
    make("online-alt", True, 310)
    make("offline-frisch", False, 30)
    make("offline-alt", False, 70)
    make("inaktiv", False, None, active=False)
    mcp_status.check_due()
    assert sorted(recorded) == ["neu", "offline-alt", "online-alt"]


def test_claim_prevents_double_check(recorded):
    server = _server()
    other = McpServer.objects.get(pk=server.pk)  # zweiter „Prozess“
    assert mcp_status.claim(server) is True
    assert mcp_status.claim(other) is False
    assert mcp_status.check_due() == []
    assert recorded == []


def test_invalidate_makes_due(recorded):
    server = _server()
    McpServer.objects.filter(pk=server.pk).update(last_checked=timezone.now(), online=True)
    mcp_status.invalidate(server.pk)
    mcp_status.check_due()
    assert recorded == ["Test"]


def test_worker_runs_periodic_check(monkeypatch):
    calls = []
    monkeypatch.setattr(mcp_status, "check_due", lambda: calls.append(1) or [])
    call_command("run_worker", "--once")
    assert calls == [1]


# --- Admin -------------------------------------------------------------------------


@pytest.fixture
def admin_client(client, django_user_model, password):
    admin_user = django_user_model.objects.create_superuser(
        username="jo", password=password, email="jo@example.invalid"
    )
    client.force_login(admin_user)
    return client


def _form_data(server, **extra):
    data = {
        "name": server.name,
        "transport": server.transport,
        "command": server.command,
        "url": server.url,
        "credentials": "",
        "active": "on",
        "timeout_seconds": "30",
        "tools_requiring_confirmation": json.dumps(server.tools_requiring_confirmation),
        "known_tools": json.dumps(server.known_tools),
    }
    data.update(extra)
    return data


def _with_tools(**kwargs):
    """Server mit gespeicherter Werkzeugliste (ohne Netz)."""
    server = _server(**kwargs)
    McpServer.objects.filter(pk=server.pk).update(
        online=True,
        last_checked=timezone.now(),
        last_online=timezone.now(),
        reported_tools=[
            {
                "name": "lesen",
                "description": 'Liest. <script>alert("x")</script>',
                "params": ["pfad", "zeilen"],
                "required": ["pfad"],
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "loeschen",
                "description": "Löscht.",
                "params": ["pfad"],
                "required": ["pfad"],
                "annotations": {"destructiveHint": True},
            },
            {"name": "alt", "description": "", "params": [], "required": []},
        ],
    )
    server.refresh_from_db()
    return server


def _key(name):
    import hashlib

    return "tool_rating_" + hashlib.sha1(name.encode()).hexdigest()[:12]


def test_change_page_lists_tools_without_connecting(admin_client, no_connect):
    server = _with_tools(known_tools=["alt", "weg"], tools_requiring_confirmation=[])
    html = admin_client.get(reverse("admin:chat_mcpserver_change", args=[server.pk])).content
    html = html.decode()
    assert not bridge.is_running()
    assert "Jetzt prüfen" in html and "?tools=1" not in html
    assert '<table class="mcp-tools">' in html
    # Beschreibung vom Server: escaped, nie als HTML.
    assert "&lt;script&gt;" in html and '<script>alert("x")' not in html
    assert "pfad*, zeilen" in html
    assert "nur lesend" in html and "verändernd" in html
    # Neu gemeldet: hervorgehoben, Vorschlag aus annotations vorbelegt.
    assert html.count('class="mcp-tool-new"') == 2
    assert "Vorschlag des Servers: ohne Rückfrage" in html
    lesen = html.split(f'name="{_key("lesen")}"')[1].split("</select>")[0]
    assert '<option value="auto" selected>' in lesen
    loeschen = html.split(f'name="{_key("loeschen")}"')[1].split("</select>")[0]
    assert '<option value="confirm" selected>' in loeschen
    alt = html.split(f'name="{_key("alt")}"')[1].split("</select>")[0]
    assert '<option value="auto" selected>' in alt
    # Eingestuft, aber nicht mehr gemeldet.
    assert "Nicht mehr gemeldet, aber eingestuft: weg" in html
    # JSON-Felder nur noch unter „Erweitert“.
    assert "Erweitert: Einstufung als JSON" in html


def test_rate_tools_via_table(admin_client):
    server = _with_tools(known_tools=["alt", "weg"], tools_requiring_confirmation=["weg"])
    data = _form_data(
        server,
        **{_key("lesen"): "auto", _key("loeschen"): "confirm", _key("alt"): ""},
    )
    url = reverse("admin:chat_mcpserver_change", args=[server.pk])
    response = admin_client.post(url, data)
    assert response.status_code == 302, response.content.decode()[:2000]
    server.refresh_from_db()
    assert set(server.known_tools) == {"weg", "lesen", "loeschen"}
    assert set(server.tools_requiring_confirmation) == {"weg", "loeschen"}
    assert server.tools_checked is not None
    assert mcp_status.unrated_count(server) == 1  # alt


def test_form_without_table_keeps_json_lists(admin_client):
    server = _with_tools(known_tools=["alt"], tools_requiring_confirmation=[])
    url = reverse("admin:chat_mcpserver_change", args=[server.pk])
    response = admin_client.post(url, _form_data(server, known_tools='["alt", "lesen"]'))
    assert response.status_code == 302
    server.refresh_from_db()
    assert server.known_tools == ["alt", "lesen"]


def test_changelist_columns(admin_client, no_connect):
    online = _with_tools(name="A", known_tools=["alt"])
    offline = _server(name="B")
    McpServer.objects.filter(pk=offline.pk).update(
        online=False,
        last_checked=timezone.now(),
        last_error="Zugang abgelehnt (HTTP 401): Token prüfen.",
    )
    _server(name="C")  # ungeprüft
    html = admin_client.get(reverse("admin:chat_mcpserver_changelist")).content.decode()
    assert "column-state_column" in html and "column-tool_count" in html
    assert "column-unrated_column" in html and "column-last_checked" in html
    assert 'title="Zugang abgelehnt (HTTP 401): Token prüfen.">offline – Zugang abgelehnt' in html
    assert "ungeprüft" in html
    row = html.split(f"/admin/chat/mcpserver/{online.pk}/change/")[1].split("</tr>")[0]
    assert '<td class="field-tool_count">3</td>' in row
    assert '<strong class="mcp-unrated">2</strong>' in row
    assert not bridge.is_running()


def test_check_now_button(admin_client, inproc):
    server = _server()
    url = reverse("admin:chat_mcpserver_check", args=[server.pk])
    assert admin_client.get(url).status_code == 405
    response = admin_client.post(url, follow=True)
    texts = [str(m) for m in get_messages(response.wsgi_request)]
    assert any("Verbindung in Ordnung, 3 Werkzeuge" in t and "lesen" in t for t in texts), texts
    server.refresh_from_db()
    assert server.online and len(server.reported_tools) == 3


def test_check_action_reports_offline(admin_client):
    server = _server(command="/nicht/da")
    response = admin_client.post(
        reverse("admin:chat_mcpserver_changelist"),
        {"action": "check_connection_action", "_selected_action": [server.pk]},
        follow=True,
    )
    texts = [str(m) for m in get_messages(response.wsgi_request)]
    assert any("Offline: Programm nicht gefunden" in t for t in texts), texts


def test_check_requires_permission(client, django_user_model, password, no_connect):
    staff = django_user_model.objects.create_user(username="st", password=password, is_staff=True)
    client.force_login(staff)
    server = _server()
    response = client.post(reverse("admin:chat_mcpserver_check", args=[server.pk]))
    assert response.status_code in (302, 403)
    server.refresh_from_db()
    assert server.last_checked is None


def test_save_checks_after_commit(admin_client, inproc, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        response = admin_client.post(
            reverse("admin:chat_mcpserver_add"),
            {
                "name": "Neu",
                "transport": "stdio",
                "command": "unbenutzt",
                "credentials": "",
                "active": "on",
                "timeout_seconds": "30",
                "tools_requiring_confirmation": "[]",
                "known_tools": "[]",
            },
        )
    assert response.status_code == 302
    assert len(callbacks) == 1
    server = McpServer.objects.get(name="Neu")
    assert server.online and len(server.reported_tools) == 3


def test_save_inactive_does_not_check(admin_client, no_connect, django_capture_on_commit_callbacks):
    server = _server()
    with django_capture_on_commit_callbacks(execute=True):
        data = _form_data(server)
        del data["active"]
        admin_client.post(reverse("admin:chat_mcpserver_change", args=[server.pk]), data)
    server.refresh_from_db()
    assert not server.active and server.last_checked is None


def test_import_checks_new_active_servers(admin_client, inproc, django_capture_on_commit_callbacks):
    existing = _server(name="alt")
    McpServer.objects.filter(pk=existing.pk).update(last_checked=timezone.now(), online=True)
    config = {
        "mcpServers": {
            "neu": {"command": "unbenutzt"},
            "platzhalter": {
                "type": "http",
                "url": "https://x.invalid/mcp",
                "headers": {"Authorization": "Bearer <YOUR_TOKEN>"},
            },
            "alt": {"command": "anders"},
        }
    }
    with django_capture_on_commit_callbacks(execute=True):
        response = admin_client.post(
            reverse("admin:chat_mcpserver_import"),
            {"config": json.dumps(config), "update_existing": "on"},
            follow=True,
        )
    texts = [str(m) for m in get_messages(response.wsgi_request)]
    assert any("„neu“: Verbindung in Ordnung" in t for t in texts), texts
    assert McpServer.objects.get(name="neu").online
    placeholder = McpServer.objects.get(name="platzhalter")
    assert not placeholder.active and placeholder.last_checked is None
    assert McpServer.objects.get(name="alt").last_checked is None  # nur zur Neuprüfung vorgemerkt
    assert inproc == [McpServer.objects.get(name="neu").pk]


# --- Chat ---------------------------------------------------------------------------


@pytest.fixture
def adult(client, password):
    user = User.objects.create_user(
        "erwachsen", password=password, role=Role.objects.get(key="adult")
    )
    client.force_login(user)
    return user


@pytest.fixture
def tool_model():
    provider = Provider.objects.create(name="Cloud", kind=Provider.Kind.ANTHROPIC)
    return AIModel.objects.create(
        provider=provider,
        model_id="claude-test",
        display_name="Claude",
        supports_tools=True,
        mcp_access=AIModel.McpAccess.ALL,
    )


def _offline(server, error="Verbindung abgelehnt: 127.0.0.1:9 nimmt keine Verbindung an."):
    McpServer.objects.filter(pk=server.pk).update(
        online=False, last_checked=timezone.now(), last_error=error
    )
    server.refresh_from_db()
    return server


def test_api_marks_offline_server(client, adult):
    up = _server(name="Oben")
    down = _offline(_server(name="Unten"))
    data = client.get(reverse("chat:api_mcp_servers")).json()
    by_name = {s["name"]: s for s in data}
    assert by_name["Oben"] == {
        "id": up.pk,
        "name": "Oben",
        "default_enabled": True,
        "online": True,
        "error": None,
    }
    assert by_name["Unten"]["online"] is False
    assert by_name["Unten"]["default_enabled"] is False
    assert by_name["Unten"]["error"] == down.last_error


def test_offline_server_not_offered(adult, tool_model, no_connect):
    server = _offline(_server(known_tools=["lesen"]))
    offline = []
    bindings = tooling.collect_tools(adult, [server.pk], tool_model, offline=offline)
    assert bindings == {}
    assert offline == ["Test"]


def test_live_failure_marks_offline(adult, tool_model):
    server = _server(command="/nicht/da")
    offline = []
    assert tooling.collect_tools(adult, [server.pk], tool_model, offline=offline) == {}
    assert offline == ["Test"]
    server.refresh_from_db()
    assert mcp_status.known_offline(server)


def test_stream_shows_hint_instead_of_error(client, adult, tool_model, monkeypatch, no_connect):
    server = _offline(_server(known_tools=["lesen"]))
    calls = []
    monkeypatch.setattr(
        registry, "get_adapter", lambda p: ScriptedAdapter(p, [answer("Hallo.")], calls)
    )
    conversation = Conversation.objects.create(user=adult)
    response = client.post(
        reverse("chat:api_messages", args=[conversation.pk]),
        json.dumps({"content": "Hi", "model": tool_model.pk, "mcp_servers": [server.pk]}),
        content_type="application/json",
    )
    events = events_of(response)
    status = [d for name, d in events if name == "status"]
    assert any("„Test“ ist offline" in d["text"] and d["level"] == "warning" for d in status)
    assert events[-1] == ("done", {"status": "complete"})
    assert not any(t.name.startswith("Test__") for t in calls[0]["tools"] or [])


def test_chat_js_greys_out_offline_servers():
    js = (Path(__file__).parents[1] / "multigpt/chat/static/chat/chat.js").read_text()
    block = js.split("if (server.online === false) {")[1].split("}")[0]
    assert "input.disabled = true" in block and "label.title" in block
    assert "is-offline" in block


def test_status_bar_mcp_only_for_admins(client, adult, django_user_model, password):
    from django.test import Client as DjangoClient

    _offline(_server(name="Unten"), error="Zugang abgelehnt (HTTP 401): Token prüfen.")
    html = client.get(reverse("chat:index")).content.decode()
    assert "MCP Unten offline" not in html  # normales Konto (adult)
    admin = DjangoClient()
    admin.force_login(
        django_user_model.objects.create_superuser("jo", "jo@example.invalid", password)
    )
    html = admin.get(reverse("chat:index")).content.decode()
    assert "MCP Unten offline – Zugang abgelehnt" in html
