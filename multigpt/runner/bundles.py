"""Annahme neuer Bundles eines Arbeitsstands (Entscheidung 2026-10-13, Pflicht 2).

Der Agent committet nach jeder Dateiänderung, erzeugt ein neues Bundle
(``git bundle create … --all``) und lädt es in Stücken hoch; der Runner-Dienst
(``gateway``) setzt die Stücke zusammen, prüft Folge, Größe und Prüfsumme und
ruft dann :func:`accept` auf (per ``sync_to_async``).

Ablauf der Annahme:

1. Größe gegen :func:`max_bytes` (Rolle, Standard 50 MB), Prüfsumme SHA-256
   (Vergleich in konstanter Zeit), Kopf ``# v2/v3 git bundle``.
2. Die Sitzung muss die Sperre des Arbeitsstands halten
   (``RunnerWorkspace.active_session``) und gehört demselben Konto.
3. ``git bundle verify`` und ``git fetch`` mit ``transfer.fsckObjects`` in ein
   leeres, temporäres Repository – ohne Hooks, ohne Konfiguration des Systems
   bzw. Nutzers, mit eigenem ``HOME``; das Bundle gilt als nicht
   vertrauenswürdiges Material und wird nie ausgecheckt oder ausgeführt.
4. Erst danach ersetzt **ein** ``UPDATE`` (unter Zeilensperre, Sperre erneut
   geprüft) Bundle, Größe, Prüfsumme und HEAD. Bis dahin bleibt der letzte
   gute Stand unverändert – auch wenn die Prüfung scheitert.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

DEFAULT_MAX_MB = 50
GIT_TIMEOUT = 60
BUNDLE_HEADERS = (b"# v2 git bundle\n", b"# v3 git bundle\n")
_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")

MSG_TOO_LARGE = "Das Bundle ist zu groß (höchstens {mb} MB je Arbeitsstand)."
MSG_CHECKSUM = "Die Prüfsumme des Bundles stimmt nicht."
MSG_FORMAT = "Das ist kein git-Bundle."
MSG_INVALID = "Das Bundle ist beschädigt oder unvollständig (git bundle verify)."
MSG_NO_WORKSPACE = "Den Arbeitsstand gibt es nicht."
MSG_NOT_LOCKED = "Die Sitzung hält den Arbeitsstand nicht (Sperre fehlt)."
MSG_NO_GIT = "Auf dem Server fehlt git; das Bundle kann nicht geprüft werden."
MSG_EMPTY = "Das Bundle enthält keine Refs."


@dataclass(frozen=True)
class AcceptResult:
    ok: bool
    head: str = ""
    size: int = 0
    message: str = ""


def max_bytes(user) -> int:
    """Höchstgröße eines Bundles für ``user`` (Rolle, sonst Einstellung, Std. 50 MB)."""
    mb = None
    role = getattr(user, "role", None) if getattr(user, "role_id", None) else None
    if role is not None:
        mb = getattr(role, "runner_max_workspace_mb", None)
    if not mb:
        mb = getattr(settings, "RUNNER_WORKSPACE_MAX_MB", DEFAULT_MAX_MB) or DEFAULT_MAX_MB
    return int(mb) * 1024 * 1024


def _git_env(home: str) -> dict[str, str]:
    """Bereinigte Umgebung: keine System-/Nutzerkonfiguration, kein Terminal."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": home,
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
        "GIT_ATTR_NOSYSTEM": "1",
    }


_SAFE_CONFIG = (
    "-c",
    f"core.hooksPath={os.devnull}",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "protocol.allow=never",
    "-c",
    "protocol.file.allow=always",  # nur für die Bundle-Datei (lokaler Pfad)
    "-c",
    "transfer.fsckObjects=true",
    "-c",
    "fetch.fsckObjects=true",
)


def _git(args: list[str], repo: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *_SAFE_CONFIG, "-C", repo, *args],
        env=env,
        capture_output=True,
        timeout=GIT_TIMEOUT,
        check=False,
        stdin=subprocess.DEVNULL,
    )


def verify(data: bytes) -> tuple[str, str]:
    """Prüft ein Bundle in einem temporären, leeren Repository.

    Ergebnis: (HEAD-Commit, "") oder ("", Fehlermeldung). Die Ausgaben von git
    gehen nie in Meldungen oder Logs (sie enthalten Inhalte des Arbeitsstands).
    """
    if not data.startswith(BUNDLE_HEADERS):
        return "", MSG_FORMAT
    if shutil.which("git") is None:
        return "", MSG_NO_GIT
    with tempfile.TemporaryDirectory(prefix="mgpt-bundle-") as tmp:
        path = os.path.join(tmp, "upload.bundle")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        repo = os.path.join(tmp, "repo.git")
        env = _git_env(tmp)
        try:
            init = subprocess.run(
                ["git", *_SAFE_CONFIG, "init", "--quiet", "--bare", "--template=", repo],
                env=env,
                capture_output=True,
                timeout=GIT_TIMEOUT,
                check=False,
                stdin=subprocess.DEVNULL,
            )
            if init.returncode != 0:
                logger.error("Bundle-Prüfung: git init fehlgeschlagen")
                return "", MSG_INVALID
            if _git(["bundle", "verify", "--quiet", path], repo, env).returncode != 0:
                return "", MSG_INVALID
            heads = _git(["bundle", "list-heads", path], repo, env)
            if heads.returncode != 0:
                return "", MSG_INVALID
            head = _head_from(heads.stdout.decode("utf-8", "replace"))
            if not head:
                return "", MSG_EMPTY
            # Objekte vollständig einlesen und mit fsck prüfen (transfer.fsckObjects).
            fetch = _git(["fetch", "--quiet", "--no-tags", path, "+refs/*:refs/mgpt/*"], repo, env)
            if fetch.returncode != 0:
                return "", MSG_INVALID
        except subprocess.TimeoutExpired:
            logger.warning("Bundle-Prüfung: Zeitüberschreitung")
            return "", MSG_INVALID
        except OSError as exc:
            logger.error("Bundle-Prüfung: %s", type(exc).__name__)
            return "", MSG_NO_GIT
    return head, ""


def _head_from(listing: str) -> str:
    """HEAD aus ``git bundle list-heads``; ohne HEAD die erste Ref."""
    first = ""
    for line in listing.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2 or not _SHA.match(parts[0]):
            continue
        if parts[1] == "HEAD":
            return parts[0]
        first = first or parts[0]
    return first


def _audit(*, session_id=None, **fields) -> None:
    try:
        from . import audit
        from .models import RunnerSession

        if session_id is not None:
            fields["session"] = RunnerSession.objects.filter(pk=session_id).first()
        audit.record(**fields)
    except Exception as exc:  # Protokoll darf die Annahme nicht scheitern lassen
        logger.error("Runner-Audit nicht geschrieben: %s", type(exc).__name__)


def accept(
    workspace_id: int,
    data: bytes,
    sha256: str,
    session_id: int | None = None,
    *,
    head: str | None = None,
    message: str | None = None,
) -> AcceptResult:
    """Nimmt ein neues Bundle an (siehe Moduldoku). Sync, mit ORM.

    ``head``/``message`` meldet der Agent (nicht vertrauenswürdig); maßgeblich
    ist der HEAD aus dem geprüften Bundle.
    """
    from .models import RunnerSession, RunnerWorkspace

    workspace = (
        RunnerWorkspace.objects.select_related("owner__role").filter(pk=workspace_id).first()
    )
    if workspace is None:
        return AcceptResult(False, message=MSG_NO_WORKSPACE)
    size = len(data or b"")
    limit = max_bytes(workspace.owner)
    if size > limit:
        return _reject(workspace, session_id, MSG_TOO_LARGE.format(mb=limit // (1024 * 1024)))
    digest = hashlib.sha256(data or b"").hexdigest()
    if not isinstance(sha256, str) or not hmac.compare_digest(digest, sha256.lower()):
        return _reject(workspace, session_id, MSG_CHECKSUM)
    if session_id is not None:
        session = RunnerSession.objects.filter(pk=session_id).first()
        if (
            session is None
            or session.workspace_id != workspace.pk
            or session.owner_id != workspace.owner_id
            or workspace.active_session_id != session.pk
        ):
            return _reject(workspace, session_id, MSG_NOT_LOCKED)
    verified_head, error = verify(data)
    if error:
        return _reject(workspace, session_id, error)
    with transaction.atomic():
        current = (
            RunnerWorkspace.objects.select_for_update()
            .filter(pk=workspace.pk)
            .values("active_session_id")
            .first()
        )
        if current is None:
            return AcceptResult(False, message=MSG_NO_WORKSPACE)
        if session_id is not None and current["active_session_id"] != session_id:
            return _reject(workspace, session_id, MSG_NOT_LOCKED)
        RunnerWorkspace.objects.filter(pk=workspace.pk).update(
            bundle=data,
            size=size,
            sha256=digest,
            head_commit=verified_head,
            updated=timezone.now(),
        )
    logger.info("Arbeitsstand %s: neues Bundle angenommen (%d Bytes)", workspace.pk, size)
    _audit(
        kind="workspace",
        owner=workspace.owner,
        session_id=session_id,
        message=f"Bundle angenommen ({size} Bytes, HEAD {verified_head[:12]})",
    )
    return AcceptResult(True, head=verified_head, size=size)


def _reject(workspace, session_id, text: str) -> AcceptResult:
    logger.warning("Arbeitsstand %s: Bundle abgelehnt (%s)", workspace.pk, text)
    _audit(
        kind="workspace",
        owner=workspace.owner,
        session_id=session_id,
        message=f"Bundle abgelehnt: {text}",
    )
    return AcceptResult(False, head=workspace.head_commit, size=workspace.size, message=text)
