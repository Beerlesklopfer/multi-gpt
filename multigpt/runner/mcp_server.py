"""MCP-Server-Eintrag „Runner“ je Konto (M14, Transport ``runner``).

Für die Chat-Schleife ist ein gekoppelter Runner ein MCP-Server wie jeder
andere; Einstufung, Rückfrage und Freigabe je Modell gelten unverändert. Der
Eintrag gehört genau einem Konto (``McpServer.owner``), nur dieses nutzt ihn
(``permissions._can_use_mcp_server``). Angelegt wird er bei der ersten Kopplung.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction

from multigpt.chat.models import McpServer

TIMEOUT_SECONDS = 600
MAX_NAME = 100


def server_name(user) -> str:
    return f"Runner ({user.get_username()})"[:MAX_NAME]


def _known_tools() -> list[str]:
    try:
        from .tools import CATALOG
    except ImportError:  # Werkzeugkatalog fehlt (nur in Teilinstallationen denkbar)
        return []
    return list(CATALOG)


def ensure_mcp_server(user) -> McpServer:
    """Eintrag des Kontos liefern bzw. anlegen (idempotent)."""
    server = McpServer.objects.filter(transport=McpServer.Transport.RUNNER, owner=user).first()
    if server is not None:
        return server
    name = server_name(user)
    for attempt in range(5):
        candidate = name if attempt == 0 else f"{name[: MAX_NAME - 4]} {attempt + 1}"
        try:
            with transaction.atomic():
                return McpServer.objects.create(
                    name=candidate,
                    transport=McpServer.Transport.RUNNER,
                    owner=user,
                    timeout_seconds=TIMEOUT_SECONDS,
                    known_tools=_known_tools(),
                )
        except IntegrityError:
            existing = McpServer.objects.filter(
                transport=McpServer.Transport.RUNNER, owner=user
            ).first()
            if existing is not None:
                return existing
    raise RuntimeError("Kein freier Name für den MCP-Server des Runners.")
