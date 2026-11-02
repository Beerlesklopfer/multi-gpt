"""Kosten von den Anbietern abfragen: Preisvorschläge, Guthaben, Tagesabgleich.

Drei Quellen, alle mit festen HTTPS-Adressen (keine Eingabe des Nutzers in der URL):

- Preisliste von LiteLLM: Die Anbieter nennen ihre Preise nicht per API. Die Liste wird von
  der Community gepflegt, darum sind es Vorschläge: Gespeichert wird erst nach Bestätigung,
  immer als neue ``ModelPrice``-Version, nie rückwirkend.
- Guthaben: DeepSeek ``/user/balance`` mit dem API-Key des Anbieters.
- Istkosten je Tag: OpenAI ``/organization/costs`` und Anthropic ``/analytics/cost`` brauchen
  einen Admin-Schlüssel (``BillingAccount.cost_key``). ``CostReport`` stellt sie den eigenen
  Buchungen gegenüber; Buchungen ändert der Abgleich nie.

Den täglichen Abruf übernimmt der Worker (``auto_sync``, abschaltbar mit
``BILLING_COST_SYNC=false``).
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import httpx
from django.conf import settings
from django.db import DatabaseError
from django.db.models import Count, Q, Sum
from django.utils import timezone

from .models import BillingAccount, CostReport, ModelPrice, UsageEntry

CATALOG_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)
CATALOG_MAX_BYTES = 64 * 1024 * 1024
TIMEOUT = 30.0
REPORT_DAYS = 7  # Nachzügler der Anbieter: die letzten Tage werden neu geholt
SYNC_INTERVAL = dt.timedelta(hours=24)
SYNC_RETRY = dt.timedelta(hours=1)

OPENAI_COSTS_URL = "https://api.openai.com/v1/organization/costs"
ANTHROPIC_COSTS_URL = "https://api.anthropic.com/v1/organizations/analytics/cost"
ANTHROPIC_BETA = "analytics-usage-cost-2026-08-24"
DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"

MILLION = Decimal(1_000_000)
MICRO = Decimal("0.000001")

logger = logging.getLogger(__name__)


class CostError(Exception):
    """Abfrage gescheitert; die Meldung ist für den Verwalter und enthält keine Schlüssel."""


def vendor(provider) -> str:
    """Anbieter im Sinne der Preisliste und der Kosten-APIs; leer = unbekannt."""
    if provider.kind == "anthropic":
        return "anthropic"
    if provider.kind == "google":
        return "gemini"
    host = urlsplit(provider.base_url or "").hostname or ""
    if host == "api.openai.com":
        return "openai"
    if host == "api.deepseek.com":
        return "deepseek"
    return ""


def _get(url: str, *, headers: dict, params: dict | None = None) -> dict:
    try:
        response = httpx.get(url, headers=headers, params=params, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        raise CostError(f"Keine Verbindung ({type(exc).__name__}).") from exc
    if response.status_code in {401, 403}:
        raise CostError(f"Schlüssel abgelehnt (HTTP {response.status_code}), Rechte prüfen.")
    if response.status_code != 200:
        raise CostError(f"Antwort HTTP {response.status_code}.")
    try:
        data = response.json()
    except ValueError as exc:
        raise CostError("Antwort ist kein JSON.") from exc
    if not isinstance(data, dict):
        raise CostError("Unerwartete Antwort.")
    return data


# --- Preisvorschläge ---------------------------------------------------------------------


@dataclass
class Suggestion:
    ai_model: object
    current: ModelPrice | None
    input: Decimal
    output: Decimal
    cached_input: Decimal | None
    cache_write: Decimal | None

    def _values(self) -> dict:
        return {
            "input": self.input,
            "output": self.output,
            "cached_input": self.cached_input,
            "cache_write": self.cache_write,
        }

    @property
    def changed(self) -> bool:
        if self.current is None:
            return True
        return any(getattr(self.current, k) != v for k, v in self._values().items())

    def describe(self) -> str:
        cached = f" · Cache {self.cached_input.normalize():f}" if self.cached_input else ""
        return (
            f"Ein {self.input.normalize():f} / Aus {self.output.normalize():f}{cached} "
            "USD je 1 Mio. Tokens"
        )

    def apply(self, note: str) -> ModelPrice:
        """Neue Preisversion ab jetzt; Gebühren und Langkontext der alten bleiben."""
        price = ModelPrice(ai_model=self.ai_model)
        if self.current is not None:
            for field in ModelPrice._meta.concrete_fields:
                if field.name not in {"id", "ai_model", "valid_from", "note"}:
                    setattr(price, field.name, getattr(self.current, field.name))
        for name, value in self._values().items():
            setattr(price, name, value)
        price.currency = "USD"
        price.note = note
        price.save()
        return price


def fetch_catalog() -> dict:
    try:
        response = httpx.get(CATALOG_URL, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        raise CostError(f"Preisliste nicht erreichbar ({type(exc).__name__}).") from exc
    if response.status_code != 200 or len(response.content) > CATALOG_MAX_BYTES:
        raise CostError(f"Preisliste nicht lesbar (HTTP {response.status_code}).")
    try:
        data = response.json()
    except ValueError as exc:
        raise CostError("Preisliste ist kein JSON.") from exc
    if not isinstance(data, dict):
        raise CostError("Preisliste hat ein unerwartetes Format.")
    return data


def _per_million(value) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        price = (Decimal(str(value)) * MILLION).quantize(MICRO)
    except (InvalidOperation, ValueError):
        return None
    return price if price >= 0 else None


def suggest(ai_model, catalog: dict) -> Suggestion | None:
    """Vorschlag für ein Chat-Modell; None, wenn die Liste es nicht eindeutig kennt.

    Der Eintrag muss zum Anbieter gehören: Gleich benannte Modelle anderer Anbieter
    (Azure, Bedrock, OpenRouter) haben andere Preise."""
    name = vendor(ai_model.provider)
    account = ai_model.provider.billing_account
    if not name or account is None or not account.is_monetary:
        return None
    for key in (ai_model.model_id, f"{name}/{ai_model.model_id}"):
        entry = catalog.get(key)
        if isinstance(entry, dict) and entry.get("litellm_provider") == name:
            break
    else:
        return None
    if entry.get("mode") != "chat":
        return None
    prices = {
        "input": _per_million(entry.get("input_cost_per_token")),
        "output": _per_million(entry.get("output_cost_per_token")),
    }
    if prices["input"] is None or prices["output"] is None:
        return None
    return Suggestion(
        ai_model=ai_model,
        current=ai_model.prices.order_by("-valid_from").first(),
        cached_input=_per_million(entry.get("cache_read_input_token_cost")),
        cache_write=_per_million(entry.get("cache_creation_input_token_cost")),
        **prices,
    )


# --- Guthaben ----------------------------------------------------------------------------


def fetch_balance(api_key: str) -> tuple[Decimal, str, bool]:
    """DeepSeek: (Guthaben, Währung, reicht für Anfragen)."""
    data = _get(DEEPSEEK_BALANCE_URL, headers={"Authorization": f"Bearer {api_key}"})
    infos = data.get("balance_infos")
    if not isinstance(infos, list) or not infos or not isinstance(infos[0], dict):
        raise CostError("Antwort ohne Guthaben.")
    preferred = [i for i in infos if isinstance(i, dict) and i.get("currency") == "USD"]
    info = (preferred or infos)[0]
    try:
        total = Decimal(str(info.get("total_balance")))
    except InvalidOperation as exc:
        raise CostError("Guthaben ist keine Zahl.") from exc
    return total, str(info.get("currency") or ""), data.get("is_available") is True


def update_balance(account: BillingAccount) -> bool:
    """Guthaben speichern, wenn das Konto einen Anbieter mit Guthaben-API hat."""
    for provider in account.providers.filter(active=True, is_local=False):
        if vendor(provider) == "deepseek" and provider.api_key:
            total, currency, available = fetch_balance(provider.api_key)
            account.balance = total
            account.balance_currency = currency
            account.balance_available = available
            account.balance_checked = timezone.now()
            account.save(
                update_fields=[
                    "balance",
                    "balance_currency",
                    "balance_available",
                    "balance_checked",
                ]
            )
            return True
    return False


# --- Istkosten je Tag --------------------------------------------------------------------


def _utc(day: dt.date) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, tzinfo=dt.UTC)


def _openai_costs(key: str, start: dt.date, end: dt.date) -> dict[dt.date, Decimal]:
    days: dict[dt.date, Decimal] = {}
    params = {
        "start_time": int(_utc(start).timestamp()),
        "end_time": int(_utc(end).timestamp()),
        "bucket_width": "1d",
        "limit": 180,
    }
    while True:
        data = _get(OPENAI_COSTS_URL, headers={"Authorization": f"Bearer {key}"}, params=params)
        for bucket in data.get("data") or []:
            day = dt.datetime.fromtimestamp(bucket["start_time"], dt.UTC).date()
            total = days.setdefault(day, Decimal(0))
            for result in bucket.get("results") or []:
                amount = result.get("amount") or {}
                if str(amount.get("currency", "usd")).lower() != "usd":
                    raise CostError("OpenAI meldet eine andere Währung als USD.")
                total += Decimal(str(amount.get("value") or 0))
            days[day] = total
        if not data.get("has_more") or not data.get("next_page"):
            return days
        params = {**params, "page": data["next_page"]}


def _anthropic_costs(key: str, start: dt.date, end: dt.date) -> dict[dt.date, Decimal]:
    """Beträge kommen in Cent als Dezimaltext, ``"41280.000000"`` sind 412,80 USD."""
    days = {start + dt.timedelta(days=offset): Decimal(0) for offset in range((end - start).days)}
    headers = {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": ANTHROPIC_BETA,
    }
    params = {
        "starting_at": _utc(start).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ending_at": _utc(end).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "time_bucket_width": "1d",
        "limit": 1000,
    }
    while True:
        data = _get(ANTHROPIC_COSTS_URL, headers=headers, params=params)
        for row in data.get("data") or []:
            day = dt.datetime.fromisoformat(row["starting_at"].replace("Z", "+00:00")).date()
            cents = Decimal(str((row.get("cost") or {}).get("amount") or 0))
            days[day] = days.get(day, Decimal(0)) + cents / 100
        if not data.get("has_more") or not data.get("next_page"):
            return days
        params = {**params, "page": data["next_page"]}


COST_FETCHERS = {"openai": _openai_costs, "anthropic": _anthropic_costs}


def fetch_costs(account: BillingAccount, start: dt.date, end: dt.date) -> dict[dt.date, Decimal]:
    for provider in account.providers.filter(is_local=False):
        fetcher = COST_FETCHERS.get(vendor(provider))
        if fetcher is not None:
            try:
                return fetcher(account.cost_key, start, end)
            except (KeyError, TypeError, ValueError, AttributeError, InvalidOperation) as exc:
                raise CostError("Antwort mit unerwartetem Aufbau.") from exc
    raise CostError("Das Konto hat keinen Anbieter mit Kosten-API (OpenAI, Anthropic).")


def update_reports(account: BillingAccount, today: dt.date | None = None) -> int:
    """Letzte abgeschlossene UTC-Tage holen; der laufende Tag wäre noch unvollständig."""
    end = today or timezone.now().astimezone(dt.UTC).date()
    start = end - dt.timedelta(days=REPORT_DAYS)
    costs = fetch_costs(account, start, end)
    for day, amount in costs.items():
        booked = UsageEntry.objects.filter(
            account=account,
            created__gte=_utc(day),
            created__lt=_utc(day + dt.timedelta(days=1)),
        ).aggregate(
            total=Sum("amount", filter=Q(currency="USD")),
            unpriced=Count("pk", filter=Q(amount__isnull=True)),
        )
        CostReport.objects.update_or_create(
            account=account,
            date=day,
            defaults={
                "amount": amount.quantize(MICRO),
                "booked": booked["total"] or Decimal(0),
                "unpriced": booked["unpriced"],
            },
        )
    return len(costs)


def sync_account(account: BillingAccount) -> list[str]:
    """Guthaben und Istkosten eines Kontos; liefert Fehlermeldungen je Teil."""
    errors = []
    try:
        update_balance(account)
    except CostError as exc:
        errors.append(f"Guthaben: {exc}")
    if account.cost_key:
        try:
            update_reports(account)
        except CostError as exc:
            errors.append(f"Istkosten: {exc}")
    return errors


class AutoSync:
    """Für den Worker: einmal täglich, nach einem Fehler stündlich."""

    def __init__(self):
        self.next_run: dt.datetime | None = None

    def run(self, now: dt.datetime | None = None) -> None:
        now = now or timezone.now()
        if not settings.BILLING_COST_SYNC or (self.next_run and now < self.next_run):
            return
        failed = False
        for account in BillingAccount.objects.filter(active=True, kind="monetary"):
            try:
                errors = sync_account(account)
            except DatabaseError:
                raise
            except Exception as exc:  # die Wartung des Workers läuft weiter
                errors = [type(exc).__name__]
            for error in errors:
                failed = True
                logger.warning("Kostenabgleich %s: %s", account.name, error)
        self.next_run = now + (SYNC_RETRY if failed else SYNC_INTERVAL)


_auto = AutoSync()


def auto_sync(now: dt.datetime | None = None) -> None:
    _auto.run(now)
