"""Online-Status von Anbietern mit ``check_status`` (Plan 8a, M4-03).

Cache prozessübergreifend in der DB: Das Ergebnis steht am ``Provider``
(``online``, ``last_checked``, ``last_online``, ``reported_models``, ``last_error``). Ein
Prozess-Cache (LocMemCache) wirkt bei 2 gunicorn-Workern nicht.

Sperre gegen Doppelprüfung – bedingtes UPDATE als Anspruch:

    UPDATE provider SET last_checked = now
     WHERE id = … AND (last_checked IS NULL OR last_checked < now - 15 s)

Nur wer dabei genau eine Zeile ändert, prüft. PostgreSQL sperrt die Zeile für
das UPDATE; ein zweites, gleichzeitiges UPDATE wartet und wertet die
WHERE-Bedingung danach neu aus (READ COMMITTED) – sie trifft dann nicht mehr
zu, es ändert 0 Zeilen und prüft nicht. Das UPDATE läuft im Autocommit, die
Zeilensperre hält also nur Millisekunden. Die Netzprüfung (bis ca. 2 s) läuft
danach **ohne** offene Transaktion; das Ergebnis wird mit einem zweiten
UPDATE geschrieben. Gegenüber ``select_for_update(skip_locked=True)`` braucht
das keine Transaktion um die Prüfung herum und lässt sich nicht versehentlich
in ein ``atomic`` mit Netzaufruf verwandeln. Stirbt der prüfende Prozess,
gilt der Anspruch einfach als Prüfung ohne neues Ergebnis; nach 15 s prüft
der nächste Aufruf erneut. Während einer laufenden Prüfung sehen andere
Anfragen den vorherigen Stand.

Lokale Modelle ohne Admin-Pflege: Meldet ein lokaler Anbieter (LM Studio)
Modelle, die es als ``AIModel`` noch nicht gibt, werden sie angelegt (aktiv,
Anzeigename = Modell-ID). Nicht mehr gemeldete werden nicht gelöscht (der
Verlauf verweist darauf); sie gelten nur als nicht verfügbar. Rollen mit
``all_models`` sehen neue Modelle sofort, Rollen mit Modellliste erst, wenn
ein Verwalter sie dort freigibt.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from . import detect
from .models import AIModel, Provider
from .providers import registry
from .providers.base import CHECK_UNEXPECTED, CheckResult

logger = logging.getLogger(__name__)

CACHE_SECONDS = 15
CHECK_TIMEOUT = 2.0
# Cloud-Anbieter ohne ``check_status``, deren letzte Prüfung fehlschlug (z. B.
# Key abgelaufen): seltener neu prüfen, bis sie wieder online sind.
OFFLINE_RECHECK_SECONDS = 60


def _stale_filter(now, seconds: int = CACHE_SECONDS):
    return Q(last_checked__isnull=True) | Q(last_checked__lt=now - timedelta(seconds=seconds))


def _claim(provider_id: int, seconds: int = CACHE_SECONDS) -> bool:
    """Prüfrecht per bedingtem UPDATE erwerben (siehe Moduldoku)."""
    now = timezone.now()
    return (
        Provider.objects.filter(_stale_filter(now, seconds), pk=provider_id).update(
            last_checked=now
        )
        == 1
    )


def known_offline(provider: Provider) -> bool:
    """Ohne ``check_status``, aber zuletzt (Admin-Prüfung, Neuprüfung) offline."""
    return not provider.check_status and provider.last_checked is not None and not provider.online


def _claim_for(provider: Provider) -> bool:
    if provider.check_status:
        return _claim(provider.pk)
    if known_offline(provider):
        return _claim(provider.pk, OFFLINE_RECHECK_SECONDS)
    return False


def _sync_local_models(provider: Provider, model_ids: list[str]) -> int:
    """Neu gemeldete Modelle eines lokalen Anbieters anlegen; Anzahl neuer.

    Fähigkeiten (Hauptart, Werkzeuge, Bilder) nach Meldung von LM Studio
    (``/api/v0/models``, ein Abruf nur wenn es neue Modelle gibt), sonst
    Heuristik; Embedding-Modelle gehören so nicht in die Chat-Auswahl.
    Bestehende Modelle bleiben unverändert.
    """
    existing = set(provider.ai_models.values_list("model_id", flat=True))
    max_len = AIModel._meta.get_field("model_id").max_length
    model_ids = [
        model_id
        for model_id in dict.fromkeys(model_ids)
        if model_id not in existing and len(model_id) <= max_len
    ]
    if not model_ids:
        return 0
    found = detect.detect(provider, model_ids)
    new = [
        detect.new_model(provider, model_id, found[model_id], active=True) for model_id in model_ids
    ]
    # ignore_conflicts: Ein paralleler Abgleich (z. B. sync-models) darf nicht stören.
    AIModel.objects.bulk_create(new, ignore_conflicts=True)
    return len(new)


def check_provider(provider: Provider, timeout: float = CHECK_TIMEOUT) -> CheckResult:
    """Prüft einen Anbieter über das Netz und schreibt das Ergebnis.

    Setzt ``online`` und ``last_error`` (Ursache bei offline, sonst leer).
    Nicht innerhalb einer Transaktion aufrufen (Netzaufruf bis ``timeout``).
    """
    try:
        result = registry.get_adapter(provider).check(timeout=timeout)
    except Exception:
        # z. B. unbekannte Anbieterart; check() selbst wirft nie.
        result = CheckResult(online=False, error=CHECK_UNEXPECTED)

    online = result.online
    fields = {"online": online, "last_error": "" if online else (result.error or "")}
    if online:
        fields["last_online"] = timezone.now()
        fields["reported_models"] = result.models
    Provider.objects.filter(pk=provider.pk).update(**fields)
    if online != provider.online:
        # Kein Log je Prüfung, nur beim Wechsel; nur die Kurzursache.
        logger.info(
            "Anbieter %s ist jetzt %s%s",
            provider.pk,
            "online" if online else "offline",
            "" if online else f" ({result.short_error})",
        )
    for key, value in fields.items():
        setattr(provider, key, value)

    if online and provider.is_local:
        created = _sync_local_models(provider, result.models)
        if created:
            logger.info("Anbieter %s: %d neue lokale Modelle angelegt", provider.pk, created)
    return result


def force_check(provider: Provider, timeout: float = CHECK_TIMEOUT) -> CheckResult:
    """Prüft sofort, ohne 15-s-Cache (Admin „Verbindung jetzt prüfen“).

    Gleiche Speicherlogik wie ``refresh``; ``last_checked`` wird wie beim
    Anspruch vorab gesetzt, damit parallele Statusabfragen nicht zusätzlich prüfen.
    """
    now = timezone.now()
    Provider.objects.filter(pk=provider.pk).update(last_checked=now)
    provider.last_checked = now
    return check_provider(provider, timeout=timeout)


def refresh(provider: Provider) -> Provider:
    """Prüft ``provider``, falls sein Status älter als 15 s ist und kein anderer
    Aufruf gerade prüft. Gibt den aktuellen Stand aus der DB zurück."""
    if _claim_for(provider):
        check_provider(provider)
    provider.refresh_from_db(
        fields=["online", "last_online", "last_checked", "reported_models", "last_error"]
    )
    return provider


def refresh_all() -> list[Provider]:
    """Alle aktiven Anbieter mit ``check_status`` sowie zuletzt offline geprüfte
    Cloud-Anbieter, bei Bedarf geprüft."""
    offline = Q(check_status=False, last_checked__isnull=False, online=False)
    providers = list(Provider.objects.filter(Q(check_status=True) | offline, active=True))
    for provider in providers:
        if _claim_for(provider):
            check_provider(provider)
    # Dieselben Anbieter neu lesen: Ein wieder erreichbarer Cloud-Anbieter
    # erscheint so einmal als online, bevor er aus der Liste fällt.
    return list(Provider.objects.filter(pk__in=[p.pk for p in providers]).order_by("name", "pk"))


def invalidate(provider_id: int) -> None:
    """Nächste Statusabfrage prüft sofort (z. B. nach Abbruch mitten im Stream)."""
    Provider.objects.filter(pk=provider_id, check_status=True).update(last_checked=None)


def model_state(ai_model: AIModel) -> tuple[bool, bool]:
    """(online, available) eines Modells nach dem letzten gespeicherten Status.

    - Lokale Anbieter mit Statusprüfung: offline bzw. nicht verfügbar, wenn das
      Modell nicht gemeldet wird (nicht geladen).
    - Andere Anbieter: offline, wenn die letzte Prüfung fehlschlug (z. B. Key
      abgelaufen oder abgelehnt); sonst online und verfügbar.
    """
    provider = ai_model.provider
    if provider.is_local and provider.check_status:
        online = provider.online
        return online, online and ai_model.model_id in (provider.reported_models or [])
    if provider.check_status or known_offline(provider):
        online = bool(provider.online)
        return online, online
    return True, True


def serialize(provider: Provider) -> dict:
    return {
        "id": provider.pk,
        "name": provider.name,
        "is_local": provider.is_local,
        "online": provider.online,
        "last_online": provider.last_online.isoformat() if provider.last_online else None,
        "last_checked": provider.last_checked.isoformat() if provider.last_checked else None,
        "models": list(provider.reported_models or []) if provider.online else [],
        # Ursache bei offline (deutscher Text ohne Key/Anbieter-Rohtext), sonst None.
        "error": None if provider.online else (provider.last_error or None),
    }
