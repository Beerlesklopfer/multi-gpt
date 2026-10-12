"""EUR-Konten auf USD umstellen (A10) und täglicher EZB-Abruf im Worker (A11).

Keine echten Netzaufrufe: Die EZB wird über respx gemockt.
"""

import datetime as dt
import io
import logging
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse
from django.utils import timezone

from multigpt.accounts import usage
from multigpt.accounts.models import Role, User
from multigpt.billing import booking, budgets, ecb, usd_switch
from multigpt.billing.models import AccountBudget, BillingAccount, ExchangeRate, ModelPrice
from multigpt.billing.pricing import Tally
from multigpt.chat.models import AIModel, Conversation, Message, Provider
from multigpt.chat.providers.base import Usage
from tests.billing_helpers import set_price

pytestmark = pytest.mark.django_db

BERLIN = ZoneInfo("Europe/Berlin")


def berlin(*args):
    return dt.datetime(*args, tzinfo=BERLIN)


def ecb_body(day: str, eur_usd: str = "1.1630") -> str:
    return (
        "<gesmes:Envelope><Cube><Cube time='" + day + "'>"
        "<Cube currency='USD' rate='" + eur_usd + "'/></Cube></Cube></gesmes:Envelope>"
    )


@pytest.fixture
def openai():
    return Provider.objects.create(name="OpenAI", kind=Provider.Kind.OPENAI_COMPAT)


@pytest.fixture
def gpt(openai):
    model = AIModel.objects.create(provider=openai, model_id="gpt-x", display_name="gpt-x")
    set_price(
        model,
        "3",
        "15",
        currency="EUR",
        cached_input="0.3",
        unit_prices={"web_search": 0.01, "image": "0.2"},
    )
    return model


@pytest.fixture
def unpriced(openai):
    return AIModel.objects.create(provider=openai, model_id="mini", display_name="mini")


@pytest.fixture
def member():
    role = Role.objects.get(key="adult")
    role.all_models = True
    role.save()
    return User.objects.create_user("mitglied", password="Geheim-Test-1234", role=role)


def book(user, model, when, tokens_in=1_000_000, tokens_out=0):
    msg = Message.objects.create(
        conversation=Conversation.objects.create(user=user),
        role="assistant",
        content="x",
        model=model,
    )
    Message.objects.filter(pk=msg.pk).update(created=when)
    msg.refresh_from_db()
    tally = Tally()
    tally.add(Usage(tokens_in, tokens_out))
    return booking.book_answer(msg, model, tally)


# --- A11: täglicher EZB-Abruf -------------------------------------------------------


def test_expected_day_weekdays_weekend_and_publish_time():
    # Montag 12.10.2026
    assert ecb.expected_day(berlin(2026, 10, 12, 10, 0)) == dt.date(2026, 10, 9)  # Freitag
    assert ecb.expected_day(berlin(2026, 10, 12, 16, 30)) == dt.date(2026, 10, 12)
    assert ecb.expected_day(berlin(2026, 10, 13, 9, 0)) == dt.date(2026, 10, 12)
    assert ecb.expected_day(berlin(2026, 10, 10, 18, 0)) == dt.date(2026, 10, 9)  # Samstag
    assert ecb.expected_day(berlin(2026, 10, 11, 18, 0)) == dt.date(2026, 10, 9)  # Sonntag


def test_default_setting_is_on(settings):
    del settings.BILLING_ECB_FETCH  # ohne Einstellung: an (Vorgabe in settings.py ebenso)
    assert ecb.enabled()


def test_auto_fetch_disabled_makes_no_request(settings):
    settings.BILLING_ECB_FETCH = False
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(ecb.ECB_URL)
        assert ecb.AutoFetch().run(berlin(2026, 10, 12, 17, 0)) is None
    assert not route.called


def test_auto_fetch_once_and_fills_missing_eur(settings, gpt, member):
    settings.BILLING_ECB_FETCH = True
    BillingAccount.objects.filter(pk=gpt.provider.billing_account_id).update(currency="USD")
    gpt.provider.billing_account.refresh_from_db()
    entry = book(member, gpt, berlin(2026, 10, 12, 11, 0))
    assert entry.amount is not None and entry.amount_eur is None  # noch kein Kurs
    auto = ecb.AutoFetch()
    now = berlin(2026, 10, 12, 17, 0)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(ecb.ECB_URL).mock(
            return_value=httpx.Response(200, text=ecb_body("2026-10-12"))
        )
        rate = auto.run(now)
        # Kurs liegt vor: weitere Durchläufe fragen nicht mehr.
        assert auto.run(now + dt.timedelta(minutes=1)) is None
        assert auto.run(berlin(2026, 10, 13, 9, 0)) is None
    assert route.call_count == 1
    assert rate.date == dt.date(2026, 10, 12) and rate.source == ExchangeRate.Source.ECB
    entry.refresh_from_db()
    assert entry.amount_eur == (entry.amount * Decimal("0.859845")).quantize(Decimal("0.000001"))


def test_auto_fetch_catch_up_before_publish_time(settings):
    """Erster Durchlauf am Morgen, Kurs des letzten Werktags fehlt: sofort holen."""
    settings.BILLING_ECB_FETCH = True
    ExchangeRate.objects.create(date=dt.date(2026, 10, 1), usd_eur=Decimal("0.85"))
    with respx.mock(assert_all_called=True) as mock:
        mock.get(ecb.ECB_URL).mock(return_value=httpx.Response(200, text=ecb_body("2026-10-09")))
        assert ecb.AutoFetch().run(berlin(2026, 10, 12, 8, 0)).date == dt.date(2026, 10, 9)
    # Kurs vom Freitag liegt vor: vor 16:30 kein Abruf.
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(ecb.ECB_URL)
        assert ecb.AutoFetch().run(berlin(2026, 10, 12, 12, 0)) is None
    assert not route.called


def test_auto_fetch_errors_only_warn_with_backoff(settings, caplog):
    settings.BILLING_ECB_FETCH = True
    auto = ecb.AutoFetch()
    now = berlin(2026, 10, 12, 17, 0)
    with respx.mock() as mock:
        route = mock.get(ecb.ECB_URL).mock(side_effect=httpx.ConnectError("kein Netz"))
        with caplog.at_level(logging.WARNING, logger="multigpt.billing.ecb"):
            assert auto.run(now) is None
        assert auto.next_try == now + dt.timedelta(minutes=5)
        assert auto.run(now + dt.timedelta(minutes=4)) is None  # Backoff
        assert route.call_count == 1
        assert auto.run(now + dt.timedelta(minutes=5)) is None
        assert auto.next_try == now + dt.timedelta(minutes=15)  # 10 Min. Abstand
        assert route.call_count == 2
    assert "EZB-Kurs nicht abrufbar" in caplog.text
    # Unerwartete Fehler stören den Worker ebenfalls nicht.
    auto = ecb.AutoFetch()
    with respx.mock() as mock:
        mock.get(ecb.ECB_URL).mock(side_effect=RuntimeError("kaputt"))
        assert auto.run(now) is None
    assert auto.failures == 1
    # Backoff wächst höchstens bis 6 Stunden.
    auto.failures = 20
    auto.next_try = None
    with respx.mock() as mock:
        mock.get(ecb.ECB_URL).mock(return_value=httpx.Response(503))
        auto.run(now)
    assert auto.next_try == now + ecb.BACKOFF_MAX
    assert not ExchangeRate.objects.exists()


def test_auto_fetch_late_publication_and_holiday(settings):
    """EZB liefert nach 16:30 noch den Vortag: stündlich bis 20 Uhr, dann Ruhe."""
    settings.BILLING_ECB_FETCH = True
    auto = ecb.AutoFetch()
    with respx.mock() as mock:
        route = mock.get(ecb.ECB_URL).mock(
            return_value=httpx.Response(200, text=ecb_body("2026-10-09"))
        )
        auto.run(berlin(2026, 10, 12, 16, 45))
        assert auto.next_try == berlin(2026, 10, 12, 17, 45)
        assert auto.run(berlin(2026, 10, 12, 17, 0)) is None
        auto.run(berlin(2026, 10, 12, 20, 15))
        assert auto.settled == dt.date(2026, 10, 12)  # Feiertag o. Ä.
        assert auto.run(berlin(2026, 10, 12, 22, 0)) is None
        assert auto.run(berlin(2026, 10, 13, 9, 0)) is None  # erwartet weiter Montag
        assert route.call_count == 2
        auto.run(berlin(2026, 10, 13, 16, 40))  # neuer Tag, neuer Versuch
        assert route.call_count == 3


def test_manual_rate_of_same_day_is_kept(settings):
    settings.BILLING_ECB_FETCH = True
    ExchangeRate.objects.create(date=dt.date(2026, 10, 9), usd_eur=Decimal("0.9"))
    with respx.mock() as mock:
        mock.get(ecb.ECB_URL).mock(return_value=httpx.Response(200, text=ecb_body("2026-10-09")))
        ecb.AutoFetch().run(berlin(2026, 10, 12, 17, 0))  # erwartet Montag, EZB liefert Freitag
    assert ExchangeRate.objects.get().usd_eur == Decimal("0.9")


def test_worker_calls_auto_fetch(monkeypatch):
    from multigpt.chat.management.commands import run_worker

    calls = []
    monkeypatch.setattr(run_worker.billing_ecb, "auto_fetch", lambda: calls.append(1))
    call_command("run_worker", "--once")
    assert calls == [1]


def test_fetch_command_prints_new_rate(settings):
    settings.BILLING_ECB_FETCH = True
    out = io.StringIO()
    with respx.mock() as mock:
        mock.get(ecb.ECB_URL).mock(return_value=httpx.Response(200, text=ecb_body("2026-10-09")))
        call_command("fetch_ecb_rate", stdout=out)
    assert "09.10.2026" in out.getvalue() and "neu" in out.getvalue()


# --- A10: EUR-Konto auf USD umstellen -----------------------------------------------


def test_plan_and_apply_switch(gpt, unpriced, member):
    account = gpt.provider.billing_account
    old_entry = book(member, gpt, berlin(2026, 10, 1, 12, 0))
    assert (old_entry.currency, old_entry.amount, old_entry.amount_eur) == (
        "EUR",
        Decimal("3"),
        Decimal("3"),
    )
    ExchangeRate.objects.create(date=dt.date(2026, 10, 9), usd_eur=Decimal("0.86"))
    now = berlin(2026, 10, 12, 10, 0)
    old_price = ModelPrice.objects.get(ai_model=gpt)

    planned = usd_switch.plan(account, now)
    assert [c.ai_model for c in planned.changes] == [gpt]
    assert planned.without_price == [unpriced]
    assert not planned.rate_stale
    new = planned.changes[0].new
    assert new.input == Decimal("3.488372")  # 3 / 0,86
    assert new.output == Decimal("17.441860")
    assert new.cached_input == Decimal("0.348837")
    assert new.cache_write is None
    assert new.unit_prices == {"web_search": 0.011628, "image": 0.232558}
    assert ModelPrice.objects.count() == 1  # Vorschau speichert nichts

    assert usd_switch.apply(planned) == 1
    account.refresh_from_db()
    assert account.currency == "USD"
    assert ModelPrice.objects.filter(ai_model=gpt).count() == 2
    old_price.refresh_from_db()
    assert old_price.input == Decimal("3") and old_price.output == Decimal("15")
    stored = ModelPrice.objects.get(ai_model=gpt, valid_from=now)
    assert stored.input == Decimal("3.488372") and "0.86" in stored.note
    old_entry.refresh_from_db()
    assert (old_entry.currency, old_entry.amount, old_entry.amount_eur) == (
        "EUR",
        Decimal("3"),
        Decimal("3"),
    )
    # Neue Buchung: USD-Preis, EUR über den Kurs.
    gpt.provider.refresh_from_db()
    entry = book(member, gpt, berlin(2026, 10, 12, 11, 0))
    assert entry.currency == "USD" and entry.amount == Decimal("3.488372")
    assert entry.amount_eur == Decimal("3.000000")
    assert entry.price_id == stored.pk


def test_switch_refused_without_rate_or_for_usd_or_future_prices(gpt, openai):
    account = gpt.provider.billing_account
    with pytest.raises(usd_switch.SwitchError, match="kein Wechselkurs"):
        usd_switch.plan(account)
    ExchangeRate.objects.create(date=dt.date(2026, 1, 1), usd_eur=Decimal("0.86"))
    ModelPrice.objects.create(
        ai_model=gpt, valid_from=timezone.now() + dt.timedelta(days=3), input=Decimal("1")
    )
    with pytest.raises(usd_switch.SwitchError, match="Zukunft"):
        usd_switch.plan(account)
    usd = BillingAccount.objects.create(name="USD-Konto", kind="monetary", currency="USD")
    with pytest.raises(usd_switch.SwitchError, match="kein monetäres Konto in EUR"):
        usd_switch.plan(usd)
    tokens = BillingAccount.objects.create(name="Lokal", kind="tokens", currency="")
    with pytest.raises(usd_switch.SwitchError):
        usd_switch.plan(tokens)


def test_switch_keeps_budgets_in_eur(gpt, member):
    """Budgets stehen in EUR und zählen ``amount_eur``: Die Umstellung ändert sie nicht."""
    account = gpt.provider.billing_account
    AccountBudget.objects.create(account=account, user=member, monthly_budget=Decimal("10"))
    book(member, gpt, timezone.now() - dt.timedelta(minutes=5))  # 3 EUR
    ExchangeRate.objects.create(date=dt.date(2026, 1, 1), usd_eur=Decimal("0.5"))
    usd_switch.apply(usd_switch.plan(account))
    gpt.provider.refresh_from_db()
    book(member, gpt, timezone.now())  # 6 USD = 3 EUR
    budget = AccountBudget.objects.get()
    assert budget.monthly_budget == Decimal("10")
    snap = budgets.snapshot(member)
    assert snap.accounts[account.pk].spent == Decimal("6")
    start, end = usage.month_bounds()
    row = usage.usage_by_account(member, start, end)[0]
    assert row["currency"] == "USD"
    assert row["amount"] == Decimal("6") and row["eur"] == Decimal("6")


def test_admin_action_preview_then_apply(admin_client, gpt):
    ExchangeRate.objects.create(date=dt.date(2026, 1, 1), usd_eur=Decimal("0.86"))
    account = gpt.provider.billing_account
    url = reverse("admin:billing_billingaccount_changelist")
    data = {"action": "switch_to_usd_action", "_selected_action": [account.pk]}
    response = admin_client.post(url, data)
    assert response.status_code == 200
    html = response.content.decode()
    assert "Auf USD umstellen" in html and "gpt-x" in html and "älter als" in html
    account.refresh_from_db()
    assert account.currency == "EUR" and ModelPrice.objects.count() == 1

    response = admin_client.post(url, {**data, "apply": "1"}, follow=True)
    assert "auf USD umgestellt" in response.content.decode()
    account.refresh_from_db()
    assert account.currency == "USD" and ModelPrice.objects.count() == 2


def test_admin_action_without_rate_shows_warning(admin_client, gpt):
    account = gpt.provider.billing_account
    url = reverse("admin:billing_billingaccount_changelist")
    response = admin_client.post(
        url, {"action": "switch_to_usd_action", "_selected_action": [account.pk]}, follow=True
    )
    assert "kein Wechselkurs" in response.content.decode()
    account.refresh_from_db()
    assert account.currency == "EUR"


def test_switch_command_preview_and_apply(gpt):
    ExchangeRate.objects.create(date=dt.date(2026, 1, 1), usd_eur=Decimal("0.8"))
    account = gpt.provider.billing_account
    out = io.StringIO()
    call_command("switch_account_to_usd", "--all-eur", stdout=out)
    assert "Eingabe 3" in out.getvalue() and "-> 3.750000" in out.getvalue()
    assert "Vorschau" in out.getvalue()
    account.refresh_from_db()
    assert account.currency == "EUR"
    call_command("switch_account_to_usd", account.name, "--apply", stdout=io.StringIO())
    account.refresh_from_db()
    assert account.currency == "USD"
    with pytest.raises(CommandError):
        call_command("switch_account_to_usd", account.name, stdout=io.StringIO())
    with pytest.raises(CommandError, match="nicht gefunden"):
        call_command("switch_account_to_usd", "gibt-es-nicht")
