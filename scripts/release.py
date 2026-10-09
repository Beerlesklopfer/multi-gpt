#!/usr/bin/env python3
"""Neue Version von MultiGPT vorbereiten (``make release VERSION=x.y.z``).

- prüft: Versionsformat, sauberer Arbeitsbaum, Branch main, Version größer als
  die aktuelle in debian/changelog, Tag existiert noch nicht;
- setzt die Version in pyproject.toml;
- stellt einen Eintrag in debian/changelog voran (Maintainer aus debian/control,
  Stichpunkte aus den Commit-Betreffs seit dem letzten Tag);
- committet beides und setzt das annotierte Tag ``v<VERSION>``.

Gepusht und gebaut wird bewusst nicht automatisch (siehe Ausgabe am Ende).
"""

import re
import subprocess
import sys
from email.utils import formatdate
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "debian" / "changelog"
CONTROL = ROOT / "debian" / "control"
PYPROJECT = ROOT / "pyproject.toml"


def run(*args, check=True) -> str:
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
    if check and result.returncode != 0:
        fail(f"{' '.join(args)} fehlgeschlagen: {result.stderr.strip()}")
    return result.stdout.strip()


def fail(message: str) -> None:
    print(f"release: {message}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    if len(sys.argv) != 2 or not re.fullmatch(r"\d+\.\d+\.\d+", sys.argv[1]):
        fail("Aufruf: make release VERSION=x.y.z (z. B. 0.2.0)")
    version = sys.argv[1]
    tag = f"v{version}"

    if run("git", "status", "--porcelain"):
        fail("Der Arbeitsbaum ist nicht sauber – erst committen.")
    if run("git", "rev-parse", "--abbrev-ref", "HEAD") != "main":
        fail("Releases nur vom Branch main.")
    if run("git", "tag", "--list", tag):
        fail(f"Tag {tag} existiert bereits.")

    current = run("dpkg-parsechangelog", "-l", str(CHANGELOG), "-S", "Version")
    newer = subprocess.run(["dpkg", "--compare-versions", version, "gt", current], cwd=ROOT)
    if newer.returncode != 0:
        fail(f"{version} ist nicht größer als die aktuelle Version {current}.")

    maintainer = re.search(r"^Maintainer:\s*(.+)$", CONTROL.read_text(), re.M)
    if not maintainer:
        fail("Kein Maintainer in debian/control.")
    source = re.search(r"^Source:\s*(\S+)$", CONTROL.read_text(), re.M).group(1)

    last_tag = run("git", "describe", "--tags", "--abbrev=0", check=False)
    log_range = f"{last_tag}..HEAD" if last_tag else "HEAD"
    subjects = run("git", "log", "--no-merges", "--format=%s", log_range).splitlines()
    if last_tag:
        bullets = [f"  * {s}" for s in subjects] or [
            "  * Keine Änderungen seit dem letzten Release."
        ]
    else:
        head = run("git", "rev-parse", "--short", "HEAD")
        bullets = [f"  * Erste getaggte Version ({len(subjects)} Commits bis {head})."]

    entry = (
        f"{source} ({version}) unstable; urgency=medium\n\n"
        + "\n".join(bullets)
        + f"\n\n -- {maintainer.group(1).strip()}  {formatdate(localtime=True)}\n\n"
    )
    CHANGELOG.write_text(entry + CHANGELOG.read_text())

    text = PYPROJECT.read_text()
    text, count = re.subn(
        r'^version = "[^"]*"$', f'version = "{version}"', text, count=1, flags=re.M
    )
    if count != 1:
        fail("Keine version-Zeile in pyproject.toml gefunden.")
    PYPROJECT.write_text(text)

    run(
        "git",
        "commit",
        "-q",
        "-m",
        f"Release {version}",
        "--",
        "debian/changelog",
        "pyproject.toml",
    )
    run("git", "tag", "-a", tag, "-m", f"MultiGPT {version}")

    print(f"Version {version} vorbereitet: Commit und Tag {tag} gesetzt.")
    print("Weiter:")
    print("  make deb                            # Paket bauen")
    print(f"  git push origin main {tag}         # veröffentlichen")


if __name__ == "__main__":
    main()
