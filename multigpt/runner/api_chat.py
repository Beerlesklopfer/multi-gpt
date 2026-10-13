"""Auswahl im Chat (8j/8t): ``GET/POST /runner/api/chat/<pk>/``.

``GET`` liefert den Stand aus ``runner.chat.get_state`` (Arbeitsstand, Modus,
Netz, Sitzung, Aufgabenliste, eigene Arbeitsstände zur Auswahl). ``POST`` mit
JSON ``{"workspace": <id>|null, "mode": "ask"|"edits"|"auto"|"",
"scratch_mode": "container"|"local"}`` ändert die Auswahl (nur übergebene
Schlüssel; ``scratch_mode`` gilt für den gewählten Arbeitsstand). Fehler als
``{"error": "<Text>"}`` wie in ``chat/api.py``; fremde Chats -> 404.
"""

from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from multigpt.accounts.permissions import Action, can
from multigpt.chat.api import _error, _json_body, api_login_required
from multigpt.chat.models import Conversation

from . import chat as runner_chat
from .models import RunnerWorkspace


def _payload(user, conversation) -> dict:
    data = runner_chat.get_state(user, conversation)
    data["workspaces"] = [
        {"id": w.pk, "name": w.name, "locked": w.locked, "size": w.size}
        for w in RunnerWorkspace.objects.filter(owner=user).order_by("name")
    ]
    return data


@require_http_methods(["GET", "POST"])
@api_login_required
def chat_state(request, pk: int):
    conversation = Conversation.objects.filter(pk=pk).first()
    if conversation is None or not can(request.user, Action.READ, conversation):
        return _error("Chat nicht gefunden.", 404)
    if request.method == "GET":
        return JsonResponse(_payload(request.user, conversation))
    data = _json_body(request)
    if data is None:
        return _error("Ungültige Anfrage.", 400)
    changes = {
        key: data[key] for key in ("workspace", "mode", "scratch_mode", "packages") if key in data
    }
    for key in ("mode", "scratch_mode"):
        if key in changes and not isinstance(changes[key], str | type(None)):
            return _error("Unbekannter Modus.", 400)
    try:
        runner_chat.set_state(request.user, conversation, **changes)
    except ValueError as exc:
        status = 403 if not can(request.user, Action.USE_RUNNER) else 400
        return _error(str(exc), status)
    return JsonResponse(_payload(request.user, conversation))
