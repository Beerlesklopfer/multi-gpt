"""Transport ``runner`` (M14-06): MCP über die Zustellung, mit Fake-Runner."""

from types import SimpleNamespace

import pytest

from multigpt.chat import mcp
from multigpt.chat.mcp import bridge
from multigpt.chat.mcp import client as mcp_client
from multigpt.chat.mcp.config import ServerConfig
from multigpt.runner import mcp_transport
from tests.runner_fakes import FakeLink


@pytest.fixture
def link():
    bridge.shutdown()
    fake = FakeLink()
    mcp_transport.set_link(fake)
    yield fake
    bridge.shutdown()
    mcp_transport.set_link(None)


def _config(pipe="p1", session=7):
    server = SimpleNamespace(pk=99, name="Runner (anna)", active=True, timeout_seconds=10)
    return ServerConfig.for_runner(server, runner_id=3, session_id=session, pipe=pipe)


def test_call_tool_roundtrip_over_link(link):
    config = _config()
    result = mcp.call_tool_with(config, "write", {"path": "a.txt", "content": "hallo"})
    assert not result.is_error
    assert link.workspace.files == {"a.txt": "hallo"}
    result = mcp.call_tool_with(config, "read", {"path": "a.txt"})
    assert "hallo" in result.text
    # Alles lief als JSON-dict über die Zustellung, mit Runner und pipe.
    assert link.opened == [(3, "p1", 7)]
    assert all(runner == 3 and pipe == "p1" for runner, pipe, _ in link.sent)
    assert all(isinstance(message, dict) for _, _, message in link.sent)


def test_tool_error_is_result_not_exception(link):
    result = mcp.call_tool_with(_config(), "read", {"path": "fehlt.txt"})
    assert result.is_error


def test_connection_per_pipe(link):
    mcp.call_tool_with(_config("p1", 1), "bash", {"command": "echo a"})
    mcp.call_tool_with(_config("p2", 2), "bash", {"command": "echo b"})

    async def stats():
        return mcp_client.connection_stats()

    keys = set(bridge.run(stats(), timeout=5))
    assert {(99, "p1"), (99, "p2")} <= keys
    mcp.close_pipe(99, "p1")
    assert (99, "p1") not in bridge.run(stats(), timeout=5)
    assert "p1" in link.closed


def test_offline_runner_gives_german_error(link):
    link.online = False
    with pytest.raises(mcp.McpError) as info:
        mcp.call_tool_with(_config(), "bash", {"command": "echo a"})
    assert "Runner (anna)" in str(info.value)


def test_from_model_rejects_runner_without_session():
    server = SimpleNamespace(
        pk=1, name="Runner", active=True, transport="runner", credentials="", timeout_seconds=5
    )
    with pytest.raises(mcp.McpError):
        ServerConfig.from_model(server)
