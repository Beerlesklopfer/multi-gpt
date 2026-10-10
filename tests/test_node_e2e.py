"""Ende-zu-Ende (M15): MultiGPT spricht mit sich selbst.

Der MCP-Client des Projekts (``multigpt/chat/mcp``, SDK ``mcp`` mit
Streamable HTTP) verbindet sich mit ``/mcp/`` eines echten Servers (pytest-django
``live_server``, WSGI wie unter gunicorn). Der Client verhandelt die Ära selbst
(``server/discover`` → 2026-07-28); ein zweiter Test erzwingt den älteren
``initialize``-Handshake, wie ihn n8n und Claude Desktop sprechen.
"""

import asyncio
import json

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from multigpt.chat import mcp
from multigpt.chat.mcp import bridge
from multigpt.chat.models import Collection, IndexRun, McpServer
from multigpt.node import scopes as S
from multigpt.node.models import ApiCall
from tests.node_support import make_key, make_user

# serialized_rollback: Startrollen aus den Datenmigrationen bleiben für spätere Tests.
pytestmark = pytest.mark.django_db(transaction=True, serialized_rollback=True)


@pytest.fixture(autouse=True)
def _fresh_bridge():
    bridge.shutdown()
    yield
    bridge.shutdown()


def test_multigpt_talks_to_itself(live_server):
    anna = make_user("anna")
    Collection.objects.create(owner=anna, name="Haus")
    _, secret = make_key(anna, [S.DOCS_READ, S.DOCS_WRITE, S.USAGE_READ])
    server = McpServer.objects.create(
        name="Selbst",
        transport="http",
        url=f"{live_server.url}/mcp/",
        credentials=json.dumps({"bearer_token": secret}),
        timeout_seconds=30,
    )
    tools = {t.name for t in mcp.list_tools(server)}
    assert {"list_collections", "upload_document", "reindex", "run_status", "usage"} <= tools
    assert "ask" not in tools  # ohne chat.ask
    result = mcp.call_tool(server, "list_collections", {})
    assert result.is_error is False
    assert "Haus" in result.text
    result = mcp.call_tool(server, "reindex", {"collection": "Haus"})
    run_id = json.loads(result.text.split("\n", 1)[1])["id"]
    result = mcp.call_tool(server, "run_status", {"run_id": run_id})
    assert f"Lauf #{run_id}" in result.text
    assert IndexRun.objects.get(pk=run_id).started_by == anna
    # Fehler als Ergebnis, nicht als Ausnahme
    result = mcp.call_tool(server, "run_status", {"run_id": 999999})
    assert result.is_error and "nicht gefunden" in result.text
    assert ApiCall.objects.filter(tool="run_status").count() == 2

    # Falscher Key: der Client meldet einen Fehler, ohne den Key zu nennen.
    server.credentials = json.dumps({"bearer_token": "mgpt_abcdefghij_" + "z" * 43})
    server.save()
    with pytest.raises(mcp.McpError) as info:
        mcp.list_tools(server, refresh=True)
    assert "z" * 43 not in str(info.value)


def test_legacy_handshake_with_sdk_client(live_server):
    anna = make_user("anna")
    _, secret = make_key(anna, [S.USAGE_READ])
    url = f"{live_server.url}/mcp"

    async def session():
        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {secret}"}) as http:
            async with Client(
                streamable_http_client(url, http_client=http), mode="legacy"
            ) as client:
                listed = await client.list_tools()
                result = await client.call_tool("usage", {})
                return [t.name for t in listed.tools], result

    names, result = asyncio.run(session())
    assert names == ["usage"]
    assert result.is_error is False
    assert "month" in (result.structured_content or {})
    assert ApiCall.objects.filter(method="initialize").exists()
