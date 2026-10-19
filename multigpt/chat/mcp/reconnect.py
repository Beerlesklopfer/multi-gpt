"""Gezieltes Neuverbinden per Benutzeraktion im Chat, ohne Werkzeugwiederholung."""

from django.db.models import F
from django.utils import timezone

from multigpt.accounts.permissions import Action, can
from multigpt.chat.models import McpServer

from .config import ServerConfig
from .errors import McpError
from .service import reconnect_with


def allowed_server(user, server) -> bool:
    if server is None or not server.active or not can(user, Action.USE_MCP_SERVER, server):
        return False
    if server.transport == McpServer.Transport.RUNNER:
        return server.owner_id == user.pk and can(user, Action.USE_RUNNER)
    return can(user, Action.ADMIN)


def allowed(user, tool_call) -> bool:
    return allowed_server(user, tool_call.server) and can(
        user, Action.WRITE, tool_call.message.conversation
    )


def reconnect(user, tool_call) -> dict:
    if not allowed(user, tool_call):
        raise PermissionError("Du darfst diese MCP-Verbindung nicht neu aufbauen.")
    return reconnect_server(user, tool_call.server, tool_call.message.conversation)


def reconnect_server(user, server, conversation=None) -> dict:
    if not allowed_server(user, server):
        raise PermissionError("Du darfst diese MCP-Verbindung nicht neu aufbauen.")
    session = None
    if server.transport == McpServer.Transport.RUNNER:
        from multigpt.runner.chat import _online_runner, pipe_for
        from multigpt.runner.models import ChatWorkspace, Runner, RunnerSession

        selection = (
            ChatWorkspace.objects.select_related("session__runner")
            .filter(
                conversation=conversation,
                session__owner=user,
                session__status=RunnerSession.Status.RUNNING,
                session__runner__status=Runner.Status.ONLINE,
            )
            .first()
        )
        if selection is None:
            # Sitzungen entstehen erst beim ersten Werkzeugaufruf: ohne Sitzung genügt ein
            # verbundener Runner, damit der Chat ihn wieder als online zeigt.
            if _online_runner(user) is None:
                raise McpError("Der Runner ist nicht verbunden.")
            now = timezone.now()
            McpServer.objects.filter(pk=server.pk).update(
                online=True, last_error="", last_checked=now, last_online=now
            )
            return {
                "server": server.name,
                "tools": 0,
                "message": "Runner verbunden; die Sitzung startet beim ersten Werkzeugaufruf.",
            }
        session = selection.session
    # Auch andere Gunicorn-Prozesse verwerfen ihre alte Verbindung beim nächsten Gebrauch.
    McpServer.objects.filter(pk=server.pk).update(connection_revision=F("connection_revision") + 1)
    server.refresh_from_db()
    if session is not None:
        config = ServerConfig.for_runner(
            server,
            runner_id=session.runner_id,
            session_id=str(session.uuid),
            pipe=pipe_for(session),
        )
    else:
        config = ServerConfig.from_model(server)
    tools = reconnect_with(config, timeout=min(config.timeout, 10))
    now = timezone.now()
    McpServer.objects.filter(pk=server.pk).update(
        online=True, last_error="", last_checked=now, last_online=now
    )
    return {"server": server.name, "tools": len(tools), "message": "MCP-Verbindung neu aufgebaut."}
