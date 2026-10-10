"""Rechte (Scopes) eines API-Keys (M15, Knoten).

Ohne Django-Modelle importierbar: ``accounts.models`` nutzt die Liste für das
Rollenfeld ``Role.api_scopes`` (Höchstmenge je Rolle).

Wirksam ist immer die Schnittmenge aus

1. den Scopes des Keys (``ApiKey.scopes``),
2. der Höchstmenge der Rolle (``Role.api_scopes``, nur mit ``Role.can_use_api``),
3. ``can()`` auf das konkrete Objekt bzw. die Funktion (Modell, Sammlung,
   Websuche, Upload, Verwalter bei Verzeichnisquellen …).

Alle drei werden bei jedem Aufruf neu aus der Datenbank gelesen. Verwalterrechte
(Konten, Rollen, Anbieter …) gibt es über Keys nie; einzige Ausnahme ist das
Einlesen von Verzeichnisquellen, das wie im Admin nur Verwaltern zusteht.
"""

CHAT_ASK = "chat.ask"
DOCS_READ = "docs.read"
DOCS_WRITE = "docs.write"
INDEX_CONTROL = "index.control"
FILES_READ = "files.read"
TOOLS_RUN = "tools.run"
USAGE_READ = "usage.read"

# Reihenfolge = Anzeige (Einstellungen, Admin, CLI-Hilfe).
SCOPES: dict[str, tuple[str, str]] = {
    CHAT_ASK: (
        "Modelle fragen",
        "Einem Modell eine Aufgabe geben (Modellwahl im Rahmen der Rolle, Budget des Kontos).",
    ),
    DOCS_READ: (
        "Dokumente lesen",
        "Sammlungen und Dokumente auflisten, durchsuchen und lesen.",
    ),
    DOCS_WRITE: (
        "Dokumente schreiben",
        "Dateien hochladen, Dokumente löschen und neu indexieren (eigene bzw. schreibbare "
        "Sammlungen), Läufe dazu verfolgen.",
    ),
    INDEX_CONTROL: (
        "Indexierung steuern",
        "Läufe überwachen und abbrechen; Verzeichnisquellen einlesen (nur Verwalter).",
    ),
    FILES_READ: (
        "Dateien abholen",
        "Erzeugte Anhänge und Dokumente herunterladen (z. B. ein PDF).",
    ),
    TOOLS_RUN: (
        "Werkzeuge ausführen",
        "create_pdf, generate_image, run_python, fetch_url und web_search direkt aufrufen "
        "(Rollenrechte und Budgets gelten).",
    ),
    USAGE_READ: ("Verbrauch lesen", "Eigener Verbrauch und Budgetstand."),
}

ALL_SCOPES: tuple[str, ...] = tuple(SCOPES)

SCOPE_CHOICES = [(key, f"{label} ({key})") for key, (label, _) in SCOPES.items()]


def clean(values) -> list[str]:
    """Nur bekannte Scopes, ohne Doppelte, in fester Reihenfolge."""
    wanted = {str(v).strip() for v in values or ()}
    return [scope for scope in ALL_SCOPES if scope in wanted]


def parse(text: str) -> list[str]:
    """Kommagetrennte Liste (CLI); ``all`` bzw. ``*`` = alle. ``ValueError`` bei
    unbekannten Namen."""
    parts = [p.strip() for p in (text or "").replace(";", ",").split(",") if p.strip()]
    if any(p in ("all", "*") for p in parts):
        return list(ALL_SCOPES)
    unknown = [p for p in parts if p not in SCOPES]
    if unknown:
        raise ValueError(
            f"Unbekannte Rechte: {', '.join(unknown)}. Erlaubt: {', '.join(ALL_SCOPES)}."
        )
    return clean(parts)


def label(scope: str) -> str:
    return SCOPES.get(scope, (scope, ""))[0]
