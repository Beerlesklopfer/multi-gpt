"""Chats verwalten (M5-02, M5-03): umbenennen, System-Prompt, archivieren, löschen.

``PATCH /api/conversations/<pk>/`` mit JSON ``{title?, system_prompt?, archived?}``
und ``DELETE /api/conversations/<pk>/``. Antworten als JSON, Fehler wie in
api.py als ``{"error": "<Text>"}``.

Rechte (immer über ``can()``):

- kein READ -> 404 (fremde Chats bleiben unsichtbar),
- READ ohne WRITE (Freigabe nur lesend) -> 403,
- Titel und System-Prompt: WRITE genügt (auch Freigabe mit Schreibrecht),
- Archivieren und Löschen: zusätzlich nur der Besitzer. Das Archiv ist die
  eigene Chatliste des Besitzers, Löschen ist endgültig – beides soll ein
  Gruppenmitglied mit Schreibrecht nicht für den Besitzer entscheiden.
"""

from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from multigpt.accounts.permissions import Action, can

from .api import _error, _json_body, api_login_required
from .models import Conversation

TITLE_MAX_LENGTH = Conversation._meta.get_field("title").max_length
SYSTEM_PROMPT_MAX_LENGTH = 20_000


def serialize_conversation(conversation: Conversation, user) -> dict:
    return {
        "id": conversation.pk,
        "title": conversation.title,
        "system_prompt": conversation.system_prompt,
        "archived": conversation.archived,
        "is_owner": conversation.user_id == user.pk,
    }


def _clean_title(raw) -> tuple[str | None, str | None]:
    if not isinstance(raw, str):
        return None, "Ungültiger Titel."
    # Nur eine Zeile, Steuerzeichen raus; Markdown-Zeichen darf man bewusst setzen.
    title = " ".join(raw.replace("\x00", " ").split())
    if not title:
        return None, "Der Titel darf nicht leer sein."
    if len(title) > TITLE_MAX_LENGTH:
        return None, f"Der Titel darf höchstens {TITLE_MAX_LENGTH} Zeichen lang sein."
    return title, None


def _clean_system_prompt(raw) -> tuple[str | None, str | None]:
    if not isinstance(raw, str):
        return None, "Ungültiger System-Prompt."
    prompt = raw.replace("\x00", "").replace("\r\n", "\n").strip()
    if len(prompt) > SYSTEM_PROMPT_MAX_LENGTH:
        return None, (
            f"Der System-Prompt darf höchstens {SYSTEM_PROMPT_MAX_LENGTH} Zeichen lang sein."
        )
    return prompt, None


@require_http_methods(["PATCH", "DELETE"])
@api_login_required
def conversation_detail(request, pk: int):
    conversation = Conversation.objects.filter(pk=pk).first()
    if conversation is None or not can(request.user, Action.READ, conversation):
        return _error("Chat nicht gefunden.", 404)
    if not can(request.user, Action.WRITE, conversation):
        return _error("Du darfst diesen Chat nur lesen.", 403)
    is_owner = conversation.user_id == request.user.pk

    if request.method == "DELETE":
        if not is_owner:
            return _error("Nur wer den Chat angelegt hat, kann ihn löschen.", 403)
        conversation.delete()
        return JsonResponse({"deleted": True, "id": pk})

    data = _json_body(request)
    if data is None:
        return _error("Ungültige Anfrage.", 400)
    fields = {}
    if "title" in data:
        title, err = _clean_title(data["title"])
        if err:
            return _error(err, 400)
        fields["title"] = title
    if "system_prompt" in data:
        prompt, err = _clean_system_prompt(data["system_prompt"])
        if err:
            return _error(err, 400)
        fields["system_prompt"] = prompt
    if "archived" in data:
        if not isinstance(data["archived"], bool):
            return _error("Ungültiger Wert für „archiviert“.", 400)
        if not is_owner:
            return _error("Nur wer den Chat angelegt hat, kann ihn archivieren.", 403)
        fields["archived"] = data["archived"]
    if not fields:
        return _error("Keine Änderung angegeben.", 400)

    # update() statt save(): "updated" bleibt, damit Umbenennen oder Archivieren
    # die Reihenfolge der Chatliste nicht verändert.
    Conversation.objects.filter(pk=conversation.pk).update(**fields)
    for key, value in fields.items():
        setattr(conversation, key, value)
    return JsonResponse(serialize_conversation(conversation, request.user))
