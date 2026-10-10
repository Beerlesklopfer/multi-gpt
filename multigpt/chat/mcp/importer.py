"""MCP-Server aus einer JSON-Konfiguration übernehmen (Admin „Aus JSON importieren“).

Verstanden wird das verbreitete Format von Claude Desktop, Claude Code, Cursor,
n8n usw.::

    {"mcpServers": {"n8n-mcp": {"type": "http", "url": "https://…/mcp-server/http",
                                "headers": {"Authorization": "Bearer …"}}}}

ebenso ``{"servers": {…}}`` (VS Code) und die Server-Liste ohne Hülle
(``{"name": {…}}``). Je Eintrag:

- ``type`` ``http``/``streamable-http``/``streamableHttp`` → Transport HTTP;
  ``url`` (bzw. ``serverUrl``), ``headers`` → Zugangsdaten ``{"headers": …}``.
- ``type`` ``stdio`` → ``command`` + ``args`` (mit :func:`shlex.join`),
  ``env`` → Zugangsdaten ``{"env": …}``.
- Ohne ``type``: ``url`` heißt HTTP, ``command`` heißt stdio.
- ``sse`` (veralteter Transport) wird abgelehnt.

**Platzhalter** wie ``<YOUR_ACCESS_TOKEN_HERE>``, ``${TOKEN}`` oder „YOUR_…“
in Headern oder Umgebungsvariablen werden nicht gespeichert: Der Server wird
ohne diese Zugangsdaten und **deaktiviert** angelegt, damit der Verwalter den
echten Wert einträgt. Neue Server haben keine eingestuften Werkzeuge – alle
Werkzeuge laufen bis zur Einstufung nur mit Rückfrage.

Meldungen enthalten nie Header- oder Umgebungswerte.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import transaction

from ..models import McpServer
from .config import parse_credentials, split_command

MAX_SERVERS = 50
NAME_MAX = 100
HTTP_TYPES = {"http", "streamable-http", "streamable_http", "streamablehttp"}
_PLACEHOLDER = re.compile(
    r"<[^<>]*>|\$\{[^}]*\}|\bYOUR[_ -]|\bDEIN[_ -]|^\s*(changeme|xxx+)\s*$", re.I
)


@dataclass
class ImportEntry:
    """Ein Server aus der JSON-Konfiguration, geprüft, noch nicht gespeichert."""

    name: str
    transport: str = ""
    command: str = ""
    url: str = ""
    credentials: str = field(default="", repr=False)
    placeholders: list[str] = field(default_factory=list)  # nur Namen, nie Werte
    error: str = ""


@dataclass
class ImportResult:
    name: str
    action: str  # "created", "updated", "skipped", "error"
    message: str


def is_placeholder(value: str) -> bool:
    return bool(_PLACEHOLDER.search(value or ""))


def _str_map(value, label: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and k and isinstance(v, str | int | float) for k, v in value.items()
    ):
        raise ValueError(f"„{label}“ muss ein Objekt aus Texten sein.")
    return {k: str(v) for k, v in value.items()}


def _split_placeholders(values: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    kept = {k: v for k, v in values.items() if not is_placeholder(v)}
    return kept, sorted(k for k in values if k not in kept)


def _entry(name: str, spec) -> ImportEntry:
    entry = ImportEntry(name=name)
    if not name or len(name) > NAME_MAX:
        entry.error = f"Der Name muss 1 bis {NAME_MAX} Zeichen lang sein."
        return entry
    if not isinstance(spec, dict):
        entry.error = "Der Eintrag ist kein JSON-Objekt."
        return entry
    kind = str(spec.get("type") or spec.get("transport") or "").strip().lower()
    url = spec.get("url") or spec.get("serverUrl") or ""
    if not kind:
        kind = "http" if url else "stdio" if spec.get("command") else ""
    try:
        if kind in HTTP_TYPES:
            if not isinstance(url, str) or not url:
                raise ValueError("Für HTTP fehlt die URL.")
            try:
                URLValidator(schemes=["http", "https"])(url)
            except ValidationError:
                raise ValueError("Die URL ist ungültig.") from None
            headers, entry.placeholders = _split_placeholders(
                _str_map(spec.get("headers"), "headers")
            )
            entry.transport, entry.url = McpServer.Transport.HTTP, url
            entry.credentials = json.dumps({"headers": headers}) if headers else ""
        elif kind == "stdio":
            command = spec.get("command")
            args = spec.get("args") or []
            if not isinstance(command, str) or not command.strip():
                raise ValueError("Für stdio fehlt „command“.")
            if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                raise ValueError("„args“ muss eine Liste von Texten sein.")
            # „command“ darf schon Argumente enthalten („npx -y paket“).
            entry.command = shlex.join([*split_command(command), *args])
            env, entry.placeholders = _split_placeholders(_str_map(spec.get("env"), "env"))
            entry.transport = McpServer.Transport.STDIO
            entry.credentials = json.dumps({"env": env}) if env else ""
        elif kind == "sse":
            raise ValueError(
                "Der veraltete Transport SSE wird nicht unterstützt. Die meisten Server "
                "bieten auch Streamable HTTP an (oft unter …/mcp bzw. …/http)."
            )
        else:
            raise ValueError("Unbekannter Typ – erwartet „http“ oder „stdio“.")
        parse_credentials(entry.credentials, entry.transport)
    except ValueError as exc:
        entry.error = str(exc)
    return entry


def parse_config(text: str) -> list[ImportEntry]:
    """JSON-Text in geprüfte Einträge zerlegen; ``ValueError`` bei Formfehlern."""
    try:
        data = json.loads(text or "")
    except ValueError:
        raise ValueError("Kein gültiges JSON (Kommas und Anführungszeichen prüfen).") from None
    if not isinstance(data, dict):
        raise ValueError("Erwartet wird ein JSON-Objekt mit „mcpServers“.")
    servers = data.get("mcpServers", data.get("servers", data))
    if not isinstance(servers, dict) or not servers:
        raise ValueError("Keine MCP-Server gefunden (erwartet „mcpServers“: {…}).")
    if len(servers) > MAX_SERVERS:
        raise ValueError(f"Höchstens {MAX_SERVERS} Server auf einmal.")
    return [_entry(str(name).strip(), spec) for name, spec in servers.items()]


def apply_import(entries: list[ImportEntry], *, update_existing: bool) -> list[ImportResult]:
    """Einträge anlegen bzw. (auf Wunsch) gleichnamige aktualisieren.

    Beim Aktualisieren bleiben Einstufungen und Zeitlimit erhalten; Zugangsdaten
    mit Platzhaltern ersetzen gespeicherte nicht.
    """
    results = []
    for entry in entries:
        if entry.error:
            results.append(ImportResult(entry.name, "error", entry.error))
            continue
        hint = ""
        if entry.placeholders:
            hint = (
                " Platzhalter statt echter Werte bei: "
                + ", ".join(entry.placeholders)
                + " – bitte im Server eintragen."
            )
        with transaction.atomic():
            server = McpServer.objects.select_for_update().filter(name=entry.name).first()
            if server is None:
                McpServer.objects.create(
                    name=entry.name,
                    transport=entry.transport,
                    command=entry.command,
                    url=entry.url,
                    credentials=entry.credentials,
                    active=not entry.placeholders,
                )
                state = " Deaktiviert, bis die Zugangsdaten stimmen." if entry.placeholders else ""
                results.append(ImportResult(entry.name, "created", "angelegt." + state + hint))
                continue
            if not update_existing:
                results.append(
                    ImportResult(entry.name, "skipped", "existiert bereits, nicht verändert.")
                )
                continue
            server.transport = entry.transport
            server.command = entry.command
            server.url = entry.url
            if not entry.placeholders:
                server.credentials = entry.credentials
            server.save()
            results.append(ImportResult(entry.name, "updated", "aktualisiert." + hint))
    return results
