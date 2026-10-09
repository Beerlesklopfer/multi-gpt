"""MCP-Client (Plan 8g, M4a-01/03).

Öffentliche, synchrone API – siehe :mod:`.service`::

    from multigpt.chat import mcp

    tools = mcp.list_tools(server)                      # list[ToolSpec]
    result = mcp.call_tool(server, "name", {"a": 1})    # ToolResult
    mcp.requires_confirmation(server, "name")           # bool

Architektur: :mod:`.bridge` (Loop-Thread je Prozess), :mod:`.client`
(Sitzungen im Loop), :mod:`.config` (Zugangsdaten-Format).
"""

from .errors import McpError, McpTimeout
from .results import ToolFile, ToolResult

# Das SDK wird erst beim ersten Gebrauch importiert (spart Startzeit für
# manage.py-Befehle und Prozesse ohne MCP).
_SERVICE_NAMES = {
    "call_tool",
    "check_connection",
    "close_server",
    "list_tools",
    "requires_confirmation",
    "shutdown",
}


def __getattr__(name):
    if name in _SERVICE_NAMES:
        from . import service

        return getattr(service, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "McpError",
    "McpTimeout",
    "ToolFile",
    "ToolResult",
    *sorted(_SERVICE_NAMES),
]
