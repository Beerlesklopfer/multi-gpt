"""MCP-Testserver für die Tests (stdio oder Streamable HTTP).

Aufruf: ``python tests/mcp_test_server.py [stdio|http PORT]``.
Ist ``MCP_TEST_PIDFILE`` gesetzt, hängt der Server beim Start seine PID an.
"""

import asyncio
import os
import sys

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.utilities.types import Image

# 1×1-PNG
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360606060000000050001a5f645400000000049454e44ae426082"
)

server = MCPServer("multigpt-test")


@server.tool()
def echo(text: str) -> str:
    """Gibt den Text zurück."""
    return text


@server.tool()
def add(a: int, b: int) -> int:
    """Addiert zwei Zahlen."""
    return a + b


@server.tool()
def fail(reason: str = "kaputt") -> str:
    """Schlägt immer fehl."""
    raise ValueError(reason)


@server.tool()
async def sleep(seconds: float) -> str:
    """Wartet."""
    await asyncio.sleep(seconds)
    return "wach"


@server.tool()
def crash() -> str:
    """Beendet den Serverprozess sofort."""
    os._exit(3)


@server.tool()
def image() -> Image:
    """Liefert ein winziges PNG."""
    return Image(data=PNG, format="png")


@server.tool()
def getenv(name: str) -> str:
    """Liest eine Umgebungsvariable."""
    return os.environ.get(name, "<leer>")


@server.tool()
def header(name: str, ctx: Context) -> str:
    """Liest einen HTTP-Header der Anfrage (nur Streamable HTTP)."""
    headers = ctx.headers or {}
    return headers.get(name.lower(), "<leer>")


@server.tool()
def pid() -> int:
    """PID des Serverprozesses."""
    return os.getpid()


if __name__ == "__main__":
    pidfile = os.environ.get("MCP_TEST_PIDFILE")
    if pidfile:
        with open(pidfile, "a") as fh:
            fh.write(f"{os.getpid()}\n")
    if len(sys.argv) > 2 and sys.argv[1] == "http":
        server.run("streamable-http", host="127.0.0.1", port=int(sys.argv[2]))
    else:
        server.run("stdio")
