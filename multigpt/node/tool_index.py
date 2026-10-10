"""MCP-Werkzeuge für Läufe und Verzeichnisquellen (M15, ``index.control``).

``run_status`` und ``list_runs`` stehen auch mit ``docs.write`` bereit, damit
ein Upload bzw. ``reindex`` bis zum Ende verfolgt werden kann. Abbrechen und
Verzeichnisquellen einlesen brauchen ``index.control``; Quellen zusätzlich die
Verwalterrolle (wie „Jetzt einlesen“ im Admin). Rechte je Lauf: ``runs``.
"""

from __future__ import annotations

from . import runs
from . import scopes as S
from .tools import NodeTool, arg_bool, arg_int, json_output, register, schema


def run_status(ctx, args):
    run = runs.get_run(ctx.user, arg_int(args, "run_id", required=True))
    data = runs.serialize_run(run)
    return json_output(data, data["progress"] + f" – {data['status_label']}.")


def list_runs(ctx, args):
    limit = arg_int(args, "limit", maximum=200) or 20
    items = runs.list_runs(ctx.user, open_only=arg_bool(args, "open_only"), limit=limit)
    return json_output(
        {"runs": [runs.serialize_run(r) for r in items]}, f"{len(items)} Lauf/Läufe."
    )


def cancel_run(ctx, args):
    run = runs.get_run(ctx.user, arg_int(args, "run_id", required=True))
    outcome = runs.cancel(ctx.user, run)
    note = " Laufende Aufträge enden nach dem aktuellen Schritt." if outcome["marked"] else ""
    return json_output({"run_id": run.pk, **outcome}, f"Lauf #{run.pk} wird abgebrochen.{note}")


def list_sources(ctx, args):
    items = runs.list_sources(ctx.user, allowed_ids=ctx.source_ids)
    return json_output(
        {"sources": [runs.serialize_source(s) for s in items]}, f"{len(items)} Quelle(n)."
    )


def start_scan(ctx, args):
    source = runs.find_source(ctx.user, args.get("source"), allowed_ids=ctx.source_ids)
    run, new = runs.start_scan(ctx.user, source)
    text = (
        f"Lauf #{run.pk} gestartet (Fortschritt mit run_status)."
        if new
        else f"Für diese Quelle läuft schon Lauf #{run.pk}."
    )
    return json_output({"new": new, **runs.serialize_run(run)}, text)


def _is_admin(ctx) -> bool:
    return runs.is_admin(ctx.user)


_RUN_ID = {"run_id": {"type": "integer", "description": "ID des Laufs."}}

register(
    NodeTool(
        name="run_status",
        title="Status eines Laufs",
        description="Fortschritt eines Laufs (Hochladen, Neu-Indexierung, Verzeichnis "
        "einlesen): Status, Zähler für Dateien und Dokumente, Fehler, Dauer. Für Abfragen "
        "in Abständen, bis „open“ false ist.",
        scopes=(S.INDEX_CONTROL, S.DOCS_WRITE),
        input_schema=schema(_RUN_ID, required=["run_id"]),
        handler=run_status,
        read_only=True,
    )
)
register(
    NodeTool(
        name="list_runs",
        title="Läufe auflisten",
        description="Die letzten Läufe, die das Konto sehen darf (Verwalter: alle), neueste "
        "zuerst; „open_only“ nur laufende.",
        scopes=(S.INDEX_CONTROL, S.DOCS_WRITE),
        input_schema=schema(
            {
                "open_only": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            }
        ),
        handler=list_runs,
        read_only=True,
    )
)
register(
    NodeTool(
        name="cancel_run",
        title="Lauf abbrechen",
        description="Bricht einen offenen Lauf ab: wartende Aufträge entfallen, laufende "
        "enden nach dem aktuellen Schritt; bereits Indexiertes bleibt.",
        scopes=(S.INDEX_CONTROL,),
        input_schema=schema(_RUN_ID, required=["run_id"]),
        handler=cancel_run,
        destructive=True,
    )
)
register(
    NodeTool(
        name="list_sources",
        title="Verzeichnisquellen",
        description="Verzeichnisquellen (Ordner auf dem Server, die in Sammlungen eingelesen "
        "werden) mit letztem Ergebnis und offenem Lauf. Nur für Verwalter.",
        scopes=(S.INDEX_CONTROL,),
        input_schema=schema(),
        handler=list_sources,
        read_only=True,
        available=_is_admin,
    )
)
register(
    NodeTool(
        name="start_scan",
        title="Verzeichnis einlesen",
        description="Liest eine Verzeichnisquelle jetzt ein (neue und geänderte Dateien "
        "indexieren, entfernte löschen). Läuft schon ein Lauf der Quelle, kommt dieser "
        "zurück. Nur für Verwalter.",
        scopes=(S.INDEX_CONTROL,),
        input_schema=schema(
            {"source": {"type": "integer", "description": "ID der Verzeichnisquelle."}},
            required=["source"],
        ),
        handler=start_scan,
        available=_is_admin,
    )
)
