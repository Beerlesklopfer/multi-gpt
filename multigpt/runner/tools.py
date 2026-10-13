"""Werkzeugkatalog des Runners (M14-06, Entscheidungen 8t vom 2026-10-13).

Der Server bietet dem Modell **seinen** Katalog an (Namen, deutsche
Beschreibungen, JSON-Schemas, Einstufung) – nicht die Werkzeugliste, die der
Agent meldet. Aufgerufen werden nur Namen aus diesem Katalog; ein
kompromittierter Runner kann so keine neuen Werkzeuge unterschieben.

**Arten** (Einstufung, nur aus diesem Modul, nie aus Text von Modell oder
Werkzeug):

* ``read`` – lesen und suchen (``read``, ``glob``, ``grep``) sowie die
  Aufgabenliste ``todo`` (bleibt auf dem Server): nie Rückfrage.
* ``edit`` – Dateien ändern (``write``, ``edit``): Rückfrage nur im Modus
  „fragen“.
* ``command`` – Befehle (``bash``, ``serve``): Rückfrage in „fragen“ und
  „Änderungen automatisch“.
* ``remote`` – ``ssh`` und ``telnet``: **immer** Rückfrage, auch in „alles
  automatisch“; nur Verwalter und Erwachsene, nie über API-Keys.

**Modi je Chat** (8t): ``ask`` (jede Änderung bestätigen), ``edits``
(Dateien ohne Rückfrage, Befehle mit), ``auto``. Vorgabe ohne Netz
``auto``, mit Netz ``edits`` (Befehle fragen). „Alles automatisch“ mit
Netz nur für Verwalter, sonst gilt ``edits``.
"""

from __future__ import annotations

from dataclasses import dataclass

from multigpt.chat.providers.base import ToolSpec

READ = "read"
EDIT = "edit"
COMMAND = "command"
REMOTE = "remote"
PUBLISH_KIND = "publish"  # Datei als Anhang in den Chat: Rückfrage nur in „fragen“

MODE_ASK = "ask"
MODE_AUTO_EDITS = "edits"
MODE_AUTO_ALL = "auto"
MODES = {
    MODE_ASK: "Fragen (jede Änderung bestätigen)",
    MODE_AUTO_EDITS: "Änderungen automatisch (Befehle fragen)",
    MODE_AUTO_ALL: "Alles automatisch",
}

# Grenzen der Argumente (der Agent prüft zusätzlich).
MAX_PATH = 1000
MAX_CONTENT = 5 * 1024 * 1024  # write/edit: ein Arbeitsstand hat höchstens 50 MB
MAX_COMMAND = 20_000
MAX_PATTERN = 2000
BASH_TIMEOUT_DEFAULT = 120
BASH_TIMEOUT_MAX = 600
MAX_TODO_ITEMS = 100
MAX_TODO_TEXT = 500
TODO_STATUSES = ("pending", "in_progress", "completed")

TODO = "todo"
PUBLISH = "publish"
SSH = "ssh"
TELNET = "telnet"
REMOTE_TOOLS = (SSH, TELNET)
# Werkzeuge, die (möglicherweise) Dateien im Arbeitsstand ändern; danach
# committet der Agent und lädt ein neues Bundle hoch (8r).
CHANGING_TOOLS = ("write", "edit", "bash")

MSG_ARGS = "Ungültige Argumente: {detail}"


@dataclass(frozen=True)
class RunnerTool:
    name: str
    kind: str
    description: str
    parameters: dict
    server_side: bool = False  # läuft auf dem Server (todo), nie im Container

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description, parameters=self.parameters)


def _schema(properties: dict, required=()) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


_PATH = {"type": "string", "description": "Pfad relativ zu /workspace (oder absolut darin)."}

_TOOLS = [
    RunnerTool(
        "read",
        READ,
        "Liest eine Textdatei aus dem Arbeitsstand (/workspace) mit Zeilennummern. Große "
        "Dateien abschnittsweise lesen: „offset“ (erste Zeile, ab 1) und „limit“ (Zeilen).",
        _schema(
            {
                "path": _PATH,
                "offset": {"type": "integer", "minimum": 1, "description": "Erste Zeile (ab 1)."},
                "limit": {"type": "integer", "minimum": 1, "description": "Anzahl Zeilen."},
            },
            ["path"],
        ),
    ),
    RunnerTool(
        "write",
        EDIT,
        "Legt eine Datei im Arbeitsstand an oder überschreibt sie vollständig. Für kleine "
        "Änderungen an bestehenden Dateien „edit“ verwenden.",
        _schema(
            {"path": _PATH, "content": {"type": "string", "description": "Neuer Inhalt."}},
            ["path", "content"],
        ),
    ),
    RunnerTool(
        "edit",
        EDIT,
        "Ersetzt in einer Datei eine Textstelle: „old_string“ muss genau einmal vorkommen "
        "(sonst mehr Kontext angeben oder „replace_all“ setzen).",
        _schema(
            {
                "path": _PATH,
                "old_string": {"type": "string", "description": "Bisheriger Text (wörtlich)."},
                "new_string": {"type": "string", "description": "Neuer Text."},
                "replace_all": {
                    "type": "boolean",
                    "description": "Alle Vorkommen ersetzen (Standard: nein).",
                },
            },
            ["path", "old_string", "new_string"],
        ),
    ),
    RunnerTool(
        "glob",
        READ,
        "Findet Dateien nach Muster, z. B. „**/*.py“ (neueste zuerst).",
        _schema(
            {
                "pattern": {"type": "string", "description": "Glob-Muster."},
                "path": {
                    "type": "string",
                    "description": "Startverzeichnis (Standard: /workspace).",
                },
            },
            ["pattern"],
        ),
    ),
    RunnerTool(
        "grep",
        READ,
        "Durchsucht Dateien im Arbeitsstand mit einem regulären Ausdruck (Volltext).",
        _schema(
            {
                "pattern": {"type": "string", "description": "Regulärer Ausdruck."},
                "path": {"type": "string", "description": "Datei oder Verzeichnis."},
                "glob": {"type": "string", "description": "Nur Dateien nach Muster, z. B. *.js."},
                "ignore_case": {"type": "boolean", "description": "Groß/klein ignorieren."},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
            ["pattern"],
        ),
    ),
    RunnerTool(
        "bash",
        COMMAND,
        "Führt einen Shell-Befehl im Container aus (Arbeitsverzeichnis /workspace, Pseudo-"
        "Terminal, Zeitlimit, Ausgabe gekürzt). Netz ist normalerweise aus.",
        _schema(
            {
                "command": {"type": "string", "description": "Befehl für bash."},
                "timeout_s": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": BASH_TIMEOUT_MAX,
                    "description": f"Zeitlimit in Sekunden (Standard {BASH_TIMEOUT_DEFAULT}).",
                },
            },
            ["command"],
        ),
    ),
    RunnerTool(
        "serve",
        COMMAND,
        "Startet die Vorschau: ein statischer HTTP-Server liefert ein Verzeichnis des "
        "Arbeitsstands aus (HTML, JS, CSS, Bilder; keine serverseitige Ausführung). Liefert "
        "die Vorschau-Adresse für den Nutzer.",
        _schema({"path": {"type": "string", "description": "Verzeichnis (Standard: /workspace)."}}),
    ),
    RunnerTool(
        PUBLISH,
        PUBLISH_KIND,
        "Legt eine Datei aus /scratch (Ablage der Sitzung) oder /workspace als Anhang in den "
        "Chat, damit der Nutzer sie herunterladen bzw. ansehen kann (Bilder, PDF, Archive, "
        "Text). Nur für Ergebnisse, die der Nutzer haben soll; Größe begrenzt.",
        _schema(
            {
                "path": {
                    "type": "string",
                    "description": "Datei unter /scratch oder /workspace.",
                },
                "name": {
                    "type": "string",
                    "description": "Anzeigename im Chat (Standard: Dateiname).",
                },
            },
            ["path"],
        ),
    ),
    RunnerTool(
        TODO,
        READ,
        "Pflegt die Aufgabenliste dieser Arbeitssitzung (im Chat sichtbar). Immer die "
        "vollständige Liste schicken; genau eine Aufgabe „in_progress“.",
        _schema(
            {
                "items": {
                    "type": "array",
                    "maxItems": MAX_TODO_ITEMS,
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "status": {"type": "string", "enum": list(TODO_STATUSES)},
                        },
                        "required": ["content", "status"],
                        "additionalProperties": False,
                    },
                }
            },
            ["items"],
        ),
        server_side=True,
    ),
    RunnerTool(
        SSH,
        REMOTE,
        "Führt einen Befehl per SSH auf einem freigegebenen Gerät aus. „host“ ist der Name "
        "aus der Liste der freigegebenen Hosts. Jeder Aufruf wird vom Nutzer bestätigt.",
        _schema(
            {
                "host": {"type": "string", "description": "Name des freigegebenen Hosts."},
                "command": {"type": "string", "description": "Befehl auf dem Gerät."},
            },
            ["host", "command"],
        ),
    ),
    RunnerTool(
        TELNET,
        REMOTE,
        "Sendet eine Eingabe per Telnet an ein freigegebenes (altes) Gerät. Telnet überträgt "
        "im Klartext. Jeder Aufruf wird vom Nutzer bestätigt.",
        _schema(
            {
                "host": {"type": "string", "description": "Name des freigegebenen Hosts."},
                "input": {"type": "string", "description": "Eingabe (Zeile bzw. Befehl)."},
            },
            ["host", "input"],
        ),
    ),
]

CATALOG: dict[str, RunnerTool] = {tool.name: tool for tool in _TOOLS}


def get(name: str | None) -> RunnerTool | None:
    return CATALOG.get(name or "")


def specs(*, remote: bool = False, hosts=()) -> list[ToolSpec]:
    """Werkzeuge für das Modell; ``ssh``/``telnet`` nur mit ``remote`` und nur,
    wenn es freigegebene Hosts des jeweiligen Protokolls gibt (``hosts``:
    Namen je Protokoll, ``{"ssh": [...], "telnet": [...]}``)."""
    out = []
    for tool in _TOOLS:
        if tool.kind == REMOTE:
            names = list((dict(hosts or {})).get(tool.name) or [])
            if not remote or not names:
                continue
            params = {**tool.parameters, "properties": dict(tool.parameters["properties"])}
            params["properties"]["host"] = {
                **params["properties"]["host"],
                "enum": sorted(names),
            }
            out.append(ToolSpec(tool.name, tool.description, params))
            continue
        out.append(tool.spec)
    return out


# --- Modi und Rückfrage --------------------------------------------------------------


def default_mode(network: bool) -> str:
    """Vorgabe (8t): ohne Netz alles automatisch, mit Netz fragen Befehle."""
    return MODE_AUTO_EDITS if network else MODE_AUTO_ALL


def effective_mode(mode: str | None, *, network: bool, is_admin: bool) -> str:
    """Wirksamer Modus: leer bzw. unbekannt -> Vorgabe; „alles automatisch“ mit
    Netz nur für Verwalter."""
    if mode not in MODES:
        mode = default_mode(network)
    if mode == MODE_AUTO_ALL and network and not is_admin:
        return MODE_AUTO_EDITS
    return mode


def needs_confirmation(name: str, mode: str) -> bool:
    """Rückfrage vor diesem Aufruf? Unbekannte Werkzeuge immer (laufen ohnehin nicht)."""
    tool = CATALOG.get(name)
    if tool is None or tool.kind == REMOTE:
        return True
    if tool.kind == READ:
        return False
    if tool.kind in (EDIT, PUBLISH_KIND):
        return mode == MODE_ASK
    return mode in (MODE_ASK, MODE_AUTO_EDITS)  # COMMAND


# --- Argumente ---------------------------------------------------------------------------


class ArgumentError(ValueError):
    """Ungültige Werkzeugargumente; Text geht als Fehlerergebnis an das Modell."""


def _text(args: dict, key: str, *, required=False, limit=MAX_PATH, allow_empty=False):
    value = args.get(key)
    if value is None:
        if required:
            raise ArgumentError(MSG_ARGS.format(detail=f"„{key}“ fehlt."))
        return None
    if not isinstance(value, str):
        raise ArgumentError(MSG_ARGS.format(detail=f"„{key}“ muss Text sein."))
    if "\x00" in value:
        raise ArgumentError(MSG_ARGS.format(detail=f"„{key}“ enthält ein Nullzeichen."))
    if len(value) > limit:
        raise ArgumentError(MSG_ARGS.format(detail=f"„{key}“ ist zu lang."))
    if not value and not allow_empty and required:
        raise ArgumentError(MSG_ARGS.format(detail=f"„{key}“ ist leer."))
    return value


def _int(args: dict, key: str, low: int, high: int):
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ArgumentError(MSG_ARGS.format(detail=f"„{key}“ muss eine ganze Zahl sein."))
    return max(low, min(high, value))


def _bool(args: dict, key: str):
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ArgumentError(MSG_ARGS.format(detail=f"„{key}“ muss true oder false sein."))
    return value


def clean_arguments(name: str, args) -> dict:
    """Prüft und normalisiert die Argumente (nur bekannte Schlüssel).

    ``ArgumentError`` bei Formfehlern. Pfade prüft der Agent zusätzlich gegen
    ``/workspace`` (``mgpt-fs``).
    """
    if not isinstance(args, dict):
        raise ArgumentError(MSG_ARGS.format(detail="Es wird ein JSON-Objekt erwartet."))
    out: dict = {}

    def put(key, value):
        if value is not None:
            out[key] = value

    if name == "read":
        put("path", _text(args, "path", required=True))
        put("offset", _int(args, "offset", 1, 10**9))
        put("limit", _int(args, "limit", 1, 100_000))
    elif name == "write":
        put("path", _text(args, "path", required=True))
        put("content", _text(args, "content", required=True, limit=MAX_CONTENT, allow_empty=True))
    elif name == "edit":
        put("path", _text(args, "path", required=True))
        old = _text(args, "old_string", required=True, limit=MAX_CONTENT)
        new = _text(args, "new_string", required=True, limit=MAX_CONTENT, allow_empty=True)
        if old == new:
            raise ArgumentError(
                MSG_ARGS.format(detail="„old_string“ und „new_string“ sind gleich.")
            )
        put("old_string", old)
        put("new_string", new)
        put("replace_all", _bool(args, "replace_all"))
    elif name == "glob":
        put("pattern", _text(args, "pattern", required=True, limit=MAX_PATTERN))
        put("path", _text(args, "path"))
    elif name == "grep":
        put("pattern", _text(args, "pattern", required=True, limit=MAX_PATTERN))
        put("path", _text(args, "path"))
        put("glob", _text(args, "glob", limit=MAX_PATTERN))
        put("ignore_case", _bool(args, "ignore_case"))
        put("max_results", _int(args, "max_results", 1, 1000))
    elif name == "bash":
        put("command", _text(args, "command", required=True, limit=MAX_COMMAND))
        timeout = _int(args, "timeout_s", 1, BASH_TIMEOUT_MAX)
        out["timeout_s"] = timeout or BASH_TIMEOUT_DEFAULT
    elif name == "serve":
        put("path", _text(args, "path"))
    elif name == PUBLISH:
        put("path", _text(args, "path", required=True))
        put("name", _text(args, "name", limit=200))
    elif name == TODO:
        out["items"] = clean_todo(args.get("items"))
    elif name == SSH:
        put("host", _text(args, "host", required=True, limit=100))
        put("command", _text(args, "command", required=True, limit=MAX_COMMAND))
    elif name == TELNET:
        put("host", _text(args, "host", required=True, limit=100))
        put("input", _text(args, "input", required=True, limit=MAX_COMMAND, allow_empty=True))
    else:
        raise ArgumentError("Unbekanntes Werkzeug.")
    return out


def clean_todo(items) -> list[dict]:
    if not isinstance(items, list) or len(items) > MAX_TODO_ITEMS:
        raise ArgumentError(
            MSG_ARGS.format(detail=f"„items“ muss eine Liste (höchstens {MAX_TODO_ITEMS}) sein.")
        )
    out = []
    for item in items:
        if not isinstance(item, dict):
            raise ArgumentError(MSG_ARGS.format(detail="Jede Aufgabe ist ein Objekt."))
        content = item.get("content")
        status = item.get("status")
        if not isinstance(content, str) or not content.strip():
            raise ArgumentError(MSG_ARGS.format(detail="Jede Aufgabe braucht „content“."))
        if status not in TODO_STATUSES:
            raise ArgumentError(
                MSG_ARGS.format(detail=f"„status“ muss {', '.join(TODO_STATUSES)} sein.")
            )
        out.append({"content": content.strip()[:MAX_TODO_TEXT], "status": status})
    return out


def todo_text(items: list[dict]) -> str:
    """Ergebnis von ``todo`` für das Modell."""
    if not items:
        return "Aufgabenliste geleert."
    marks = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
    lines = [f"{marks[i['status']]} {i['content']}" for i in items]
    return "Aufgabenliste aktualisiert:\n" + "\n".join(lines)
