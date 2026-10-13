"""Arbeitsstände als git-Bundle (Entscheidung 2026-10-13, 8q–8s).

Ein Arbeitsstand gehört einem Konto und liegt als Bundle (``git bundle create
… --all``) in ``RunnerWorkspace.bundle``. Hier: Grenzen je Rolle, anlegen
(leeres Repository mit einem leeren Anfangs-Commit, denn ein Bundle ohne Refs
gibt es nicht), Verlauf lesen, Sperre (eine Sitzung je Arbeitsstand), löschen.
Die Annahme neuer Bundles vom Runner (Prüfung, atomar) liegt in ``bundles.py``.

**Sicherheit:** Das Bundle ist nicht vertrauenswürdiges Material. Der Server
liest es nur – ``git`` läuft in einem eigenen temporären Verzeichnis, als
nacktes Repository ohne Arbeitsbaum, ohne Hooks (``core.hooksPath=/dev/null``),
ohne System- und Nutzerkonfiguration und mit Zeitlimit. Es wird nichts
ausgecheckt und nichts ausgeführt.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from multigpt.accounts.permissions import Action, can

from .models import RunnerWorkspace

MAX_NAME = 100
GIT_TIMEOUT = 20
HISTORY_LIMIT = 100
DEFAULT_BRANCH = "main"
# Autor des Anfangs-Commits (keine echte Adresse).
GIT_AUTHOR = ("MultiGPT", "runner@multigpt.invalid")
_NAME_RE = re.compile(r"^[\w .,()+-]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
# Trennzeichen im git-log-Format (Unit Separator), kommt in Metadaten praktisch nie vor.
_SEP = "\x1f"


class WorkspaceError(ValueError):
    """Fehler mit deutscher Meldung für die Oberfläche."""


@dataclass(frozen=True)
class WorkspaceLimits:
    max_count: int
    max_bytes: int

    @property
    def max_mb(self) -> int:
        return self.max_bytes // (1024 * 1024)


@dataclass(frozen=True)
class Commit:
    sha: str
    author: str
    date: datetime | None
    subject: str

    @property
    def short(self) -> str:
        return self.sha[:10]


def limits_for(user) -> WorkspaceLimits:
    """Grenzen aus der Rolle (ohne Rolle: 10 und ``RUNNER_WORKSPACE_MAX_MB``)."""
    role = user.role if getattr(user, "role_id", None) else None
    count = role.runner_max_workspaces if role else 10
    megabytes = (
        role.runner_max_workspace_mb if role else getattr(settings, "RUNNER_WORKSPACE_MAX_MB", 50)
    )
    return WorkspaceLimits(max_count=count, max_bytes=megabytes * 1024 * 1024)


def own_workspaces(user):
    """Arbeitsstände des Kontos, ohne Bundle (Listen)."""
    return RunnerWorkspace.objects.filter(owner=user).select_related("active_session")


def get_own(user, pk: int, *, with_bundle: bool = False) -> RunnerWorkspace | None:
    """Arbeitsstand ``pk`` nur, wenn er ``user`` gehört – auch Verwalter sehen keine fremden."""
    qs = RunnerWorkspace.objects.with_bundle() if with_bundle else RunnerWorkspace.objects
    return qs.filter(pk=pk, owner=user).first()


def clean_name(name: str) -> str:
    name = " ".join((name or "").split())[:MAX_NAME]
    if not name:
        raise WorkspaceError("Bitte einen Namen für den Arbeitsstand angeben.")
    if not _NAME_RE.match(name):
        raise WorkspaceError(
            "Der Name darf Buchstaben, Ziffern, Leerzeichen und . , ( ) + - _ enthalten."
        )
    return name


# --- git (nur lesend, ohne Hooks) ---------------------------------------------


def git_env(home: str | os.PathLike) -> dict[str, str]:
    """Bereinigte Umgebung für git: keine Konfiguration von System und Nutzer,
    keine Rückfragen, keine Hilfsprogramme aus der Umgebung."""
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
        "GIT_OPTIONAL_LOCKS": "0",
    }


# Für jeden Aufruf: keine Hooks, kein fsmonitor, nur lokale Bundles als Quelle.
_GIT_SAFE = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "protocol.allow=never",
    "-c",
    "protocol.file.allow=always",
    "-c",
    f"init.defaultBranch={DEFAULT_BRANCH}",
    "-c",
    "gc.auto=0",
)


def run_git(args: Sequence[str], *, cwd, home, timeout: int = GIT_TIMEOUT) -> str:
    """git mit sicheren Optionen; ``WorkspaceError`` bei Fehler oder Zeitüberschreitung."""
    try:
        result = subprocess.run(
            ["git", *_GIT_SAFE, *args],
            cwd=cwd,
            env=git_env(home),
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise WorkspaceError("git hat zu lange gebraucht.") from None
    except OSError:
        raise WorkspaceError("git ist auf dem Server nicht verfügbar.") from None
    if result.returncode != 0:
        raise WorkspaceError("Das Bundle des Arbeitsstands ist nicht lesbar.")
    return result.stdout.decode("utf-8", errors="replace")


@contextmanager
def scratch_dir():
    """Temporäres Verzeichnis (nur für den Server-Prozess lesbar), danach weg."""
    with tempfile.TemporaryDirectory(prefix="mgpt-ws-") as path:
        os.chmod(path, 0o700)
        yield Path(path)


@contextmanager
def opened_bundle(data: bytes):
    """Bundle in ein nacktes Repository holen; liefert (Repo-Pfad, HOME-Pfad)."""
    with scratch_dir() as tmp:
        bundle = tmp / "ws.bundle"
        bundle.write_bytes(data)
        repo = tmp / "repo.git"
        run_git(["init", "-q", "--bare", str(repo)], cwd=tmp, home=tmp)
        run_git(["fetch", "-q", str(bundle), "+refs/*:refs/*"], cwd=repo, home=tmp)
        yield repo, tmp, bundle


def bundle_head(data: bytes) -> str:
    """Commit, auf den HEAD im Bundle zeigt (sonst der erste Kopf), oder ''."""
    with scratch_dir() as tmp:
        bundle = tmp / "ws.bundle"
        bundle.write_bytes(data)
        run_git(["init", "-q", "--bare", "probe.git"], cwd=tmp, home=tmp)
        heads = run_git(["bundle", "list-heads", str(bundle)], cwd=tmp / "probe.git", home=tmp)
    refs = {}
    for line in heads.splitlines():
        sha, _, ref = line.partition(" ")
        if _SHA_RE.match(sha):
            refs[ref.strip()] = sha
    return (
        refs.get("HEAD")
        or refs.get(f"refs/heads/{DEFAULT_BRANCH}")
        or next(iter(refs.values()), "")
    )


def empty_bundle() -> bytes:
    """Bundle eines neuen Repositorys mit einem leeren Anfangs-Commit auf ``main``."""
    name, email = GIT_AUTHOR
    with scratch_dir() as tmp:
        repo = tmp / "repo"
        run_git(["init", "-q", str(repo)], cwd=tmp, home=tmp)
        run_git(
            [
                "-c",
                f"user.name={name}",
                "-c",
                f"user.email={email}",
                "commit",
                "-q",
                "--allow-empty",
                "--no-verify",
                "-m",
                "Arbeitsstand angelegt",
            ],
            cwd=repo,
            home=tmp,
        )
        target = tmp / "ws.bundle"
        run_git(["bundle", "create", "-q", str(target), "--all"], cwd=repo, home=tmp)
        return target.read_bytes()


def history(workspace: RunnerWorkspace, limit: int = HISTORY_LIMIT) -> list[Commit]:
    """Verlauf (``git log``) aus dem Bundle, neueste zuerst. Nur lesend."""
    data = workspace.bundle
    if data is None:
        data = (
            RunnerWorkspace.objects.with_bundle()
            .values_list("bundle", flat=True)
            .get(pk=workspace.pk)
        )
    if not data:
        return []
    data = bytes(data)
    head = bundle_head(data)
    fmt = _SEP.join(["%H", "%an", "%aI", "%s"])
    with opened_bundle(data) as (repo, home, _bundle):
        target = [head] if head else ["--all"]
        out = run_git(
            ["log", f"--max-count={int(limit)}", f"--format={fmt}", "--no-color", *target, "--"],
            cwd=repo,
            home=home,
        )
    commits = []
    for line in out.splitlines():
        parts = line.split(_SEP)
        if len(parts) != 4:
            continue
        sha, author, date, subject = parts
        try:
            when = datetime.fromisoformat(date)
        except ValueError:
            when = None
        commits.append(Commit(sha=sha, author=author[:100], date=when, subject=subject[:300]))
    return commits


# --- Anlegen, Löschen, Sperre ----------------------------------------------------


def store_bundle_fields(workspace: RunnerWorkspace, data: bytes, head: str = "") -> None:
    """Bundle samt Größe, Prüfsumme und HEAD an das Objekt setzen (ohne Speichern)."""
    workspace.bundle = data
    workspace.size = len(data)
    workspace.sha256 = hashlib.sha256(data).hexdigest()
    workspace.head_commit = head
    workspace.updated = timezone.now()


def clean_scratch_mode(value: str | None) -> str:
    value = value or RunnerWorkspace.ScratchMode.CONTAINER
    if value not in RunnerWorkspace.ScratchMode.values:
        raise WorkspaceError("Unbekannter Ort für das Scratchpad.")
    return value


def create_workspace(owner, name: str, scratch_mode: str | None = None) -> RunnerWorkspace:
    """Neuen Arbeitsstand mit leerem Repository anlegen (Grenzen der Rolle)."""
    if not can(owner, Action.USE_RUNNER):
        raise WorkspaceError("Deine Rolle erlaubt den Runner nicht.")
    name = clean_name(name)
    scratch_mode = clean_scratch_mode(scratch_mode)
    limits = limits_for(owner)
    if RunnerWorkspace.objects.filter(owner=owner).count() >= limits.max_count:
        raise WorkspaceError(
            f"Höchstens {limits.max_count} Arbeitsstände je Konto – bitte einen löschen."
        )
    if RunnerWorkspace.objects.filter(owner=owner, name=name).exists():
        raise WorkspaceError(f"Es gibt schon einen Arbeitsstand „{name}“.")
    data = empty_bundle()
    workspace = RunnerWorkspace(owner=owner, name=name, scratch_mode=scratch_mode)
    store_bundle_fields(workspace, data, bundle_head(data))
    try:
        with transaction.atomic():
            workspace.save()
    except IntegrityError:
        raise WorkspaceError(f"Es gibt schon einen Arbeitsstand „{name}“.") from None
    workspace.bundle = None  # nicht im Speicher halten
    return workspace


def update_workspace(workspace: RunnerWorkspace, name: str, scratch_mode: str) -> None:
    """Name und Ort des Scratchpads ändern; den Ort nicht während einer Sitzung
    (der Agent bindet ihn beim Start ein)."""
    name = clean_name(name)
    scratch_mode = clean_scratch_mode(scratch_mode)
    if (
        RunnerWorkspace.objects.filter(owner=workspace.owner_id, name=name)
        .exclude(pk=workspace.pk)
        .exists()
    ):
        raise WorkspaceError(f"Es gibt schon einen Arbeitsstand „{name}“.")
    qs = RunnerWorkspace.objects.filter(pk=workspace.pk)
    if scratch_mode != workspace.scratch_mode:
        qs = qs.filter(active_session__isnull=True)
    try:
        with transaction.atomic():
            updated = qs.update(name=name, scratch_mode=scratch_mode)
    except IntegrityError:
        raise WorkspaceError(f"Es gibt schon einen Arbeitsstand „{name}“.") from None
    if not updated:
        raise WorkspaceError(
            "Der Ort des Scratchpads lässt sich nicht ändern, solange eine Sitzung den "
            "Arbeitsstand nutzt."
        )
    workspace.name, workspace.scratch_mode = name, scratch_mode


def delete_workspace(workspace: RunnerWorkspace) -> None:
    """Löschen; nicht, solange eine Sitzung ihn nutzt."""
    deleted, _ = RunnerWorkspace.objects.filter(
        pk=workspace.pk, active_session__isnull=True
    ).delete()
    if not deleted:
        raise WorkspaceError(
            "Der Arbeitsstand ist gerade in einer Sitzung in Benutzung. Bitte die Sitzung "
            "zuerst beenden."
        )


def acquire(workspace: RunnerWorkspace, session) -> bool:
    """Sperre für ``session`` setzen (atomar). False, wenn eine andere Sitzung ihn hält.

    Eine Sperre einer beendeten Sitzung gilt als frei.
    """
    from .models import RunnerSession

    stale = Q(active_session__isnull=True) | Q(
        active_session__status__in=[
            RunnerSession.Status.STOPPED,
            RunnerSession.Status.FAILED,
        ]
    )
    updated = RunnerWorkspace.objects.filter(
        Q(pk=workspace.pk) & (stale | Q(active_session=session))
    ).update(active_session=session, locked_at=timezone.now())
    if updated:
        workspace.active_session = session
    return bool(updated)


def release(workspace: RunnerWorkspace, session=None) -> bool:
    """Sperre lösen; mit ``session`` nur, wenn genau sie die Sperre hält."""
    qs = RunnerWorkspace.objects.filter(pk=workspace.pk)
    if session is not None:
        qs = qs.filter(active_session=session)
    updated = qs.update(active_session=None, locked_at=None)
    if updated:
        workspace.active_session = None
        workspace.locked_at = None
    return bool(updated)
