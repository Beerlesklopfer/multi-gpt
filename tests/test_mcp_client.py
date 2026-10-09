"""MCP-Client (M4a-03) gegen den Testserver aus tests/mcp_test_server.py.

Die meisten Tests verbinden sich im Prozess (In-Memory-Transport des SDK, kein
Kindprozess). Die stdio-Tests starten den Testserver als echten Kindprozess.
"""

import json
import os
import shlex
import sys
import threading
import time
from pathlib import Path

import pytest
from mcp import Client

from multigpt.chat import mcp
from multigpt.chat.mcp import bridge
from multigpt.chat.mcp import client as mcp_client
from multigpt.chat.models import McpServer
from tests import mcp_test_server

SERVER_SCRIPT = Path(__file__).with_name("mcp_test_server.py")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _fresh_bridge():
    bridge.shutdown()
    yield
    bridge.shutdown()


@pytest.fixture
def inproc(monkeypatch):
    """Verbindet im Prozess statt über stdio; zählt die Verbindungsaufbauten."""
    calls = []

    def factory(config):
        calls.append(config.name)
        return Client(mcp_test_server.server, cache=None)

    monkeypatch.setattr(mcp_client, "_make_client", factory)
    return calls


def _server(name="Test", **kwargs):
    defaults = {
        "transport": "stdio",
        "command": shlex.join([sys.executable, str(SERVER_SCRIPT)]),
        "timeout_seconds": 30,
    }
    defaults.update(kwargs)
    return McpServer.objects.create(name=name, **defaults)


# --- Im Prozess --------------------------------------------------------------


def test_list_tools_returns_toolspecs(inproc):
    tools = {t.name: t for t in mcp.list_tools(_server())}
    assert {"echo", "add", "fail", "sleep", "image"} <= set(tools)
    add = tools["add"]
    assert add.description == "Addiert zwei Zahlen."
    assert add.parameters["type"] == "object"
    assert set(add.parameters["properties"]) == {"a", "b"}


def test_call_tool_text_result(inproc):
    result = mcp.call_tool(_server(), "add", {"a": 2, "b": 3})
    assert result.text == "5"
    assert result.is_error is False
    assert result.raw["content"][0] == {"type": "text", "text": "5"}


def test_tool_error_is_result_not_exception(inproc):
    result = mcp.call_tool(_server(), "fail", {"reason": "kaputt"})
    assert result.is_error is True
    assert result.text


def test_unknown_tool_is_error(inproc):
    try:
        result = mcp.call_tool(_server(), "gibt_es_nicht", {})
    except mcp.McpError:
        return
    assert result.is_error is True


def test_image_result(inproc):
    result = mcp.call_tool(_server(), "image", {})
    assert len(result.images) == 1
    assert result.images[0].mime_type == "image/png"
    assert result.images[0].data.startswith(PNG_SIGNATURE)
    # Binärdaten stehen nicht im JSON für ToolCall.result.
    assert result.raw["content"][0]["data"] == {"omitted_bytes": len(result.images[0].data)}
    json.dumps(result.raw)


def test_call_timeout_raises_and_connection_survives(inproc):
    server = _server()
    start = time.monotonic()
    with pytest.raises(mcp.McpTimeout):
        mcp.call_tool(server, "sleep", {"seconds": 10}, timeout=0.5)
    assert time.monotonic() - start < 3
    assert mcp.call_tool(server, "echo", {"text": "weiter"}).text == "weiter"
    assert len(inproc) == 1


def test_parallel_first_use_connects_once(inproc):
    server = _server()
    barrier = threading.Barrier(8)
    results, errors = [], []

    def worker():
        barrier.wait()
        try:
            results.append(len(mcp.list_tools(server)))
        except Exception as exc:  # pragma: no cover - Fehler sichtbar machen
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(results) == 8
    assert inproc == ["Test"]


def test_parallel_calls_on_one_session(inproc):
    server = _server()
    results = []

    def worker(i):
        results.append(mcp.call_tool(server, "add", {"a": i, "b": 1}).text)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results, key=int) == [str(i + 1) for i in range(8)]
    assert len(inproc) == 1


def test_tool_list_is_cached_and_invalidated_on_change(inproc, monkeypatch):
    server = _server()
    mcp.list_tools(server)
    listed = []
    original = Client.list_tools

    async def counting(self, *args, **kwargs):
        listed.append(1)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(Client, "list_tools", counting)
    mcp.list_tools(server)
    assert listed == []  # aus dem Cache
    mcp.list_tools(server, refresh=True)
    assert listed == [1]
    server.credentials = '{"env": {"NEU": "1"}}'
    server.save()
    mcp.list_tools(server)
    assert listed == [1, 1]
    assert len(inproc) == 2  # geänderte Verbindungsdaten: neu verbunden


def test_inactive_server_is_refused(inproc):
    with pytest.raises(mcp.McpError, match="deaktiviert"):
        mcp.call_tool(_server(active=False), "echo", {"text": "x"})
    assert inproc == []


def test_arguments_must_be_object(inproc):
    with pytest.raises(mcp.McpError):
        mcp.call_tool(_server(), "echo", ["x"])


def test_check_connection_refreshes(inproc):
    assert "echo" in {t.name for t in mcp.check_connection(_server())}


# --- Rückfragepflicht --------------------------------------------------------


def test_requires_confirmation_rules():
    server = McpServer(
        name="R",
        transport="stdio",
        command="x",
        known_tools=["lesen", "schreiben"],
        tools_requiring_confirmation=["schreiben", "senden"],
    )
    assert mcp.requires_confirmation(server, "lesen") is False
    assert mcp.requires_confirmation(server, "schreiben") is True
    assert mcp.requires_confirmation(server, "senden") is True
    assert mcp.requires_confirmation(server, "unbekannt") is True


def test_new_server_requires_confirmation_for_everything():
    server = McpServer(name="N", transport="stdio", command="x")
    assert mcp.requires_confirmation(server, "echo") is True


# --- Echte stdio-Kindprozesse -----------------------------------------------


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_stdio_list_call_env_and_image():
    server = _server(credentials=json.dumps({"env": {"MULTIGPT_TEST_VALUE": "geheim-42"}}))
    assert "getenv" in {t.name for t in mcp.list_tools(server)}
    assert mcp.call_tool(server, "add", {"a": 20, "b": 22}).text == "42"
    assert mcp.call_tool(server, "getenv", {"name": "MULTIGPT_TEST_VALUE"}).text == "geheim-42"
    # Die Umgebung der App wird nicht vererbt (nur die Grundumgebung des SDK).
    assert mcp.call_tool(server, "getenv", {"name": "DATABASE_URL"}).text == "<leer>"
    assert mcp.call_tool(server, "image", {}).images[0].data.startswith(PNG_SIGNATURE)


def test_stdio_crash_raises_and_reconnects():
    server = _server()
    first_pid = int(mcp.call_tool(server, "pid", {}).text)
    with pytest.raises(mcp.McpError) as info:
        mcp.call_tool(server, "crash", {})
    assert not isinstance(info.value, mcp.McpTimeout)
    second_pid = int(mcp.call_tool(server, "pid", {}).text)
    assert second_pid != first_pid


def test_stdio_parallel_first_use_starts_one_process(tmp_path):
    pidfile = tmp_path / "pids"
    server = _server(credentials=json.dumps({"env": {"MCP_TEST_PIDFILE": str(pidfile)}}))
    barrier = threading.Barrier(8)
    errors = []

    def worker():
        barrier.wait()
        try:
            mcp.list_tools(server)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(pidfile.read_text().split()) == 1


def test_shutdown_terminates_stdio_child():
    server = _server()
    pid = int(mcp.call_tool(server, "pid", {}).text)
    assert _alive(pid)
    bridge.shutdown()
    deadline = time.monotonic() + 10
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(pid)


def test_stdio_missing_command_gives_german_error():
    server = _server(command="/nicht/vorhanden/mcp-server --flag")
    with pytest.raises(mcp.McpError, match="konnte nicht gestartet werden"):
        mcp.list_tools(server, timeout=10)


def test_http_unreachable_gives_error_without_secret():
    server = _server(
        name="Web",
        transport="http",
        command="",
        url="http://127.0.0.1:9/mcp",
        credentials="token-sehr-geheim",
    )
    with pytest.raises(mcp.McpError) as info:
        mcp.list_tools(server, timeout=10)
    assert "token-sehr-geheim" not in str(info.value)


@pytest.fixture
def http_server_url():
    import socket
    import subprocess

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    proc = subprocess.Popen(
        [sys.executable, str(SERVER_SCRIPT), "http", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        bridge.shutdown()
        proc.terminate()
        proc.wait(timeout=10)


def test_http_list_call_and_bearer_header(http_server_url):
    server = _server(
        name="Web",
        transport="http",
        command="",
        url=http_server_url,
        credentials=json.dumps({"bearer_token": "tok-123", "headers": {"X-Extra": "ja"}}),
    )
    assert "header" in {t.name for t in mcp.list_tools(server)}
    assert mcp.call_tool(server, "add", {"a": 1, "b": 2}).text == "3"
    assert mcp.call_tool(server, "header", {"name": "Authorization"}).text == "Bearer tok-123"
    assert mcp.call_tool(server, "header", {"name": "X-Extra"}).text == "ja"
