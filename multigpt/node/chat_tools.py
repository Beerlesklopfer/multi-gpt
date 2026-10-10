"""Eingebaute Chat-Werkzeuge für die Indexierung (M15): ``index_status`` (lesend,
ohne Rückfrage) sowie ``start_reindex``, ``start_scan`` und ``cancel_run``
(verändernd, immer mit Rückfrage im Chat wie MCP-Werkzeuge mit Rückfrage).

Damit lässt sich die Indexierung im Chat automatisieren („Lies den NAS-Ordner
neu ein und sag mir, wenn er fertig ist“). Rechte wie im Admin bzw. bei den
Sammlungen (``runs``): angeboten nur, wenn das Konto etwas davon darf, und vor
jedem Aufruf erneut geprüft. Die Rückfrage hängt nur an der Registrierung
(``confirm``), nie an Text von Modell oder Dokumenten – ein Dokument, das „brich
alle Läufe ab“ enthält, kann nichts ohne Bestätigung auslösen.
"""

from __future__ import annotations

import json

from multigpt.chat import tooling
from multigpt.chat.api_collections import writable_collection_ids
from multigpt.chat.providers.base import ToolSpec
from multigpt.rag import paths as source_paths
from multigpt.rag.models import DirectorySource

from . import runs

INDEX_STATUS = "index_status"
START_REINDEX = "start_reindex"
START_SCAN = "start_scan"
CANCEL_RUN = "cancel_run"
LABEL = "Indexierung"


def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1, default=str)


def _can_watch(user, ai_model=None) -> bool:
    return runs.is_admin(user) or bool(writable_collection_ids(user))


def _can_scan(user, ai_model=None) -> bool:
    return runs.is_admin(user) and source_paths.enabled() and DirectorySource.objects.exists()


def _always() -> bool:
    return True


def _args(arguments) -> dict:
    return arguments if isinstance(arguments, dict) else {}


def _guard(fn):
    def run(user, arguments, sources):
        try:
            return tooling.BuiltinResult(fn(user, _args(arguments)))
        except runs.RunError as exc:
            return tooling.BuiltinResult(str(exc), True)

    return run


@_guard
def _index_status(user, args) -> str:
    if args.get("run_id") is not None:
        return _json(runs.serialize_run(runs.get_run(user, args["run_id"])))
    items = runs.list_runs(user, open_only=bool(args.get("open_only")), limit=10)
    data: dict = {"runs": [runs.serialize_run(r) for r in items]}
    if runs.is_admin(user):
        queue = runs.queue_state()
        data["queue"] = {
            "due": queue.due,
            "waiting": queue.waiting,
            "running": queue.running,
            "failed": queue.failed,
            "worker_warning": queue.worker_warning,
        }
        data["sources"] = [runs.serialize_source(s) for s in runs.list_sources(user)]
    return _json(data)


@_guard
def _start_reindex(user, args) -> str:
    if args.get("document_id") is not None:
        run = runs.reindex_documents(user, [runs.find_document(user, args["document_id"])])
    elif args.get("collection") not in (None, ""):
        run = runs.reindex_collection(user, runs.find_collection(user, args["collection"]))
    else:
        raise runs.RunError("Bitte „collection“ oder „document_id“ angeben.")
    return f"Lauf #{run.pk} gestartet. Fortschritt mit index_status (run_id {run.pk})."


@_guard
def _start_scan(user, args) -> str:
    run, new = runs.start_scan(user, runs.find_source(user, args.get("source")))
    if new:
        return f"Lauf #{run.pk} gestartet. Fortschritt mit index_status (run_id {run.pk})."
    return f"Für diese Quelle läuft schon Lauf #{run.pk}."


@_guard
def _cancel_run(user, args) -> str:
    run = runs.get_run(user, args.get("run_id"))
    outcome = runs.cancel(user, run)
    tail = " Laufende Aufträge enden nach dem aktuellen Schritt." if outcome["marked"] else ""
    return f"Lauf #{run.pk} wird abgebrochen ({outcome['removed']} Aufträge entfernt).{tail}"


_TOOLS = (
    (
        INDEX_STATUS,
        "Zeigt Läufe der Dokument-Indexierung (Hochladen, Neu-Indexierung, Verzeichnis "
        "einlesen) mit Fortschritt; für Verwalter auch Warteschlange und Verzeichnisquellen. "
        "Mit „run_id“ genau ein Lauf, mit „open_only“ nur laufende.",
        {
            "run_id": {"type": "integer"},
            "open_only": {"type": "boolean"},
        },
        [],
        _can_watch,
        _index_status,
        None,
    ),
    (
        START_REINDEX,
        "Indexiert eine Sammlung („collection“, ID oder Name) oder ein Dokument "
        "(„document_id“) neu. Der Nutzer muss bestätigen.",
        {
            "collection": {"type": "string", "description": "ID oder Name der Sammlung."},
            "document_id": {"type": "integer"},
        },
        [],
        lambda user, ai_model=None: bool(writable_collection_ids(user)),
        _start_reindex,
        _always,
    ),
    (
        START_SCAN,
        "Liest eine Verzeichnisquelle („source“, ID aus index_status) jetzt ein. Nur für "
        "Verwalter; der Nutzer muss bestätigen.",
        {"source": {"type": "integer"}},
        ["source"],
        _can_scan,
        _start_scan,
        _always,
    ),
    (
        CANCEL_RUN,
        "Bricht einen laufenden Lauf ab („run_id“). Der Nutzer muss bestätigen.",
        {"run_id": {"type": "integer"}},
        ["run_id"],
        _can_watch,
        _cancel_run,
        _always,
    ),
)


def register() -> None:
    for name, description, properties, required, available, run, confirm in _TOOLS:
        parameters = {"type": "object", "properties": properties}
        if required:
            parameters["required"] = required
        tooling.register_builtin(
            tooling.BuiltinTool(
                name=name,
                label=LABEL,
                spec=ToolSpec(name=name, description=description, parameters=parameters),
                available=available,
                run=run,
                confirm=confirm,
            )
        )


register()
