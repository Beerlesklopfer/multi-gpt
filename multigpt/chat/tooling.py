"""Werkzeuge für die Werkzeugschleife (M4a-04): Auswahl der MCP-Server,
Werkzeugnamen mit Server-Präfix und Ausführung eines einzelnen Aufrufs.

Die Schleife selbst (Runden, Rückfrage, Speicherung) steht in ``services``.

**Werkzeugnamen:** Jedes Werkzeug bekommt den Namen ``<Server>__<Werkzeug>``,
auch wenn nur ein Server eingeschaltet ist – so bleiben Namen im Verlauf gültig,
wenn später ein zweiter Server dazukommt. Erlaubt sind nur ``[a-zA-Z0-9_-]``
und höchstens 64 Zeichen (OpenAI; Anthropic erlaubt 128, Gemini 64). Passt der
Name nicht, enthält er andere Zeichen oder ist er schon vergeben, wird er
gekürzt und bekommt einen kurzen Hash aus Server-ID und Originalnamen. Die
Abbildung Name -> (Server, Originalname) wird je Runde in der Nachricht
gespeichert; ausgeführt wird immer nach dieser gespeicherten Zuordnung.

**Sicherheit (Plan 9):** Ob ein Aufruf eine Rückfrage braucht, entscheidet nur
``mcp.requires_confirmation`` anhand der DB – nie Text von Modell oder Werkzeug.
Rechte (``can(USE_MCP_SERVER)``) werden vor dem Anbieten und vor jedem Aufruf
geprüft.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from django.core.files.base import ContentFile

from multigpt.accounts.permissions import Action, can

from . import mcp
from .models import Attachment, McpServer, ToolCall
from .providers.base import ToolSpec

logger = logging.getLogger(__name__)

MAX_NAME_LENGTH = 64
SERVER_PREFIX_LENGTH = 24
SEPARATOR = "__"
# Ergebnistext für Modell und Protokoll; das SSE-Event kürzt stärker.
MAX_RESULT_CHARS = 50_000
EVENT_RESULT_CHARS = 4_000
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024

MSG_REJECTED = "Vom Nutzer abgelehnt."
MSG_NOT_ALLOWED = "Dieses Werkzeug steht nicht zur Verfügung."
MSG_UNKNOWN = "Unbekanntes Werkzeug."
MSG_TIMEOUT = "Zeitüberschreitung: Das Werkzeug hat nicht innerhalb von {seconds} s geantwortet."
MSG_FAILED = "Das Werkzeug konnte nicht ausgeführt werden."
MSG_NOT_CONFIRMED = "Nicht ausgeführt: Der Aufruf wurde nicht bestätigt."
MSG_ROUND_LIMIT = "Nicht ausgeführt: Die Höchstzahl an Werkzeugrunden ist erreicht."
MSG_UNANSWERED = "Nicht ausgeführt: Die Rückfrage wurde nicht beantwortet."
MSG_ABORTED = "Abgebrochen."
MSG_EMPTY = "(Das Werkzeug hat kein Ergebnis geliefert.)"

_INVALID = re.compile(r"[^a-zA-Z0-9_-]+")


# --- Server und Werkzeuge -----------------------------------------------------


def available_servers(user) -> list[McpServer]:
    """Aktive MCP-Server, die ``user`` nutzen darf (Plan 8g)."""
    return [s for s in McpServer.objects.filter(active=True) if can(user, Action.USE_MCP_SERVER, s)]


def enabled_server_ids(user, requested) -> list[int]:
    """Eingeschaltete Server für eine Antwort: ``requested`` None -> alle
    erlaubten (Voreinstellung nach Rolle), sonst die erlaubten davon."""
    allowed = [s.pk for s in available_servers(user)]
    if requested is None:
        return allowed
    wanted = set(requested)
    return [pk for pk in allowed if pk in wanted]


def _slug(text: str) -> str:
    return _INVALID.sub("_", text or "").strip("_") or "x"


def _hashed(server_pk: int, tool: str, base: str) -> str:
    digest = hashlib.sha1(f"{server_pk}:{tool}".encode()).hexdigest()[:8]
    return f"{base[: MAX_NAME_LENGTH - 9]}_{digest}"


def tool_name(server: McpServer, tool: str, taken=()) -> str:
    """Name für das Modell: ``<Server>__<Werkzeug>``, eindeutig und gültig."""
    base = f"{_slug(server.name)[:SERVER_PREFIX_LENGTH]}{SEPARATOR}{_slug(tool)}"
    clean = _slug(tool) == tool
    if clean and len(base) <= MAX_NAME_LENGTH and base not in taken:
        return base
    return _hashed(server.pk, tool, base)


@dataclass(frozen=True)
class Binding:
    """Ein angebotenes Werkzeug: Name für das Modell -> Server und Originalname.

    Eingebaute Werkzeuge (``builtin`` gesetzt, z. B. ``web_search``) haben
    keinen Server; sie laufen ohne Rückfrage, Rechte prüft die Schleife.
    """

    name: str
    server_id: int | None
    server_name: str
    tool: str
    spec: ToolSpec
    builtin: str | None = None


# --- Eingebaute Werkzeuge und fester Kontext (M7-06, M8-04) ---------------------------
#
# Eingebaute Werkzeuge laufen ohne MCP-Server und ohne Rückfrage (sie lesen nur);
# ``available`` wird vor dem Anbieten und vor jedem Aufruf erneut geprüft.
# Namen enthalten nie "__" und können so nicht mit MCP-Namen zusammenfallen.
# Die Module registrieren sich beim Import (``services`` importiert sie).

MSG_BAD_QUERY = "Bitte einen Suchbegriff im Argument „query“ angeben."


@dataclass(frozen=True)
class BuiltinResult:
    """Ergebnis eines eingebauten Werkzeugs (Text für Modell und Anzeige)."""

    text: str
    is_error: bool = False


@dataclass(frozen=True)
class BuiltinTool:
    """``available(user, ai_model) -> bool``;
    ``run(user, arguments: dict, sources: SourceCollector) -> BuiltinResult``."""

    name: str
    label: str
    spec: ToolSpec
    available: Callable[[Any, Any], bool]
    run: Callable[[Any, dict, Any], BuiltinResult]


@dataclass(frozen=True)
class ContextResult:
    """Ergebnis eines festen Kontext-Ablaufs (z. B. Websuche vor dem Anbieteraufruf).

    ``entries``: nummerierte ``sources.ContextEntry``; ``notes``: Hinweise an das
    Modell (z. B. Suche fehlgeschlagen); ``notice``: Hinweis für den Nutzer.
    """

    entries: list = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    notice: str = ""


_BUILTINS: dict[str, BuiltinTool] = {}
# Schlüssel -> (Reihenfolge, Generator-Funktion(turn, sources) -> return ContextResult)
_CONTEXT_PROVIDERS: dict[str, tuple[int, Callable]] = {}


def register_builtin(tool: BuiltinTool) -> None:
    if SEPARATOR in tool.name or _slug(tool.name) != tool.name:
        raise ValueError("Ungültiger Name für ein eingebautes Werkzeug.")
    _BUILTINS[tool.name] = tool


def get_builtin(name: str | None) -> BuiltinTool | None:
    return _BUILTINS.get(name or "")


def register_context_provider(key: str, provider: Callable, *, order: int = 100) -> None:
    """``provider(turn, sources)`` ist ein Generator: yieldet SSE-Events
    (z. B. ``status``) und liefert per ``return`` ein ``ContextResult``.
    ``order``: kleinere Werte laufen zuerst (Dokumente 10 vor Web 100) – die
    Reihenfolge bestimmt die Quellennummern, unabhängig von der Importfolge."""
    _CONTEXT_PROVIDERS[key] = (order, provider)


def context_providers() -> list[tuple[str, Callable]]:
    ordered = sorted(_CONTEXT_PROVIDERS.items(), key=lambda item: item[1][0])
    return [(key, provider) for key, (_, provider) in ordered]


def builtin_bindings(user, ai_model) -> dict[str, Binding]:
    """Eingebaute Werkzeuge für werkzeugfähige Modelle, nach Recht und Einstellung."""
    if not ai_model.supports_tools:
        return {}
    return {
        tool.name: Binding(
            name=tool.name,
            server_id=None,
            server_name=tool.label,
            tool=tool.name,
            spec=tool.spec,
            builtin=tool.name,
        )
        for tool in _BUILTINS.values()
        if tool.available(user, ai_model)
    }


def collect_tools(user, server_ids) -> dict[str, Binding]:
    """Werkzeuge der eingeschalteten, erlaubten Server. Nicht erreichbare Server
    werden übersprungen (nur ID und Fehlerart im Log)."""
    bindings: dict[str, Binding] = {}
    servers = McpServer.objects.filter(pk__in=list(server_ids or []), active=True).order_by(
        "name", "pk"
    )
    for server in servers:
        if not can(user, Action.USE_MCP_SERVER, server):
            continue
        try:
            specs = mcp.list_tools(server)
        except mcp.McpError as exc:
            logger.warning("MCP-Server %s: Werkzeugliste fehlt (%s)", server.pk, type(exc).__name__)
            continue
        for spec in sorted(specs, key=lambda s: s.name):
            name = tool_name(server, spec.name, bindings)
            bindings[name] = Binding(
                name=name,
                server_id=server.pk,
                server_name=server.name,
                tool=spec.name,
                spec=ToolSpec(name=name, description=spec.description, parameters=spec.parameters),
            )
    return bindings


def resolve_server(user, call: dict, enabled_ids) -> tuple[McpServer | None, str]:
    """Server eines gespeicherten Aufrufs laden und Rechte prüfen.

    Ergebnis: (Server, "") oder (None, Fehlertext für das Modell).
    """
    if not call.get("server_id") or not call.get("tool"):
        return None, MSG_UNKNOWN
    if call["server_id"] not in set(enabled_ids or []):
        return None, MSG_NOT_ALLOWED
    server = McpServer.objects.filter(pk=call["server_id"]).first()
    if server is None or not can(user, Action.USE_MCP_SERVER, server):
        return None, MSG_NOT_ALLOWED
    return server, ""


def needs_confirmation(server: McpServer, tool: str) -> bool:
    """Nur aus der DB (Plan 9): Liste des Verwalters bzw. nicht eingestuft."""
    return mcp.requires_confirmation(server, tool)


# --- Ausführung ------------------------------------------------------------------


@dataclass
class Outcome:
    """Ergebnis eines Aufrufs für Modell und Protokoll."""

    status: str
    text: str
    is_error: bool
    attachment_ids: list[int] = field(default_factory=list)


def _attachment(message, tool_call: ToolCall, item, kind: str) -> Attachment | None:
    if not item.data or len(item.data) > MAX_ATTACHMENT_BYTES:
        return None
    ext = mimetypes.guess_extension(item.mime_type or "") or ".bin"
    attachment = Attachment(message=message, tool_call=tool_call, kind=kind)
    attachment.file.save(f"tool{ext}", ContentFile(item.data), save=True)
    return attachment


def _store_files(message, tool_call: ToolCall, result) -> list[int]:
    ids = []
    items = [(f, Attachment.Kind.IMAGE) for f in result.images]
    for item in result.files:
        kind = (
            Attachment.Kind.AUDIO
            if (item.mime_type or "").startswith("audio/")
            else Attachment.Kind.FILE
        )
        items.append((item, kind))
    for item, kind in items:
        try:
            attachment = _attachment(message, tool_call, item, kind)
        except Exception as exc:
            logger.error("Anhang zu Werkzeugaufruf %s: %s", tool_call.pk, type(exc).__name__)
            continue
        if attachment is not None:
            ids.append(attachment.pk)
    return ids


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[… gekürzt]"


def execute(server: McpServer, tool_call: ToolCall, message) -> Outcome:
    """Führt den Aufruf über MCP aus (Zeitlimit des Servers) und protokolliert
    Ergebnis, Status und Dauer im ``ToolCall``. Rechte und Rückfrage prüft der
    Aufrufer vorher."""
    started = time.monotonic()
    try:
        result = mcp.call_tool(server, tool_call.tool, tool_call.arguments)
    except mcp.McpTimeout:
        outcome = Outcome(
            ToolCall.Status.TIMEOUT, MSG_TIMEOUT.format(seconds=server.timeout_seconds), True
        )
    except mcp.McpError as exc:
        outcome = Outcome(ToolCall.Status.ERROR, str(exc) or MSG_FAILED, True)
    except Exception as exc:
        logger.error("Werkzeugaufruf %s fehlgeschlagen: %s", tool_call.pk, type(exc).__name__)
        outcome = Outcome(ToolCall.Status.ERROR, MSG_FAILED, True)
    else:
        ids = _store_files(message, tool_call, result)
        text = result.text or ""
        if ids:
            note = f"[{len(ids)} Datei(en) als Anhang gespeichert und dem Nutzer angezeigt.]"
            text = f"{text}\n\n{note}" if text else note
        status = ToolCall.Status.ERROR if result.is_error else ToolCall.Status.OK
        outcome = Outcome(status, text or MSG_EMPTY, bool(result.is_error), ids)
    elapsed = record(tool_call, outcome, started)
    logger.info(
        "Werkzeugaufruf %s (Server %s): %s in %d ms",
        tool_call.pk,
        server.pk,
        outcome.status,
        int(elapsed * 1000),
    )
    return outcome


def record(tool_call: ToolCall, outcome: Outcome, started: float) -> float:
    """Ergebnis (gekürzt), Status und Dauer im ``ToolCall`` speichern; liefert die Dauer."""
    outcome.text = _clip(outcome.text, MAX_RESULT_CHARS)
    elapsed = time.monotonic() - started
    tool_call.status = outcome.status
    tool_call.result = {"text": outcome.text, "is_error": outcome.is_error}
    tool_call.duration = timedelta(seconds=elapsed)
    tool_call.save(update_fields=["status", "result", "duration"])
    return elapsed


def close_call(tool_call: ToolCall, status: str, text: str) -> None:
    """Aufruf ohne Ausführung abschließen (abgelehnt, verboten, Limit, …)."""
    tool_call.status = status
    tool_call.result = {"text": text, "is_error": True}
    tool_call.save(update_fields=["status", "result"])


# --- Events -------------------------------------------------------------------------


def server_label(tool_call: ToolCall) -> str:
    """Anzeigename des Servers; eingebaute Werkzeuge (ohne Server) mit eigenem Namen."""
    if tool_call.server_id:
        return tool_call.server.name
    builtin = _BUILTINS.get(tool_call.tool)
    return builtin.label if builtin else ""


def call_event(tool_call: ToolCall, server_name: str = "") -> dict:
    return {
        "id": tool_call.pk,
        "server": server_name or server_label(tool_call),
        "tool": tool_call.tool,
        "arguments": tool_call.arguments,
        "status": tool_call.status,
    }


def result_event(tool_call: ToolCall, attachment_ids=None) -> dict:
    if attachment_ids is None:
        attachment_ids = list(tool_call.attachments.values_list("pk", flat=True))
    return {
        "id": tool_call.pk,
        "status": tool_call.status,
        "result": _clip(tool_call.result_text, EVENT_RESULT_CHARS),
        "duration_ms": tool_call.duration_ms or 0,
        "attachment_ids": attachment_ids,
    }
