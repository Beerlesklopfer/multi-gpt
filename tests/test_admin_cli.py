"""Verwaltungsbefehle für mgpt-ctl: prices, providers, models, budgets, sandbox_check."""

import io
from decimal import Decimal

import pytest
from django.core.management import CommandError, call_command

from multigpt.accounts.models import Role
from multigpt.billing import provider_costs
from multigpt.billing.models import BillingAccount, ModelPrice
from multigpt.chat import sandbox
from multigpt.chat.models import AIModel, Provider
from tests.test_api_stream import make_user

pytestmark = pytest.mark.django_db


def run(*args) -> str:
    out = io.StringIO()
    call_command(*args, stdout=out)
    return out.getvalue()


@pytest.fixture
def cloud():
    account = BillingAccount.objects.create(name="Claude-Konto", kind="monetary", currency="EUR")
    provider = Provider.objects.create(name="Claude", kind="anthropic", billing_account=account)
    return AIModel.objects.create(
        provider=provider, model_id="claude-opus-5-5", display_name="Opus"
    )


@pytest.fixture
def local():
    provider = Provider.objects.create(
        name="Eliezer", kind="lmstudio", base_url="http://gpu.invalid:1234", is_local=True
    )
    return AIModel.objects.create(
        provider=provider, model_id="openai/gpt-oss-20b", display_name="gpt-oss"
    )


def test_prices_suggest_preview_and_apply(cloud, monkeypatch):
    monkeypatch.setattr(provider_costs, "fetch_catalog", lambda: {"claude-opus-5-5": {}})
    suggestion = provider_costs.Suggestion(
        cloud, None, Decimal(4), Decimal(20), Decimal("0.2"), None
    )
    monkeypatch.setattr(provider_costs, "suggest", lambda m, c: suggestion)
    preview = run("prices", "suggest")
    assert "neu: Ein 4 / Aus 20" in preview and "--apply" in preview
    assert not ModelPrice.objects.filter(ai_model=cloud).exists()
    applied = run("prices", "suggest", "--apply")
    assert "1 neue Preisversion" in applied
    price = ModelPrice.objects.get(ai_model=cloud)
    assert price.input == Decimal(4) and price.currency == "USD"
    assert "kein Preis" not in run("prices", "list")
    with pytest.raises(CommandError, match="Unbekannte Modelle"):
        run("prices", "suggest", "gibts-nicht")


def test_providers_set_and_validation(local):
    with pytest.raises(CommandError, match="Speicherbudget"):
        run("providers", "set", "eliezer", "--manage-models", "an")
    out = run(
        "providers",
        "set",
        "Eliezer",
        "--manage-models",
        "an",
        "--vram-budget",
        "24",
        "--vram-reserve",
        "2,0",
        "--check-status",
        "aus",
    )
    assert "Gespeichert" in out
    provider = Provider.objects.get(name="Eliezer")
    assert provider.manage_models and provider.vram_budget_gb == Decimal("24")
    assert provider.vram_reserve_gb == Decimal("2.0") and not provider.check_status
    assert "Eliezer" in run("providers", "list")
    for bad in (("--active", "vielleicht"), ("--vram-budget", "viel")):
        with pytest.raises(CommandError):
            run("providers", "set", "Eliezer", *bad)
    with pytest.raises(CommandError, match="Unbekannter Anbieter"):
        run("providers", "set", "Nirgendwo", "--active", "an")


def test_models_set(local):
    run("models", "set", "openai/gpt-oss-20b", "--load-priority", "60", "--pinned", "an")
    local.refresh_from_db()
    assert local.load_priority == 60 and local.pinned
    with pytest.raises(CommandError):
        run("models", "set", "openai/gpt-oss-20b", "--load-priority", "101")
    with pytest.raises(CommandError, match="Nichts zu ändern"):
        run("models", "set", "openai/gpt-oss-20b")
    assert "gpt-oss-20b" in run("models", "list", "--provider", "eliezer")


def test_budgets_role_and_user():
    run("budgets", "set", "--role", "adult", "50")
    assert Role.objects.get(key="adult").monthly_budget == Decimal("50")
    user = make_user("adult", "Lena")
    run("budgets", "set", "--user", "lena", "12,50")
    user.refresh_from_db()
    assert user.monthly_budget_override == Decimal("12.50")
    listing = run("budgets", "list")
    assert "50,00 €" in listing and "12,50 €" in listing
    run("budgets", "set", "--user", "Lena", "aus")
    user.refresh_from_db()
    assert user.monthly_budget_override is None
    with pytest.raises(CommandError):
        run("budgets", "set", "--role", "gibtsnicht", "5")


def test_sandbox_check(monkeypatch):
    monkeypatch.setattr(sandbox, "status", lambda refresh=False: sandbox.Status(True))
    assert "nutzbar" in run("sandbox_check")
    monkeypatch.setattr(
        sandbox, "status", lambda refresh=False: sandbox.Status(False, "kein bwrap")
    )
    with pytest.raises(CommandError, match="kein bwrap"):
        run("sandbox_check")
