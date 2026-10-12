"""Abruf des EZB-Referenzkurses USD (M6-09), seit A11 standardmäßig AN.

Ausschalten über ``BILLING_ECB_FETCH=false`` in der Umgebung (z. B. Server
ohne Internet). Eingeschaltet gibt es im Admin unter „Wechselkurse“ den Knopf
„EZB-Kurs abrufen“, den Befehl ``manage.py fetch_ecb_rate`` und den täglichen
Abruf durch den Worker (``AutoFetch``, einmal je Minute geprüft).

SSRF-sicher: Abgerufen wird ausschließlich die feste HTTPS-Adresse
``ECB_URL`` – ohne Weiterleitungen, mit Zeit- und Größenlimit; keine Eingabe
des Nutzers fließt in die URL. Die EZB veröffentlicht EUR -> USD (z. B.
1,1630); gespeichert wird der Kehrwert (1 USD in EUR). Von Hand gepflegte
Kurse desselben Tages werden nicht überschrieben.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import httpx
from django.conf import settings
from django.db import DatabaseError
from django.utils import timezone

from .booking import TIME_ZONE, fill_missing_eur
from .models import ExchangeRate

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
TIMEOUT = 10.0
MAX_BYTES = 64 * 1024
_DAY = re.compile(r"""time=['"](\d{4}-\d{2}-\d{2})['"]""")
logger = logging.getLogger(__name__)
_USD = re.compile(r"""currency=['"]USD['"]\s+rate=['"]([0-9.]+)['"]""")


class EcbError(Exception):
    """Abruf oder Inhalt unbrauchbar; Text ist für Verwalter gedacht."""


def enabled() -> bool:
    return bool(getattr(settings, "BILLING_ECB_FETCH", True))


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
    try:
        day = dt.date.fromisoformat(day)
    except ValueError:
        raise EcbError("Das Datum in der Antwort der EZB ist ungültig.") from None
    rate, created = ExchangeRate.objects.get_or_create(
        date=day, defaults={"usd_eur": usd_eur, "source": ExchangeRate.Source.ECB}
    )
    if not created and rate.source == ExchangeRate.Source.ECB and rate.usd_eur != usd_eur:
        rate.usd_eur = usd_eur
        rate.save(update_fields=["usd_eur"])
    fill_missing_eur()
    return rate, created


# --- Täglicher Abruf durch den Worker (A11) ---------------------------------------

# Die EZB veröffentlicht werktags gegen 16:00 Uhr MEZ/MESZ; etwas Puffer.
PUBLISH_TIME = dt.time(16, 30)
# Nach der Veröffentlichungszeit stündlich nachfragen, falls die EZB noch den
# Vortag liefert (Verspätung) – höchstens bis LATE_UNTIL, danach erst morgen.
LATE_RETRY = dt.timedelta(hours=1)
LATE_UNTIL = dt.time(20, 0)
# Backoff bei Fehlern (kein Internet, EZB gestört): 5 Min., verdoppelt bis 6 Std.
BACKOFF_START = dt.timedelta(minutes=5)
BACKOFF_MAX = dt.timedelta(hours=6)


def expected_day(local: dt.datetime) -> dt.date:
    """Neuester Tag, für den die EZB zur Ortszeit ``local`` einen Kurs haben sollte:
    werktags ab PUBLISH_TIME heute, sonst der letzte Werktag davor. Feiertage
    (TARGET) kennt die Funktion nicht; dafür sorgt ``AutoFetch.settled``."""
    day = local.date()
    if day.weekday() < 5 and local.time() >= PUBLISH_TIME:
        return day
    day -= dt.timedelta(days=1)
    while day.weekday() >= 5:
        day -= dt.timedelta(days=1)
    return day


class AutoFetch:
    """Zustand des täglichen Abrufs im Worker-Prozess (nur im Speicher).

    ``run`` ist billig, wenn nichts zu tun ist (eine Abfrage), und wirft nie –
    außer ``DatabaseError``, die der Worker selbst behandelt. Fehler beim Abruf
    landen als Warnung im Log, der nächste Versuch folgt mit Backoff.
    """

    def __init__(self):
        self.next_try: dt.datetime | None = None
        self.failures = 0
        # Erwarteter Tag, für den die EZB (Feiertag) nichts Neueres hatte.
        self.settled: dt.date | None = None

    def due(self, now: dt.datetime) -> bool:
        if not enabled():
            return False
        if self.next_try is not None and now < self.next_try:
            return False
        expected = expected_day(timezone.localtime(now, TIME_ZONE))
        if expected == self.settled:
            return False
        return not ExchangeRate.objects.filter(date__gte=expected).exists()

    def run(self, now: dt.datetime | None = None) -> ExchangeRate | None:
        """Kurs abrufen, wenn fällig; liefert den Kurs oder None."""
        now = now or timezone.now()
        if not self.due(now):
            return None
        try:
            rate, created = fetch()
        except DatabaseError:
            raise
        except Exception as exc:  # kein Internet, EZB gestört, unerwarteter Inhalt
            self.failures += 1
            delay = min(BACKOFF_START * 2 ** (self.failures - 1), BACKOFF_MAX)
            self.next_try = now + delay
            reason = str(exc) if isinstance(exc, EcbError) else type(exc).__name__
            logger.warning(
                "EZB-Kurs nicht abrufbar (%s); nächster Versuch in %d Min.",
                reason,
                delay.total_seconds() // 60,
            )
            return None
        self.failures = 0
        self.next_try = None
        local = timezone.localtime(now, TIME_ZONE)
        expected = expected_day(local)
        if rate.date < expected:
            # EZB noch nicht so weit (Verspätung) oder Feiertag ohne Kurs.
            if expected == local.date() and local.time() < LATE_UNTIL:
                self.next_try = now + LATE_RETRY
            else:
                self.settled = expected
        logger.info("EZB-Kurs %s (%s)", rate, "neu" if created else "vorhanden")
        return rate


_auto = AutoFetch()


def auto_fetch(now: dt.datetime | None = None) -> ExchangeRate | None:
    """Für den Worker (Wartungsblock, einmal je Minute): Tageskurs holen, wenn fällig."""
    return _auto.run(now)
