"""Werkzeuge des MCP-Servers (M15): Verzeichnis, Aufrufkontext, Ergebnisse.

Jedes Werkzeug gehört zu einem oder mehreren Scopes (``scopes``). Angeboten
(``tools/list``) und ausgeführt (``tools/call``) wird es nur, wenn der Key
einen davon wirksam hat (Key ∩ Rolle, ``keys.effective_scopes``) und
``available`` für das Konto zutrifft. Innerhalb des Werkzeugs gilt ``can()`` auf
jedes Objekt, wie in der Oberfläche.

Handler: ``handler(ctx, args) -> ToolOutput``. Ein Handler darf auch ein
Generator sein: Er yieldet ``Progress`` (Fortschritt, im SSE-Modus als
``notifications/progress``) und liefert das Ergebnis per ``return``.

Fehler im Werkzeug (``ToolFailure``, ``RunError``) werden als Ergebnis mit
``isError`` gemeldet, damit ein aufrufendes Modell sich korrigieren kann;
unbekannte Werkzeuge sind Protokollfehler (siehe ``protocol``).

Daten von außen (Prompts, Dateien, URLs) und alle Ergebnisse gelten als nicht
vertrauenswürdig: Quellmaterial wird wie im Chat markiert, nie als Anweisung
behandelt, und Rückfragepflichten hebt kein Inhalt auf.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from multigpt.chat import citations

from . import scopes as api_scopes


class ToolFailure(Exception):
    """Werkzeug lehnt ab bzw. scheitert; Text geht als Ergebnis mit isError zurück."""


@dataclass(frozen=True)
class Progress:
    message: str


@dataclass
class FileItem:
    """Datei im Ergebnis (EmbeddedResource mit base64)."""

    uri: str
    mime_type: str
    data: bytes
    name: str = ""


@dataclass
class ToolOutput:
    text: str
    data: dict | None = None
    is_error: bool = False
    files: list[FileItem] = field(default_factory=list)


@dataclass
class CallContext:
    key: Any
    user: Any
    scopes: frozenset[str]
    ip: str | None = None
    _collections: list[int] | None = None
    _sources: list[int] | None = None
    _loaded: bool = False

    def _load(self) -> None:
        if not self._loaded:
            ids = list(self.key.collections.values_list("pk", flat=True))
            self._collections = ids or None
            src = list(self.key.sources.values_list("pk", flat=True))
            self._sources = src or None
            self._loaded = True

    @property
    def collection_ids(self) -> list[int] | None:
        """Einschränkung des Keys auf Sammlungen; None = keine Einschränkung."""
        self._load()
        return self._collections

    @property
    def source_ids(self) -> list[int] | None:
        self._load()
        return self._sources

    def has(self, scope: str) -> bool:
        return scope in self.scopes


@dataclass(frozen=True)
class NodeTool:
    name: str
    title: str
    description: str
    scopes: tuple[str, ...]
    input_schema: dict
    handler: Callable
    read_only: bool = False
    destructive: bool = False
    open_world: bool = False
    available: Callable[[CallContext], bool] | None = None

    def offered(self, ctx: CallContext) -> bool:
        if not any(scope in ctx.scopes for scope in self.scopes):
            return False
        return self.available is None or bool(self.available(ctx))


_TOOLS: dict[str, NodeTool] = {}
_LOADED = False


def register(tool: NodeTool) -> None:
    _TOOLS[tool.name] = tool


def _load_modules() -> None:
    global _LOADED
    if _LOADED:
        return
    # Reihenfolge = Reihenfolge in tools/list.
    from . import tool_ask, tool_builtin, tool_docs, tool_files, tool_index  # noqa: F401

    _LOADED = True


def all_tools() -> list[NodeTool]:
    """Alle Werkzeuge, geordnet nach Scope (Reihenfolge in ``scopes.SCOPES``)."""
    _load_modules()
    order = {scope: n for n, scope in enumerate(api_scopes.ALL_SCOPES)}
    return sorted(_TOOLS.values(), key=lambda tool: order.get(tool.scopes[0], 99))


def get(name: str) -> NodeTool | None:
    _load_modules()
    return _TOOLS.get(name or "")


def offered(ctx: CallContext) -> list[NodeTool]:
    return [tool for tool in all_tools() if tool.offered(ctx)]


# --- Hilfen für Handler ---------------------------------------------------------------


def schema(properties: dict | None = None, required=()) -> dict:
    data: dict = {"type": "object", "properties": properties or {}}
    if required:
        data["required"] = list(required)
    return data


def as_json(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def json_output(data: dict, summary: str = "") -> ToolOutput:
    """Strukturiertes Ergebnis; der Text enthält dieselben Daten als JSON."""
    text = as_json(data)
    return ToolOutput(f"{summary}\n{text}" if summary else text, data)


def arg_text(args: dict, name: str, *, required: bool = False, max_len: int = 10_000) -> str:
    value = args.get(name)
    if value is None or value == "":
        if required:
            raise ToolFailure(f"Bitte das Argument „{name}“ angeben.")
        return ""
    if not isinstance(value, str):
        raise ToolFailure(f"Das Argument „{name}“ muss ein Text sein.")
    if len(value) > max_len:
        raise ToolFailure(f"Das Argument „{name}“ ist zu lang (höchstens {max_len} Zeichen).")
    return value


def arg_bool(args: dict, name: str, default: bool = False) -> bool:
    value = args.get(name, default)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ToolFailure(f"Das Argument „{name}“ muss true oder false sein.")
    return value


def arg_int(args: dict, name: str, *, required: bool = False, minimum: int = 1, maximum=None):
    value = args.get(name)
    if value is None:
        if required:
            raise ToolFailure(f"Bitte das Argument „{name}“ angeben (ganze Zahl).")
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ToolFailure(f"Das Argument „{name}“ muss eine ganze Zahl ab {minimum} sein.")
    if maximum is not None and value > maximum:
        raise ToolFailure(f"Das Argument „{name}“ darf höchstens {maximum} sein.")
    return value


class MemorySources:
    """Quellen ohne Chatnachricht (lesende Werkzeuge über die API).

    Gleiche Schnittstelle wie ``chat.sources.SourceCollector`` (``add``,
    ``prefs``, ``message.tool_state``), aber nichts wird gespeichert. Die
    Quellen gehen als ``sources`` ins strukturierte Ergebnis.
    """

    def __init__(self, user, collections: list[int] | None = None):
        state = {"collections": list(collections)} if collections else {}
        self.message = SimpleNamespace(pk=None, tool_state=state, conversation=None)
        self.prefs = citations.prefs_for(user)
        self.items: list[dict] = []
        self.changed = False

    def add(self, kind, title: str, url: str = "", *, chunk=None, **where) -> int:
        chunk_id = getattr(chunk, "pk", chunk)
        location = {k: v for k, v in where.items() if k != "biblio" and v not in (None, "")}
        entry = {"kind": str(kind), "title": (title or "")[:500], "url": url or ""}
        if chunk_id:
            entry["chunk_id"] = chunk_id
        entry.update(location)
        for n, existing in enumerate(self.items, start=1):
            if existing == entry:
                return n
        self.items.append(entry)
        self.changed = True
        return len(self.items)

    def numbered(self) -> list[dict]:
        return [{"n": n, **item} for n, item in enumerate(self.items, start=1)]
