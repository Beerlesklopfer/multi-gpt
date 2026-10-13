"""Datenbankzugriffe des Runner-Dienstes (M14-04/05).

Alle Funktionen sind synchron und nutzen das ORM. ``gateway.py`` ruft sie aus
dem Event-Loop nur über ``asgiref.sync.sync_to_async`` auf (ein ORM-Thread,
eine Datenbankverbindung). Rahmen kommen bereits geprüft an (``protocol``);
Inhalte vom Agent gelten trotzdem als nicht vertrauenswürdig: Sitzungen werden
immer gegen den Runner (und damit das Konto) der Verbindung geprüft.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.db import DEFAULT_DB_ALIAS, close_old_connections, connections, transaction
from django.utils import timezone
from psycopg import sql

from multigpt.accounts.permissions import Action, can

from . import audit, limits, protocol, tokens, trust
from .delivery import runner_channel
from .models import Runner, RunnerAudit, RunnerMessage, RunnerSession, RunnerToken, RunnerWorkspace

logger = logging.getLogger(__name__)

QUOTA_GRACE_SECONDS = 60
MSG_GONE = "Container nach Neuverbindung nicht mehr vorhanden"


def _setting(name: str, default: int) -> int:
    try:
        value = int(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def failure_limit() -> int:
    return _setting("RUNNER_FAILURE_LIMIT", _setting("API_FAILURE_LIMIT", 10))


def failure_window() -> timedelta:
    minutes = _setting("RUNNER_FAILURE_WINDOW_MINUTES", _setting("API_FAILURE_WINDOW_MINUTES", 15))
    return timedelta(minutes=minutes)


def fresh() -> None:
    """Vor jedem Aufruf aus dem Dienst: abgelaufene bzw. kaputte Verbindungen ersetzen."""
    close_old_connections()


# --- Audit ---------------------------------------------------------------------


def _audit(kind: str, **fields) -> None:
    try:
        audit.record(kind, **fields)
    except Exception:  # Audit darf den Dienst nicht anhalten
        logger.exception("Runner-Audit nicht geschrieben")


# --- Anmeldung -----------------------------------------------------------------


@dataclass
class Auth:
    code: int | None  # Close-Code bei Fehler, sonst None
    token_id: int | None = None
    owner_id: int | None = None
    reason: str = ""


def auth_blocked(ip: str | None) -> bool:
    """Zu viele fehlgeschlagene Anmeldungen dieser IP im Zeitfenster?"""
    if not ip:
        return False
    since = timezone.now() - failure_window()
    count = RunnerAudit.objects.filter(
        kind=RunnerAudit.Kind.AUTH_FAILED, ip=ip, created__gte=since
    ).count()
    return count >= failure_limit()


def authenticate(raw: str, ip: str | None) -> Auth:
    """Token aus ``hello`` prüfen; Fehlschläge landen im Audit (Drosselung je IP)."""
    fresh()
    token = tokens.authenticate(raw)
    if token is None:
        _audit(RunnerAudit.Kind.AUTH_FAILED, ip=ip or None, message="Token ungültig")
        return Auth(protocol.CLOSE_TOKEN, reason="Token ungültig")
    if not can(token.owner, Action.USE_RUNNER):
        _audit(
            RunnerAudit.Kind.AUTH_FAILED,
            owner=token.owner,
            ip=ip or None,
            message="Konto ohne Recht „Runner nutzen“",
        )
        return Auth(protocol.CLOSE_FORBIDDEN, token.pk, token.owner_id, "Kein Recht")
    tokens.touch(token, ip)
    return Auth(None, token.pk, token.owner_id)


def access_code(token_id: int) -> int | None:
    """Frische Prüfung von Token, Konto und Recht: None = in Ordnung, sonst Close-Code."""
    fresh()
    token = RunnerToken.objects.select_related("owner", "owner__role").filter(pk=token_id).first()
    if token is None or not token.is_usable() or not token.owner.is_active:
        return protocol.CLOSE_TOKEN
    if not can(token.owner, Action.USE_RUNNER):
        return protocol.CLOSE_FORBIDDEN
    return None


# --- Verbindung ------------------------------------------------------------------


@dataclass
class Connected:
    runner_id: int
    connected_at: object
    frames: list[dict] = field(default_factory=list)  # sofort an den Agent
    services: list[str] = field(default_factory=list)  # welcome.services
    catalog_state: str = "missing"


def connect(token_id: int, hello: dict, ip: str | None) -> Connected:
    """Runner anlegen bzw. aktualisieren, online setzen, Ist-Zustand abgleichen."""
    fresh()
    now = timezone.now()
    engine = hello["engine"]
    # Dienstkatalog (Release-Signatur) prüfen; nur gültige Kataloge geben Dienste frei.
    check = trust.check_catalog(hello)
    services = trust.grant(check, now)
    with transaction.atomic():
        runner, _created = Runner.objects.select_for_update().get_or_create(token_id=token_id)
        runner.name = hello["hostname"][:200]
        runner.engine = engine["name"]
        runner.engine_version = engine["version"][:50]
        runner.agent_version = hello["agent_version"][:50]
        runner.protocol_version = hello["protocol"]
        runner.rootless = engine["rootless"]
        runner.status = Runner.Status.ONLINE
        runner.connected_at = now
        runner.last_seen = now
        runner.catalog_state = check.state
        runner.catalog_release = check.release[:50]
        runner.services = services
        runner.save()
    frames = reconcile(runner, hello.get("sessions") or [], now)
    if not check.trusted:
        _audit(
            RunnerAudit.Kind.TRUST,
            owner=runner.token.owner,
            runner=runner,
            ip=ip or None,
            message=f"keine Werkzeuge: {check.message}",
        )
    elif check.state == "dev":
        logger.warning("Runner %s: unsignierter Katalog angenommen (Entwicklung).", runner.pk)
    _audit(
        RunnerAudit.Kind.CONNECT,
        owner=runner.token.owner,
        runner=runner,
        ip=ip or None,
        message=(
            f"verbunden (Agent {runner.agent_version}, {runner.engine} {runner.engine_version})"
        ),
    )
    return Connected(runner.pk, now, frames, services, check.state)


def _session_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (TypeError, ValueError):
        return None


def reconcile(runner: Runner, reported: list[dict], now=None) -> list[dict]:
    """Gemeldeten Ist-Zustand mit der Datenbank abgleichen (Hello).

    - In der DB aktiv, vom Agent nicht gemeldet: Container weg → ``stopped``,
      Laufzeit abschließen, Sperre lösen.
    - Gemeldet und in der DB aktiv: Zustand übernehmen.
    - Gemeldet, aber unbekannt oder schon beendet: ``session_stop`` an den Agent.
    """
    now = now or timezone.now()
    by_uuid = {}
    for item in reported:
        key = _session_uuid(item["session"])
        if key is not None:
            by_uuid[key] = item
    frames = []
    active = RunnerSession.objects.filter(runner=runner, status__in=RunnerSession.ACTIVE_STATES)
    seen = set()
    for session in active:
        item = by_uuid.get(session.uuid)
        if item is None:
            _end_session(
                session,
                RunnerSession.Status.STOPPED,
                "Container nach Neuverbindung nicht mehr vorhanden",
                now,
            )
            continue
        seen.add(session.uuid)
        state = item["state"]
        if state in ("stopped", "failed"):
            _end_session(session, state, "vom Agent beendet gemeldet", now)
        else:
            session.status = state if state != "unknown" else RunnerSession.Status.UNKNOWN
            session.save(update_fields=["status"])
    for key, item in by_uuid.items():
        if key not in seen and item["state"] not in ("stopped", "failed"):
            frames.append({"type": "session_stop", "session": item["session"], "reason": "error"})
    for item in reported:
        if _session_uuid(item["session"]) is None and item["state"] not in ("stopped", "failed"):
            frames.append({"type": "session_stop", "session": item["session"], "reason": "error"})
    return frames


def _end_session(session: RunnerSession, status: str, reason: str, now=None) -> None:
    """Sitzung beenden: Laufzeit abschließen, Status, Sperre des Arbeitsstands lösen."""
    now = now or timezone.now()
    try:
        from multigpt.billing import runtime
    except ImportError:  # pragma: no cover
        runtime = None
    if runtime is not None and session.ended is None:
        try:
            runtime.finish_session(session, now)
        except Exception:
            logger.exception("Laufzeit der Runner-Sitzung %s nicht abgeschlossen", session.pk)
    session.status = status
    session.ended = session.ended or now
    if not session.stop_reason:
        session.stop_reason = reason[:200]
    session.preview_port = None
    session.save(update_fields=["status", "ended", "stop_reason", "preview_port"])
    RunnerWorkspace.objects.filter(active_session=session).update(
        active_session=None, locked_at=None
    )


def disconnect(runner_id: int, connected_at) -> None:
    """Verbindung beendet: Runner offline, aktive Sitzungen ``unknown``.

    Nur, wenn keine neuere Verbindung den Runner übernommen hat (``connected_at``).
    """
    fresh()
    with transaction.atomic():
        updated = Runner.objects.filter(pk=runner_id, connected_at=connected_at).update(
            status=Runner.Status.OFFLINE
        )
        if updated:
            RunnerSession.objects.filter(
                runner_id=runner_id, status__in=RunnerSession.ACTIVE_STATES
            ).update(status=RunnerSession.Status.UNKNOWN)


# --- Heartbeat -------------------------------------------------------------------


@dataclass
class Beat:
    code: int | None = None  # Close-Code, wenn Token/Recht nicht mehr gelten
    frames: list[dict] = field(default_factory=list)


def heartbeat(runner_id: int, token_id: int, sessions: list[dict]) -> Beat:
    """Lebenszeichen: Token und Recht frisch prüfen, ``last_seen``, Laufzeit buchen."""
    code = access_code(token_id)
    if code is not None:
        return Beat(code)
    now = timezone.now()
    Runner.objects.filter(pk=runner_id).update(last_seen=now, status=Runner.Status.ONLINE)
    beat = Beat()
    try:
        from multigpt.billing import runtime
    except ImportError:  # pragma: no cover
        runtime = None
    for item in sessions:
        key = _session_uuid(item["session"])
        session = (
            RunnerSession.objects.filter(runner_id=runner_id, uuid=key).first() if key else None
        )
        if session is None or session.ended is not None:
            if item["state"] not in ("stopped", "failed"):
                beat.frames.append(
                    {"type": "session_stop", "session": item["session"], "reason": "error"}
                )
            continue
        state = item["state"]
        if state in ("stopped", "failed"):
            _end_session(session, state, "vom Agent beendet gemeldet", now)
            continue
        if state != session.status and state in RunnerSession.Status.values:
            session.status = state
            session.save(update_fields=["status"])
        if runtime is not None:
            try:
                runtime.record_heartbeat(session, running=state == "running", now=now)
            except Exception:
                logger.exception("Laufzeit der Runner-Sitzung %s nicht gebucht", session.pk)
    return beat


# --- Nachrichten ---------------------------------------------------------------------


def take_messages(runner_id: int) -> list[tuple[int, str, dict]]:
    """Wartende Rahmen an den Runner lesen und löschen: [(id, Antwortkanal, Rahmen)]."""
    fresh()
    query = sql.SQL(
        "DELETE FROM {} WHERE runner_id = %s AND direction = %s "
        "RETURNING id, reply_channel, payload"
    ).format(sql.Identifier(RunnerMessage._meta.db_table))
    with connections[DEFAULT_DB_ALIAS].cursor() as cursor:
        cursor.execute(query, [runner_id, RunnerMessage.Direction.TO_RUNNER])
        rows = cursor.fetchall()
    out = []
    for pk, channel, payload in sorted(rows, key=lambda row: row[0]):
        if isinstance(payload, str | bytes):  # Django lädt jsonb als Text
            payload = json.loads(payload)
        out.append((pk, channel, payload))
    return out


def post_reply(runner_id: int, channel: str, frame: dict) -> None:
    """Rahmen vom Agent an einen Prozess (Antwortkanal) weitergeben."""
    if not channel:
        return
    fresh()
    payload = {key: value for key, value in frame.items() if value is not None}
    pipe = None
    if frame.get("type") == "mcp":
        pipe = _session_uuid(frame["pipe"])
    with transaction.atomic():
        message = RunnerMessage.objects.create(
            runner_id=runner_id,
            direction=RunnerMessage.Direction.TO_SERVER,
            pipe=pipe,
            reply_channel=channel,
            kind=frame["type"],
            payload=payload,
        )
        with connections[DEFAULT_DB_ALIAS].cursor() as cursor:
            cursor.execute("SELECT pg_notify(%s, %s)", [channel, str(message.pk)])


def notify_runner(runner_id: int) -> None:
    """Runner-Kanal wecken (z. B. nach dem LISTEN, um Verpasstes zu holen)."""
    with connections[DEFAULT_DB_ALIAS].cursor() as cursor:
        cursor.execute("SELECT pg_notify(%s, '')", [runner_channel(runner_id)])


# --- Sitzungen -----------------------------------------------------------------------


def own_session(runner_id: int, session_id: str) -> RunnerSession | None:
    key = _session_uuid(session_id)
    if key is None:
        return None
    return (
        RunnerSession.objects.select_related("owner", "owner__role")
        .filter(runner_id=runner_id, uuid=key)
        .first()
    )


def sessions_of_user(runner_id: int, user_id: int) -> list[str]:
    """Aktive Sitzungen eines Kontos auf diesem Runner (für den Stopp wegen Kontingent)."""
    fresh()
    return [
        str(value)
        for value in RunnerSession.objects.filter(
            runner_id=runner_id, owner_id=user_id, status__in=RunnerSession.ACTIVE_STATES
        ).values_list("uuid", flat=True)
    ]


def mark_quota_stop(session_ids: list[str]) -> None:
    keys = [key for key in map(_session_uuid, session_ids) if key]
    RunnerSession.objects.filter(uuid__in=keys, quota_stop_sent__isnull=True).update(
        quota_stop_sent=timezone.now(), status=RunnerSession.Status.STOPPING
    )


def still_running(runner_id: int, session_ids: list[str]) -> bool:
    """Ist eine der Sitzungen noch nicht beendet (Stopp unbestätigt)?"""
    fresh()
    keys = [key for key in map(_session_uuid, session_ids) if key]
    return RunnerSession.objects.filter(
        runner_id=runner_id, uuid__in=keys, ended__isnull=True
    ).exists()


def _limits_mismatch(requested: dict, actual: dict | None) -> list[str]:
    """Abweichungen der Ist- von den Soll-Grenzen (``limits.mismatches``).

    Im Protokoll ist ``network`` ein Modus (``none``/``allowlist``/``default``),
    in den Soll-Grenzen ein Wahrheitswert (Netz an/aus). Die Positivliste
    (``allowlist``, nur freigegebene Hosts über das Relay, 8w) ist kein freies Netz.
    """
    if not actual or not requested:
        return []
    actual = dict(actual)
    if isinstance(actual.get("network"), str):
        actual["network"] = actual["network"] == "default"
    return limits.mismatches(requested, actual)


def session_state(runner_id: int, frame: dict) -> list[dict]:
    """``session_state`` übernehmen; liefert Rahmen an den Agent (Stopp bei Abweichung)."""
    fresh()
    session = own_session(runner_id, frame["session"])
    if session is None:
        if frame["state"] in ("stopped", "failed"):
            return []
        return [{"type": "session_stop", "session": frame["session"], "reason": "error"}]
    state = frame["state"]
    if state in ("stopped", "failed"):
        if session.ended is None:
            _end_session(session, state, frame.get("error") or "", None)
        return []
    if session.ended is not None:
        return [{"type": "session_stop", "session": frame["session"], "reason": "error"}]
    fields = ["status"]
    session.status = state if state in RunnerSession.Status.values else session.status
    if frame.get("limits"):
        session.limits_actual = frame["limits"]
        fields.append("limits_actual")
    if frame.get("preview_port") is not None:
        session.preview_port = frame["preview_port"]
        fields.append("preview_port")
    session.save(update_fields=fields)
    wrong = _limits_mismatch(session.limits_requested or {}, frame.get("limits"))
    if wrong:
        _audit(
            RunnerAudit.Kind.LIMITS,
            owner=session.owner,
            session=session,
            runner=session.runner,
            message="Ist-Grenzen weichen ab: " + ", ".join(wrong),
        )
        session.status = RunnerSession.Status.STOPPING
        session.stop_reason = "Grenzen weichen ab: " + ", ".join(wrong)
        session.save(update_fields=["status", "stop_reason"])
        return [{"type": "session_stop", "session": frame["session"], "reason": "mismatch"}]
    return []


def preview(runner_id: int, frame: dict) -> None:
    """Vorschau-Status an der Sitzung festhalten: nur der Port.

    Das Token des Agents (für den lokalen Port des Rechners) bleibt beim Agent;
    ``RunnerSession.preview_token`` erzeugt der Server selbst (Pfad der Vorschau
    in MultiGPT).
    """
    fresh()
    session = own_session(runner_id, frame["session"])
    if session is None or session.ended is not None:
        return
    session.preview_port = frame["port"] if frame["running"] else None
    session.save(update_fields=["preview_port"])


# --- Bundles -------------------------------------------------------------------------


@dataclass
class BundleOut:
    data: bytes | None
    sha256: str = ""
    head: str = ""
    error: str = ""


def load_bundle(runner_id: int, session_id: str) -> BundleOut:
    """Bundle des Arbeitsstands der Sitzung (nur an den Runner der Sitzung)."""
    fresh()
    session = own_session(runner_id, session_id)
    if session is None or session.ended is not None or session.workspace_id is None:
        return BundleOut(None, error="Sitzung unbekannt oder ohne Arbeitsstand.")
    workspace = (
        RunnerWorkspace.objects.with_bundle()
        .filter(pk=session.workspace_id, owner_id=session.owner_id)
        .first()
    )
    if workspace is None or not workspace.bundle:
        return BundleOut(None, error="Der Arbeitsstand hat kein Bundle.")
    data = bytes(workspace.bundle)
    return BundleOut(data, hashlib.sha256(data).hexdigest(), workspace.head_commit)


def bundle_limit(runner_id: int, session_id: str) -> int:
    """Höchstgröße eines hochgeladenen Bundles für diese Sitzung (Rolle)."""
    fresh()
    session = own_session(runner_id, session_id)
    if session is None:
        return _setting("RUNNER_WORKSPACE_MAX_MB", 50) * 1024 * 1024
    from . import bundles

    return min(bundles.max_bytes(session.owner), protocol.MAX_BUNDLE_BYTES)


@dataclass
class BundleIn:
    ok: bool
    head: str = ""
    error: str = ""


def accept_bundle(
    runner_id: int, session_id: str, data: bytes, sha256: str, head: str, message: str
) -> BundleIn:
    """Hochgeladenes Bundle an die Annahme (``bundles.accept`` von ``tools``) geben."""
    fresh()
    session = own_session(runner_id, session_id)
    if session is None or session.ended is not None or session.workspace_id is None:
        return BundleIn(False, error="Sitzung unbekannt oder beendet.")
    from . import bundles

    try:
        result = bundles.accept(
            session.workspace_id,
            data,
            sha256,
            session_id=session.pk,
            head=head or "",
            message=message or "",
        )
    except Exception:
        logger.exception("Bundle der Sitzung %s nicht angenommen", session.pk)
        return BundleIn(False, error="Das Bundle konnte nicht angenommen werden.")
    if not getattr(result, "ok", False):
        return BundleIn(False, error=getattr(result, "message", "") or "Bundle abgelehnt.")
    return BundleIn(True, head=getattr(result, "head", "") or head or "")
