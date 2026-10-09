"""Modellfeld für verschlüsselte Geheimnisse (API-Keys, MCP-Zugangsdaten)."""

import json

from django.db import models

from .crypto import decrypt, encrypt


class EncryptedTextField(models.TextField):
    """Textfeld, das in der DB nur als Fernet-Token steht.

    Im Python-Objekt liegt der Klartext; beim Speichern wird verschlüsselt,
    beim Laden entschlüsselt. Leerer Text und ``None`` bleiben unverschlüsselt
    (es gibt dann nichts zu schützen). Fernet ist nicht deterministisch, Filtern
    nach dem Inhalt ist daher nicht möglich.
    """

    description = "Verschlüsselter Text"

    def from_db_value(self, value, expression, connection):
        if value is None or value == "":
            return value
        return decrypt(value)

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value is None or value == "":
            return value
        return encrypt(value)


def mask_secret(value: str | None, visible: int = 4) -> str:
    """Maskiert ein Geheimnis für die Anzeige: nur die letzten Zeichen.

    Bei kurzen Werten (unter 8 Zeichen) wird gar nichts gezeigt, damit die
    Anzeige den Wert nicht weitgehend verrät.
    """
    if not value:
        return ""
    if len(value) < 8:
        return "••••"
    return "••••" + value[-visible:]


class UnicodeJSONEncoder(json.JSONEncoder):
    """JSON ohne ``\\uXXXX``-Escapes für Nicht-ASCII-Zeichen.

    PostgreSQL wandelt ``\\u``-Escapes in jsonb in die Server-Kodierung um und
    scheitert damit an Datenbanken mit ``SQL_ASCII`` (so angelegte
    Entwicklungs-Cluster) bei Umlauten. UTF-8 direkt geht in beiden Fällen.
    """

    def __init__(self, *args, **kwargs):
        kwargs["ensure_ascii"] = False
        super().__init__(*args, **kwargs)
