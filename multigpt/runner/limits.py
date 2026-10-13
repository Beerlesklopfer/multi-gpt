"""Grenzen eines Kontos für Runner-Sitzungen aus der Rolle (M14-01).

Der Server setzt die Werte, der Agent übernimmt sie als Container-Optionen und
meldet die Ist-Werte zurück (Vergleich: ``mismatches``). Über M15-Keys gibt es
nie Netz (8o). Dazu kommt das Scratchpad (``scratch_mode`` aus dem Arbeitsstand,
``scratch_mb`` aus der Rolle); beim Modus „local“ nennt der Server dem Agent nur
die Kennung des Arbeitsstands, nie einen Pfad.

**Paketquellen** (Nutzer, 2026-10-13): Mit dem Rollenrecht ``runner_packages``
erreicht der Container über einen Proxy nur die Hosts aus
``RUNNER_PACKAGE_HOSTS`` (Netzmodus ``packages``); die Container-Schicht ist dann
beschreibbar (Grenze ``rootfs_mb``). Das ist kein freies Netz: „Alles
automatisch“ bleibt erlaubt, und über API-Keys gilt es mit dem Rollenrecht
ebenso. Hat die Rolle freies Netz (``runner_network``), geht das vor.
"""

from __future__ import annotations

from multigpt.accounts.permissions import Action, can

# Ohne Rolle (Superuser-Notfallzugang): kleinste sinnvolle Werte.
_FALLBACK = {
    "cpus": 1,
    "memory_mb": 1024,
    "pids": 256,
    "tmpfs_mb": 512,
    "network": False,
    "packages": False,
    "scratch_mb": 1024,
}
SCRATCH_CONTAINER = "container"
SCRATCH_LOCAL = "local"
LIMIT_KEYS = ("cpus", "memory_mb", "pids", "tmpfs_mb", "network", "packages", "rootfs_mb")
# Wahrheitswerte (an/aus) statt Zahlen
_FLAG_KEYS = ("network", "packages")
ROOTFS_MB = 4096  # Vorgabe der Container-Schicht, falls die Rolle nichts sagt


def session_limits(user, *, via_api_key: bool = False, workspace=None) -> dict:
    """Soll-Grenzen: ``{"cpus", "memory_mb", "pids", "tmpfs_mb", "network",
    "packages", "scratch_mode", "scratch_mb"}``, mit ``packages`` zusätzlich
    ``rootfs_mb``. ``scratch_mode`` kommt aus ``workspace`` (ohne Arbeitsstand:
    ``container``). Ob der verbundene Runner Paketquellen kann, prüft der
    Aufrufer (``without_packages``)."""
    role = user.role if getattr(user, "role_id", None) else None
    if role is None:
        limits = dict(_FALLBACK)
    else:
        limits = {
            "cpus": role.runner_max_cpus,
            "memory_mb": role.runner_max_memory_mb,
            "pids": role.runner_max_pids,
            "tmpfs_mb": role.runner_tmpfs_mb,
            "network": bool(role.runner_network),
            "packages": bool(getattr(role, "runner_packages", False)),
            "scratch_mb": role.runner_scratch_mb,
        }
    if via_api_key:
        limits["network"] = False
    if limits["network"]:
        limits["packages"] = False  # freies Netz geht vor
    if limits["packages"]:
        limits["rootfs_mb"] = int(getattr(role, "runner_rootfs_mb", 0) or ROOTFS_MB)
    mode = getattr(workspace, "scratch_mode", "") or SCRATCH_CONTAINER
    limits["scratch_mode"] = (
        mode if mode in (SCRATCH_CONTAINER, SCRATCH_LOCAL) else SCRATCH_CONTAINER
    )
    return limits


def without_packages(limits: dict) -> dict:
    """Grenzen ohne Paketquellen (Runner kann den Modus nicht)."""
    out = {key: value for key, value in limits.items() if key != "rootfs_mb"}
    out["packages"] = False
    return out


def package_hosts() -> list[str]:
    """Freigegebene Paket-Hosts (``RUNNER_PACKAGE_HOSTS``), nur gültige DNS-Namen."""
    from django.conf import settings

    from .protocol import MAX_PACKAGE_HOSTS, PACKAGE_HOST_RE

    out: list[str] = []
    for host in getattr(settings, "RUNNER_PACKAGE_HOSTS", None) or ():
        host = str(host).strip().lower()
        if PACKAGE_HOST_RE.fullmatch(host) and host not in out:
            out.append(host)
    return out[:MAX_PACKAGE_HOSTS]


def mismatches(requested: dict, actual: dict) -> list[str]:
    """Namen der Grenzen, bei denen der Ist-Wert großzügiger als der Soll-Wert ist
    (oder fehlt). Leer = in Ordnung."""
    wrong = []
    for key in LIMIT_KEYS:
        want, got = requested.get(key), (actual or {}).get(key)
        if want is None:
            continue
        if got is None:
            wrong.append(key)
        elif key in _FLAG_KEYS:
            if bool(got) and not bool(want):
                wrong.append(key)
        else:
            try:
                if float(got) > float(want):
                    wrong.append(key)
            except (TypeError, ValueError):
                wrong.append(key)
    return wrong


def can_use(user) -> bool:
    return can(user, Action.USE_RUNNER)
