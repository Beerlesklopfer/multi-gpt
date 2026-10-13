"""Zeitkontingent „Laufzeit“ des Runners (M14-11).

Gezählt werden Container-Minuten je Konto und Kalendermonat (Europe/Berlin)
auf dem einzigen Laufzeitkonto (``BillingAccount.Kind.RUNTIME``). Das
Kontingent steht in ``AccountBudget.monthly_minutes`` (Person vor Rolle, leer =
unbegrenzt) und läuft über ``budgets.snapshot`` (Einheit „min“) wie die
übrigen Budgets.

Regeln (Plan M14, „Zeitkontingent im Kontenrahmen“):

- **Maßgeblich ist die Serverzeit.** Jeder Heartbeat, der eine Sitzung als
  „läuft“ meldet, zählt die Zeit seit dem letzten gezählten Zeitpunkt
  (``RunnerSession.counted_until``, beim ersten Mal ``started``), höchstens
  ``2 × HEARTBEAT_SECONDS``. Lücken (Runner getrennt, Heartbeats verloren)
  zählen so nicht mit.
- **Eine Buchung je Sitzung und Monat** (``UsageEntry.Kind.RUNTIME``,
  ``units={"runtime_minute": n, "seconds": s}``), fortgeschrieben über
  ``RunnerSession.usage_entry``. Solange die Sitzung läuft, zählen nur volle
  Minuten; am Sitzungsende (und beim Monatswechsel für den alten Monat) wird
  auf die angefangene Minute aufgerundet.
- **Durchsetzung:** ab 80 % Hinweis (Chat, Seite „Runner“, Kopfzeile), bei
  100 % Stopp aller Sitzungen des Kontos über ``mgpt_runner_ctl`` und keine
  neuen Starts (``runtime_allows``). Die Arbeitsstände bleiben erhalten.

Aufgerufen wird ``record_heartbeat`` aus dem Runner-Dienst (über
``sync_to_async``) und ``finish_session`` beim Sitzungsende bzw. bei der
Bestätigung des Stopps.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from . import budgets
from .models import BillingAccount, UsageEntry

log = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 30
MAX_GAP_SECONDS = 2 * HEARTBEAT_SECONDS
RUNTIME_ACCOUNT_NAME = "Runner-Laufzeit"
UNIT = "runtime_minute"


# --- Konto und Stand ---------------------------------------------------------------


def runtime_account() -> BillingAccount:
    """Das Laufzeitkonto; fehlt es (z. B. gelöscht), wird es neu angelegt."""
    account = BillingAccount.objects.filter(kind=BillingAccount.Kind.RUNTIME).first()
    if account is not None:
        return account
    name = RUNTIME_ACCOUNT_NAME
    if BillingAccount.objects.filter(name=name).exists():
        name = f"{RUNTIME_ACCOUNT_NAME} (Container)"
    account, _ = BillingAccount.objects.get_or_create(
        name=name, defaults={"kind": BillingAccount.Kind.RUNTIME, "currency": ""}
    )
    return account


@dataclass(frozen=True)
class RuntimeState:
    """Laufzeit im Monat: ``spent``/``limit`` in Minuten, ``limit`` None = unbegrenzt."""

    spent: int
    limit: int | None
    level: str

    @property
    def percent(self) -> int | None:
        if self.limit is None:
            return None
        return 100 if self.limit <= 0 else int(self.spent * 100 / self.limit)

    @property
    def remaining(self) -> int | None:
        return None if self.limit is None else max(self.limit - self.spent, 0)

    @property
    def exhausted(self) -> bool:
        return self.level == budgets.LEVEL_EXHAUSTED

    @property
    def warning(self) -> bool:
        return self.level == budgets.LEVEL_WARNING

    def message(self) -> str:
        """Hinweis ab 80 % bzw. Sperrtext; leer bei Stufe ok."""
        if self.exhausted:
            return budgets.RUNTIME_EXHAUSTED_MESSAGE
        if not self.warning:
            return ""
        from multigpt.accounts.usage import format_minutes

        return (
            f"{self.percent} % deines Laufzeit-Kontingents (Runner) sind verbraucht "
            f"({self.spent} von {format_minutes(self.limit)})."
        )

    def as_dict(self) -> dict:
        return {
            "spent_minutes": self.spent,
            "limit_minutes": self.limit,
            "remaining_minutes": self.remaining,
            "percent": self.percent,
            "level": self.level,
            "message": self.message(),
        }


def limit_for(user) -> int | None:
    """Wirksames Kontingent in Minuten (Person vor Rolle); None = unbegrenzt."""
    account = BillingAccount.objects.filter(kind=BillingAccount.Kind.RUNTIME).first()
    if account is None:
        return None
    row = budgets.limits_for(user).get(account.pk)
    return None if row is None else row.monthly_minutes


def minutes_used(user, now=None) -> int:
    """Gebuchte Minuten von ``user`` im Kalendermonat von ``now``."""
    from multigpt.accounts.usage import month_bounds

    start, end = month_bounds(now)
    row = UsageEntry.objects.filter(
        user_id=user.pk, kind=UsageEntry.Kind.RUNTIME, created__gte=start, created__lt=end
    ).aggregate(minutes=budgets.minutes_sum())
    return int(row["minutes"] or 0)


def runtime_state(user, now=None) -> RuntimeState:
    """Laufzeit-Stand des Kontos im laufenden Monat (zwei Abfragen)."""
    limit = limit_for(user)
    spent = minutes_used(user, now)
    return RuntimeState(spent, limit, budgets.level_for(spent, limit))


def runtime_allows(user, now=None) -> bool:
    """Darf eine neue Sitzung starten? Nein, wenn das Kontingent ausgeschöpft ist."""
    return not runtime_state(user, now).exhausted


# --- Buchung -----------------------------------------------------------------------


def _month_start(when: dt.datetime) -> dt.datetime:
    from multigpt.accounts.usage import month_bounds

    return month_bounds(when)[0]


def _entry_minutes(seconds: int, *, closed: bool) -> int:
    return math.ceil(seconds / 60) if closed else seconds // 60


def _locked(session):
    """Sitzung frisch und gesperrt laden (Heartbeats und Ende können sich überholen)."""
    return type(session).objects.select_for_update().get(pk=session.pk)


def _add_seconds(session, seconds: int, when: dt.datetime) -> None:
    """``seconds`` auf die Buchung des Monats von ``when`` schreiben.

    Gehört die bisherige Buchung zu einem früheren Monat, wird sie
    abgeschlossen (aufgerundet) und eine neue begonnen.
    """
    entry = None
    if session.usage_entry_id:
        entry = UsageEntry.objects.select_for_update().filter(pk=session.usage_entry_id).first()
    month = _month_start(when)
    if entry is not None and entry.created < month:
        _close_entry(session, entry)
        entry = None
    if seconds <= 0 and entry is None:
        return
    if entry is None:
        entry = UsageEntry.objects.create(
            kind=UsageEntry.Kind.RUNTIME,
            account=runtime_account(),
            user_id=session.owner_id,
            model_name="Runner",
            created=max(month, session.started) if session.started else when,
            units={UNIT: 0, "seconds": 0},
        )
        session.usage_entry = entry
    units = dict(entry.units or {})
    old_minutes = int(units.get(UNIT, 0))
    total = int(units.get("seconds", 0)) + max(int(seconds), 0)
    new_minutes = _entry_minutes(total, closed=False)
    units.update({UNIT: max(new_minutes, old_minutes), "seconds": total})
    entry.units = units
    entry.save(update_fields=["units"])
    session.minutes += max(new_minutes - old_minutes, 0)


def _close_entry(session, entry: UsageEntry) -> None:
    """Buchung abschließen: auf die angefangene Minute aufrunden."""
    units = dict(entry.units or {})
    old_minutes = int(units.get(UNIT, 0))
    closed = _entry_minutes(int(units.get("seconds", 0)), closed=True)
    if closed > old_minutes:
        units[UNIT] = closed
        entry.units = units
        entry.save(update_fields=["units"])
        session.minutes += closed - old_minutes


def _count(session, now: dt.datetime) -> None:
    """Zeit seit dem letzten gezählten Zeitpunkt (höchstens 2 × Intervall) buchen."""
    since = session.counted_until or session.started or now
    seconds = (now - since).total_seconds()
    seconds = int(min(max(seconds, 0), MAX_GAP_SECONDS))
    month = _month_start(now)
    if seconds and now - dt.timedelta(seconds=seconds) < month:
        # Monatswechsel im Intervall: den Teil davor dem alten Monat gutschreiben.
        before = int((month - (now - dt.timedelta(seconds=seconds))).total_seconds())
        _add_seconds(session, before, month - dt.timedelta(microseconds=1))
        seconds -= before
    _add_seconds(session, seconds, now)
    session.counted_until = now


def record_heartbeat(session, running: bool, now=None) -> RuntimeState:
    """Heartbeat einer Sitzung buchen und den Stand des Kontos liefern.

    ``running``: meldet der Agent die Sitzung als laufend? Sonst wird nur der
    Zeitpunkt festgehalten (nichts gezählt). Ist das Kontingent danach
    ausgeschöpft, geht der Stopp an alle Sitzungen des Kontos
    (``enforce``). Beendete Sitzungen zählen nie.
    """
    now = now or timezone.now()
    with transaction.atomic():
        current = _locked(session)
        if current.ended is None:
            if running:
                _count(current, now)
            else:
                current.counted_until = now
            current.save(update_fields=["counted_until", "minutes", "usage_entry"])
    _copy(current, session)
    state = runtime_state(current.owner, now)
    if state.exhausted and running and current.ended is None:
        enforce(current.owner, state)
    return state


def finish_session(session, now=None, *, count: bool = True) -> int:
    """Sitzungsende: Restzeit zählen (``count``) und auf die Minute aufrunden.

    Setzt ``ended`` (Status setzt der Aufrufer). Liefert die Minuten der Sitzung
    gesamt; ein weiterer Aufruf nach dem Ende zählt nichts mehr.
    """
    now = now or timezone.now()
    with transaction.atomic():
        current = _locked(session)
        if current.ended is not None:
            return current.minutes
        if count and current.status == current.Status.RUNNING:
            _count(current, now)
        current.counted_until = now
        current.ended = now
        if current.usage_entry_id:
            entry = UsageEntry.objects.select_for_update().get(pk=current.usage_entry_id)
            _close_entry(current, entry)
        current.save(update_fields=["counted_until", "minutes", "usage_entry", "ended"])
    _copy(current, session)
    return current.minutes


def _copy(source, target) -> None:
    if source is not target:
        for name in ("counted_until", "minutes", "usage_entry_id", "ended"):
            setattr(target, name, getattr(source, name))


# --- Durchsetzung ------------------------------------------------------------------


def enforce(user, state: RuntimeState | None = None) -> bool:
    """Bei ausgeschöpftem Kontingent alle Sitzungen des Kontos stoppen.

    Der Stopp läuft über ``mgpt_runner_ctl`` (Runner-Dienst sendet
    ``session_stop`` mit Grund ``quota``); Arbeitsstände bleiben erhalten.
    Liefert True, wenn ein Stopp angefordert wurde.
    """
    state = state or runtime_state(user)
    if not state.exhausted:
        return False
    _stop_sessions(user.pk)
    return True


def _stop_sessions(user_id: int) -> None:
    try:
        from multigpt.runner import control
    except ImportError:  # Steuerung (gateway) noch nicht vorhanden
        log.warning("Laufzeit ausgeschöpft, aber keine Runner-Steuerung vorhanden.")
        return
    control.stop_user_sessions(user_id, reason="quota")
