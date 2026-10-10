"""API-Keys anlegen, prüfen, widerrufen (M15).

Format: ``mgpt_<präfix>_<geheim>`` – Präfix 10 Zeichen ``[a-z0-9]`` (öffentlich,
eindeutig, zur Suche), Geheimnis 256 Bit als URL-sicheres base64. Gespeichert
wird nur ``sha256(key)`` (Begründung in ``models``). Ablauf und Widerruf
greifen sofort, weil jeder Aufruf den Key frisch aus der Datenbank liest.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.utils import timezone

from multigpt.accounts.permissions import Action, can

from . import scopes as api_scopes
from .models import KEY_PREFIX, ApiKey

PREFIX_LENGTH = 10
_PREFIX_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
_KEY_RE = re.compile(rf"^{KEY_PREFIX}([a-z0-9]{{{PREFIX_LENGTH}}})_([A-Za-z0-9_-]{{40,64}})$")
MAX_NAME = 100


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _new_prefix() -> str:
    return "".join(secrets.choice(_PREFIX_ALPHABET) for _ in range(PREFIX_LENGTH))


@dataclass(frozen=True)
class Created:
    """Neu angelegter Key; ``secret`` ist der Klartext und wird nur einmal gezeigt."""

    key: ApiKey
    secret: str


def role_scopes(user) -> frozenset[str]:
    """Höchstmenge der Rolle (leer ohne Recht „API-Keys/MCP-Zugang“)."""
    if user is None or not user.is_active:
        return frozenset()
    if user.is_superuser:
        return frozenset(api_scopes.ALL_SCOPES)
    if not can(user, Action.USE_API):
        return frozenset()
    role = user.role
    return frozenset(api_scopes.clean(role.api_scopes or []))


def effective_scopes(key: ApiKey) -> frozenset[str]:
    """Schnittmenge Key ∩ Rolle (``can()`` auf Objekte prüfen die Werkzeuge)."""
    if not key.is_usable():
        return frozenset()
    return frozenset(api_scopes.clean(key.scopes)) & role_scopes(key.owner)


def create_key(owner, name: str, scopes, *, expires_at=None, collections=(), sources=()) -> Created:
    """Key anlegen. ``ValueError`` (deutsch), wenn das Konto keine Keys anlegen
    darf oder Rechte verlangt, die seine Rolle nicht hat."""
    name = " ".join((name or "").split())[:MAX_NAME]
    if not name:
        raise ValueError("Bitte einen Namen für den Key angeben.")
    allowed = role_scopes(owner)
    if not allowed:
        raise ValueError("Dieses Konto darf keine API-Keys anlegen (Rolle).")
    wanted = api_scopes.clean(scopes)
    if not wanted:
        raise ValueError("Bitte mindestens ein Recht wählen.")
    too_much = [s for s in wanted if s not in allowed]
    if too_much:
        raise ValueError(f"Diese Rechte erlaubt die Rolle nicht: {', '.join(too_much)}.")
    if expires_at is not None and expires_at <= timezone.now():
        raise ValueError("Das Ablaufdatum liegt in der Vergangenheit.")
    for _attempt in range(5):
        prefix = _new_prefix()
        secret = f"{KEY_PREFIX}{prefix}_{secrets.token_urlsafe(32)}"
        try:
            with transaction.atomic():
                key = ApiKey.objects.create(
                    owner=owner,
                    name=name,
                    prefix=prefix,
                    key_hash=hash_key(secret),
                    scopes=wanted,
                    expires_at=expires_at,
                )
                if collections:
                    key.collections.set(collections)
                if sources:
                    key.sources.set(sources)
        except IntegrityError:
            continue  # Präfix schon vergeben (sehr unwahrscheinlich)
        return Created(key, secret)
    raise RuntimeError("Kein freies Präfix gefunden.")


def revoke(key: ApiKey) -> bool:
    """Widerrufen (sofort wirksam). False, wenn schon widerrufen."""
    updated = ApiKey.objects.filter(pk=key.pk, active=True).update(
        active=False, revoked_at=timezone.now()
    )
    key.refresh_from_db(fields=["active", "revoked_at"])
    return bool(updated)


def bearer_token(header: str | None) -> str | None:
    """``Authorization: Bearer <key>`` -> Key; sonst None."""
    if not header:
        return None
    scheme, _, value = header.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    value = value.strip()
    return value or None


def authenticate(raw: str | None, now=None) -> ApiKey | None:
    """Gültigen, aktiven, nicht abgelaufenen Key eines aktiven Kontos liefern.

    Der Vergleich läuft in konstanter Zeit über den Hash; unbekannte Präfixe
    vergleichen gegen einen Platzhalter, damit die Laufzeit nichts verrät.
    """
    if not raw or len(raw) > 200:
        return None
    match = _KEY_RE.match(raw)
    if match is None:
        return None
    key = (
        ApiKey.objects.select_related("owner", "owner__role").filter(prefix=match.group(1)).first()
    )
    expected = key.key_hash if key is not None else "0" * 64
    if not hmac.compare_digest(expected, hash_key(raw)) or key is None:
        return None
    if not key.is_usable(now) or not key.owner.is_active:
        return None
    return key


def touch(key: ApiKey, ip: str | None) -> None:
    """„Zuletzt benutzt“ setzen (ein UPDATE, ohne den Rest des Keys zu schreiben)."""
    now = timezone.now()
    ApiKey.objects.filter(pk=key.pk).update(last_used_at=now, last_used_ip=ip or None)
    key.last_used_at, key.last_used_ip = now, ip or None
