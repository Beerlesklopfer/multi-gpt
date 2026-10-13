"""Steuerkanal ``mgpt_runner_ctl`` zum Runner-Dienst (M14-04/05).

Web, Worker und Admin lösen hierüber sofortige Reaktionen des Runner-Dienstes
aus, ohne ihn direkt anzusprechen: Widerruf eines Tokens (Verbindung zu mit
4401 in unter einer Sekunde), geänderte Rechte (frische Prüfung, ggf. 4403),
Stopp einer Sitzung bzw. aller Sitzungen eines Kontos (Kontingent).

Die Nutzlast ist klein und steht direkt im NOTIFY (JSON, weit unter der Grenze
von knapp 8000 Byte). ``pg_notify`` in einer offenen Transaktion wird erst beim
Commit zugestellt; nach einem Rollback gar nicht. Ist der Runner-Dienst gerade
nicht da, geht die Benachrichtigung verloren; der nächste Heartbeat prüft Token,
Rechte und Kontingent ohnehin frisch aus der Datenbank (höchstens 30 s Verzug).
"""

from __future__ import annotations

import json
import logging

from django.db import DEFAULT_DB_ALIAS, connections

from . import protocol

logger = logging.getLogger(__name__)

CTL_CHANNEL = "mgpt_runner_ctl"

KIND_REVOKE = "revoke"  # {token}
KIND_RIGHTS = "rights"  # {user}
KIND_STOP = "stop"  # {session, reason}
KIND_QUOTA = "quota"  # {user, reason}
KIND_CONNECTED = "connected"  # {runner, conn} – neue Verbindung ersetzt alte (Dienst intern)
KINDS = frozenset({KIND_REVOKE, KIND_RIGHTS, KIND_STOP, KIND_QUOTA, KIND_CONNECTED})


def notify_ctl(kind: str, **data) -> None:
    """Steuerereignis an alle Prozesse des Runner-Dienstes senden."""
    if kind not in KINDS:
        raise ValueError(f"unbekanntes Steuerereignis {kind!r}")
    payload = json.dumps({"kind": kind, **data}, separators=(",", ":"))
    with connections[DEFAULT_DB_ALIAS].cursor() as cursor:
        cursor.execute("SELECT pg_notify(%s, %s)", [CTL_CHANNEL, payload])


def revoke_token(token_id: int) -> None:
    """Token widerrufen bzw. gelöscht: laufende Verbindung sofort schließen (4401)."""
    notify_ctl(KIND_REVOKE, token=int(token_id))


def rights_changed(user_id: int) -> None:
    """Konto oder Rolle geändert: Recht ``USE_RUNNER`` frisch prüfen (sonst 4403)."""
    notify_ctl(KIND_RIGHTS, user=int(user_id))


def stop_session(session_uuid, reason: str = "user") -> None:
    """Sitzung beenden (``session_stop`` an den Agent)."""
    if reason not in protocol.STOP_REASONS:
        raise ValueError(f"unbekannter Grund {reason!r}")
    notify_ctl(KIND_STOP, session=str(session_uuid), reason=reason)


def stop_user_sessions(user_id: int, reason: str = "quota") -> None:
    """Alle aktiven Sitzungen eines Kontos beenden (z. B. Kontingent bei 100 %).

    Bestätigt der Agent den Stopp nicht binnen ``QUOTA_GRACE_SECONDS`` (60 s),
    trennt der Runner-Dienst die Verbindung (Close 4402).
    """
    if reason not in protocol.STOP_REASONS:
        raise ValueError(f"unbekannter Grund {reason!r}")
    notify_ctl(KIND_QUOTA, user=int(user_id), reason=reason)
