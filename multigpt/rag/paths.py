"""Pfadprüfung für Verzeichnisquellen (Agent crawler).

Eine Verzeichnisquelle darf nur unterhalb einer erlaubten Wurzel aus
``settings.RAG_SOURCE_ROOTS`` liegen. Geprüft wird mit ``os.path.realpath``
(``..`` und Symlinks dürfen nicht hinausführen) – bei der Anlage, bei jedem
Einlesen und für jede einzelne Datei.

Dateien werden nie über ihren Pfadnamen geöffnet, sondern Komponente für
Komponente ab der (vom Verwalter konfigurierten) Wurzel mit ``openat`` und
``O_NOFOLLOW``. Damit kann auch ein Symlink, der zwischen Prüfung und Öffnen
ausgetauscht wird (TOCTOU), nicht aus der Wurzel herausführen. Geöffnet
werden nur reguläre Dateien (keine FIFOs, Geräte oder Verzeichnisse).
"""

import os
import stat

from django.conf import settings

MSG_DISABLED = (
    "Verzeichnisquellen sind ausgeschaltet: In der Konfiguration ist RAG_SOURCE_ROOTS leer."
)
MSG_NOT_ABSOLUTE = "Bitte einen absoluten Pfad angeben (beginnt mit /)."
MSG_OUTSIDE = "Der Pfad liegt außerhalb der erlaubten Verzeichnisse (RAG_SOURCE_ROOTS)."
MSG_MISSING = "Das Verzeichnis gibt es nicht (oder es ist für den Dienst nicht sichtbar)."
MSG_NOT_DIR = "Der Pfad ist kein Verzeichnis."
MSG_UNREADABLE = (
    "Der Dienstnutzer darf das Verzeichnis nicht lesen. Bitte Leserechte vergeben "
    "(z. B. Gruppe, in der der Nutzer „multi-gpt“ ist, mit r-x auf Verzeichnisse und "
    "r auf Dateien). Liegt es unter /home, braucht der Dienst zusätzlich ein "
    "systemd-Drop-in (ProtectHome)."
)
MSG_FILE_MISSING = "Die Datei des Dokuments fehlt auf dem Server."
MSG_NOT_REGULAR = "Kein reguläres Dokument (Symlink, Verzeichnis oder Gerät)."


class SourcePathError(Exception):
    """Pfad unzulässig oder nicht lesbar; ``message`` ist deutsch und ohne Pfad."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def allowed_roots() -> list[str]:
    """Erlaubte Wurzeln als echte Pfade (realpath); relative Einträge zählen nicht."""
    roots = []
    for raw in getattr(settings, "RAG_SOURCE_ROOTS", None) or []:
        raw = str(raw).strip()
        if not raw or not os.path.isabs(raw):
            continue
        real = os.path.realpath(raw)
        if real not in roots:
            roots.append(real)
    return roots


def enabled() -> bool:
    return bool(allowed_roots())


def _is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def root_for(real_path: str) -> str | None:
    """Die (längste) erlaubte Wurzel, unter der ``real_path`` liegt."""
    matches = [r for r in allowed_roots() if _is_within(real_path, r)]
    return max(matches, key=len) if matches else None


def resolve(path: str) -> str:
    """Pfad gegen die Wurzeln prüfen und als echten Pfad zurückgeben."""
    if not allowed_roots():
        raise SourcePathError(MSG_DISABLED)
    if not path or not os.path.isabs(path) or "\x00" in path:
        raise SourcePathError(MSG_NOT_ABSOLUTE)
    real = os.path.realpath(path)
    if root_for(real) is None:
        raise SourcePathError(MSG_OUTSIDE)
    return real


def check_directory(path: str) -> str:
    """Verzeichnis für eine Quelle prüfen: unter einer Wurzel, vorhanden, lesbar.

    Rückgabe: echter Pfad. Geprüft wird mit den Rechten des laufenden Prozesses
    (im Betrieb der Dienstnutzer ``multi-gpt``).
    """
    real = resolve(path)
    try:
        fd = open_under_root(real, directory=True)
    except FileNotFoundError as exc:
        raise SourcePathError(MSG_MISSING) from exc
    except NotADirectoryError as exc:
        raise SourcePathError(MSG_NOT_DIR) from exc
    except PermissionError as exc:
        raise SourcePathError(MSG_UNREADABLE) from exc
    except OSError as exc:
        raise SourcePathError(MSG_UNREADABLE) from exc
    try:
        # Lesen (Einträge auflisten) muss klappen, nicht nur Betreten.
        with os.scandir(fd) as it:
            next(it, None)
    except PermissionError as exc:
        raise SourcePathError(MSG_UNREADABLE) from exc
    except OSError as exc:
        raise SourcePathError(MSG_UNREADABLE) from exc
    finally:
        os.close(fd)
    return real


def open_under_root(real_path: str, *, directory: bool = False) -> int:
    """``real_path`` ab seiner Wurzel komponentenweise ohne Symlinks öffnen.

    Rückgabe: Dateideskriptor (Aufrufer schließt). Wirft ``SourcePathError``,
    wenn der Pfad außerhalb der Wurzeln liegt, sonst ``OSError`` (z. B.
    ``ELOOP`` bei einem Symlink, ``FileNotFoundError``). Für Dateien wird
    zusätzlich geprüft, dass es eine reguläre Datei ist.
    """
    root = root_for(real_path)
    if root is None:
        raise SourcePathError(MSG_OUTSIDE)
    rel = os.path.relpath(real_path, root)
    parts = [] if rel == "." else rel.split(os.sep)
    if any(p in ("", ".", "..") for p in parts):
        raise SourcePathError(MSG_OUTSIDE)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | cloexec)
    try:
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            flags = os.O_RDONLY | os.O_NOFOLLOW | cloexec
            if last and not directory:
                # O_NONBLOCK: Eine FIFO darf das Öffnen nicht blockieren.
                flags |= os.O_NONBLOCK
            else:
                flags |= os.O_DIRECTORY
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        if not directory:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise SourcePathError(MSG_NOT_REGULAR)
            os.set_blocking(fd, True)
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_file(base_real: str, rel_path: str):
    """Datei ``rel_path`` unterhalb des Quellverzeichnisses sicher öffnen.

    Prüft das Quellverzeichnis und den Dateipfad erneut gegen die Wurzeln und
    öffnet ohne Symlinks. Rückgabe: binäres Dateiobjekt (Aufrufer schließt).
    Fehler als ``SourcePathError`` mit deutscher Meldung.
    """
    if not rel_path or os.path.isabs(rel_path) or "\x00" in rel_path:
        raise SourcePathError(MSG_OUTSIDE)
    base = resolve(base_real)
    full = os.path.normpath(os.path.join(base, rel_path))
    if not _is_within(full, base) or full == base:
        raise SourcePathError(MSG_OUTSIDE)
    try:
        fd = open_under_root(full)
    except SourcePathError:
        raise
    except FileNotFoundError as exc:
        raise SourcePathError(MSG_FILE_MISSING) from exc
    except PermissionError as exc:
        raise SourcePathError(MSG_UNREADABLE) from exc
    except OSError as exc:
        # ELOOP (Symlink), ENOTDIR (Komponente ist kein Verzeichnis) u. a.
        raise SourcePathError(MSG_NOT_REGULAR) from exc
    return os.fdopen(fd, "rb")
