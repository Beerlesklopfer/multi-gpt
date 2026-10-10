"""Preisberechnung (M6-07): Preisversion wählen und Betrag einer Buchung rechnen.

Regeln (Quellen und Stand in docs/wiki/Kosten-und-Budgets.md):

- Preise je 1 Mio. Tokens in der Währung des Kontos.
- Eingabe ohne Cache = ``tokens_in - cached_read - cache_write - cache_write_1h``
  zum Eingabepreis; gelesene Cache-Tokens zum Preis „Eingabe aus Cache“,
  geschriebene zum Preis „Cache schreiben“ (5 Min. bzw. 1 Std.). Fehlt ein
  Cache-Preis, gilt der Eingabepreis (bzw. für 1 Std. der 5-Min.-Preis).
- Ausgabe einschließlich Reasoning/Thinking zum Ausgabepreis (Anthropic,
  OpenAI und Gemini rechnen Nachdenk-Tokens als Ausgabe ab).
- Langkontext: Liegt die *gesamte* Eingabe einer einzelnen Anfrage (inkl.
  Cache) über der Schwelle, gelten für diese Anfrage alle Langkontext-Preise
  (leere Langkontext-Preise: Normalpreis). Deshalb rechnet eine Antwort mit
  Werkzeugrunden je Anbieteraufruf (``rounds``).
- Einheiten (``unit_prices``): Menge / Menge-je-Preis × Preis; Varianten wie
  ``image:high:1024x1024`` fallen auf ``image:high`` und ``image`` zurück.
  Einheiten ohne Preis kosten nichts.
- Ohne Preisversion oder mit leerer Version: Betrag unbekannt (None).
  Eingabe- oder Ausgabepreis allein: der andere zählt 0 (wie bisher).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone

from .models import UNITS, BillingAccount, ModelPrice, validate_unit_key

MILLION = Decimal(1_000_000)
AMOUNT_STEP = Decimal("0.000001")
ZERO = Decimal(0)


@dataclass
class Round:
    """Tokens eines Anbieteraufrufs (Felder wie ``providers.base.Usage``)."""

    tokens_in: int = 0
    tokens_out: int = 0
    cached_read: int = 0
    cache_write: int = 0
    cache_write_1h: int = 0
    reasoning: int = 0

    KEYS = {
        "in": "tokens_in",
        "out": "tokens_out",
        "cr": "cached_read",
        "cw": "cache_write",
        "cw1h": "cache_write_1h",
        "re": "reasoning",
    }

    @classmethod
    def from_usage(cls, usage) -> Round:
        return cls(
            **{name: max(int(getattr(usage, name, 0) or 0), 0) for name in cls.KEYS.values()}
        )

    @classmethod
    def from_json(cls, data: dict) -> Round:
        return cls(**{name: int(data.get(key) or 0) for key, name in cls.KEYS.items()})

    def to_json(self) -> dict:
        return {key: getattr(self, name) for key, name in self.KEYS.items() if getattr(self, name)}

    @property
    def uncached_in(self) -> int:
        return max(self.tokens_in - self.cached_read - self.cache_write - self.cache_write_1h, 0)


@dataclass
class Tally:
    """Verbrauch einer Buchung: Anbieteraufrufe und Einheiten."""

    rounds: list[Round] = field(default_factory=list)
    units: dict = field(default_factory=dict)

    def add(self, usage) -> None:
        """Usage eines Anbieteraufrufs hinzufügen (``providers.base.Usage``)."""
        self.rounds.append(Round.from_usage(usage))
        for unit, qty in (getattr(usage, "units", None) or {}).items():
            try:
                validate_unit_key(unit)
                qty = Decimal(str(qty))
            except (ValueError, ArithmeticError):
                continue
            if qty > 0:
                self.units[unit] = _plain(Decimal(str(self.units.get(unit, 0))) + qty)

    def total(self, name: str) -> int:
        return sum(getattr(r, name) for r in self.rounds)

    def totals(self) -> dict:
        return {name: self.total(name) for name in Round.KEYS.values()}


def _plain(value: Decimal):
    """Menge für JSON: ganze Zahl, wenn möglich, sonst String."""
    return int(value) if value == value.to_integral_value() else str(value)


def price_at(ai_model, when=None) -> ModelPrice | None:
    """Gültige Preisversion des Modells zum Zeitpunkt ``when`` (Standard: jetzt)."""
    if ai_model is None or ai_model.pk is None:
        return None
    when = when or timezone.now()
    cache = getattr(ai_model, "_prices_cache", None)
    if cache is not None:  # vorgeladen (prefetch), ohne weitere Abfrage
        candidates = [p for p in cache if p.valid_from <= when]
        return max(candidates, key=lambda p: p.valid_from, default=None)
    return (
        ModelPrice.objects.filter(ai_model=ai_model, valid_from__lte=when)
        .order_by("-valid_from")
        .first()
    )


def _rate(price: ModelPrice, name: str, long: bool) -> Decimal:
    """Preis je 1 Mio. Tokens mit Rückfall (Cache -> Eingabe, Langkontext -> normal)."""
    fallback = {
        "cached_input": "input",
        "cache_write": "input",
        "cache_write_1h": "cache_write",
    }
    current = name
    while current:
        value = getattr(price, f"long_{current}") if long else None
        if value is None:
            value = getattr(price, current)
        if value is not None:
            return value
        current = fallback.get(current)
    return ZERO


def is_long(price: ModelPrice, rnd: Round) -> bool:
    threshold = price.long_context_threshold
    return bool(threshold) and rnd.tokens_in > threshold


def round_amount(price: ModelPrice, rnd: Round) -> Decimal:
    long = is_long(price, rnd)
    total = (
        rnd.uncached_in * _rate(price, "input", long)
        + rnd.cached_read * _rate(price, "cached_input", long)
        + rnd.cache_write * _rate(price, "cache_write", long)
        + rnd.cache_write_1h * _rate(price, "cache_write_1h", long)
        + rnd.tokens_out * _rate(price, "output", long)
    )
    return Decimal(total) / MILLION


def unit_price(price: ModelPrice, unit: str) -> Decimal | None:
    """Preis je Einheit mit Rückfall auf kürzere Schlüssel (image:high:1024 -> image)."""
    prices = price.unit_prices if isinstance(price.unit_prices, dict) else {}
    key = unit
    while key:
        if key in prices:
            try:
                return Decimal(str(prices[key]))
            except ArithmeticError:
                return None
        key = key.rpartition(":")[0]
    return None


def units_amount(price: ModelPrice, units: dict) -> Decimal:
    total = ZERO
    for unit, qty in (units or {}).items():
        try:
            base = validate_unit_key(unit)
            qty = Decimal(str(qty))
        except (ValueError, ArithmeticError):
            continue
        value = unit_price(price, unit)
        if value is not None:
            total += qty / UNITS[base][1] * value
    return total


def amount(price: ModelPrice | None, tally: Tally) -> Decimal | None:
    """Betrag in der Kontowährung; None ohne (bzw. mit leerer) Preisversion."""
    if price is None or price.is_empty:
        return None
    total = sum((round_amount(price, r) for r in tally.rounds), ZERO)
    total += units_amount(price, tally.units)
    return total.quantize(AMOUNT_STEP, rounding=ROUND_HALF_UP)


def quote(account: BillingAccount, price: ModelPrice | None, tally: Tally) -> Decimal | None:
    """Betrag je Kontoart: monetär nach Preis, Token- und Pauschalkonten None."""
    if account is None or account.kind != BillingAccount.Kind.MONETARY:
        return None
    return amount(price, tally)


def describe(price: ModelPrice | None, account: BillingAccount | None) -> str:
    """Kurze Preisinfo für den title der Modellauswahl, z. B.
    „OpenAI (USD) · 2,50 / 10,00 je 1 Mio. Tokens“."""
    if account is None:
        return ""
    if account.kind == BillingAccount.Kind.TOKENS:
        return f"Konto {account.name} · nur Tokens gezählt"
    if account.kind == BillingAccount.Kind.FLAT:
        return f"Konto {account.name} · Pauschale"
    head = f"Konto {account.name} ({account.currency})"
    if price is None or price.is_empty:
        return f"{head} · kein Preis hinterlegt"
    parts = []
    if price.input is not None or price.output is not None:
        parts.append(
            f"Ein {_num(price.input)} / Aus {_num(price.output)} "
            f"{account.currency} je 1 Mio. Tokens"
        )
    if price.cached_input is not None:
        parts.append(f"Cache {_num(price.cached_input)}")
    if price.long_context_threshold:
        parts.append(f"ab {price.long_context_threshold:,} Tokens Langkontext".replace(",", "."))
    return " · ".join([head, *parts])


def _num(value) -> str:
    if value is None:
        return "–"
    text = f"{Decimal(value).normalize():f}"
    if "." in text and len(text.split(".")[1]) < 2:
        text = f"{Decimal(value):.2f}"
    elif "." not in text:
        text = f"{Decimal(value):.2f}"
    return text.replace(".", ",")
