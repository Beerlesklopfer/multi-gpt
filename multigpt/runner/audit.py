"""Audit-Log des Runners (M14-12): gekürzte Argumente ohne Geheimnisse, nie Ausgaben.

``record()`` ist der einzige Weg, ``RunnerAudit`` zu schreiben. Argumente
werden als JSON gekürzt; Werte unter Schlüsseln, die nach Geheimnis klingen
(Passwort, Token, Schlüssel …), und Zeichenfolgen, die wie Tokens bzw. private
Schlüssel aussehen, werden ersetzt.
"""

from __future__ import annotations

import json
import re

from .models import RunnerAudit

MAX_ARGUMENTS = 1000
MAX_VALUE = 300
MAX_MESSAGE = 500
REDACTED = "[entfernt]"

_SECRET_KEYS = re.compile(
    r"pass(word|wd)?|secret|token|api[_-]?key|private[_-]?key|credential|auth|cookie",
    re.IGNORECASE,
)
_SECRET_VALUES = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|$)"
    r"|\b(mgrt|mgpt)_[a-z0-9]{10}_[A-Za-z0-9_-]{20,}"
    r"|\bsk-[A-Za-z0-9_-]{16,}"
    r"|\bBearer\s+[A-Za-z0-9._~+/=-]{8,}",
    re.DOTALL,
)


def _clean(value, depth: int = 0):
    if depth > 4:
        return "…"
    if isinstance(value, dict):
        return {
            str(k)[:50]: (REDACTED if _SECRET_KEYS.search(str(k)) else _clean(v, depth + 1))
            for k, v in list(value.items())[:30]
        }
    if isinstance(value, list | tuple):
        return [_clean(v, depth + 1) for v in list(value)[:30]]
    if isinstance(value, str):
        value = _SECRET_VALUES.sub(REDACTED, value)
        return value if len(value) <= MAX_VALUE else value[:MAX_VALUE] + "…"
    if value is None or isinstance(value, bool | int | float):
        return value
    return str(value)[:MAX_VALUE]


def redact(arguments) -> str:
    """Argumente als gekürztes JSON ohne Geheimnisse."""
    if arguments in (None, "", {}, []):
        return ""
    text = json.dumps(_clean(arguments), ensure_ascii=False, separators=(", ", ": "))
    return text if len(text) <= MAX_ARGUMENTS else text[: MAX_ARGUMENTS - 1] + "…"


def record(
    kind: str = RunnerAudit.Kind.TOOL,
    *,
    owner=None,
    session=None,
    runner=None,
    tool: str = "",
    arguments=None,
    exit_code: int | None = None,
    duration_ms: int | None = None,
    message: str = "",
    ip: str | None = None,
) -> RunnerAudit:
    """Eintrag anlegen. ``owner`` fällt auf den Besitzer der Sitzung zurück."""
    if owner is None and session is not None:
        owner = session.owner
    message = _SECRET_VALUES.sub(REDACTED, message or "")
    return RunnerAudit.objects.create(
        kind=kind,
        owner=owner,
        session=session,
        runner=runner,
        tool=(tool or "")[:100],
        arguments=redact(arguments),
        exit_code=exit_code,
        duration_ms=max(0, int(duration_ms)) if duration_ms is not None else None,
        message=message[:MAX_MESSAGE],
        ip=ip or None,
    )


_CLEANUP_BATCH = 5000


def cleanup(now=None) -> int:
    """Einträge älter als ``API_AUDIT_DAYS`` (Standard 90) löschen (Worker, M14-12).

    Wie ``node.audit.cleanup`` in Stapeln, damit eine lange Liste den Worker
    nicht aufhält; der Rest folgt in der nächsten Minute.
    """
    from datetime import timedelta

    from django.conf import settings
    from django.utils import timezone

    now = now or timezone.now()
    cutoff = now - timedelta(days=getattr(settings, "API_AUDIT_DAYS", 90))
    ids = list(
        RunnerAudit.objects.filter(created__lt=cutoff).values_list("pk", flat=True)[:_CLEANUP_BATCH]
    )
    if not ids:
        return 0
    deleted, _ = RunnerAudit.objects.filter(pk__in=ids).delete()
    return deleted
