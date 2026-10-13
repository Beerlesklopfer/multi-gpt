"""TLS des Agents: normale Prüfung oder festgehaltener Fingerprint (Frage 8i)."""

from __future__ import annotations

import hashlib
import hmac
import ssl


class PinError(Exception):
    pass


def client_context(pin: str | None) -> ssl.SSLContext:
    """SSL-Kontext für die Verbindung zum Server.

    Ohne ``pin`` wird wie üblich gegen die System-CAs und den Rechnernamen
    geprüft. Mit ``pin`` (selbst signiertes Zertifikat, z. B. snakeoil) prüft der
    Agent stattdessen den SHA-256 des Server-Zertifikats nach dem Handshake
    (``check_pin``), bevor er den Token sendet.
    """
    if pin is None:
        ctx = ssl.create_default_context()
    else:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def fingerprint(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def check_pin(ssl_object, pin: str | None) -> None:
    """Fingerprint des Server-Zertifikats prüfen; wirft PinError."""
    if pin is None:
        return
    if ssl_object is None:
        raise PinError("Keine TLS-Verbindung, Fingerprint nicht prüfbar.")
    der = ssl_object.getpeercert(binary_form=True)
    if not der or not hmac.compare_digest(fingerprint(der), pin):
        raise PinError("Zertifikat des Servers passt nicht zum festgehaltenen Fingerprint.")
