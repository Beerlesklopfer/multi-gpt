"""Optionaler Abruf des EZB-Referenzkurses USD (M6-09), standardmäßig AUS.

Einschalten über ``BILLING_ECB_FETCH=true`` in der Umgebung; dann gibt es im
Admin unter „Wechselkurse“ den Knopf „EZB-Kurs abrufen“ und den Befehl
``manage.py fetch_ecb_rate`` (z. B. werktags per Timer).

SSRF-sicher: Abgerufen wird ausschließlich die feste HTTPS-Adresse
``ECB_URL`` – ohne Weiterleitungen, mit Zeit- und Größenlimit; keine Eingabe
des Nutzers fließt in die URL. Die EZB veröffentlicht EUR -> USD (z. B.
1,1630); gespeichert wird der Kehrwert (1 USD in EUR). Von Hand gepflegte
Kurse desselben Tages werden nicht überschrieben.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import httpx
from django.conf import settings

from .booking import fill_missing_eur
from .models import ExchangeRate

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
TIMEOUT = 10.0
MAX_BYTES = 64 * 1024
_DAY = re.compile(r"""time=['"](\d{4}-\d{2}-\d{2})['"]""")
_USD = re.compile(r"""currency=['"]USD['"]\s+rate=['"]([0-9.]+)['"]""")


class EcbError(Exception):
    """Abruf oder Inhalt unbrauchbar; Text ist für Verwalter gedacht."""


def enabled() -> bool:
    return bool(getattr(settings, "BILLING_ECB_FETCH", False))


def parse(body: str) -> tuple[str, Decimal]:
    """(Datum JJJJ-MM-TT, 1 USD in EUR) aus der Tagesdatei der EZB."""
    day, usd = _DAY.search(body), _USD.search(body)
    if not day or not usd:
        raise EcbError("Die Antwort der EZB enthält keinen USD-Kurs.")
    try:
        eur_usd = Decimal(usd[1])
    except InvalidOperation:
        raise EcbError("Der USD-Kurs der EZB ist keine Zahl.") from None
    if eur_usd <= 0:
        raise EcbError("Der USD-Kurs der EZB ist ungültig.")
    return day[1], (Decimal(1) / eur_usd).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def download() -> str:
    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=False) as client:
            with client.stream("GET", ECB_URL) as response:
                if response.status_code != 200:
                    raise EcbError(f"Die EZB antwortete mit HTTP {response.status_code}.")
                data = b""
                for chunk in response.iter_bytes():
                    data += chunk
                    if len(data) > MAX_BYTES:
                        raise EcbError("Die Antwort der EZB ist unerwartet groß.")
    except httpx.HTTPError as exc:
        raise EcbError(f"Die EZB ist nicht erreichbar ({type(exc).__name__}).") from None
    return data.decode("utf-8", errors="replace")


def fetch() -> tuple[ExchangeRate, bool]:
    """Tageskurs abrufen und speichern: (Kurs, neu angelegt). EcbError bei Fehlern."""
    if not enabled():
        raise EcbError("Der Abruf des EZB-Kurses ist ausgeschaltet (BILLING_ECB_FETCH).")
    day, usd_eur = parse(download())
    rate, created = ExchangeRate.objects.get_or_create(
        date=day, defaults={"usd_eur": usd_eur, "source": ExchangeRate.Source.ECB}
    )
    if not created and rate.source == ExchangeRate.Source.ECB and rate.usd_eur != usd_eur:
        rate.usd_eur = usd_eur
        rate.save(update_fields=["usd_eur"])
    fill_missing_eur()
    return rate, created
