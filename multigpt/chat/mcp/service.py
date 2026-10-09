"""Synchrone MCP-API für Django-Views und die Werkzeugschleife.

Alle Funktionen lesen den ``McpServer`` im aufrufenden Thread aus und übergeben
nur eine unveränderliche :class:`ServerConfig` an den Loop-Thread (kein ORM im
Loop). Fehler: :class:`McpTimeout` bei Zeitüberschreitung, :class:`McpError`
bei Verbindungs- und Protokollfehlern (deutsche Meldung, ohne Zugangsdaten).
Werkzeug-Ausführungsfehler (MCP ``isError``) sind keine Exception, sondern
``ToolResult.is_error``.
"""

from __future__ import annotations

from multigpt.chat.providers.base import ToolSpec

from . import bridge, client
from .config import ServerConfig
from .errors import McpError
from .results import ToolResult


def _timeout(config: ServerConfig, timeout: float | None) -> float:
    return float(timeout) if timeout and timeout > 0 else config.timeout


def _to_spec(tool) -> ToolSpec:
    schema = tool.input_schema if isinstance(tool.input_schema, dict) else {}
    return ToolSpec(
        name=tool.name,
        description=tool.description or tool.title or "",
        parameters=schema or {"type": "object"},
    )


def list_tools(server, timeout: float | None = None, *, refresh: bool = False) -> list[ToolSpec]:
    """Werkzeuge des Servers (gecacht je Prozess und Server, 5 Minuten).

    Der Cache verfällt, sobald sich Befehl, URL oder Zugangsdaten ändern;
    ``refresh=True`` fragt immer neu.
    """
    config = ServerConfig.from_model(server)
    seconds = _timeout(config, timeout)
    tools = bridge.run(
        client.list_tools(config, seconds, refresh=refresh), seconds + bridge.GRACE_SECONDS
    )
    return [_to_spec(t) for t in tools]


def call_tool(
    server, name: str, arguments: dict | None = None, timeout: float | None = None
) -> ToolResult:
    """Führt ein Werkzeug aus. ``timeout`` None: ``server.timeout_seconds``.

    Prüft weder Rechte noch Rückfragepflicht – das ist Aufgabe der Aufrufer
    (``can()`` und :func:`requires_confirmation`).
    """
    if not isinstance(name, str) or not name:
        raise McpError("Kein Werkzeugname angegeben.")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise McpError("Werkzeugargumente müssen ein JSON-Objekt sein.")
    config = ServerConfig.from_model(server)
    seconds = _timeout(config, timeout)
    return bridge.run(
        client.call_tool(config, name, arguments, seconds), seconds + bridge.GRACE_SECONDS
    )


def requires_confirmation(server, tool_name: str) -> bool:
    """True, wenn das Werkzeug erst nach Bestätigung laufen darf.

    Das gilt für Werkzeuge in ``tools_requiring_confirmation`` und für alle, die
    der Verwalter noch nicht eingestuft hat (nicht in ``known_tools``, Plan 8g).
    Hängt nur von der DB ab, nie von Modell- oder Werkzeugausgaben.
    """
    confirm = server.tools_requiring_confirmation or []
    known = server.known_tools or []
    return tool_name in confirm or tool_name not in known


def check_connection(server, timeout: float | None = None) -> list[ToolSpec]:
    """Verbindet (falls nötig) und fragt die Werkzeugliste neu ab."""
    return list_tools(server, timeout, refresh=True)


def close_server(server_or_pk, timeout: float = 10.0) -> None:
    """Schließt die Sitzung dieses Prozesses zum Server (z. B. nach dem Löschen)."""
    pk = getattr(server_or_pk, "pk", server_or_pk)
    if pk is None:
        return
    bridge.run(client.close_server(pk), timeout)


def shutdown() -> None:
    bridge.shutdown()
