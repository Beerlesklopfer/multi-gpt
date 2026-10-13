"""Runner im Chat (M14-06, Entscheidungen 8j, 8o, 8t, 8u vom 2026-10-13).

**Arbeitsstand im Chat (8j):** Je Chat wird ein Arbeitsstand gewählt und
gemerkt (``ChatWorkspace``). Er muss dem Konto gehören, das schreibt; in
geteilten Chats bekommen andere Konten keine Runner-Werkzeuge. Der **erste**
Werkzeugaufruf startet die Sitzung (``session_start`` über ``delivery``), die
Sperre je Arbeitsstand (``workspaces.acquire``) erlaubt nur eine Sitzung. Eine
laufende Sitzung desselben Kontos mit denselben Bedingungen (Netz, API-Key)
wird weiterverwendet, auch wenn der Arbeitsstand vorher in einem anderen Chat
lief; eine fremde bzw. abweichende Sitzung meldet „belegt“.

**Werkzeuge und Rückfrage (8t):** Katalog und Einstufung in ``runner.tools``;
der Modus kommt aus ``ChatWorkspace.mode`` (leer: Vorgabe nach Netz).
``setup_host``/``telnet`` fragen immer, nur mit Rollenrecht „Fernzugriff“ und
nie über API-Keys; den Zugang (bei ``setup_host`` zusätzlich einen frischen
Kopplungs-Token für das Zielsystem) setzt :func:`execute` erst für den
bestätigten Aufruf ein (nie in ``ToolCall.arguments``, nie im Log). ``todo``
läuft auf dem Server. Angeboten wird nur, was der verbundene Runner in einem
gültig signierten Dienstkatalog gemeldet hat (``Runner.services``, trust.py).

**Paketquellen (Nutzer, 2026-10-13):** Mit dem Rollenrecht „Paketquellen“
startet die Sitzung im Netzmodus ``packages`` (Proxy mit Positivliste
``RUNNER_PACKAGE_HOSTS``, beschreibbare Schicht bis ``rootfs_mb``) – aber nur,
wenn der verbundene Runner ``apt_install`` in seinem signierten Katalog hat.
Dann gibt es ``apt_install`` (Einstufung wie Befehle) und kein
``setup_host``/``telnet`` (Proxy und Relay schließen sich vorerst aus).

**Runden (8u):** Runner-Antworten haben eigene :data:`MAX_ROUNDS` (100); danach
fragt der Chat, ob weitergearbeitet werden soll (Schleife in ``services``).

**API-Keys (8o):** Über einen M15-Key mit ``runner.use`` (``ask`` mit
``workspace``) nur Arbeitsstände des Kontos, nur ohne Netz, ohne setup_host/telnet,
Modus „alles automatisch“ (eine Rückfrage kann über den Key niemand bestätigen).

Alles hier läuft im Request- bzw. Worker-Thread (ORM); der MCP-Aufruf selbst
geht über die Brücke in den Loop-Thread (``chat.mcp.call_tool_with``).
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass, field

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from multigpt.accounts.permissions import Action, can
from multigpt.chat import mcp, tooling
from multigpt.chat.mcp.config import ServerConfig
from multigpt.chat.models import McpServer, ToolCall

from . import publish as runner_publish
from . import tools
from .models import ChatWorkspace, Runner, RunnerSession, RunnerWorkspace

logger = logging.getLogger(__name__)

# Höchstzahl Anbieteraufrufe je Antwort, danach Rückfrage „weiterarbeiten?“ (8u).
MAX_ROUNDS = 100
START_TIMEOUT = 120.0  # Container starten und Bundle klonen
WAIT_POLL = 0.5
COMMAND_SECONDS = 600
OUTPUT_BYTES = 64 * 1024
IDLE_MINUTES = 15
SCRATCH_MB = 1024  # Vorgabe für /scratch, falls die Rolle nichts sagt
SETUP_SECONDS = 300.0  # setup_host: Programm übertragen, koppeln, Dienst starten
EXTRA_SECONDS = 15  # Zuschlag auf das Zeitlimit von bash (Rückkanal, Abschluss)

OPTION_API_KEY = "runner_api_key"  # Turn.options: ID des Keys (Nutzung über M15)
OPTION_ONLY = "runner_only"  # Turn.options: nur Runner-Werkzeuge (ask ohne „tools“)
EVENT_TODO = "runner_todo"
EVENT_SESSION = "runner_session"

MSG_NO_WORKSPACE = "Für diesen Chat ist kein Arbeitsstand gewählt."
MSG_NO_RUNNER = "Kein Runner verbunden. Bitte den Runner auf dem Rechner starten (Seite „Runner“)."
MSG_BUSY = (
    "Der Arbeitsstand wird gerade in einer anderen Sitzung benutzt. Bitte diese zuerst beenden."
)
MSG_START_FAILED = "Die Sitzung im Runner konnte nicht gestartet werden."
MSG_NOT_RUNNING = "Die Sitzung im Runner läuft nicht mehr. Bitte erneut versuchen."
MSG_HOST = "Unbekannter oder nicht freigegebener Host „{name}“."
MSG_HOST_PROTOCOL = "Der Host „{name}“ ist nicht für {tool} eingerichtet."
MSG_REMOTE_DENIED = "Einrichtung und telnet sind hier nicht erlaubt."
MSG_PACKAGES_RUNNING = (
    "Paketquellen lassen sich nur ohne laufende Sitzung umschalten – bitte die Sitzung "
    "zuerst beenden."
)
MSG_NO_PACKAGES = (
    "Paketquellen sind in dieser Sitzung nicht freigegeben (Rollenrecht „Paketquellen“ "
    "und ein aktueller Runner nötig); apt_install ist nicht verfügbar."
)
MSG_NOT_GRANTED = (
    "Der Dienst „{name}“ ist für diesen Runner nicht freigegeben (Dienstkatalog nicht "
    "gültig signiert oder Dienst nicht eingestuft)."
)
MSG_NO_PUBLIC_URL = (
    "Die Adresse von MultiGPT für das Zielsystem ist unbekannt (Einstellung RUNNER_PUBLIC_URL)."
)
MSG_NO_IMAGE = "Für den Runner ist kein Arbeits-Image mit Digest eingerichtet."
MSG_ROUND_LIMIT = (
    "Höchstzahl von {rounds} Werkzeugrunden erreicht – weiterarbeiten? Bestätigen setzt "
    "die Arbeit für weitere {rounds} Runden fort, Ablehnen beendet die Antwort."
)


class RunnerUnavailable(Exception):
    """Sitzung nicht verfügbar; ``str()`` ist eine deutsche Meldung für das Modell."""


# --- Kontext einer Antwort ------------------------------------------------------------


@dataclass
class RunnerContext:
    """Runner-Lage einer Antwort (aus DB; je Antwort einmal gebildet)."""

    user: object
    conversation: object
    server: McpServer
    selection: ChatWorkspace
    workspace: RunnerWorkspace
    mode: str
    network: bool
    api_key_id: int | None = None
    packages: bool = False  # Netzmodus „packages“ (Paketquellen, apt_install)
    hosts: dict[str, list[str]] = field(default_factory=dict)
    # Dienste des verbundenen Runners (None: kein Runner online, ohne Filter) und
    # freigegebene neue Dienste aus dem signierten Katalog (Name → RunnerTool).
    services: frozenset[str] | None = None
    extra: dict = field(default_factory=dict)

    @property
    def via_api_key(self) -> bool:
        return self.api_key_id is not None

    def specs(self):
        return tools.specs(
            remote=bool(self.hosts) and not self.packages,
            hosts=self.hosts,
            services=self.services,
            extra=self.extra,
            packages=self.packages,
        )

    def needs_confirmation(self, tool: str) -> bool:
        return tools.needs_confirmation(tool, self.mode, self.extra)

    def tool(self, name: str):
        return tools.get(name) or self.extra.get(name)


def runner_server(user) -> McpServer | None:
    return McpServer.objects.filter(
        transport=McpServer.Transport.RUNNER, owner=user, active=True
    ).first()


def _role_network(user, via_api_key: bool) -> bool:
    from .limits import session_limits

    return bool(session_limits(user, via_api_key=via_api_key)["network"])


def _role_packages(user, via_api_key: bool) -> bool:
    from .limits import session_limits

    return bool(session_limits(user, via_api_key=via_api_key).get("packages"))


def packages_possible(user, via_api_key: bool, services) -> bool:
    """Paketquellen für diese Sitzung: Rollenrecht (ohne freies Netz), freigegebene
    Hosts und ein Runner, der ``apt_install`` gültig signiert anbietet."""
    from .limits import package_hosts

    return (
        services is not None
        and tools.APT_INSTALL in services
        and _role_packages(user, via_api_key)
        and bool(package_hosts())
    )


def _remote_hosts(user) -> dict[str, list[str]]:
    from .hosts import visible_hosts

    out: dict[str, list[str]] = {}
    for host in visible_hosts(user):
        out.setdefault(host["protocol"], []).append(host["name"])
    return out


def context_for(user, conversation, server_ids, *, api_key_id=None) -> RunnerContext | None:
    """Kontext, wenn diese Antwort Runner-Werkzeuge bekommt, sonst None.

    Bedingungen: Recht „Runner nutzen“, eigener aktiver Runner-Server unter den
    eingeschalteten Servern, gewählter Arbeitsstand des Kontos.
    """
    if conversation is None or not can(user, Action.USE_RUNNER):
        return None
    server = runner_server(user)
    if server is None or server.pk not in set(server_ids or ()):
        return None
    selection = (
        ChatWorkspace.objects.select_related("workspace").filter(conversation=conversation).first()
    )
    if selection is None or selection.workspace is None:
        return None
    if selection.workspace.owner_id != user.pk:
        return None
    via_key = api_key_id is not None
    network = _role_network(user, via_key)
    if via_key:
        mode = tools.MODE_AUTO_ALL
        hosts: dict[str, list[str]] = {}
    else:
        mode = tools.effective_mode(selection.mode, network=network, is_admin=_is_admin(user))
        hosts = _remote_hosts(user)
    runner = _online_runner(user)
    services = frozenset(runner.services or ()) if runner is not None else None
    packages = bool(selection.packages) and packages_possible(user, via_key, services)
    extra = {}
    if services:
        from . import trust

        extra = trust.extra_tools(services)
    return RunnerContext(
        user=user,
        conversation=conversation,
        server=server,
        selection=selection,
        workspace=selection.workspace,
        mode=mode,
        network=network,
        api_key_id=api_key_id,
        hosts=hosts,
        services=services,
        extra=extra,
        packages=packages,
    )


def context_for_turn(turn, server_ids) -> RunnerContext | None:
    return context_for(
        turn.user,
        turn.conversation,
        server_ids,
        api_key_id=(turn.options or {}).get(OPTION_API_KEY),
    )


def _is_admin(user) -> bool:
    return can(user, Action.ADMIN)


# --- Auswahl im Chat (API für die Oberfläche) ---------------------------------------


def get_state(user, conversation) -> dict:
    """Stand für die Oberfläche: Arbeitsstand, Modus, Netz, Sitzung, Aufgabenliste."""
    selection = (
        ChatWorkspace.objects.select_related("workspace", "session")
        .filter(conversation=conversation)
        .first()
    )
    usable = can(user, Action.USE_RUNNER)
    network = _role_network(user, False) if usable else False
    # Paketquellen: Rollenrecht; „runner“ sagt, ob der verbundene Runner es kann.
    packages = _role_packages(user, False) if usable else False
    runner = _online_runner(user) if packages else None
    is_admin = _is_admin(user)
    mode = selection.mode if selection else ""
    workspace = selection.workspace if selection else None
    session = selection.session if selection else None
    allowed = [
        m for m in tools.MODES if not (m == tools.MODE_AUTO_ALL and network and not is_admin)
    ]
    return {
        "available": can(user, Action.USE_RUNNER) and runner_server(user) is not None,
        "workspace_id": workspace.pk if workspace and workspace.owner_id == user.pk else None,
        "workspace_name": workspace.name if workspace and workspace.owner_id == user.pk else "",
        "mode": mode,
        "mode_default": not mode,
        "effective_mode": tools.effective_mode(mode, network=network, is_admin=is_admin),
        "modes": [{"value": m, "label": tools.MODES[m]} for m in tools.MODES],
        "modes_allowed": allowed,
        "network": network,
        "packages": packages,
        "packages_selected": bool(selection.packages) if selection else True,
        "packages_runner": bool(
            runner is not None and packages_possible(user, False, frozenset(runner.services or ()))
        ),
        "session": (
            {
                "id": session.pk,
                "uuid": str(session.uuid),
                "status": session.status,
                "packages": bool((session.limits_requested or {}).get("packages")),
            }
            if session is not None and session.owner_id == user.pk
            else None
        ),
        "todo": list(selection.todo or []) if selection else [],
        # Scratchpad des Runners: Ort je Arbeitsstand (gilt ab der nächsten Sitzung).
        "workspace_scratch_mode": (
            workspace.scratch_mode if workspace and workspace.owner_id == user.pk else ""
        ),
        "scratch_modes": [
            {"value": value, "label": label} for value, label in RunnerWorkspace.ScratchMode.choices
        ],
    }


_UNSET = object()


def set_state(
    user,
    conversation,
    *,
    workspace=_UNSET,
    mode=_UNSET,
    scratch_mode=_UNSET,
    packages=_UNSET,
) -> dict:
    """Auswahl ändern. ``workspace``: ID oder None; ``mode``: Wert aus
    ``tools.MODES`` oder "" (Vorgabe); ``scratch_mode``: Ort des Scratchpads des
    gewählten Arbeitsstands (nicht während einer Sitzung); ``packages``:
    Paketquellen in diesem Chat an/aus (nicht während einer Sitzung). ``ValueError``
    mit deutscher Meldung."""
    if not can(user, Action.USE_RUNNER):
        raise ValueError("Der Runner ist für dieses Konto nicht freigegeben.")
    if not can(user, Action.WRITE, conversation):
        raise ValueError("In diesem Chat darf das Konto nicht schreiben.")
    selection, _ = ChatWorkspace.objects.get_or_create(conversation=conversation)
    fields = []
    if workspace is not _UNSET:
        target = None
        if workspace not in (None, ""):
            try:
                pk = int(workspace)
            except (TypeError, ValueError):
                raise ValueError("Unbekannter Arbeitsstand.") from None
            target = RunnerWorkspace.objects.filter(pk=pk, owner=user).first()
            if target is None:
                raise ValueError("Unbekannter Arbeitsstand.")
        if (target.pk if target else None) != selection.workspace_id:
            selection.workspace = target
            selection.session = None  # neue Auswahl: nächste Sitzung beim ersten Aufruf
            selection.todo = []
            fields += ["workspace", "session", "todo"]
    if mode is not _UNSET:
        mode = mode or ""
        if mode and mode not in tools.MODES:
            raise ValueError("Unbekannter Modus.")
        if mode == tools.MODE_AUTO_ALL and _role_network(user, False) and not _is_admin(user):
            raise ValueError("„Alles automatisch“ mit Netz ist nur für Verwalter erlaubt.")
        selection.mode = mode
        fields.append("mode")
    if packages is not _UNSET:
        if not isinstance(packages, bool):
            raise ValueError("Paketquellen: an oder aus erwartet.")
        if packages != selection.packages:
            current = selection.session
            if current is not None and current.is_active:
                raise ValueError(MSG_PACKAGES_RUNNING)
            selection.packages = packages
            fields.append("packages")
    if fields:
        selection.save(update_fields=[*fields, "updated"])
    if scratch_mode is not _UNSET:
        from . import workspaces

        target = selection.workspace
        if target is None or target.owner_id != user.pk:
            raise ValueError(MSG_NO_WORKSPACE)
        workspaces.update_workspace(target, target.name, scratch_mode or "")
    return get_state(user, conversation)


# --- Sitzung -------------------------------------------------------------------------------


def _online_runner(user) -> Runner | None:
    from .delivery import stale_after

    return (
        Runner.objects.filter(
            token__owner=user,
            token__revoked_at__isnull=True,
            status=Runner.Status.ONLINE,
            last_seen__gte=timezone.now() - stale_after(),
        )
        .order_by("-last_seen", "-pk")
        .first()
    )


_PROCESS_SALT = uuid.uuid4()


def _new_salt() -> None:
    global _PROCESS_SALT
    _PROCESS_SALT = uuid.uuid4()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_new_salt)


def pipe_for(session: RunnerSession) -> str:
    """MCP-Verbindung (pipe, UUID) dieses Prozesses zur Sitzung: je Prozess eine
    eigene, damit Antworten an den richtigen Prozess gehen."""
    return str(uuid.uuid5(_PROCESS_SALT, str(session.uuid)))


def _usable(session: RunnerSession | None, ctx: RunnerContext) -> bool:
    return (
        session is not None
        and session.is_active
        and session.status != RunnerSession.Status.STOPPING
        and session.owner_id == ctx.user.pk
        and session.workspace_id == ctx.workspace.pk
        and session.network == ctx.network
        and bool((session.limits_requested or {}).get("packages")) == ctx.packages
        and session.api_key_id == ctx.api_key_id
    )


def _session_frame(session: RunnerSession, workspace: RunnerWorkspace, ctx) -> dict:
    from . import protocol

    image = getattr(settings, "RUNNER_WORK_IMAGE", "") or ""
    if not protocol.IMAGE_RE.match(image):
        raise RunnerUnavailable(MSG_NO_IMAGE)
    limits = session.limits_requested
    frame = {
        "type": "session_start",
        "session": str(session.uuid),
        "workspace": str(workspace.pk),
        "image": image,
        "cpus": limits["cpus"],
        "memory_mb": limits["memory_mb"],
        "pids": limits["pids"],
        "tmpfs_mb": limits["tmpfs_mb"],
        "network": "default" if session.network else "none",
        "command_seconds": int(getattr(settings, "RUNNER_COMMAND_SECONDS", COMMAND_SECONDS)),
        "output_bytes": int(getattr(settings, "RUNNER_OUTPUT_BYTES", OUTPUT_BYTES)),
        "idle_minutes": int(getattr(settings, "RUNNER_IDLE_MINUTES", IDLE_MINUTES)),
    }
    # Scratchpad des Runners: /scratch als tmpfs (container) oder Ordner am
    # Browser-Rechner (local, ~/MultiGPT/Scratch/<uuid>/ – der Server nennt nur die UUID).
    known = protocol.FRAMES["session_start"][1]
    if "scratch_mode" in known:
        frame["scratch_mode"] = limits.get("scratch_mode") or getattr(
            workspace, "scratch_mode", "container"
        )
    if "scratch_mb" in known:
        role = ctx.user.role if getattr(ctx.user, "role_id", None) else None
        frame["scratch_mb"] = int(
            limits.get("scratch_mb") or getattr(role, "runner_scratch_mb", 0) or SCRATCH_MB
        )
    if "workspace_uuid" in known and getattr(workspace, "uuid", None):
        frame["workspace_uuid"] = str(workspace.uuid)
    if limits.get("packages"):
        # Paketquellen: nur die Hosts der Einstellung über den Proxy im Relay;
        # Container-Schicht beschreibbar bis rootfs_mb. Kein allow (schließt sich aus).
        from .limits import package_hosts

        frame["network"] = "packages"
        frame["package_hosts"] = package_hosts()
        frame["rootfs_mb"] = int(limits["rootfs_mb"])
    allow = _allowlist(ctx) if not (session.network or limits.get("packages")) else []
    if allow:
        # 8w: setup_host/telnet erreichen nur freigegebene Hosts (Positivliste setzt der Agent).
        frame["network"] = "allowlist"
        frame["allow"] = allow
    if workspace.size and workspace.sha256:
        info = {"size": workspace.size, "sha256": workspace.sha256}
        if workspace.head_commit:
            info["head"] = workspace.head_commit
        frame["bundle"] = info
    return frame


def _allowlist(ctx: RunnerContext) -> list[dict]:
    if ctx.via_api_key or not ctx.hosts or ctx.packages:
        return []
    from .hosts import hosts_for

    return [{"host": h.address, "port": h.port} for h in hosts_for(ctx.user)]


def _start_remote(runner: Runner, frame: dict, session: RunnerSession) -> dict:
    """``session_start`` senden und auf ``session_state`` running/failed warten."""
    from . import delivery

    final = {"running", "failed", "stopped"}
    return delivery.request(
        runner.pk,
        frame,
        timeout=START_TIMEOUT,
        session=session,
        accept=lambda f: f.get("type") == "session_state" and f.get("state") in final,
    )


def _fail(session: RunnerSession, workspace: RunnerWorkspace, reason: str) -> None:
    from . import workspaces

    RunnerSession.objects.filter(pk=session.pk).update(
        status=RunnerSession.Status.FAILED, ended=timezone.now(), stop_reason=reason[:200]
    )
    workspaces.release(workspace, session)


def ensure_session(ctx: RunnerContext) -> RunnerSession:
    """Laufende Sitzung für den Arbeitsstand des Chats; startet sie bei Bedarf.

    ``RunnerUnavailable`` mit deutscher Meldung, wenn das nicht geht.
    """
    from . import audit, workspaces
    from .limits import package_hosts, session_limits, without_packages

    workspace = RunnerWorkspace.objects.filter(pk=ctx.workspace.pk, owner=ctx.user).first()
    if workspace is None:
        raise RunnerUnavailable(MSG_NO_WORKSPACE)
    current = None
    if workspace.active_session_id:
        current = RunnerSession.objects.filter(pk=workspace.active_session_id).first()
    if _usable(current, ctx):
        session = _wait_running(current)
        _remember(ctx, session)
        return session
    if current is not None and current.is_active:
        raise RunnerUnavailable(MSG_BUSY)
    runner = _online_runner(ctx.user)
    if runner is None:
        raise RunnerUnavailable(MSG_NO_RUNNER)
    try:
        from multigpt.billing import budgets, runtime

        if not runtime.runtime_allows(ctx.user):
            raise RunnerUnavailable(budgets.RUNTIME_EXHAUSTED_MESSAGE)
    except ImportError:  # Kontingent (M14-11) noch nicht installiert
        pass
    limits = session_limits(ctx.user, via_api_key=ctx.via_api_key, workspace=workspace)
    if limits.get("packages") and not (
        ctx.packages and tools.APT_INSTALL in (runner.services or ()) and package_hosts()
    ):
        limits = without_packages(limits)  # Runner kann es nicht bzw. keine Hosts
    with transaction.atomic():
        session = RunnerSession.objects.create(
            owner=ctx.user,
            workspace=workspace,
            runner=runner,
            conversation=ctx.conversation,
            api_key_id=ctx.api_key_id,
            status=RunnerSession.Status.STARTING,
            confirm_mode=ctx.mode,
            network=bool(limits["network"]),
            limits_requested=limits,
        )
        if not workspaces.acquire(workspace, session):
            transaction.set_rollback(True)
            raise RunnerUnavailable(MSG_BUSY)
    try:
        frame = _session_frame(session, workspace, ctx)
        answer = _start_remote(runner, frame, session)
    except Exception as exc:
        message = getattr(exc, "message", "") or MSG_START_FAILED
        if isinstance(exc, RunnerUnavailable):
            message = str(exc)
        logger.warning("Runner-Sitzung %s: Start gescheitert (%s)", session.pk, type(exc).__name__)
        _fail(session, workspace, message)
        audit.record("session", owner=ctx.user, session=session, runner=runner, message=message)
        raise RunnerUnavailable(message) from None
    if answer.get("state") != "running":
        message = MSG_START_FAILED
        _fail(session, workspace, str(answer.get("error") or message))
        audit.record("session", owner=ctx.user, session=session, runner=runner, message=message)
        raise RunnerUnavailable(message)
    RunnerSession.objects.filter(pk=session.pk, status=RunnerSession.Status.STARTING).update(
        status=RunnerSession.Status.RUNNING
    )
    session.refresh_from_db()
    if session.status != RunnerSession.Status.RUNNING:
        raise RunnerUnavailable(MSG_NOT_RUNNING)
    audit.record(
        "session",
        owner=ctx.user,
        session=session,
        runner=runner,
        message="Sitzung gestartet" + (" (API-Key)" if ctx.via_api_key else ""),
    )
    _remember(ctx, session)
    return session


def _wait_running(session: RunnerSession) -> RunnerSession:
    """Startet die Sitzung gerade (anderer Aufruf), kurz auf „läuft“ warten."""
    deadline = time.monotonic() + START_TIMEOUT
    while session.status in (RunnerSession.Status.STARTING, RunnerSession.Status.UNKNOWN):
        if time.monotonic() >= deadline:
            raise RunnerUnavailable(MSG_NOT_RUNNING)
        time.sleep(WAIT_POLL)
        session.refresh_from_db()
    if session.status != RunnerSession.Status.RUNNING:
        raise RunnerUnavailable(MSG_NOT_RUNNING)
    if session.runner_id is None:
        raise RunnerUnavailable(MSG_NO_RUNNER)
    return session


def _remember(ctx: RunnerContext, session: RunnerSession) -> None:
    if ctx.selection.session_id != session.pk:
        ChatWorkspace.objects.filter(pk=ctx.selection.pk).update(session=session)
        ctx.selection.session = session
    if session.conversation_id != getattr(ctx.conversation, "pk", None):
        RunnerSession.objects.filter(pk=session.pk).update(conversation=ctx.conversation)


# --- Ausführung ------------------------------------------------------------------------------


def _remote_arguments(ctx: RunnerContext, name: str, args: dict) -> dict:
    """Zugang zum Host nur für diesen bestätigten Aufruf einsetzen."""
    from .hosts import connection_for, get_host

    if ctx.via_api_key or not can(ctx.user, Action.USE_RUNNER_HOSTS):
        raise tools.ArgumentError(MSG_REMOTE_DENIED)
    host = get_host(ctx.user, args["host"])
    if host is None:
        raise tools.ArgumentError(MSG_HOST.format(name=args["host"]))
    if host.protocol != tools.HOST_PROTOCOL.get(name):
        raise tools.ArgumentError(MSG_HOST_PROTOCOL.format(name=host.name, tool=name))
    if name == tools.SETUP and host.auth_method != host.AuthMethod.KEY:
        raise tools.ArgumentError(
            f"Der Host „{host.name}“ hat keinen SSH-Schlüssel – Einrichtung nur per Schlüssel."
        )
    access = connection_for(host)
    out = {k: v for k, v in args.items() if k != "host"}
    out["host"] = {
        "name": access["name"],
        "address": access["address"],
        "port": access["port"],
        "user": access.get("username") or "",
        "host_key": access.get("host_key") or "",
        "secret_type": "password" if access.get("auth_method") == "password" else "key",
        "secret": access.get("secret") or "",
    }
    return out


def _pairing(ctx: RunnerContext, host_name: str):
    """Kopplung für ``setup_host``: frischer Token (nur für diesen Aufruf gezeigt),
    Adresse, Fingerprint und öffentlicher Installationsschlüssel des Servers,
    Programme mit Prüfsumme. Liefert (Daten, Token-Objekt)."""
    from . import quadlet, tokens, trust

    base = trust.public_url()
    if not base:
        raise tools.ArgumentError(MSG_NO_PUBLIC_URL)
    key = trust.public_key_text()
    if not key:
        raise tools.ArgumentError("Der Installationsschlüssel des Servers fehlt.")
    info = trust.dist_info()
    if not info.binaries:
        raise tools.ArgumentError("Auf dem Server liegen keine Runner-Programme zum Herunterladen.")
    try:
        created = tokens.create_token(ctx.user, f"Einrichtung {host_name}"[: tokens.MAX_NAME])
    except ValueError as exc:
        raise tools.ArgumentError(str(exc)) from None
    data = {
        "url": base,
        "token": created.secret,
        "ca_sha256": quadlet.ca_fingerprint(),
        "server_key": key,
        # Arbeits-Image mit Digest für ``pair --images`` (wie auf der Kopplungsseite).
        "images": [settings.RUNNER_WORK_IMAGE],
        "download": {
            arch: {"url": f"{base}/runner/download/{arch}", "sha256": item["sha256"]}
            for arch, item in info.binaries.items()
        },
    }
    return data, created.token


def _timeout(server: McpServer, name: str, args: dict) -> float:
    base = float(server.timeout_seconds or 30)
    if name == "bash":
        return float(args.get("timeout_s") or tools.BASH_TIMEOUT_DEFAULT) + EXTRA_SECONDS
    if name == tools.SETUP:
        return max(base, SETUP_SECONDS)
    if name == tools.APT_INSTALL:
        return max(base, float(tools.APT_TIMEOUT + EXTRA_SECONDS))
    return base


def _run_todo(ctx: RunnerContext, args: dict) -> tooling.Outcome:
    items = args["items"]
    ChatWorkspace.objects.filter(pk=ctx.selection.pk).update(todo=items, updated=timezone.now())
    ctx.selection.todo = items
    outcome = tooling.Outcome(ToolCall.Status.OK, tools.todo_text(items), False)
    outcome.events.append((EVENT_TODO, {"items": items}))
    return outcome


def execute(ctx: RunnerContext, server: McpServer, tool_call: ToolCall, message) -> tooling.Outcome:
    """Führt einen Runner-Aufruf aus und protokolliert ihn (``ToolCall`` und Audit).

    Rechte und Rückfrage prüft die Schleife vorher (wie bei ``tooling.execute``).
    """
    from . import audit

    started = time.monotonic()
    name = tool_call.tool
    tool = ctx.tool(name)
    session = None
    exit_code = None
    audit_args: dict = {}
    pairing_token = None
    try:
        if tool is None:
            raise tools.ArgumentError(tooling.MSG_UNKNOWN)
        remote = tool.kind in (tools.REMOTE, tools.SETUP_KIND)
        if remote and (ctx.via_api_key or not can(ctx.user, Action.USE_RUNNER_HOSTS)):
            raise tools.ArgumentError(MSG_REMOTE_DENIED)
        if tool.name in tools.REMOTE_TOOLS and (not ctx.hosts or ctx.packages):
            raise tools.ArgumentError(MSG_REMOTE_DENIED)
        if name == tools.APT_INSTALL and not ctx.packages:
            raise tools.ArgumentError(MSG_NO_PACKAGES)
        if tools.get(name) is not None:
            args = tools.clean_arguments(name, tool_call.arguments or {})
        else:
            args = tools.clean_extra_arguments(tool_call.arguments or {})
        audit_args = dict(args)
        if tool.server_side:
            outcome = _run_todo(ctx, args)
        else:
            if ctx.services is not None and name not in ctx.services:
                raise tools.ArgumentError(MSG_NOT_GRANTED.format(name=name))
            session = ensure_session(ctx)
            granted = set((session.runner.services if session.runner else None) or ())
            if name not in granted:
                raise tools.ArgumentError(MSG_NOT_GRANTED.format(name=name))
            if tool.name in tools.REMOTE_TOOLS:
                args = _remote_arguments(ctx, name, args)
            if name == tools.SETUP:
                args["pairing"], pairing_token = _pairing(ctx, args["host"]["name"])
            config = ServerConfig.for_runner(
                server,
                runner_id=session.runner_id,
                session_id=str(session.uuid),
                pipe=pipe_for(session),
            )
            RunnerSession.objects.filter(pk=session.pk).update(last_tool_at=timezone.now())
            if name == tools.PUBLISH:
                result, outcome = _run_publish(ctx, server, config, tool_call, message, args)
            else:
                result = mcp.call_tool_with(config, name, args, _timeout(server, name, args))
                outcome = _outcome(result, tool_call, message)
            structured = (result.raw or {}).get("structuredContent") or {}
            if isinstance(structured.get("exit_code"), int):
                exit_code = structured["exit_code"]
            if name == "serve" and not outcome.is_error:
                outcome.text = f"{outcome.text}\n\n{preview_note(session)}"
    except (tools.ArgumentError, runner_publish.PublishError) as exc:
        outcome = tooling.Outcome(ToolCall.Status.ERROR, str(exc), True)
    except RunnerUnavailable as exc:
        outcome = tooling.Outcome(ToolCall.Status.ERROR, str(exc), True)
    except mcp.McpTimeout:
        seconds = int(_timeout(server, name, audit_args))
        outcome = tooling.Outcome(
            ToolCall.Status.TIMEOUT, tooling.MSG_TIMEOUT.format(seconds=seconds), True
        )
    except mcp.McpError as exc:
        outcome = tooling.Outcome(ToolCall.Status.ERROR, str(exc) or tooling.MSG_FAILED, True)
    except Exception as exc:
        logger.error("Runner-Aufruf %s fehlgeschlagen: %s", tool_call.pk, type(exc).__name__)
        outcome = tooling.Outcome(ToolCall.Status.ERROR, tooling.MSG_FAILED, True)
    if pairing_token is not None and outcome.is_error:
        # Einrichtung gescheitert: den Token für das Zielsystem gleich widerrufen.
        from . import tokens

        tokens.revoke(pairing_token)
    elapsed = tooling.record(tool_call, outcome, started)
    if tool is not None and not (tool.server_side):
        try:
            audit.record(
                "remote" if tool.kind in (tools.REMOTE, tools.SETUP_KIND) else "tool",
                owner=ctx.user,
                session=session,
                runner=session.runner if session is not None else None,
                tool=name,
                arguments=audit_args,  # ohne Zugang (nur Hostname)
                exit_code=exit_code,
                duration_ms=int(elapsed * 1000),
                message=outcome.status,
            )
        except Exception as exc:  # Protokoll darf den Aufruf nicht scheitern lassen
            logger.error("Runner-Audit für %s: %s", tool_call.pk, type(exc).__name__)
    logger.info(
        "Runner-Aufruf %s (%s): %s in %d ms",
        tool_call.pk,
        name,
        outcome.status,
        int(elapsed * 1000),
    )
    if session is not None:
        outcome.events.append(
            (EVENT_SESSION, {"id": session.pk, "uuid": str(session.uuid), "status": session.status})
        )
    return outcome


def _run_publish(ctx, server, config, tool_call, message, args):
    """``publish``: Datei aus dem Container als Anhang der Antwort (siehe ``publish``)."""
    transfer = str(uuid.uuid4())
    limit = runner_publish.max_bytes(ctx.user)
    call_args = {**args, "transfer": transfer, "max_bytes": limit}
    with runner_publish.subscribe(config.runner_id, transfer) as sub:
        result = mcp.call_tool_with(config, tools.PUBLISH, call_args, _timeout(server, "", args))
        if result.is_error:
            return result, tooling.Outcome(
                ToolCall.Status.ERROR, result.text or tooling.MSG_FAILED, True
            )
        embedded = [*result.files, *result.images]
        if embedded:
            data, sent_name = embedded[0].data, embedded[0].name
        else:
            data, sent_name, _ = runner_publish.collect(sub, limit)
    name = args.get("name") or sent_name or args["path"]
    attachment = runner_publish.store(message, tool_call, name, data, ctx.user)
    text = (
        f"Datei veröffentlicht: {attachment.original_name} ({attachment.size} Bytes, "
        f"Anhang #{attachment.pk}). {tooling.NOTE_ATTACHMENT_SHOWN}"
    )
    return result, tooling.Outcome(ToolCall.Status.OK, text, False, [attachment.pk])


def _outcome(result, tool_call: ToolCall, message) -> tooling.Outcome:
    ids = tooling._store_files(message, tool_call, result)
    text = result.text or ""
    if ids:
        note = (
            f"[{len(ids)} Datei(en) als Anhang gespeichert und dem Nutzer angezeigt. "
            f"{tooling.NOTE_ATTACHMENT_SHOWN}]"
        )
        text = f"{text}\n\n{note}" if text else note
    status = ToolCall.Status.ERROR if result.is_error else ToolCall.Status.OK
    return tooling.Outcome(status, text or tooling.MSG_EMPTY, bool(result.is_error), ids)


def preview_url(session: RunnerSession) -> str:
    """Adresse der Vorschau über MultiGPT (Ansicht von ui, Token im Pfad)."""
    return f"/runner/vorschau/{session.uuid}/{session.preview_token}/"


def preview_note(session: RunnerSession) -> str:
    return (
        f"Vorschau für den Nutzer: {preview_url(session)} (im Chat als abgeschotteter "
        "Rahmen; nur statische Dateien)."
    )


def round_limit_text() -> str:
    return MSG_ROUND_LIMIT.format(rounds=MAX_ROUNDS)
