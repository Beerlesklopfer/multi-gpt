"""Kopplungs-Tokens des Runners anlegen, prüfen, widerrufen (M14-02).

Format: ``mgrt_<präfix>_<geheim>`` – Präfix 10 Zeichen ``[a-z0-9]`` (öffentlich,
eindeutig, zur Suche), Geheimnis 256 Bit als URL-sicheres base64. Gespeichert
wird nur ``sha256(token)``, verglichen in konstanter Zeit (Begründung wie bei
``node.keys``). Standardablauf 90 Tage. Ein Widerruf gilt sofort und trennt
eine laufende Verbindung über ``mgpt_runner_ctl`` (``control.revoke_token``).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from multigpt.accounts.permissions import Action, can

from . import control
from .models import TOKEN_PREFIX, RunnerToken

PREFIX_LENGTH = 10
DEFAULT_DAYS = 90
MAX_NAME = 100
_PREFIX_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
_TOKEN_RE = re.compile(rf"^{TOKEN_PREFIX}([a-z0-9]{{{PREFIX_LENGTH}}})_([A-Za-z0-9_-]{{40,64}})$")


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _new_prefix() -> str:
    return "".join(secrets.choice(_PREFIX_ALPHABET) for _ in range(PREFIX_LENGTH))


def default_expiry(now=None):
    days = getattr(settings, "RUNNER_TOKEN_DAYS", DEFAULT_DAYS) or DEFAULT_DAYS
    return (now or timezone.now()) + timedelta(days=days)


@dataclass(frozen=True)
class Created:
    """Neu angelegter Token; ``secret`` ist der Klartext und wird nur einmal gezeigt."""

    token: RunnerToken
    secret: str


_DEFAULT = object()


def create_token(owner, name: str = "", *, expires_at=_DEFAULT) -> Created:
    """Token anlegen. ``expires_at``: Standard 90 Tage, ``None`` = kein Ablauf.

    ``ValueError`` (deutsch), wenn die Rolle den Runner nicht erlaubt.
    """
    if not can(owner, Action.USE_RUNNER):
        raise ValueError("Dieses Konto darf keinen Runner koppeln (Rolle).")
    name = " ".join((name or "").split())[:MAX_NAME]
    if not name:
        name = f"Runner {timezone.localtime():%d.%m.%Y %H:%M}"
    if expires_at is _DEFAULT:
        expires_at = default_expiry()
    if expires_at is not None and expires_at <= timezone.now():
        raise ValueError("Das Ablaufdatum liegt in der Vergangenheit.")
    for _attempt in range(5):
        prefix = _new_prefix()
        secret = f"{TOKEN_PREFIX}{prefix}_{secrets.token_urlsafe(32)}"
        try:
            with transaction.atomic():
                token = RunnerToken.objects.create(
                    owner=owner,
                    name=name,
                    prefix=prefix,
                    token_hash=hash_token(secret),
                    expires_at=expires_at,
                )
        except IntegrityError:
            continue  # Präfix schon vergeben (sehr unwahrscheinlich)
        return Created(token, secret)
    raise RuntimeError("Kein freies Präfix gefunden.")


def revoke(token: RunnerToken) -> bool:
    """Widerrufen (sofort) und den Runner-Dienst benachrichtigen. False, wenn schon widerrufen."""
    with transaction.atomic():
        updated = RunnerToken.objects.filter(pk=token.pk, revoked_at__isnull=True).update(
            revoked_at=timezone.now()
        )
        if updated:
            # Zustellung beim Commit; der Runner-Dienst schließt mit 4401.
            control.revoke_token(token.pk)
    token.refresh_from_db(fields=["revoked_at"])
    return bool(updated)


def find(ref: str) -> RunnerToken | None:
    """Token nach ID oder Präfix (``mgrt_abc…``) – für die Kommandozeile."""
    ref = (ref or "").strip()
    if ref.isdigit():
        return RunnerToken.objects.filter(pk=int(ref)).first()
    prefix = ref.removeprefix(TOKEN_PREFIX).rstrip("…").split("_", 1)[0]
    return RunnerToken.objects.filter(prefix=prefix).first() if prefix else None


def authenticate(raw: str | None, now=None) -> RunnerToken | None:
    """Gültigen, nicht widerrufenen, nicht abgelaufenen Token eines aktiven Kontos liefern.

    Vergleich in konstanter Zeit über den Hash; unbekannte Präfixe vergleichen
    gegen einen Platzhalter. Das Rollenrecht (``Action.USE_RUNNER``) prüft der
    Aufrufer getrennt (4401 Token vs. 4403 Recht).
    """
    if not raw or len(raw) > 200:
        return None
    match = _TOKEN_RE.match(raw)
    if match is None:
        return None
    token = (
        RunnerToken.objects.select_related("owner", "owner__role")
        .filter(prefix=match.group(1))
        .first()
    )
    expected = token.token_hash if token is not None else "0" * 64
    if not hmac.compare_digest(expected, hash_token(raw)) or token is None:
        return None
    if not token.is_usable(now) or not token.owner.is_active:
        return None
    return token


def touch(token: RunnerToken, ip: str | None) -> None:
    """„Zuletzt benutzt“ setzen (ein UPDATE)."""
    now = timezone.now()
    RunnerToken.objects.filter(pk=token.pk).update(last_used_at=now, last_used_ip=ip or None)
    token.last_used_at, token.last_used_ip = now, ip or None
