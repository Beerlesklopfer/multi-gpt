"""Version von MultiGPT für Oberfläche, Admin und MCP-Server.

Im Quellcode-Ordner (Entwicklung) gilt ``pyproject.toml`` – die Paketdaten einer
editierbaren Installation veralten dort, sobald ``make release`` die Version
anhebt. Im Debian-Paket fehlt ``pyproject.toml``, dort gelten die Paketdaten.

**Stand (``app_build``):** kurzer Git-Hash, mit ``-dirty`` bei uncommitteten Änderungen.
Paketbau (``debian/rules``) und ``deploy.sh``/``deploy.fish`` schreiben ihn in
``multigpt/BUILD``; ein neues Paket überschreibt die Datei, ein Deploy ebenso. Ohne
Datei (Entwicklung) fragt ``git`` den Quellordner. Oberfläche und Admin zeigen
„0.4.0 (d9face2)“; MCP und Runner melden weiter nur die Version.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from functools import cache
from importlib import metadata
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"
BUILD_FILE = Path(__file__).resolve().parent / "BUILD"
_BUILD_RE = re.compile(r"^[0-9a-f]{7,40}(-dirty)?$")


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


@cache
def app_build() -> str:
    """Kurzer Git-Hash des laufenden Stands (ggf. mit ``-dirty``), sonst ``""``."""
    try:
        value = BUILD_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        value = _git_build(BUILD_FILE.parent.parent)
    return value if _BUILD_RE.match(value) else ""


def _git_build(root: Path) -> str:
    if not (root / ".git").exists():
        return ""
    try:
        rev = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--", "multigpt", "pyproject.toml"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    return f"{rev}-dirty" if dirty else rev


def version_label() -> str:
    """„0.4.0 (d9face2)“ für Oberfläche und Admin; ohne Stand nur die Version."""
    build = app_build()
    return f"{app_version()} ({build})" if build else app_version()


def context(request) -> dict:
    """Kontextprozessor: ``app_version`` (mit Stand) in allen Templates (Menü, Anmeldung)."""
    return {"app_version": version_label()}
