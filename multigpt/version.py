"""Version von MultiGPT für Oberfläche, Admin und MCP-Server.

Im Quellcode-Ordner (Entwicklung) gilt ``pyproject.toml`` – die Paketdaten einer
editierbaren Installation veralten dort, sobald ``make release`` die Version
anhebt. Im Debian-Paket fehlt ``pyproject.toml``, dort gelten die Paketdaten.
"""

from __future__ import annotations

import tomllib
from functools import cache
from importlib import metadata
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


@cache
def app_version() -> str:
    try:
        with PYPROJECT.open("rb") as fh:
            return str(tomllib.load(fh)["project"]["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        pass
    try:
        return metadata.version("multi-gpt")
    except metadata.PackageNotFoundError:
        return "0"


def context(request) -> dict:
    """Kontextprozessor: ``app_version`` in allen Templates (Menü, Anmeldung)."""
    return {"app_version": app_version()}
