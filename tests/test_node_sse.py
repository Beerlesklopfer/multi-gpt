"""Legacy-Transport HTTP+SSE (M15, ``node/sse.py``): Handshake, Zustellung über
LISTEN/NOTIFY (zweite Verbindung, zweiter Prozess), Sitzung gehört zum Key,
Widerruf, Grenzen, Aufräumen und Ende-zu-Ende mit dem SDK-Client im SSE-Modus.

Die Zustellung braucht echte Commits (NOTIFY kommt erst nach dem Commit an, die
LISTEN-Verbindung sieht nur Festgeschriebenes), deshalb ``transaction=True``.
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest
from django.db import connection
from django.utils import timezone

from multigpt.node import keys, sse
from multigpt.node import scopes as S
from multigpt.node.models import ApiCall, SseMessage, SseSession
from tests.node_support import make_key, make_user

# serialized_rollback: Startrollen aus den Datenmigrationen bleiben für spätere Tests.
pytestmark = pytest.mark.django_db(transaction=True, serialized_rollback=True)

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _hosts(settings):
    settings.ALLOWED_HOSTS = ["testserver", "nas.example", "localhost", "127.0.0.1"]


@pytest.fixture
def anna():
    return make_user("anna")


@pytest.fixture
def key(anna):
    return make_key(anna, [S.USAGE_READ])


def open_stream(client, secret, **headers):
    if secret is not None:
        headers["HTTP_AUTHORIZATION"] = f"Bearer {secret}"
    return client.get("/mcp/sse", HTTP_ACCEPT="text/event-stream", **headers)


def parse(chunk: bytes) -> tuple[str, str]:
    """Ein SSE-Event -> (event, data)."""
    name, data = "", ""
    for line in chunk.decode().splitlines():
        if line.startswith("event: "):
            name = line[7:]
        elif line.startswith("data: "):
            data = line[6:]
    return name, data


def handshake(client, secret):
    response = open_stream(client, secret)
    assert response.status_code == 200, response
    events = iter(response.streaming_content)
    name, endpoint = parse(next(events))
    assert name == "endpoint"
    return response, events, endpoint


def post(client, secret, endpoint, body):
    return client.post(
        endpoint,
        data=json.dumps(body),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {secret}",
    )


def next_message(events) -> dict:
    """Nächstes ``message``-Event (Keepalives überspringen)."""
    for chunk in events:
        name, data = parse(chunk)
        if name == "message":
            return json.loads(data)
    raise AssertionError("Stream ohne Nachricht beendet")


def test_handshake_sends_endpoint_with_session(client, key):
    api_key, secret = key
    response, events, endpoint = handshake(client, secret)
    assert response["Content-Type"] == "text/event-stream"
    assert response["X-Accel-Buffering"] == "no"
    assert "no-cache" in response["Cache-Control"]
    path, _, query = endpoint.partition("?")
    assert path == "/mcp/messages"
    session_id = query.removeprefix("session_id=")
    assert sse.SESSION_RE.match(session_id)  # 128 Bit hex
    assert SseSession.objects.get(pk=session_id).key == api_key
    assert sse.open_streams() == 1
    response.close()
    # Verbindungsende: Sitzung weg, Platz frei, ein Audit-Eintrag ohne Inhalte.
    assert not SseSession.objects.exists()
    assert sse.open_streams() == 0
    entry = ApiCall.objects.get(method=sse.STREAM_METHOD)
    assert entry.key == api_key and entry.status == ApiCall.Status.OK
    assert entry.response_bytes > 0


def test_stream_needs_key_origin_and_get(client, key):
    _, secret = key
    assert open_stream(client, None).status_code == 401
    assert open_stream(client, "mgpt_abcdefghij_" + "x" * 43).status_code == 401
    assert open_stream(client, secret, HTTP_ORIGIN="https://evil.example").status_code == 403
    assert client.post("/mcp/sse", HTTP_AUTHORIZATION=f"Bearer {secret}").status_code == 405
    assert client.get("/mcp/messages", HTTP_AUTHORIZATION=f"Bearer {secret}").status_code == 405
    # Streamable HTTP bleibt unverändert: GET auf /mcp/ ist 405.
    assert client.get("/mcp/", HTTP_AUTHORIZATION=f"Bearer {secret}").status_code == 405
    assert not SseSession.objects.exists()
    assert sse.open_streams() == 0


def test_post_202_and_answer_in_stream(client, key, settings):
    # Langes Keepalive: kommt die Antwort schnell, dann über NOTIFY, nicht über
    # die Sicherheitsabfrage nach dem Intervall.
    settings.MCP_SSE_KEEPALIVE_SECONDS = 30
    _, secret = key
    response, events, endpoint = handshake(client, secret)
    try:
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
        }
        answer = post(client, secret, endpoint, init)
        assert answer.status_code == 202
        assert answer.content == b""
        started = time.monotonic()
        message = next_message(events)
        assert time.monotonic() - started < 5
        assert message["id"] == 1
        assert message["result"]["protocolVersion"] == "2024-11-05"
        assert message["result"]["serverInfo"]["name"] == "multi-gpt"
        # Benachrichtigung: 202, nichts im Stream
        note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        assert post(client, secret, endpoint, note).status_code == 202
        for request_id, method in ((2, "tools/list"), (3, "unknown/method")):
            body = {"jsonrpc": "2.0", "id": request_id, "method": method}
            assert post(client, secret, endpoint, body).status_code == 202
        listed = next_message(events)
        assert listed["id"] == 2
        assert [t["name"] for t in listed["result"]["tools"]] == ["usage"]
        error = next_message(events)
        assert error["id"] == 3 and error["error"]["code"] == -32601
        body = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "usage", "arguments": {}},
        }
        assert post(client, secret, endpoint, body).status_code == 202
        result = next_message(events)
        assert result["id"] == 4 and result["result"]["isError"] is False
        # Kaputtes JSON bekommt direkt 400 (keine ID, an die man antworten könnte).
        bad = client.post(
            endpoint,
            data="{",
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {secret}",
        )
        assert bad.status_code == 400
        assert not SseMessage.objects.exists()  # gesendet = gelöscht
        assert ApiCall.objects.filter(tool="usage", http_status=202).count() == 1
    finally:
        response.close()


def test_delivery_from_another_process(client, key, settings):
    """NOTIFY aus einem zweiten Prozess (wie ein anderer gunicorn-Worker)."""
    settings.MCP_SSE_KEEPALIVE_SECONDS = 30
    _, secret = key
    response, events, endpoint = handshake(client, secret)
    session_id = endpoint.split("session_id=", 1)[1]
    # Zweiter Prozess mit denselben Einstellungen, aber auf der Test-Datenbank.
    code = (
        "import sys, django; django.setup()\n"
        "from django.db import connection\n"
        "connection.settings_dict['NAME'] = sys.argv[1]\n"
        "from multigpt.node import sse\n"
        "assert sse.deliver(sys.argv[2], {'jsonrpc': '2.0', 'id': 'x', 'result': {}})\n"
    )
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": "multigpt.settings"}
    try:
        done = subprocess.run(
            [sys.executable, "-c", code, connection.settings_dict["NAME"], session_id],
            cwd=ROOT,
            env=env,
            capture_output=True,
            timeout=60,
        )
        assert done.returncode == 0, done.stderr.decode()[-2000:]
        started = time.monotonic()
        assert next_message(events) == {"jsonrpc": "2.0", "id": "x", "result": {}}
        assert time.monotonic() - started < 5
    finally:
        response.close()


def test_wrong_or_foreign_session_is_404(client, key):
    api_key, secret = key
    _, other_secret = make_key(make_user("bert"), [S.USAGE_READ])
    _, second_secret = make_key(api_key.owner, [S.USAGE_READ], name="Zweiter")
    response, events, endpoint = handshake(client, secret)
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    try:
        # Sitzung eines anderen Kontos bzw. eines anderen Keys desselben Kontos
        assert post(client, other_secret, endpoint, ping).status_code == 404
        assert post(client, second_secret, endpoint, ping).status_code == 404
        # unbekannte, kaputte und fehlende Sitzung
        assert post(client, secret, "/mcp/messages?session_id=" + "0" * 32, ping).status_code == 404
        assert post(client, secret, "/mcp/messages?session_id=../x", ping).status_code == 404
        assert post(client, secret, "/mcp/messages", ping).status_code == 404
        # ohne Key 401 (vor der Sitzungsprüfung)
        assert client.post(endpoint, data="{}", content_type="application/json").status_code == 401
        assert post(client, secret, endpoint, ping).status_code == 202
        assert next_message(events) == {"jsonrpc": "2.0", "id": 1, "result": {}}
    finally:
        response.close()
    # Nach dem Ende des Streams gibt es die Sitzung nicht mehr.
    assert post(client, secret, endpoint, ping).status_code == 404


def test_revoked_key_ends_stream(client, key, settings):
    settings.MCP_SSE_KEEPALIVE_SECONDS = 0.2
    api_key, secret = key
    response, events, _endpoint = handshake(client, secret)
    assert parse(next(events)) == ("", "")  # Keepalive-Kommentar
    keys.revoke(api_key)
    started = time.monotonic()
    rest = list(events)  # endet von selbst
    assert time.monotonic() - started < 3
    assert all(chunk == b": keepalive\n\n" for chunk in rest)
    assert not SseSession.objects.exists()
    assert sse.open_streams() == 0
    response.close()


def test_stream_ends_after_max_duration(client, key, settings):
    settings.MCP_SSE_KEEPALIVE_SECONDS = 0.1
    settings.MCP_SSE_MAX_MINUTES = 0.5 / 60  # 0,5 s
    _, secret = key
    response, events, _endpoint = handshake(client, secret)
    started = time.monotonic()
    list(events)
    assert time.monotonic() - started < 3
    assert not SseSession.objects.exists()
    response.close()


def test_limits_per_key_and_process(client, key, settings):
    _, secret = key
    first = open_stream(client, secret)
    second = open_stream(client, secret)
    assert first.status_code == second.status_code == 200
    third = open_stream(client, secret)
    assert third.status_code == 503
    assert int(third["Retry-After"]) > 0
    assert ApiCall.objects.filter(method="sse", http_status=503).count() == 1
    second.close()  # nie gelesen: close() gibt trotzdem alles frei
    assert sse.open_streams() == 1
    settings.MCP_SSE_PER_PROCESS = 1
    _, bert_secret = make_key(make_user("bert"), [S.USAGE_READ])
    blocked = open_stream(client, bert_secret)
    assert blocked.status_code == 503  # Prozess voll, obwohl bert noch nichts offen hat
    first.close()
    assert sse.open_streams() == 0
    again = open_stream(client, bert_secret)
    assert again.status_code == 200
    again.close()
    assert not SseSession.objects.exists()


def test_cleanup_removes_orphaned_sessions(key):
    api_key, _ = key
    old = SseSession.objects.create(
        id="a" * 32, key=api_key, last_seen=timezone.now() - timedelta(minutes=10)
    )
    SseMessage.objects.create(session=old, payload="{}")
    fresh = SseSession.objects.create(id="b" * 32, key=api_key)
    assert sse.cleanup() == 2  # Sitzung und ihre Nachricht
    assert list(SseSession.objects.values_list("pk", flat=True)) == [fresh.pk]
    # Verwaiste Sitzungen zählen nicht gegen die Grenze je Key.
    SseSession.objects.create(id="c" * 32, key=api_key, last_seen=old.last_seen)
    assert sse.open_session(api_key) is not None
    assert sse.open_session(api_key) is None  # zwei lebende: Grenze erreicht


def test_sdk_client_in_sse_mode(live_server, settings):
    """Ende-zu-Ende: ``mcp.client.sse.sse_client`` (SDK) gegen den echten Server."""
    from mcp import Client
    from mcp.client.sse import sse_client

    settings.ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]
    settings.MCP_SSE_KEEPALIVE_SECONDS = 0.5
    anna = make_user("anna")
    _, secret = make_key(anna, [S.USAGE_READ, S.DOCS_READ])
    url = f"{live_server.url}/mcp/sse"

    async def session():
        transport = sse_client(url, headers={"Authorization": f"Bearer {secret}"}, timeout=30)
        async with Client(transport, mode="legacy") as client:
            listed = await client.list_tools()
            result = await client.call_tool("usage", {})
            return sorted(t.name for t in listed.tools), result

    names, result = asyncio.run(session())
    assert "usage" in names and "list_collections" in names
    assert "ask" not in names
    assert result.is_error is False
    assert "month" in (result.structured_content or {})
    # Audit-Eintrag des POST entsteht erst nach der Zustellung; der Stream endet
    # beim nächsten Schreibversuch nach dem Trennen (Keepalive) und räumt auf.
    wanted = {"initialize", "tools/list", "tools/call"}
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and (
        SseSession.objects.exists()
        or not wanted <= set(ApiCall.objects.values_list("method", flat=True))
    ):
        time.sleep(0.2)
    assert wanted <= set(ApiCall.objects.values_list("method", flat=True))
    assert not SseSession.objects.exists()
    assert ApiCall.objects.filter(method=sse.STREAM_METHOD).exists()


def test_large_file_over_sse_becomes_link(client, anna, settings, tmp_path):
    """LOP C18: Über SSE passt nur ein Event bis etwa 1 MB. Große Dateien kommen
    deshalb als resource_link mit Hinweis, kleine weiter eingebettet."""
    from django.core.files.base import ContentFile

    from multigpt.chat.models import Attachment, Conversation, Message
    from multigpt.node import protocol

    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.MCP_SSE_KEEPALIVE_SECONDS = 30
    conversation = Conversation.objects.create(user=anna)
    message = Message.objects.create(conversation=conversation, role="assistant", content="")
    ids = []
    for name, size in (("klein.txt", 1000), ("gross.txt", protocol.SSE_INLINE_BYTES)):
        attachment = Attachment(
            message=message,
            conversation=conversation,
            kind=Attachment.Kind.FILE,
            mime_type="text/plain",
            size=size,
            original_name=name,
        )
        attachment.file.save(name, ContentFile(b"x" * size), save=True)
        ids.append(attachment.pk)
    _, secret = make_key(anna, [S.FILES_READ])
    response, events, endpoint = handshake(client, secret)
    try:
        for request_id, pk in enumerate(ids, start=1):
            body = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": "get_file", "arguments": {"attachment_id": pk}},
            }
            assert post(client, secret, endpoint, body).status_code == 202
        small = next_message(events)["result"]
        large = next_message(events)["result"]
    finally:
        response.close()
    assert [c["type"] for c in small["content"]] == ["text", "resource", "text"]
    assert [c["type"] for c in large["content"]] == ["text", "resource_link", "text"]
    assert "Streamable HTTP" in large["content"][0]["text"]
    assert len(json.dumps(large)) < 1024 * 1024
