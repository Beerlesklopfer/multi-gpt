"""Verbrauch und Monatsbudgets (M6-02, M6-03, Plan 8f/12).

Abnahme M6: Ein Konto mit ausgeschöpftem Budget kann nur noch lokale
(kostenfreie) Modelle nutzen – auch bei direktem API-Aufruf.
"""

import datetime as dt
import json
import re
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from multigpt.accounts import usage
from multigpt.accounts.models import Role, User
from multigpt.accounts.permissions import Action, budget_allows, can, model_permitted
from multigpt.chat import services
from multigpt.chat.models import AIModel, Attachment, Conversation, Message, Provider
from multigpt.chat.providers import registry
from multigpt.chat.providers.base import Delta, Done, ProviderAdapter, Usage
from tests.billing_helpers import book_stored, set_price

pytestmark = pytest.mark.django_db

PASSWORD = "Geheim-Test-1234"
UTC = dt.UTC
BERLIN = usage.USAGE_TIME_ZONE


# --- Daten ------------------------------------------------------------------------


@pytest.fixture
def provider():
    return Provider.objects.create(name="Cloud", kind=Provider.Kind.OPENAI_COMPAT)


@pytest.fixture
def local_provider():
    return Provider.objects.create(
        name="LM Studio", kind=Provider.Kind.OPENAI_COMPAT, is_local=True
    )


@pytest.fixture
def paid_model(provider):
    model = AIModel.objects.create(
        provider=provider, model_id="gpt-paid", display_name="Bezahlmodell"
    )
    set_price(model, "2.5", "10")
    return model


@pytest.fixture
def unpriced_model(provider):
    return AIModel.objects.create(provider=provider, model_id="free", display_name="Ohne Preise")


@pytest.fixture
def local_model(local_provider):
    # Preise beim lokalen Anbieter zählen nicht: lokal ist immer kostenfrei.
    model = AIModel.objects.create(
        provider=local_provider, model_id="llama", display_name="Llama lokal"
    )
    set_price(model, "1", "1")
    return model


@pytest.fixture
def teen(paid_model, unpriced_model, local_model):
    role = Role.objects.get(key="teen")  # Budget 10 €
    role.allowed_models.set([paid_model, unpriced_model, local_model])
    return User.objects.create_user("jugend", password=PASSWORD, role=role)


@pytest.fixture
def adult():
    return User.objects.create_user(
        "erwachsen", password=PASSWORD, role=Role.objects.get(key="adult")
    )


def add_cost(user, amount, *, model=None, when=None, conversation=None, parent=None, **fields):
    """Antwort mit Kosten anlegen (created optional gesetzt)."""
    conversation = conversation or Conversation.objects.create(user=user)
    msg = Message.objects.create(
        conversation=conversation,
        parent=parent,
        role=Message.Role.ASSISTANT,
        content="x",
        model=model,
        cost=None if amount is None else Decimal(amount),
        **fields,
    )
    if when is not None:
        Message.objects.filter(pk=msg.pk).update(created=when)
        msg.created = when
    book_stored(msg)  # Buchung wie aus dem Bestand (billing)
    return msg


# --- Fake-Adapter -----------------------------------------------------------------


class FakeAdapter(ProviderAdapter):
    calls: list = []

    def stream(self, model_id, messages, system=None, tools=None, **params):
        FakeAdapter.calls.append(model_id)
        yield Delta("Hallo")
        yield Usage(10, 5)
        yield Done("stop")


@pytest.fixture
def fake(monkeypatch):
    FakeAdapter.calls = []
    monkeypatch.setattr(registry, "get_adapter", lambda provider: FakeAdapter(provider))
    return FakeAdapter


def post_message(client, conversation, **data):
    return client.post(
        reverse("chat:api_messages", args=[conversation.pk]),
        json.dumps(data),
        content_type="application/json",
    )


def sse_events(response):
    raw = b"".join(response.streaming_content).decode()
    events = []
    for block in raw.split("\n\n"):
        if block.strip():
            lines = dict(line.split(": ", 1) for line in block.split("\n"))
            events.append((lines["event"], json.loads(lines["data"])))
    return events


# --- Monatsgrenzen ----------------------------------------------------------------


def test_month_bounds_uses_berlin_calendar_month():
    # 31.10. 23:30 UTC ist in Berlin schon der 1.11. 00:30 (MEZ).
    start, end = usage.month_bounds(dt.datetime(2026, 10, 31, 23, 30, tzinfo=UTC))
    assert start == dt.datetime(2026, 11, 1, tzinfo=BERLIN)
    assert end == dt.datetime(2026, 12, 1, tzinfo=BERLIN)
    assert start.astimezone(UTC) == dt.datetime(2026, 10, 31, 23, 0, tzinfo=UTC)
    # Sommerzeit: 1.4. 00:00 MESZ = 31.3. 22:00 UTC.
    start, _ = usage.month_bounds(dt.datetime(2026, 3, 31, 22, 30, tzinfo=UTC))
    assert start.astimezone(UTC) == dt.datetime(2026, 3, 31, 22, 0, tzinfo=UTC)
    # Jahreswechsel
    start, end = usage.month_bounds(dt.date(2026, 12, 15))
    assert (start.year, start.month, end.year, end.month) == (2026, 12, 2027, 1)
    assert usage.month_label(start) == "Dezember 2026"


def test_spent_counts_by_berlin_month(teen):
    # 30.9. 22:30 UTC = 1.10. 00:30 Berlin -> Oktober
    add_cost(teen, "1.00", when=dt.datetime(2026, 9, 30, 22, 30, tzinfo=UTC))
    # 30.9. 21:30 UTC = 30.9. 23:30 Berlin -> September
    add_cost(teen, "2.00", when=dt.datetime(2026, 9, 30, 21, 30, tzinfo=UTC))
    # 31.10. 23:10 UTC = 1.11. 00:10 Berlin -> November
    add_cost(teen, "4.00", when=dt.datetime(2026, 10, 31, 23, 10, tzinfo=UTC))
    assert usage.spent(teen, dt.date(2026, 10, 1)) == Decimal("1.00")
    assert usage.spent(teen, dt.date(2026, 9, 1)) == Decimal("2.00")
    assert usage.spent(teen, dt.date(2026, 11, 1)) == Decimal("4.00")


def test_month_change_resets_budget(teen, paid_model):
    add_cost(teen, "10.00", when=dt.datetime(2026, 9, 15, 12, tzinfo=UTC))
    assert usage.budget_state(teen, dt.datetime(2026, 9, 30, 12, tzinfo=UTC)).level == "exhausted"
    assert usage.budget_state(teen, dt.datetime(2026, 10, 1, 0, 5, tzinfo=BERLIN)).level == "ok"


# --- Summen -----------------------------------------------------------------------


def test_spent_includes_versions_attachments_and_archived_chats(teen, adult, paid_model):
    conv = Conversation.objects.create(user=teen)
    question = Message.objects.create(conversation=conv, role=Message.Role.USER, content="?")
    add_cost(teen, "0.50", conversation=conv, parent=question, model=paid_model)
    # Neu erzeugen: zweite Version derselben Frage zählt auch
    second = add_cost(teen, "0.25", conversation=conv, parent=question, model=paid_model)
    add_cost(teen, None, conversation=conv, parent=question)  # ohne Preise: 0
    book_stored(
        attachment=Attachment.objects.create(
            message=second,
            kind=Attachment.Kind.IMAGE,
            file="x.png",
            generated_by_model=paid_model,
            cost=Decimal("0.04"),
        )
    )
    archived = Conversation.objects.create(user=teen, archived=True)
    add_cost(teen, "1.00", conversation=archived)
    add_cost(adult, "99.00")  # fremdes Konto zählt nicht
    assert usage.spent(teen) == Decimal("1.79")
    assert usage.spent_by_user(None, *usage.month_bounds()) == {
        teen.pk: Decimal("1.79"),
        adult.pk: Decimal("99.00"),
    }


def test_spent_is_one_aggregate_per_table(teen, django_assert_num_queries):
    conv = Conversation.objects.create(user=teen)
    for _ in range(20):
        add_cost(teen, "0.01", conversation=conv)
    with django_assert_num_queries(1):  # eine Aggregation über die Buchungen
        assert usage.spent(teen) == Decimal("0.20")


def test_usage_by_model_and_monthly_totals(
    teen, paid_model, local_model, django_assert_num_queries
):
    add_cost(teen, "0.30", model=paid_model, tokens_in=100, tokens_out=50)
    add_cost(teen, "0.20", model=paid_model, tokens_in=10, tokens_out=5)
    add_cost(teen, "0", model=local_model, tokens_in=1000, tokens_out=500)
    rows = usage.usage_by_model(teen, *usage.month_bounds())
    assert [(r["model"], r["answers"], r["tokens_in"], r["cost"]) for r in rows] == [
        ("Bezahlmodell", 2, 110, Decimal("0.50")),
        ("Llama lokal", 1, 1000, Decimal("0")),
    ]
    assert rows[1]["is_local"] is True
    with django_assert_num_queries(1):
        months = usage.monthly_totals(teen, 12)
    assert len(months) == 12
    assert months[0]["cost"] == Decimal("0.50")
    assert months[0]["tokens_in"] == 1110
    assert all(m["cost"] == 0 for m in months[1:])
    starts = [m["start"] for m in months]
    assert starts == sorted(starts, reverse=True)


def test_monthly_totals_buckets_in_berlin(teen):
    now = dt.datetime(2026, 10, 15, 12, tzinfo=UTC)
    add_cost(teen, "1.00", when=dt.datetime(2026, 9, 30, 22, 30, tzinfo=UTC))  # Okt. Berlin
    add_cost(teen, "2.00", when=dt.datetime(2025, 11, 2, tzinfo=UTC))  # vor 11 Monaten
    add_cost(teen, "8.00", when=dt.datetime(2025, 10, 2, tzinfo=UTC))  # zu alt
    months = usage.monthly_totals(teen, 12, now=now)
    assert (months[0]["label"], months[0]["cost"]) == ("Oktober 2026", Decimal("1.00"))
    assert (months[-1]["label"], months[-1]["cost"]) == ("November 2025", Decimal("2.00"))
    assert sum(m["cost"] for m in months) == Decimal("3.00")


# --- Budget und Stufen ------------------------------------------------------------


def test_budget_override_before_role(teen):
    assert usage.budget_for(teen) == Decimal("10.00")
    teen.monthly_budget_override = Decimal("3.00")
    assert usage.budget_for(teen) == Decimal("3.00")
    teen.monthly_budget_override = Decimal("0")
    assert usage.budget_for(teen) == Decimal("0")
    assert usage.budget_state(teen).level == "exhausted"  # Budget 0: nur kostenfreie


def test_unlimited_budget(adult, paid_model):
    add_cost(adult, "500.00")
    state = usage.budget_state(adult)
    assert (state.budget, state.ratio, state.level, state.percent) == (None, None, "ok", None)
    assert budget_allows(adult, paid_model)
    assert usage.warning_text(state) == ""


@pytest.mark.parametrize(
    ("amount", "level"),
    [("0", "ok"), ("7.99", "ok"), ("8.00", "warning"), ("9.99", "warning"), ("10.00", "exhausted")],
)
def test_levels(teen, amount, level):
    add_cost(teen, amount)
    state = usage.budget_state(teen)
    assert state.level == level
    assert state.remaining == max(Decimal("10.00") - Decimal(amount), Decimal(0))


def test_model_is_free():
    local = Provider.objects.create(name="L", kind=Provider.Kind.OPENAI_COMPAT, is_local=True)
    cloud = Provider.objects.create(name="C", kind=Provider.Kind.OPENAI_COMPAT)

    def model(provider, model_id, *prices):
        m = AIModel.objects.create(provider=provider, model_id=model_id, display_name=model_id)
        if prices:
            set_price(m, *prices)
        return m

    assert usage.model_is_free(model(local, "l", 5, None))
    assert usage.model_is_free(model(cloud, "none"))
    assert usage.model_is_free(model(cloud, "zero", 0, 0))
    assert not usage.model_is_free(model(cloud, "paid", None, "0.1"))


def test_exhausted_budget_allows_only_free_models(teen, paid_model, unpriced_model, local_model):
    assert can(teen, Action.USE_MODEL, paid_model)
    add_cost(teen, "10.00")
    assert not can(teen, Action.USE_MODEL, paid_model)
    assert model_permitted(teen, paid_model)  # Rolle erlaubt es weiterhin
    assert can(teen, Action.USE_MODEL, local_model)
    assert can(teen, Action.USE_MODEL, unpriced_model)
    # Override hebt die Sperre auf
    teen.monthly_budget_override = Decimal("20.00")
    assert can(teen, Action.USE_MODEL, paid_model)


def test_chat_models_for_marks_blocked(teen, paid_model, local_model):
    add_cost(teen, "10.00")
    models = {m.pk: m.blocked_by_budget for m in services.chat_models_for(teen)}
    assert models[paid_model.pk] is True
    assert models[local_model.pk] is False
    assert paid_model not in services.available_chat_models(teen)


def test_format_eur():
    assert usage.format_eur(Decimal("1.234")) == "1,23 €"
    assert usage.format_eur(Decimal("0.000060")) == "0,0001 €"
    assert usage.format_eur(Decimal("0.0042")) == "0,0042 €"
    assert usage.format_eur(Decimal("0")) == "0,00 €"
    assert usage.format_eur(Decimal("1234.5")) == "1 234,50 €"
    assert usage.format_eur(None) == "–"


# --- API: Abnahme M6 ---------------------------------------------------------------


def test_exhausted_account_can_only_use_local_models_via_api(
    client, teen, paid_model, unpriced_model, local_model, fake
):
    """Abnahme M6: auch per direktem API-Aufruf kein Anbieteraufruf für Bezahlmodelle."""
    client.force_login(teen)
    conv = Conversation.objects.create(user=teen)
    add_cost(teen, "10.00")

    response = post_message(client, conv, content="Hallo", model=paid_model.pk)
    assert response.status_code == 403
    assert response.json() == {"error": usage.BUDGET_EXHAUSTED_MESSAGE}
    assert fake.calls == []
    assert not conv.messages.exists()  # nichts angelegt

    response = post_message(client, conv, content="Hallo", model=local_model.pk)
    assert response.status_code == 200
    names = [n for n, _ in sse_events(response)]
    assert names[-1] == "done"
    assert fake.calls == ["llama"]

    # Neu erzeugen mit Bezahlmodell: ebenfalls gesperrt
    response = post_message(client, conv, regenerate=True, model=paid_model.pk)
    assert response.status_code == 403
    assert response.json()["error"] == usage.BUDGET_EXHAUSTED_MESSAGE

    # Modell ohne Preise verursacht keine Kosten und bleibt nutzbar
    response = post_message(client, conv, content="Noch was", model=unpriced_model.pk)
    assert response.status_code == 200
    sse_events(response)
    assert fake.calls == ["llama", "free"]


def test_not_permitted_model_keeps_generic_error(client, teen, provider, fake):
    other = AIModel.objects.create(provider=provider, model_id="x", display_name="X")
    set_price(other, 1)
    client.force_login(teen)
    conv = Conversation.objects.create(user=teen)
    response = post_message(client, conv, content="Hallo", model=other.pk)
    assert response.status_code == 403
    assert response.json()["error"] == "Dieses Modell steht dir nicht zur Verfügung."


def test_models_api_reports_blocked_by_budget(client, teen, paid_model, local_model):
    client.force_login(teen)
    data = client.get(reverse("chat:api_models")).json()
    assert {m["id"]: m["blocked_by_budget"] for m in data}[paid_model.pk] is False
    add_cost(teen, "10.00")
    data = {m["id"]: m for m in client.get(reverse("chat:api_models")).json()}
    assert data[paid_model.pk]["blocked_by_budget"] is True
    assert data[local_model.pk]["blocked_by_budget"] is False


def test_sse_status_warning_from_80_percent(client, teen, paid_model, fake):
    client.force_login(teen)
    conv = Conversation.objects.create(user=teen)
    response = post_message(client, conv, content="Hallo", model=paid_model.pk)
    assert "status" not in [n for n, _ in sse_events(response)]

    add_cost(teen, "8.50")
    response = post_message(client, conv, content="Weiter", model=paid_model.pk)
    status = [d for n, d in sse_events(response) if n == "status"]
    assert len(status) == 1
    assert status[0]["level"] == "warning"
    assert "85 %" in status[0]["text"]


# --- /api/usage/ und Seite ----------------------------------------------------------


def test_usage_api_returns_only_own_data(client, teen, adult, paid_model, local_model):
    add_cost(teen, "8.20", model=paid_model, tokens_in=1000, tokens_out=200)
    add_cost(teen, "0", model=local_model, tokens_in=5, tokens_out=5)
    add_cost(adult, "50.00", model=paid_model, tokens_in=9999, tokens_out=9999)
    client.force_login(teen)
    data = client.get(reverse("api_usage")).json()
    assert Decimal(data["spent"]) == Decimal("8.20")
    assert Decimal(data["budget"]) == Decimal("10.00")
    assert data["level"] == "warning"
    assert data["percent"] == 82
    assert data["month"] == timezone.localtime(timezone.now(), BERLIN).strftime("%Y-%m")
    models = {m["model"]: m for m in data["models"]}
    assert models["Bezahlmodell"]["tokens_in"] == 1000  # ohne die 9999 des anderen Kontos
    assert models["Llama lokal"]["is_local"] is True
    # Kein Parameter für fremde Konten
    other = client.get(reverse("api_usage"), {"user": adult.pk}).json()
    assert other["spent"] == data["spent"]


def test_usage_api_requires_login(client):
    response = client.get(reverse("api_usage"))
    assert response.status_code == 403
    assert response.json() == {"error": "Nicht angemeldet."}


def test_usage_page(client, teen, adult, paid_model, local_model):
    add_cost(teen, "9.00", model=paid_model, tokens_in=1234, tokens_out=56)
    add_cost(adult, "3.00", model=local_model)  # fremd: darf nicht erscheinen
    client.force_login(teen)
    response = client.get(reverse("usage"))
    assert response.status_code == 200
    html = response.content.decode()
    assert "Mein Verbrauch" in html
    assert "Bezahlmodell" in html
    assert "Llama lokal" not in html
    assert "1.234" in html
    assert "9,00 € von 10,00 € verbraucht (90 %)" in html
    assert html.count('<tr class="is-current">') == 1
    assert html.count("<meter") == 1 + 12
    assert re.search(r'value="9\.0+"', html) and 'max="10.00"' in html
    # Hinweis ab 80 % auch in der Kopfzeile, Link in der Seitenleiste
    assert "budget-banner-warning" in html
    assert 'href="/verbrauch/"' in html
    # Kein Inline-JS
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)
    assert not re.search(r"\son[a-z]+\s*=", html)


def test_usage_page_unlimited_and_login(client, adult):
    assert client.get(reverse("usage")).status_code == 302
    client.force_login(adult)
    html = client.get(reverse("usage")).content.decode()
    assert "kein Monatsbudget festgelegt" in html
    assert "budget-banner" not in html
    assert "noch keine Antworten" in html


def test_banner_exhausted_on_chat_page(client, teen):
    add_cost(teen, "12.00")
    client.force_login(teen)
    html = client.get(reverse("chat:index")).content.decode()
    assert "budget-banner-exhausted" in html
    assert usage.BUDGET_EXHAUSTED_MESSAGE in html
