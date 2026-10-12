"""Templates: Stolperfallen, die Django nicht meldet."""

import re
from pathlib import Path

import multigpt

ROOT = Path(multigpt.__file__).resolve().parent


def test_no_multiline_hash_comments():
    """``{# … #}`` gilt nur für eine Zeile; mehrzeilig landet der Kommentar als Text
    auf der Seite (so geschehen in der Statusleiste). Dafür ``{% comment %}``."""
    offenders = []
    for path in ROOT.rglob("*.html"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for start in [m.start() for m in re.finditer(r"\{#", line)]:
                if "#}" not in line[start:]:
                    offenders.append(f"{path.relative_to(ROOT)}:{number}")
    assert not offenders, "Mehrzeilige {# #}-Kommentare: " + ", ".join(offenders)
