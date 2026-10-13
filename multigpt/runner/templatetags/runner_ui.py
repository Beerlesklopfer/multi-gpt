"""Template-Tags der Runner-Oberfläche (M14-11/12, ui).

- ``{% runner_live_status %}``: Live-Status auf der Seite „Runner“ (Laufzeit,
  Sitzungen; runner_status.js aktualisiert alle 5 s).
- ``{% runner_chat_panel %}``: Auswahl von Arbeitsstand und Rückfrage-Modus,
  Aufgabenliste, Terminal und Vorschau im Chat (runner_chat.js). Nur mit Recht
  „Runner nutzen“ und eigenem Runner-Server; sonst leer.
"""

from django import template

from multigpt.accounts.permissions import Action, can
from multigpt.billing import runtime
from multigpt.billing.budgets import WARNING_RATIO

register = template.Library()


def _user(context):
    request = context.get("request")
    user = getattr(request, "user", None)
    return user if user is not None and user.is_authenticated else None


@register.inclusion_tag("runner/_live_status.html", takes_context=True)
def runner_live_status(context):
    user = _user(context)
    if user is None:
        return {}
    state = runtime.runtime_state(user)
    return {
        "request": context.get("request"),
        "runtime": state,
        "runtime_low": (state.limit * WARNING_RATIO) if state.limit else 0,
    }


def _runner_server(user):
    """Runner-MCP-Server des Kontos, wenn es den Runner nutzen darf (sonst None)."""
    if user is None or not can(user, Action.USE_RUNNER):
        return None
    from multigpt.runner.chat import runner_server

    return runner_server(user)  # None: noch nie gekoppelt


@register.inclusion_tag("runner/_chat_panel.html", takes_context=True)
def runner_chat_panel(context):
    """Runner-Bereich im Chat; ``show`` False, wenn das Konto keinen Runner nutzen darf."""
    from multigpt.runner.workspaces import own_workspaces

    user = _user(context)
    server = _runner_server(user)
    if server is None:
        return {"show": False}
    return {
        "show": True,
        "request": context.get("request"),
        "runner_server_id": server.pk,
        "workspaces": list(own_workspaces(user)),
        "conversation": context.get("active_conversation"),
    }


@register.inclusion_tag("runner/_chat_workbench.html", takes_context=True)
def runner_chat_workbench(context):
    """Aufgabenliste, Terminal und Vorschau des Chats (sichtbar, sobald es etwas zu zeigen gibt)."""
    show = _runner_server(_user(context)) is not None
    return {"show": show, "request": context.get("request")}
