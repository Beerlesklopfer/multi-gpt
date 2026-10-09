"""Symmetrische Verschlüsselung mit Fernet (Plan Abschnitt 9).

Der Schlüssel kommt aus ``settings.FIELD_ENCRYPTION_KEY`` und wird erst beim
ersten Ver- oder Entschlüsseln gelesen, nicht beim Import. So laufen
``collectstatic`` im Paketbau und ``manage.py check`` auch ohne Schlüssel.
"""

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class DecryptionError(Exception):
    """Ein gespeicherter Wert passt nicht zum konfigurierten Schlüssel."""


@lru_cache(maxsize=4)
def _fernet_for(key: str) -> Fernet:
    try:
        return Fernet(key)
    except (ValueError, TypeError):
        # Bewusst ohne Verkettung: Die Originalmeldung enthält nichts Nützliches,
        # und der Schlüssel selbst soll in keinem Traceback auftauchen.
        raise ImproperlyConfigured(
            "FIELD_ENCRYPTION_KEY ist ungültig: erwartet werden 32 Byte, "
            "URL-sicher Base64-kodiert (Fernet-Schlüssel)."
        ) from None


def get_fernet() -> Fernet:
    key = getattr(settings, "FIELD_ENCRYPTION_KEY", "") or ""
    key = key.strip()
    if not key:
        raise ImproperlyConfigured(
            "FIELD_ENCRYPTION_KEY ist nicht gesetzt. Ohne ihn lassen sich "
            "API-Keys und Zugangsdaten weder speichern noch lesen."
        )
    return _fernet_for(key)


def encrypt(plaintext: str) -> str:
    """Verschlüsselt Text und liefert das Fernet-Token als ASCII-Text."""
    return get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    """Entschlüsselt ein Fernet-Token; falscher Schlüssel → DecryptionError."""
    try:
        return get_fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError):
        raise DecryptionError(
            "Ein verschlüsselter Wert lässt sich nicht entschlüsseln. "
            "Passt FIELD_ENCRYPTION_KEY zu dieser Datenbank?"
        ) from None
