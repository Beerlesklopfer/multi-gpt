"""Zeitkontingent „Laufzeit“ des Runners (M14-11).

Heartbeats mit gefälschter Uhr; Lücken über 2 × Intervall zählen nicht;
Monatswechsel; Person vor Rolle; Stopp bei 100 %; Arbeitsstand bleibt.
"""

import datetime as dt

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from multigpt.accounts import usage
from multigpt.accounts.models import Role
from multigpt.billing import budgets, runtime
from multigpt.billing.models import AccountBudget, BillingAccount, UsageEntry
from multigpt.runner.models import RunnerSession, RunnerWorkspace

pytestmark = pytest.mark.django_db

BERLIN = usage.USAGE_TIME_ZONE
T0 = dt.datetime(2026, 10, 13, 12, 0, tzinfo=BERLIN)


def at(seconds: float) -> dt.datetime:
    return T0 + dt.timedelta(seconds=seconds)


@pytest.fixture
def adult(django_user_model, password):
    role = Role.objects.get(key=Role.ADULT)
    return django_user_model.objects.create_user(username="erik", password=password, role=role)


@pytest.fixture
def workspace(adult):
    return RunnerWorkspace.objects.create(owner=adult, name="spiel")


def make_session(user, workspace=None, started=T0):
    return RunnerSession.objects.create(
        owner=user,
        workspace=workspace,
        status=RunnerSession.Status.RUNNING,
        started=started,
    )


@pytest.fixture
def stops(monkeypatch):
    """Stopp-Aufrufe abfangen (Steuerung über mgpt_runner_ctl gehört gateway)."""
    calls = []
    monkeypatch.setattr(runtime, "_stop_sessions", lambda user_id: calls.append(user_id))
    return calls


def set_limit(minutes, *, role=None, user=None):
    account = runtime.runtime_account()
    AccountBudget.objects.update_or_create(
        account=account, role=role, user=user, defaults={"monthly_minutes": minutes}
    )


def entry_of(session) -> UsageEntry:
    session.refresh_from_db()
    return UsageEntry.objects.get(pk=session.usage_entry_id)


# --- Konto und Migration ----------------------------------------------------------------


def test_migration_creates_runtime_account_with_adult_quota():
    account = BillingAccount.objects.get(kind=BillingAccount.Kind.RUNTIME)
    assert account.currency == ""
    row = AccountBudget.objects.get(account=account, role__key=Role.ADULT)
    assert row.monthly_minutes == 600
    assert not AccountBudget.objects.filter(account=account, role__key=Role.TEEN).exists()


def test_only_one_runtime_account():
    other = BillingAccount(name="Zweite Laufzeit", kind=BillingAccount.Kind.RUNTIME)
    with pytest.raises(ValidationError):
        other.full_clean()


def test_budget_kinds_are_checked():
    account = runtime.runtime_account()
    role = Role.objects.get(key=Role.TEEN)
    with pytest.raises(ValidationError):
        AccountBudget(account=account, role=role, monthly_tokens=10).full_clean()
    tokens = BillingAccount.objects.create(name="Lokal-Test", kind="tokens")
    with pytest.raises(ValidationError):
        AccountBudget(account=tokens, role=role, monthly_minutes=10).full_clean()
    AccountBudget(account=account, role=role, monthly_minutes=10).full_clean()


# --- Zählen -------------------------------------------------------------------------------


def test_heartbeats_count_server_time(adult, workspace, stops):
    session = make_session(adult, workspace)
    for i in range(1, 9):  # 8 × 30 s = 4 min
        runtime.record_heartbeat(session, True, now=at(30 * i))
    session.refresh_from_db()
    assert session.minutes == 4
    assert session.counted_until == at(240)
    entry = entry_of(session)
    assert entry.kind == UsageEntry.Kind.RUNTIME
    assert entry.user_id == adult.pk
    assert entry.units["runtime_minute"] == 4
    assert entry.account.kind == BillingAccount.Kind.RUNTIME
    # Eine Buchung je Sitzung und Monat, keine Zeile je Minute.
    assert UsageEntry.objects.filter(kind="runtime").count() == 1


def test_gap_longer_than_two_intervals_is_capped(adult, workspace, stops):
    session = make_session(adult, workspace)
    runtime.record_heartbeat(session, True, now=at(30))
    runtime.record_heartbeat(session, True, now=at(30 + 3600))  # eine Stunde Funkstille
    session.refresh_from_db()
    assert entry_of(session).units["seconds"] == 30 + runtime.MAX_GAP_SECONDS


def test_not_running_heartbeat_does_not_count(adult, workspace, stops):
    session = make_session(adult, workspace)
    runtime.record_heartbeat(session, False, now=at(30))
    runtime.record_heartbeat(session, False, now=at(60))
    session.refresh_from_db()
    assert session.minutes == 0
    assert session.usage_entry_id is None


def test_finish_rounds_up_and_is_idempotent(adult, workspace, stops):
    session = make_session(adult, workspace)
    runtime.record_heartbeat(session, True, now=at(30))
    runtime.record_heartbeat(session, True, now=at(60))
    runtime.record_heartbeat(session, True, now=at(90))  # 1:30
    assert runtime.finish_session(session, now=at(100)) == 2  # 1:40 -> 2 min
    assert runtime.finish_session(session, now=at(400)) == 2
    assert entry_of(session).units["runtime_minute"] == 2


def test_ended_session_never_counts(adult, workspace, stops):
    session = make_session(adult, workspace)
    RunnerSession.objects.filter(pk=session.pk).update(ended=at(10))
    runtime.record_heartbeat(session, True, now=at(30))
    session.refresh_from_db()
    assert session.minutes == 0


def test_month_change_splits_bookings(adult, workspace, stops):
    end_of_month = dt.datetime(2026, 10, 31, 23, 59, 30, tzinfo=BERLIN)
    session = make_session(adult, workspace, started=end_of_month - dt.timedelta(minutes=3))
    t = end_of_month - dt.timedelta(minutes=3)
    for _ in range(12):  # 6 min, davon 3 im Oktober (bis 23:59:30) + 30 s + 2:30 im November
        t += dt.timedelta(seconds=30)
        runtime.record_heartbeat(session, True, now=t)
    entries = list(UsageEntry.objects.filter(kind="runtime").order_by("created"))
    assert len(entries) == 2
    october, november = entries
    assert october.created.astimezone(BERLIN).month == 10
    assert november.created.astimezone(BERLIN).month == 11
    assert october.units["seconds"] == 210
    assert october.units["runtime_minute"] == 4  # beim Monatswechsel aufgerundet
    assert november.units["seconds"] == 150
    oct_now = dt.datetime(2026, 10, 15, tzinfo=BERLIN)
    nov_now = dt.datetime(2026, 11, 15, tzinfo=BERLIN)
    assert runtime.minutes_used(adult, oct_now) == 4
    assert runtime.minutes_used(adult, nov_now) == 2


# --- Kontingent ---------------------------------------------------------------------------


def test_person_before_role(adult):
    assert runtime.limit_for(adult) == 600
    set_limit(30, user=adult)
    assert runtime.limit_for(adult) == 30


def test_empty_means_unlimited(adult, workspace, stops):
    set_limit(None, role=adult.role)
    session = make_session(adult, workspace)
    for i in range(1, 50):
        runtime.record_heartbeat(session, True, now=at(30 * i))
    state = runtime.runtime_state(adult, now=at(1500))
    assert state.limit is None and state.level == budgets.LEVEL_OK
    assert runtime.runtime_allows(adult, now=at(1500))
    assert stops == []


def test_warning_from_80_percent(adult, workspace, stops):
    set_limit(5, user=adult)
    session = make_session(adult, workspace)
    for i in range(1, 9):  # 4 von 5 min
        state = runtime.record_heartbeat(session, True, now=at(30 * i))
    assert state.level == budgets.LEVEL_WARNING
    assert state.percent == 80
    assert "80 %" in state.message()
    assert stops == []


def test_stop_at_100_percent_keeps_workspace(adult, workspace, stops):
    set_limit(2, user=adult)
    session = make_session(adult, workspace)
    for i in range(1, 5):
        state = runtime.record_heartbeat(session, True, now=at(30 * i))
    assert state.exhausted
    assert stops == [adult.pk]
    assert not runtime.runtime_allows(adult, now=at(130))
    assert state.message() == budgets.RUNTIME_EXHAUSTED_MESSAGE
    assert RunnerWorkspace.objects.filter(pk=workspace.pk).exists()


def test_zero_quota_blocks_start(adult):
    set_limit(0, user=adult)
    assert not runtime.runtime_allows(adult, now=T0)


def test_other_account_is_independent(adult, workspace, django_user_model, stops):
    other = django_user_model.objects.create_user(
        username="olga", password="x-Geheim-1234", role=adult.role
    )
    set_limit(1, user=other)
    session = make_session(adult, workspace)
    for i in range(1, 5):
        runtime.record_heartbeat(session, True, now=at(30 * i))
    assert runtime.minutes_used(other, now=at(200)) == 0
    assert runtime.runtime_allows(other, now=at(200))


# --- Anzeige ------------------------------------------------------------------------------


def _book(user, minutes, when=None):
    return UsageEntry.objects.create(
        kind=UsageEntry.Kind.RUNTIME,
        account=runtime.runtime_account(),
        user=user,
        model_name="Runner",
        units={"runtime_minute": minutes, "seconds": minutes * 60},
        **({"created": when} if when else {}),
    )


def test_snapshot_and_banner_know_minutes(adult):
    _book(adult, 500)
    snap = budgets.snapshot(adult)
    states = [s for s in snap.states() if s.unit == "min"]
    assert len(states) == 1
    assert states[0].spent == 500 and states[0].limit == 600
    assert states[0].level == budgets.LEVEL_WARNING
    assert "Laufzeit" in states[0].warning_text()


def test_usage_page_shows_minutes(client, adult, password):
    _book(adult, 42)
    client.force_login(adult)
    response = client.get(reverse("usage"))
    body = response.content.decode()
    assert "42 Minuten" in body
    assert "600 Minuten" in body
    assert "Container-Laufzeit" in body
    # Laufzeit erscheint nicht als „gelöschtes Modell“.
    assert "Gelöschtes Modell" not in body


def test_usage_api_lists_runtime_account(client, adult, password):
    _book(adult, 7)
    client.force_login(adult)
    data = client.get(reverse("api_usage")).json()
    runtime_rows = [a for a in data["accounts"] if a["unit"] == "min"]
    assert runtime_rows and runtime_rows[0]["spent"] == "7"
    assert all(m["model"] != "Gelöschtes Modell" for m in data["models"])


def test_family_views_show_minutes(client, adult, django_user_model, password):
    admin = django_user_model.objects.create_user(
        username="mama", password=password, role=Role.objects.get(key=Role.ADMIN)
    )
    _book(adult, 13)
    client.force_login(admin)
    detail = client.get(reverse("family:member", args=[adult.pk])).content.decode()
    assert "Laufzeit 13 Minuten" in detail
    assert "13 von 600 Minuten" in detail
    overview = client.get(reverse("family:usage")).content.decode()
    assert "Laufzeit 13 Minuten" in overview
