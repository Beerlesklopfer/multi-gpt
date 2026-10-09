"""Online-Status von Anbietern mit ``check_status`` (Plan 8a, M4-03).

Cache prozessübergreifend in der DB: Das Ergebnis steht am ``Provider``
(``online``, ``last_checked``, ``last_online``, ``reported_models``). Ein
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

from .models import AIModel, Provider
from .providers import registry

logger = logging.getLogger(__name__)

CACHE_SECONDS = 15
CHECK_TIMEOUT = 2.0


def _stale_filter(now):
    return Q(last_checked__isnull=True) | Q(last_checked__lt=now - timedelta(seconds=CACHE_SECONDS))


def _claim(provider_id: int) -> bool:
    """Prüfrecht per bedingtem UPDATE erwerben (siehe Moduldoku)."""
    now = timezone.now()
    return Provider.objects.filter(_stale_filter(now), pk=provider_id).update(last_checked=now) == 1


def _capability_for(model_id: str) -> str:
    # LM Studio meldet unter /v1/models auch Embedding-Modelle; die gehören
    # nicht in die Chat-Auswahl.
    if "embed" in model_id.lower():
        return AIModel.Capability.EMBEDDING
    return AIModel.Capability.CHAT


def _sync_local_models(provider: Provider, model_ids: list[str]) -> int:
    """Neu gemeldete Modelle eines lokalen Anbieters anlegen; Anzahl neuer."""
    existing = set(provider.ai_models.values_list("model_id", flat=True))
    max_len = AIModel._meta.get_field("model_id").max_length
    new = [
        AIModel(
            provider=provider,
            model_id=model_id,
            display_name=model_id,
            capability=_capability_for(model_id),
            active=True,
        )
        for model_id in model_ids
        if model_id not in existing and len(model_id) <= max_len
    ]
    # ignore_conflicts: Ein paralleler Abgleich (z. B. sync-models) darf nicht stören.
    AIModel.objects.bulk_create(new, ignore_conflicts=True)
    return len(new)


def check_provider(provider: Provider) -> None:
    """Prüft einen Anbieter über das Netz und schreibt das Ergebnis.

    Nicht innerhalb einer Transaktion aufrufen (Netzaufruf bis ca. 2 s).
    """
    try:
        adapter = registry.get_adapter(provider)
        model_ids = list(dict.fromkeys(adapter.list_models(timeout=CHECK_TIMEOUT)))
        online = True
    except Exception:
        # ProviderError (nicht erreichbar, Timeout, HTTP-Fehler) oder Adapter
        # ohne Modellliste: offline. Kein Log je Prüfung, nur beim Wechsel.
        model_ids, online = [], False

    fields = {"online": online}
    if online:
        fields["last_online"] = timezone.now()
        fields["reported_models"] = model_ids
    Provider.objects.filter(pk=provider.pk).update(**fields)
    if online != provider.online:
        logger.info("Anbieter %s ist jetzt %s", provider.pk, "online" if online else "offline")
    for key, value in fields.items():
        setattr(provider, key, value)

    if online and provider.is_local:
        created = _sync_local_models(provider, model_ids)
        if created:
            logger.info("Anbieter %s: %d neue lokale Modelle angelegt", provider.pk, created)


def refresh(provider: Provider) -> Provider:
    """Prüft ``provider``, falls sein Status älter als 15 s ist und kein anderer
    Aufruf gerade prüft. Gibt den aktuellen Stand aus der DB zurück."""
    if provider.check_status and _claim(provider.pk):
        check_provider(provider)
    provider.refresh_from_db(fields=["online", "last_online", "last_checked", "reported_models"])
    return provider


def refresh_all() -> list[Provider]:
    """Alle aktiven Anbieter mit ``check_status``, bei Bedarf geprüft."""
    providers = list(Provider.objects.filter(active=True, check_status=True))
    for provider in providers:
        if _claim(provider.pk):
            check_provider(provider)
    return list(Provider.objects.filter(active=True, check_status=True).order_by("name", "pk"))


def invalidate(provider_id: int) -> None:
    """Nächste Statusabfrage prüft sofort (z. B. nach Abbruch mitten im Stream)."""
    Provider.objects.filter(pk=provider_id, check_status=True).update(last_checked=None)


def model_state(ai_model: AIModel) -> tuple[bool, bool]:
    """(online, available) eines Modells nach dem letzten gespeicherten Status.

    Nur lokale Anbieter mit Statusprüfung können offline oder nicht verfügbar
    sein; alle anderen gelten als online und verfügbar.
    """
    provider = ai_model.provider
    if not (provider.is_local and provider.check_status):
        return True, True
    online = provider.online
    return online, online and ai_model.model_id in (provider.reported_models or [])


def serialize(provider: Provider) -> dict:
    return {
        "id": provider.pk,
        "name": provider.name,
        "is_local": provider.is_local,
        "online": provider.online,
        "last_online": provider.last_online.isoformat() if provider.last_online else None,
        "last_checked": provider.last_checked.isoformat() if provider.last_checked else None,
        "models": list(provider.reported_models or []) if provider.online else [],
    }
