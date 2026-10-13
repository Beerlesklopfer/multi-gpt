"""Freigegebene Hosts für ssh/telnet (Entscheidung 2026-10-13, 8v/8w).

Das Modell sieht nur Namen und Beschreibung (``visible_hosts``); Adresse,
Benutzer und Zugang gibt ``connection_for`` nur für einen bestätigten Aufruf an
den Runner weiter. Über API-Keys gibt es diese Werkzeuge nie (8o).
"""

from __future__ import annotations

from django.db.models import Q

from multigpt.accounts.permissions import Action, can

from .models import RunnerHost


def hosts_for(user):
    """Nutzbare Hosts des Kontos: aktiv, freigegeben (Rolle oder Person), Rollenrechte
    „Runner nutzen“ und „Fernzugriff“, bei SSH mit bestätigtem Host-Schlüssel."""
    if not (can(user, Action.USE_RUNNER) and can(user, Action.USE_RUNNER_HOSTS)):
        return RunnerHost.objects.none()
    allowed = Q(allowed_users=user)
    if user.role_id:
        allowed |= Q(allowed_roles=user.role_id)
    qs = RunnerHost.objects.filter(allowed, active=True).distinct()
    ready = Q(protocol=RunnerHost.Protocol.TELNET) | (
        ~Q(host_key="") & Q(host_key_confirmed_at__isnull=False)
    )
    return qs.filter(ready).order_by("name")


def visible_hosts(user) -> list[dict]:
    """Für das Modell: nur Name, Protokoll und Beschreibung."""
    return [
        {"name": h.name, "protocol": h.protocol, "description": h.description}
        for h in hosts_for(user)
    ]


def get_host(user, name: str) -> RunnerHost | None:
    return hosts_for(user).filter(name=name).first()


def connection_for(host: RunnerHost) -> dict:
    """Zugang für genau einen bestätigten Aufruf (Rahmen an den Runner, nie ins Log)."""
    data = {
        "name": host.name,
        "protocol": host.protocol,
        "address": host.address,
        "port": host.port,
        "username": host.username,
        "auth_method": host.auth_method,
        "host_key": host.host_key,
    }
    if host.auth_method != RunnerHost.AuthMethod.NONE:
        data["secret"] = host.secret
    return data
