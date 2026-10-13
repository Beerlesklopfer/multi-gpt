"""systemd-User-Unit des Runners: Vorlage der Kopplungsseite und die Datei, die
``multigpt-runner pair`` schreibt (runner-rs), müssen dieselben Anweisungen haben."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "multigpt/runner/templates/runner/multigpt-runner.service"
AGENT = ROOT / "runner-rs/multigpt-runner/systemd/multigpt-runner.service"


def directives(path: Path) -> list[str]:
    """Anweisungen ohne Kommentare und Template-Tags; eingesetzte Werte nur als Name."""
    text = re.sub(r"\{%.*?%\}", "", path.read_text())
    out = []
    for line in text.splitlines():
        line = line.strip().replace('"', "")
        if not line or line.startswith(("#", ";")):
            continue
        # Werte aus der Kopplung ({{ x }} bzw. @X@) zählen nur als Platzhalter.
        line = re.sub(r"\{\{\s*\w+\s*\}\}|@\w+@", "<wert>", line)
        out.append(line)
    return out


def test_unit_templates_have_same_directives():
    server = [d for d in directives(SERVER) if "MGPT_CA_SHA256" not in d]
    agent = [d for d in directives(AGENT) if "MGPT_CA_SHA256" not in d]
    assert server == agent
