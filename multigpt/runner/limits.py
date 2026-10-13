"""Grenzen eines Kontos für Runner-Sitzungen aus der Rolle (M14-01).

Der Server setzt die Werte, der Agent übernimmt sie als Container-Optionen und
meldet die Ist-Werte zurück (Vergleich: ``mismatches``). Über M15-Keys gibt es
nie Netz (8o). Dazu kommt das Scratchpad (``scratch_mode`` aus dem Arbeitsstand,
``scratch_mb`` aus der Rolle); beim Modus „local“ nennt der Server dem Agent nur
die Kennung des Arbeitsstands, nie einen Pfad.
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
    "scratch_mb": 1024,
}
SCRATCH_CONTAINER = "container"
SCRATCH_LOCAL = "local"
LIMIT_KEYS = ("cpus", "memory_mb", "pids", "tmpfs_mb", "network")


def session_limits(user, *, via_api_key: bool = False, workspace=None) -> dict:
    """Soll-Grenzen: ``{"cpus", "memory_mb", "pids", "tmpfs_mb", "network",
    "scratch_mode", "scratch_mb"}``. ``scratch_mode`` kommt aus ``workspace``
    (ohne Arbeitsstand: ``container``)."""
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
            "scratch_mb": role.runner_scratch_mb,
        }
    if via_api_key:
        limits["network"] = False
    mode = getattr(workspace, "scratch_mode", "") or SCRATCH_CONTAINER
    limits["scratch_mode"] = (
        mode if mode in (SCRATCH_CONTAINER, SCRATCH_LOCAL) else SCRATCH_CONTAINER
    )
    return limits


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
        elif key == "network":
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
