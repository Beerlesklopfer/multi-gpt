"""Audit-Log (``ApiCall``) und Drosselung des MCP-Endpunkts (M15).

**Drosselung (Entscheidung):** über die Zeilen des Audit-Logs in PostgreSQL statt
über django-axes oder einen Cache. axes ist auf Anmeldungen mit Benutzername und
Passwort zugeschnitten; der Standard-Cache von Django (LocMem) gilt je
gunicorn-Prozess, die Grenzen würden sich also mit der Zahl der Worker
vervielfachen. Die Zählung über indizierte Zeilen gilt für alle Prozesse.

- **Fehlversuche je IP:** ab ``API_FAILURE_LIMIT`` ungültigen Keys in
  ``API_FAILURE_WINDOW_MINUTES`` antwortet der Endpunkt dieser IP mit 429,
  auch mit gültigem Key (wie die Login-Sperre).
- **Aufrufe je Key:** höchstens ``API_RATE_LIMIT_PER_MINUTE`` je Minute.

Aufbewahrung: ``cleanup`` (Worker, einmal je Minute geprüft) löscht Einträge
älter als ``API_AUDIT_DAYS`` Tage.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .models import ApiCall

logger = logging.getLogger(__name__)

_CLEANUP_BATCH = 5000


def _setting(name: str, default: int) -> int:
    try:
        return max(1, int(getattr(settings, name, default)))
    except (TypeError, ValueError):
        return default


def failure_limit() -> int:
    return _setting("API_FAILURE_LIMIT", 10)


def failure_window() -> timedelta:
    return timedelta(minutes=_setting("API_FAILURE_WINDOW_MINUTES", 15))


def rate_limit() -> int:
    return _setting("API_RATE_LIMIT_PER_MINUTE", 120)


def ip_blocked(ip: str | None, now=None) -> bool:
    if not ip:
        return False
    now = now or timezone.now()
    failures = ApiCall.objects.filter(
        ip=ip, status=ApiCall.Status.UNAUTHORIZED, created__gte=now - failure_window()
    ).count()
    return failures >= failure_limit()


def key_limited(key, now=None) -> bool:
    now = now or timezone.now()
    calls = ApiCall.objects.filter(key=key, created__gte=now - timedelta(minutes=1)).count()
    return calls >= rate_limit()


def record(
    *,
    key=None,
    method: str = "",
    tool: str = "",
    status: str,
    http_status: int = 200,
    duration_ms: int = 0,
    request_bytes: int = 0,
    response_bytes: int = 0,
    ip: str | None = None,
) -> None:
    """Eintrag schreiben; Fehler dabei verhindern die Antwort nie."""
    try:
        ApiCall.objects.create(
            key=key,
            method=(method or "")[:50],
            tool=(tool or "")[:100],
            status=status,
            http_status=http_status,
            duration_ms=max(0, int(duration_ms)),
            request_bytes=max(0, int(request_bytes)),
            response_bytes=max(0, int(response_bytes)),
            ip=ip or None,
        )
    except Exception as exc:
        logger.error("API-Aufruf nicht protokolliert: %s", type(exc).__name__)


def cleanup(now=None) -> int:
    """Einträge älter als ``API_AUDIT_DAYS`` (Standard 90) löschen."""
    now = now or timezone.now()
    cutoff = now - timedelta(days=_setting("API_AUDIT_DAYS", 90))
    ids = list(
        ApiCall.objects.filter(created__lt=cutoff).values_list("pk", flat=True)[:_CLEANUP_BATCH]
    )
    if not ids:
        return 0
    deleted, _ = ApiCall.objects.filter(pk__in=ids).delete()
    return deleted
