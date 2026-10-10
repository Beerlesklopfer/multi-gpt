"""Kontenrahmen, Preise, Kurse, Buchungen und Budgets je Konto (multigpt/billing, M6).

Beispiel-Payloads der Anbieter: wörtlich aus der Doku (Stand 2026-10-10), wo es
welche gibt; Quellen stehen am jeweiligen Test. Für Gemini gibt es auf
ai.google.dev kein ausgefülltes Stream-Beispiel, das Payload folgt dem Schema
von ``UsageMetadata``.
"""

import csv
import datetime as dt
import importlib
import io
import json
from decimal import Decimal

import httpx
import pytest
import respx
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.urls import reverse
from django.utils import timezone

from multigpt.accounts import usage
from multigpt.accounts.models import Role, User
from multigpt.accounts.permissions import Action, can
from multigpt.billing import booking, budgets, ecb, pricing
from multigpt.billing.models import (
    AccountBudget,
    BillingAccount,
    ExchangeRate,
    ModelPrice,
    UsageEntry,
)
from multigpt.billing.pricing import Round, Tally
from multigpt.chat import services
from multigpt.chat.models import AIModel, Conversation, Message, Provider, Share
from multigpt.chat.providers import registry
from multigpt.chat.providers.base import Delta, Done, ProviderAdapter, Usage
from tests import test_provider_anthropic as anth
from tests import test_provider_google as gem
from tests import test_provider_openai_compat as oai
from tests.billing_helpers import set_price
from tests.test_budget import post_message, sse_events

pytestmark = pytest.mark.django_db

PASSWORD = "Geheim-Test-1234"
UTC = dt.UTC


# --- Daten ------------------------------------------------------------------------


@pytest.fixture
def anthropic():
    return Provider.objects.create(name="Anthropic", kind=Provider.Kind.ANTHROPIC)


@pytest.fixture
def openai():
    return Provider.objects.create(name="OpenAI", kind=Provider.Kind.OPENAI_COMPAT)


@pytest.fixture
def lmstudio():
    return Provider.objects.create(
        name="LM Studio", kind=Provider.Kind.OPENAI_COMPAT, is_local=True
    )


def make_model(provider, model_id, *prices, **fields):
    model = AIModel.objects.create(provider=provider, model_id=model_id, display_name=model_id)
    if prices or fields:
        set_price(model, *prices, currency=fields.pop("currency", "USD"), **fields)
    return model


@pytest.fixture
def claude(anthropic):
    # Struktur wie Anthropic: Cache lesen 0,1×, schreiben 1,25× (5 Min.) bzw. 2× (1 Std.).
    return make_model(
        anthropic,
        "claude-x",
        "3",
        "15",
        cached_input="0.3",
        cache_write="3.75",
        cache_write_1h="6",
        unit_prices={"web_search": "0.01"},
    )


@pytest.fixture
def gpt(openai):
    return make_model(openai, "gpt-x", "2", "8", cached_input="0.5")


@pytest.fixture
def llama(lmstudio):
    return make_model(lmstudio, "llama")


@pytest.fixture
def member(claude, gpt, llama):
    role = Role.objects.get(key="adult")
    role.all_models = True
    role.save()
    return User.objects.create_user("mitglied", password=PASSWORD, role=role)


def rate(day, value):
    return ExchangeRate.objects.create(date=day, usd_eur=Decimal(value))


def answer(user, model, *, author=None, when=None, conversation=None):
    conversation = conversation or Conversation.objects.create(user=user)
    msg = Message.objects.create(
        conversation=conversation, role="assistant", content="x", model=model, author=author
    )
    if when is not None:
        Message.objects.filter(pk=msg.pk).update(created=when)
        msg.refresh_from_db()
    return msg


def book(user, model, *rounds, when=None, units=None, author=None):
    """Antwort mit Verbrauch buchen; ``rounds``: Usage je Anbieteraufruf."""
    msg = answer(user, model, when=when, author=author)
    tally = Tally()
    for r in rounds:
        tally.add(r)
    if units:
        tally.units.update(units)
    return booking.book_answer(msg, model, tally)


# --- Usage aus den echten Feldern der Adapter --------------------------------------


def test_anthropic_usage_from_docs():
    """Quellen: platform.claude.com/docs/en/build-with-claude/streaming (Websuche-
    Beispiel, message_delta kumulativ) und …/prompt-caching (cache_creation)."""
    start = {
        "type": "message_start",
        "message": {
            "id": "msg_01G",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {
                "input_tokens": 2679,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "output_tokens": 3,
            },
        },
    }
    delta = {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {
            "input_tokens": 2048,
            "cache_read_input_tokens": 1800,
            "cache_creation_input_tokens": 248,
            "output_tokens": 503,
            "cache_creation": {"ephemeral_5m_input_tokens": 148, "ephemeral_1h_input_tokens": 100},
            "output_tokens_details": {"thinking_tokens": 312},
            "server_tool_use": {"web_search_requests": 1},
        },
    }
    body = anth.sse(start, anth.block_start(), anth.text("Hi"), anth.block_stop(), delta, anth.STOP)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{anth.BASE}/messages").mock(return_value=oai.sse_response(body))
        events = anth.run()
    u = next(e for e in events if isinstance(e, Usage))
    assert (u.tokens_in, u.tokens_out) == (2048 + 1800 + 248, 503)
    assert (u.cached_read, u.cache_write, u.cache_write_1h, u.reasoning) == (1800, 148, 100, 312)
    assert u.units == {"web_search": 1}
    assert u == Usage(4096, 503)  # Vergleich wie bisher nur über die Summen


def test_anthropic_without_split_counts_all_as_5_minutes():
    body = anth.sse(
        anth.start(input_tokens=10, cache_creation_input_tokens=20, cache_read_input_tokens=5),
        anth.block_start(),
        anth.text("x"),
        anth.block_stop(),
        anth.message_delta(output_tokens=7),
        anth.STOP,
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{anth.BASE}/messages").mock(return_value=oai.sse_response(body))
        u = next(e for e in anth.run() if isinstance(e, Usage))
    assert (u.tokens_in, u.cached_read, u.cache_write, u.cache_write_1h) == (35, 5, 20, 0)
    assert u.units == {} and u.reasoning == 0


def test_openai_usage_from_docs():
    """Quelle: developers.openai.com/api/reference/resources/chat/subresources/
    completions/methods/retrieve (Felder prompt_tokens_details/completion_tokens_details);
    Werte wie im Reasoning-Beispiel (…/guides/reasoning)."""
    usage_chunk = {
        "choices": [],
        "usage": {
            "prompt_tokens": 1075,
            "completion_tokens": 1186,
            "total_tokens": 2261,
            "prompt_tokens_details": {
                "audio_tokens": 0,
                "cache_write_tokens": 0,
                "cached_tokens": 1024,
                "image_tokens": 0,
                "text_tokens": 0,
            },
            "completion_tokens_details": {
                "accepted_prediction_tokens": 0,
                "audio_tokens": 0,
                "reasoning_tokens": 1024,
                "rejected_prediction_tokens": 0,
                "text_tokens": 0,
            },
        },
    }
    body = oai.sse(oai.content("Hi"), oai.content("", finish="stop"), usage_chunk)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{oai.BASE}/chat/completions").mock(return_value=oai.sse_response(body))
        u = next(e for e in oai.run() if isinstance(e, Usage))
    assert (u.tokens_in, u.tokens_out, u.cached_read, u.reasoning) == (1075, 1186, 1024, 1024)
    assert u.cache_write == 0


def test_openai_predicted_outputs_example_and_lmstudio_sums():
    """Quelle: developers.openai.com/api/docs/guides/predicted-outputs (usage wörtlich);
    LM Studio meldet nur die beiden Summen."""
    doc = {
        "prompt_tokens": 59,
        "completion_tokens": 24,
        "total_tokens": 83,
        "prompt_tokens_details": {"cached_tokens": 0, "audio_tokens": 0},
        "completion_tokens_details": {
            "reasoning_tokens": 0,
            "audio_tokens": 0,
            "accepted_prediction_tokens": 14,
            "rejected_prediction_tokens": 2,
        },
    }
    for data, expected in (
        (doc, (59, 24, 0, 0)),
        ({"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19}, (12, 7, 0, 0)),
    ):
        body = oai.sse(oai.content("x", finish="stop"), {"choices": [], "usage": data})
        with respx.mock(assert_all_called=False) as mock:
            mock.post(f"{oai.BASE}/chat/completions").mock(return_value=oai.sse_response(body))
            u = next(e for e in oai.run() if isinstance(e, Usage))
        assert (u.tokens_in, u.tokens_out, u.cached_read, u.reasoning) == expected


def test_gemini_usage_cached_and_thoughts():
    """Schema: ai.google.dev/api/generate-content#UsageMetadata – promptTokenCount
    enthält cachedContentTokenCount, thoughtsTokenCount kommt zu den Kandidaten hinzu."""
    meta = {
        "promptTokenCount": 3000,
        "cachedContentTokenCount": 2048,
        "candidatesTokenCount": 120,
        "thoughtsTokenCount": 380,
        "totalTokenCount": 3500,
    }
    body = gem.sse(gem.chunk("Hallo", finish="STOP", usage=meta))
    with respx.mock(assert_all_called=False) as mock:
        mock.post(gem.URL).mock(return_value=oai.sse_response(body))
        u = next(e for e in gem.run() if isinstance(e, Usage))
    assert (u.tokens_in, u.tokens_out, u.cached_read, u.reasoning) == (3000, 500, 2048, 380)


# --- Berechnung --------------------------------------------------------------------


def test_amount_with_cache_and_reasoning(claude):
    price = pricing.price_at(claude)
    tally = Tally()
    # 4096 Eingabe: 1800 gelesen, 148 (5 Min.) + 100 (1 Std.) geschrieben; 503 Ausgabe
    # inkl. 312 Reasoning; eine Websuche.
    tally.add(
        Usage(
            4096,
            503,
            cached_read=1800,
            cache_write=148,
            cache_write_1h=100,
            reasoning=312,
            units={"web_search": 1},
        )
    )
    expected = (
        Decimal(2048) * 3 + 1800 * Decimal("0.3") + 148 * Decimal("3.75") + 100 * 6 + 503 * 15
    ) / 1_000_000 + Decimal("0.01")
    assert pricing.amount(price, tally) == expected.quantize(Decimal("0.000001"))


def test_missing_cache_prices_fall_back_to_input(gpt):
    price = pricing.price_at(gpt)
    tally = Tally([Round(1_000_000, 0, cached_read=500_000, cache_write=250_000)])
    # 250k normal (2) + 500k gelesen (0,5) + 250k geschrieben ohne eigenen Preis (2)
    assert pricing.amount(price, tally) == Decimal("1.25")


def test_long_context_tier_per_request(openai):
    model = make_model(
        openai,
        "long",
        "1",
        "2",
        long_context_threshold=200_000,
        long_input="2",
        long_output="3",
    )
    price = pricing.price_at(model)
    short = Round(200_000, 1_000_000)
    long = Round(200_001, 1_000_000)
    assert pricing.amount(price, Tally([short])) == Decimal("2.2")
    assert pricing.amount(price, Tally([long])) == Decimal("3.400002")
    # Zwei kurze Anfragen einer Antwort (Werkzeugrunden) bleiben im Normaltarif.
    assert pricing.amount(price, Tally([short, short])) == Decimal("4.4")


def test_unit_variants_and_empty_price(openai):
    model = make_model(openai, "img", unit_prices={"image": "0.04", "image:high": "0.17"})
    price = pricing.price_at(model)
    tally = Tally(units={"image:high:1024x1024": 2, "image:low": 1})
    assert pricing.amount(price, tally) == Decimal("0.38")
    bare = AIModel.objects.create(provider=openai, model_id="bare", display_name="bare")
    assert pricing.amount(pricing.price_at(bare), tally) is None
    ModelPrice.objects.create(ai_model=bare)  # leere Version: weiter „ohne Preis“
    assert pricing.amount(pricing.price_at(bare), tally) is None


def test_unit_keys_validated(gpt):
    price = ModelPrice(ai_model=gpt, unit_prices={"web_search": 0.01, "foo": 1, "image": -1})
    with pytest.raises(ValidationError) as exc:
        price.clean()
    text = str(exc.value)
    assert "foo" in text and "image" in text


def test_price_version_by_date(member, gpt):
    ModelPrice.objects.filter(ai_model=gpt).update(valid_from=dt.datetime(2026, 1, 1, tzinfo=UTC))
    set_price(gpt, "4", "16", currency="USD", valid_from=dt.datetime(2026, 6, 1, tzinfo=UTC))
    old = book(member, gpt, Usage(1_000_000, 0), when=dt.datetime(2026, 5, 31, 12, tzinfo=UTC))
    new = book(member, gpt, Usage(1_000_000, 0), when=dt.datetime(2026, 6, 2, tzinfo=UTC))
    assert (old.amount, new.amount) == (Decimal("2"), Decimal("4"))
    assert old.price.valid_from < new.price.valid_from
    assert old.currency == "USD"


# --- Kontoarten und Währung --------------------------------------------------------


def test_default_accounts(anthropic, openai, lmstudio):
    assert (anthropic.billing_account.kind, anthropic.billing_account.currency) == (
        "monetary",
        "USD",
    )
    assert anthropic.billing_account.name == "Anthropic"
    assert lmstudio.billing_account.kind == "tokens"
    second = Provider.objects.create(name="Ollama", kind="openai_compat", is_local=True)
    assert second.billing_account_id == lmstudio.billing_account_id  # gemeinsames Token-Konto
    # Bündeln: OpenRouter auf das OpenAI-Konto
    router = Provider.objects.create(
        name="OpenRouter", kind="openai_compat", billing_account=openai.billing_account
    )
    assert set(openai.billing_account.providers.all()) == {openai, router}


def test_tokens_account_counts_only_tokens(member, llama):
    entry = book(member, llama, Usage(1000, 200, cached_read=100, reasoning=50))
    assert entry.amount is None and entry.amount_eur is None and entry.currency == ""
    assert (entry.tokens_in, entry.tokens_out, entry.cached_read, entry.reasoning) == (
        1000,
        200,
        100,
        50,
    )
    assert booking.compat_cost(entry) == Decimal(0)  # Message.cost wie bisher 0


def test_flat_account_counts_requests(member, openai):
    flat = BillingAccount.objects.create(name="Abo", kind="flat")
    provider = Provider.objects.create(
        name="Abo-Dienst", kind="openai_compat", billing_account=flat
    )
    model = make_model(provider, "abo")
    entry = book(member, model, Usage(10, 5), Usage(20, 5))
    assert entry.amount is None and entry.requests == 2
    assert usage.model_is_free(model)
    with pytest.raises(ValidationError):
        AccountBudget(account=flat, role=member.role, monthly_budget=Decimal(1)).clean()


def test_usd_without_rate_is_unknown_then_filled(member, gpt):
    entry = book(member, gpt, Usage(1_000_000, 0))
    assert entry.amount == Decimal("2") and entry.amount_eur is None and entry.eur_missing
    assert booking.missing_eur_count() == 1
    # Budget zählt den Betrag ohne Kurs 1:1 (vorsichtig), nie 0.
    assert usage.spent(member) == Decimal("2")
    rate(timezone.localdate() - dt.timedelta(days=3), "0.9")
    assert usage.spent(member) == Decimal("1.8")  # neuester Kurs als Schätzung
    assert booking.fill_missing_eur() == 1
    entry.refresh_from_db()
    assert entry.amount_eur == Decimal("1.8") and entry.rate == Decimal("0.9")
    assert Message.objects.get(pk=entry.message_id).cost == Decimal("1.8")


def test_rate_valid_at_booking_day(member, gpt):
    rate(dt.date(2026, 3, 1), "0.95")
    rate(dt.date(2026, 3, 10), "0.90")
    early = book(member, gpt, Usage(1_000_000, 0), when=dt.datetime(2026, 3, 9, 22, tzinfo=UTC))
    # 9.3. 23:30 Berlin -> noch der Kurs vom 1.3.
    late = book(member, gpt, Usage(1_000_000, 0), when=dt.datetime(2026, 3, 9, 23, 30, tzinfo=UTC))
    # 10.3. 00:30 Berlin -> Kurs vom 10.3.
    assert (early.amount_eur, early.rate_date) == (Decimal("1.9"), dt.date(2026, 3, 1))
    assert (late.amount_eur, late.rate_date) == (Decimal("1.8"), dt.date(2026, 3, 10))


def test_eur_account_needs_no_rate(member, anthropic):
    model = make_model(anthropic, "eur-model", "1", "1", currency="EUR")
    entry = book(member, model, Usage(1_000_000, 1_000_000))
    assert entry.amount == entry.amount_eur == Decimal("2") and entry.rate is None


def test_currency_locked_once_in_use(gpt):
    account = gpt.provider.billing_account
    account.currency = "EUR"
    with pytest.raises(ValidationError):
        account.clean()


# --- Budgets je Konto ---------------------------------------------------------------


@pytest.fixture
def eur(member, claude, gpt):
    """Alle Bezahlkonten in EUR, damit die Budgets ohne Kurse rechnen."""
    BillingAccount.objects.filter(kind="monetary").update(currency="EUR")
    for model in (claude, gpt):
        model.provider.billing_account.refresh_from_db()
    return member


def test_account_budget_blocks_only_its_models(eur, claude, gpt, llama):
    AccountBudget.objects.create(
        account=claude.provider.billing_account, role=eur.role, monthly_budget=Decimal("1")
    )
    book(eur, claude, Usage(0, 1_000_000))  # 15 € > 1 €
    assert not can(eur, Action.USE_MODEL, claude)
    assert can(eur, Action.USE_MODEL, gpt) and can(eur, Action.USE_MODEL, llama)
    blocked = {m.pk: m for m in services.chat_models_for(eur)}
    assert blocked[claude.pk].blocked_by_budget
    assert "„Anthropic“" in blocked[claude.pk].budget_reason
    assert not blocked[gpt.pk].blocked_by_budget and not blocked[llama.pk].blocked_by_budget


def test_user_budget_before_role_and_warning(eur, claude):
    account = claude.provider.billing_account
    AccountBudget.objects.create(account=account, role=eur.role, monthly_budget=Decimal("1"))
    AccountBudget.objects.create(account=account, user=eur, monthly_budget=Decimal("100"))
    book(eur, claude, Usage(0, 6_000_000))  # 90 €
    assert can(eur, Action.USE_MODEL, claude)
    texts = usage.warnings_for(eur, claude)
    assert texts == ["90 % des Budgets für „Anthropic“ sind verbraucht (90,00 € von 100,00 €)."]


def test_token_quota_for_local(member, llama, gpt):
    AccountBudget.objects.create(
        account=llama.provider.billing_account, role=member.role, monthly_tokens=1000
    )
    book(member, llama, Usage(700, 100))
    assert can(member, Action.USE_MODEL, llama)
    assert "80 %" in usage.warnings_for(member, llama)[0]
    book(member, llama, Usage(150, 50))
    assert not can(member, Action.USE_MODEL, llama)
    assert "Token-Kontingent für „Lokale Modelle“" in usage.blocked_reason(member, llama)
    assert can(member, Action.USE_MODEL, gpt)
    with pytest.raises(ValidationError):  # Token-Konten: kein EUR-Budget
        AccountBudget(
            account=llama.provider.billing_account, role=member.role, monthly_budget=1
        ).clean()


def test_total_budget_over_monetary_accounts(eur, claude, gpt, llama):
    eur.monthly_budget_override = Decimal("10")
    eur.save()
    book(eur, claude, Usage(0, 400_000))  # 6 €
    book(eur, gpt, Usage(0, 500_000))  # 4 €
    assert usage.budget_state(eur).level == "exhausted"
    assert not can(eur, Action.USE_MODEL, claude) and not can(eur, Action.USE_MODEL, gpt)
    assert can(eur, Action.USE_MODEL, llama)
    assert usage.blocked_reason(eur, gpt) == usage.BUDGET_EXHAUSTED_MESSAGE


def test_inactive_account_blocks(member, gpt):
    BillingAccount.objects.filter(pk=gpt.provider.billing_account_id).update(active=False)
    gpt.provider.billing_account.refresh_from_db()
    assert "deaktiviert" in usage.blocked_reason(member, gpt)


class FakeAdapter(ProviderAdapter):
    def stream(self, model_id, messages, system=None, tools=None, **params):
        yield Delta("Hallo")
        yield Usage(5000, 300, cached_read=4000, reasoning=100)
        yield Done("stop")


def test_stream_books_answer_and_api_blocks_per_account(client, eur, claude, gpt, monkeypatch):
    monkeypatch.setattr(registry, "get_adapter", lambda provider: FakeAdapter(provider))
    client.force_login(eur)
    conv = Conversation.objects.create(user=eur)
    events = sse_events(post_message(client, conv, content="Hallo", model=gpt.pk))
    assert events[-1] == ("done", {"status": "complete"})
    entry = UsageEntry.objects.get(kind="answer")
    assert (entry.tokens_in, entry.cached_read, entry.reasoning, entry.requests) == (
        5000,
        4000,
        100,
        1,
    )
    # 1000 × 2 + 4000 × 0,5 + 300 × 8 je 1 Mio. = 0,0064 €
    assert entry.amount_eur == Decimal("0.0064")
    assert entry.message.cost == Decimal("0.0064") and entry.user_id == eur.pk
    usage_event = next(d for n, d in events if n == "usage")
    assert usage_event["cost"] == "0.006400"
    # Konto-Budget ausgeschöpft: 403 mit Text je Konto, anderes Konto weiter nutzbar.
    AccountBudget.objects.create(
        account=gpt.provider.billing_account, user=eur, monthly_budget=Decimal("0")
    )
    response = post_message(client, conv, content="Noch mal", model=gpt.pk)
    assert response.status_code == 403
    assert "„OpenAI“" in response.json()["error"]
    data = {m["id"]: m for m in client.get(reverse("chat:api_models")).json()}
    assert data[gpt.pk]["blocked_by_budget"] and not data[claude.pk]["blocked_by_budget"]
    assert data[claude.pk]["billing_title"].startswith("Konto Anthropic (EUR)")


def test_shared_chat_books_on_author(member, gpt):
    owner = User.objects.create_user("besitzerin", password=PASSWORD, role=member.role)
    conv = Conversation.objects.create(user=owner)
    Share.objects.create(conversation=conv, user=member, can_write=True)
    msg = answer(owner, gpt, author=member, conversation=conv)
    entry = booking.book_answer(msg, gpt, Tally([Round(10, 10)]))
    assert entry.user_id == member.pk
    legacy = answer(owner, gpt, conversation=conv)  # ohne Verfasser: Besitzerin
    assert booking.book_answer(legacy, gpt, Tally()).user_id == owner.pk


# --- Datenmigration ----------------------------------------------------------------


migration = importlib.import_module("multigpt.chat.migrations.0025_provider_billing_account")
BEFORE = [("chat", "0024_web_fetch")]


def _migrate(targets):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)
    return executor


def test_data_migration_creates_accounts_prices_entries_idempotent():
    with connection.cursor() as cursor:  # Schemaänderungen nach Inserts in einer Transaktion
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
    executor = _migrate(BEFORE)
    old = executor.loader.project_state(BEFORE).apps
    OldProvider = old.get_model("chat", "Provider")
    OldModel = old.get_model("chat", "AIModel")
    OldConv = old.get_model("chat", "Conversation")
    OldMessage = old.get_model("chat", "Message")
    OldAttachment = old.get_model("chat", "Attachment")
    user = User.objects.create_user("alt", role=Role.objects.get(key="adult"))
    other = User.objects.create_user("absender", role=Role.objects.get(key="adult"))
    cloud = OldProvider.objects.create(name="Cloud", kind="openai_compat")
    free = OldProvider.objects.create(name="Gratis", kind="openai_compat")
    local = OldProvider.objects.create(name="Lokal", kind="openai_compat", is_local=True)
    paid = OldModel.objects.create(
        provider=cloud, model_id="p", display_name="P", price_in=Decimal("2.5"), price_out=10
    )
    OldModel.objects.create(provider=free, model_id="f", display_name="F")
    llama = OldModel.objects.create(provider=local, model_id="l", display_name="L", price_in=1)
    conv = OldConv.objects.create(user_id=user.pk)
    first = OldMessage.objects.create(
        conversation=conv,
        role="assistant",
        model=paid,
        tokens_in=100,
        tokens_out=50,
        cost=Decimal("0.00075"),
        author_id=other.pk,
    )
    OldMessage.objects.filter(pk=first.pk).update(created=dt.datetime(2026, 2, 3, tzinfo=UTC))
    OldMessage.objects.create(
        conversation=conv, role="assistant", model=llama, cost=0, tokens_in=7, tokens_out=3
    )
    OldMessage.objects.create(conversation=conv, role="assistant", cost=Decimal("1"))  # gelöscht
    OldMessage.objects.create(conversation=conv, role="user", content="Frage")
    OldAttachment.objects.create(
        message=first, kind="image", file="x.png", generated_by_model=paid, cost=Decimal("0.04")
    )

    _migrate(executor.loader.graph.leaf_nodes())

    cloud_now = Provider.objects.get(name="Cloud")
    assert (cloud_now.billing_account.name, cloud_now.billing_account.currency) == ("Cloud", "EUR")
    assert Provider.objects.get(name="Gratis").billing_account.currency == "USD"
    assert Provider.objects.get(name="Lokal").billing_account.kind == "tokens"
    price = ModelPrice.objects.get(ai_model_id=paid.pk)
    assert (price.input, price.output) == (Decimal("2.5"), Decimal("10"))
    assert price.valid_from == dt.datetime(2026, 2, 3, tzinfo=UTC)  # früheste Nutzung
    assert not ModelPrice.objects.filter(ai_model_id=llama.pk).exists()
    entries = {(e.kind, e.model_name): e for e in UsageEntry.objects.all()}
    assert len(entries) == 4
    paid_entry = entries[("answer", "P")]
    assert paid_entry.user_id == other.pk and paid_entry.legacy
    assert (paid_entry.amount, paid_entry.amount_eur, paid_entry.price_id) == (
        Decimal("0.00075"),
        Decimal("0.00075"),
        price.pk,
    )
    assert entries[("answer", "L")].amount is None and entries[("answer", "L")].tokens_in == 7
    assert entries[("answer", "")].account.name == migration.ORPHAN_ACCOUNT
    assert entries[("attachment", "P")].units == {"image": 1}
    assert usage.spent(user) == Decimal("1")  # gelöschtes Modell, Verfasser fehlt -> Besitzer
    assert usage.spent(other, dt.date(2026, 2, 1)) == Decimal("0.00075")
    assert usage.spent(other) == Decimal("0.04")  # Bild im laufenden Monat, auf den Verfasser

    # Rückwärts (Preise zurück ans Modell) und erneut vorwärts: nichts doppelt.
    counts = (
        BillingAccount.objects.count(),
        ModelPrice.objects.count(),
        UsageEntry.objects.count(),
    )
    executor = _migrate(BEFORE)
    old = executor.loader.project_state(BEFORE).apps
    assert old.get_model("chat", "AIModel").objects.get(pk=paid.pk).price_in == Decimal("2.5")
    _migrate(executor.loader.graph.leaf_nodes())
    assert counts == (
        BillingAccount.objects.count(),
        ModelPrice.objects.count(),
        UsageEntry.objects.count(),
    )


# --- Anzeigen, Admin, CSV ----------------------------------------------------------


def test_usage_page_per_account(client, member, gpt, llama):
    book(member, gpt, Usage(1_000_000, 0, cached_read=250_000))
    book(member, llama, Usage(1234, 56, reasoning=10))
    client.force_login(member)
    html = client.get(reverse("usage")).content.decode()
    assert "Je Abrechnungskonto" in html
    assert "OpenAI" in html and "Lokale Modelle" in html and "nur Tokens" in html
    assert "1,63 $" in html and "Kurs fehlt" in html  # 750k × 2 + 250k × 0,5 = 1,625
    assert "1.234" in html
    data = client.get(reverse("api_usage")).json()
    assert data["accounts"] == []  # keine Budgets je Konto
    other = client.get(reverse("usage"), {"monat": "1999-01"})
    assert other.status_code == 200  # unbekannter Monat -> laufender


def test_family_usage_per_member_and_account(admin_client, member, gpt, llama):
    book(member, gpt, Usage(1_000_000, 0))
    book(member, llama, Usage(10, 5))
    html = admin_client.get(reverse("family:usage")).content.decode()
    assert "Konto OpenAI" in html and "Konto Lokale Modelle" in html
    assert "2,00 $" in html and "nur Tokens" in html
    html = admin_client.get(reverse("family:member", args=[member.pk])).content.decode()
    assert "Lokale Modelle" in html


def test_admin_pages_and_missing_rate_warning(admin_client, member, gpt):
    entry = book(member, gpt, Usage(1000, 10))
    for name in (
        "billing_billingaccount",
        "billing_modelprice",
        "billing_exchangerate",
        "billing_usageentry",
    ):
        response = admin_client.get(reverse(f"admin:{name}_changelist"))
        assert response.status_code == 200, name
    html = admin_client.get(reverse("admin:billing_usageentry_changelist")).content.decode()
    assert "keinen EUR-Betrag" in html and "Als CSV exportieren" in html
    assert (
        admin_client.get(reverse("admin:billing_usageentry_change", args=[entry.pk])).status_code
        == 200
    )
    html = admin_client.get(reverse("admin:chat_aimodel_change", args=[gpt.pk])).content.decode()
    assert "Preisversion" in html
    # Kurs speichern trägt EUR nach.
    admin_client.post(
        reverse("admin:billing_exchangerate_add"),
        {"date": timezone.localdate().isoformat(), "usd_eur": "0.9", "source": "manual"},
    )
    entry.refresh_from_db()
    assert entry.amount_eur is not None


def test_csv_export_per_month_and_account(admin_client, member, gpt, llama):
    member.username = "=boese"
    member.save()
    book(member, gpt, Usage(1_000_000, 0), when=dt.datetime(2026, 9, 15, tzinfo=UTC))
    book(member, llama, Usage(10, 5), when=dt.datetime(2026, 9, 16, tzinfo=UTC))
    book(member, gpt, Usage(10, 5), when=dt.datetime(2026, 8, 16, tzinfo=UTC))
    url = reverse("admin:billing_usageentry_csv")
    response = admin_client.get(url, {"monat": "2026-09", "konto": gpt.provider.billing_account_id})
    assert response["Content-Type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig")), delimiter=";"))
    assert rows[0][:3] == ["Zeitpunkt", "Konto", "Kontoart"]
    assert len(rows) == 2
    row = dict(zip(rows[0], rows[1], strict=True))
    assert row["Nutzer"] == "'=boese"  # keine Formel
    assert row["Betrag"] == "2,000000" and row["Währung"] == "USD" and row["Betrag EUR"] == ""
    all_rows = admin_client.get(url).content.decode("utf-8-sig").strip().split("\n")
    assert len(all_rows) == 4


def test_csv_needs_admin(client, member):
    client.force_login(member)
    response = client.get(reverse("admin:billing_usageentry_csv"))
    assert response.status_code in (302, 403)


# --- EZB (optional, standardmäßig aus) ---------------------------------------------

ECB_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope><Cube><Cube time='2026-10-09'>
<Cube currency='USD' rate='1.1630'/><Cube currency='JPY' rate='170.1'/>
</Cube></Cube></gesmes:Envelope>"""


def test_ecb_parse_and_disabled_by_default(settings):
    assert ecb.parse(ECB_SAMPLE) == ("2026-10-09", Decimal("0.859845"))
    with pytest.raises(ecb.EcbError):
        ecb.parse("<x/>")
    settings.BILLING_ECB_FETCH = False
    with pytest.raises(ecb.EcbError, match="ausgeschaltet"):
        ecb.fetch()


def test_ecb_fetch_only_fixed_url_without_redirects(settings):
    settings.BILLING_ECB_FETCH = True
    with respx.mock(assert_all_called=True) as mock:
        mock.get(ecb.ECB_URL).mock(return_value=httpx.Response(200, text=ECB_SAMPLE))
        rate_obj, created = ecb.fetch()
    assert created and rate_obj.source == "ecb" and rate_obj.usd_eur == Decimal("0.859845")
    with respx.mock() as mock:
        mock.get(ecb.ECB_URL).mock(
            return_value=httpx.Response(302, headers={"Location": "http://intern.local/"})
        )
        with pytest.raises(ecb.EcbError, match="HTTP 302"):
            ecb.fetch()


def test_budget_snapshot_without_budgets_needs_no_usage_query(member, django_assert_num_queries):
    with django_assert_num_queries(1):  # nur die Budgets je Konto
        snap = budgets.snapshot(member)
    assert snap.total is None and snap.accounts == {}


def test_json_units_in_entry(member, claude):
    entry = book(member, claude, Usage(10, 10, units={"web_search": 2}))
    assert entry.units == {"web_search": 2}
    assert json.loads(json.dumps(entry.rounds)) == [{"in": 10, "out": 10}]
