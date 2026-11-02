"""Kosten der Anbieter (billing/provider_costs.py): Preisvorschläge, Guthaben, Tagesabgleich.

HTTP über respx mit den Beispielen aus den Dokus (OpenAI-Spec, Anthropic Usage and Cost
Analytics API, DeepSeek get-user-balance).
"""

import datetime as dt
from decimal import Decimal

import httpx
import pytest
import respx
from django.urls import reverse

from multigpt.billing import provider_costs
from multigpt.billing.models import BillingAccount, CostReport, ModelPrice, UsageEntry
from multigpt.chat.models import AIModel, Provider

CATALOG = {
    "gpt-5": {
        "litellm_provider": "openai",
        "mode": "chat",
        "input_cost_per_token": 1.25e-06,
        "output_cost_per_token": 1e-05,
        "cache_read_input_token_cost": 1.25e-07,
    },
    "deepseek/deepseek-v4-pro": {
        "litellm_provider": "deepseek",
        "mode": "chat",
        "input_cost_per_token": 1.32e-06,
        "output_cost_per_token": 3.96e-06,
    },
    "azure-only": {
        "litellm_provider": "azure",
        "mode": "chat",
        "input_cost_per_token": 1e-06,
        "output_cost_per_token": 1e-06,
    },
    "image-model": {
        "litellm_provider": "openai",
        "mode": "image_generation",
        "input_cost_per_token": 1e-06,
    },
}


def make_model(kind="openai_compat", url="https://api.openai.com/v1", model_id="gpt-5", key="k"):
    account = BillingAccount.objects.create(name=f"Konto {model_id}", currency="USD")
    provider = Provider.objects.create(
        name=f"Anbieter {model_id}", kind=kind, base_url=url, api_key=key, billing_account=account
    )
    return AIModel.objects.create(provider=provider, model_id=model_id, display_name=model_id)


@pytest.mark.django_db
def test_suggest_converts_to_prices_per_million():
    suggestion = provider_costs.suggest(make_model(), CATALOG)
    assert suggestion.input == Decimal("1.25") and suggestion.output == Decimal("10")
    assert suggestion.cached_input == Decimal("0.125") and suggestion.cache_write is None
    assert suggestion.changed


@pytest.mark.django_db
def test_suggest_finds_prefixed_entries_of_the_same_vendor():
    model = make_model(url="https://api.deepseek.com/v1", model_id="deepseek-v4-pro")
    assert provider_costs.suggest(model, CATALOG).output == Decimal("3.96")


@pytest.mark.django_db
@pytest.mark.parametrize("model_id", ["azure-only", "image-model", "unknown"])
def test_suggest_ignores_foreign_or_unusable_entries(model_id):
    assert provider_costs.suggest(make_model(model_id=model_id), CATALOG) is None


@pytest.mark.django_db
def test_suggest_skips_accounts_that_do_not_bill_money():
    model = make_model()
    account = model.provider.billing_account
    account.kind, account.currency = BillingAccount.Kind.TOKENS, ""
    account.save()
    assert provider_costs.suggest(model, CATALOG) is None


@pytest.mark.django_db
def test_apply_adds_a_version_and_keeps_fees_of_the_old_one():
    model = make_model()
    old = ModelPrice.objects.create(
        ai_model=model,
        valid_from=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        input=Decimal(1),
        output=Decimal(2),
        unit_prices={"web_search": 0.01},
    )
    new = provider_costs.suggest(model, CATALOG).apply("Liste")
    old.refresh_from_db()
    assert old.input == Decimal(1)
    assert new.pk != old.pk and new.input == Decimal("1.25") and new.currency == "USD"
    assert new.unit_prices == {"web_search": 0.01} and new.note == "Liste"
    assert not provider_costs.suggest(model, CATALOG).changed


@pytest.mark.django_db
@respx.mock
def test_balance_of_deepseek_is_stored():
    model = make_model(url="https://api.deepseek.com/v1", model_id="deepseek-v4-pro", key="sk-d")
    route = respx.get(provider_costs.DEEPSEEK_BALANCE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "is_available": True,
                "balance_infos": [
                    {"currency": "CNY", "total_balance": "110.00"},
                    {"currency": "USD", "total_balance": "12.50"},
                ],
            },
        )
    )
    account = model.provider.billing_account
    assert provider_costs.update_balance(account)
    account.refresh_from_db()
    assert account.balance == Decimal("12.5") and account.balance_currency == "USD"
    assert account.balance_available is True and account.balance_checked
    assert route.calls[0].request.headers["authorization"] == "Bearer sk-d"


@pytest.mark.django_db
@respx.mock
def test_errors_do_not_contain_the_key():
    model = make_model(
        url="https://api.deepseek.com/v1", model_id="deepseek-v4-pro", key="sk-geheim"
    )
    account = model.provider.billing_account
    respx.get(provider_costs.DEEPSEEK_BALANCE_URL).mock(
        return_value=httpx.Response(401, text="bad key sk-geheim")
    )
    errors = provider_costs.sync_account(account)
    assert len(errors) == 1 and "sk-geheim" not in errors[0] and "401" in errors[0]


def _usage(account, day, amount):
    return UsageEntry.objects.create(
        account=account,
        created=dt.datetime(day.year, day.month, day.day, 12, tzinfo=dt.UTC),
        amount=amount,
        currency="USD" if amount is not None else "",
    )


@pytest.mark.django_db
@respx.mock
def test_openai_report_compares_with_bookings_and_pages():
    day = dt.date(2026, 11, 1)
    stamp = int(dt.datetime(2026, 11, 1, tzinfo=dt.UTC).timestamp())
    model = make_model()
    account = model.provider.billing_account
    account.cost_key = "sk-admin-x"
    account.save()
    _usage(account, day, Decimal("1.50"))
    _usage(account, day, None)
    first = {
        "object": "page",
        "has_more": True,
        "next_page": "p2",
        "data": [
            {
                "object": "bucket",
                "start_time": stamp,
                "end_time": stamp + 86400,
                "results": [
                    {
                        "object": "organization.costs.result",
                        "amount": {"value": 2.25, "currency": "usd"},
                    }
                ],
            }
        ],
    }
    second = {"object": "page", "has_more": False, "next_page": None, "data": []}
    route = respx.get(provider_costs.OPENAI_COSTS_URL).mock(
        side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=second)]
    )
    assert provider_costs.update_reports(account, today=dt.date(2026, 11, 2)) == 1
    report = CostReport.objects.get()
    assert report.date == day and report.amount == Decimal("2.25")
    assert report.booked == Decimal("1.5") and report.unpriced == 1
    assert report.difference == Decimal("0.75")
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer sk-admin-x"
    assert request.url.params["bucket_width"] == "1d"
    assert route.calls[1].request.url.params["page"] == "p2"


@pytest.mark.django_db
@respx.mock
def test_anthropic_amounts_are_cents():
    model = make_model(kind="anthropic", url="", model_id="claude-x")
    account = model.provider.billing_account
    account.cost_key = "sk-ant-admin-x"
    account.save()
    route = respx.get(provider_costs.ANTHROPIC_COSTS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "starting_at": "2026-11-01T00:00:00Z",
                        "ending_at": "2026-11-02T00:00:00Z",
                        "cost": {"amount": "41280.000000", "currency": "USD"},
                    }
                ],
                "has_more": False,
                "next_page": None,
            },
        )
    )
    provider_costs.update_reports(account, today=dt.date(2026, 11, 2))
    report = CostReport.objects.get(date=dt.date(2026, 11, 1))
    assert report.amount == Decimal("412.8")
    assert CostReport.objects.filter(amount=0).count() == provider_costs.REPORT_DAYS - 1
    headers = route.calls[0].request.headers
    assert headers["x-api-key"] == "sk-ant-admin-x"
    assert headers["anthropic-beta"] == provider_costs.ANTHROPIC_BETA


@pytest.mark.django_db
@respx.mock
def test_no_key_means_no_cost_request():
    account = make_model().provider.billing_account
    route = respx.get(provider_costs.OPENAI_COSTS_URL)
    assert provider_costs.sync_account(account) == [] and not route.called


@pytest.mark.django_db
def test_auto_sync_runs_once_a_day_and_can_be_switched_off(settings, monkeypatch):
    make_model()
    calls = []
    monkeypatch.setattr(provider_costs, "sync_account", lambda a: calls.append(a) or [])
    auto = provider_costs.AutoSync()
    now = dt.datetime(2026, 11, 2, 12, tzinfo=dt.UTC)
    auto.run(now)
    assert calls == []  # conftest: aus
    settings.BILLING_COST_SYNC = True
    auto.run(now)
    auto.run(now + dt.timedelta(hours=1))
    assert len(calls) == 1
    auto.run(now + dt.timedelta(hours=25))
    assert len(calls) == 2


@pytest.mark.django_db
def test_auto_sync_retries_sooner_after_a_failure(settings, monkeypatch):
    settings.BILLING_COST_SYNC = True
    make_model()
    monkeypatch.setattr(provider_costs, "sync_account", lambda a: ["kaputt"])
    auto = provider_costs.AutoSync()
    now = dt.datetime(2026, 11, 2, 12, tzinfo=dt.UTC)
    auto.run(now)
    assert auto.next_run == now + provider_costs.SYNC_RETRY


@pytest.mark.django_db
@respx.mock
def test_admin_preview_saves_only_after_confirmation(admin_client):
    model = make_model()
    respx.get(provider_costs.CATALOG_URL).mock(return_value=httpx.Response(200, json=CATALOG))
    url = reverse("admin:chat_aimodel_changelist")
    data = {"action": "suggest_prices_action", "_selected_action": [model.pk]}
    page = admin_client.post(url, data)
    assert "Ein 1.25 / Aus 10" in page.content.decode() and not model.prices.exists()
    admin_client.post(url, {**data, "apply": "1"})
    assert model.prices.get().note.startswith("LiteLLM-Preisliste")


@pytest.mark.django_db
def test_admin_keeps_the_key_secret_and_lists_reports(admin_client):
    account = make_model().provider.billing_account
    account.cost_key = "sk-admin-geheim"
    account.save()
    CostReport.objects.create(
        account=account, date=dt.date(2026, 11, 1), amount=Decimal(3), booked=Decimal(1)
    )
    page = admin_client.get(reverse("admin:billing_billingaccount_change", args=[account.pk]))
    assert "sk-admin-geheim" not in page.content.decode()
    assert page.context["adminform"].form.fields["cost_key"]
    listing = admin_client.get(reverse("admin:billing_costreport_changelist"))
    assert listing.status_code == 200
