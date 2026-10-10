"""JSON-Endpunkte der Projekte (M5-07, chat/projects.py).

- ``GET /api/projects/[?archived=1]``: eigene Projekte (ohne Inhalte),
- ``POST /api/projects/`` ``{name, description?, instructions?, default_model?,
  temperature?, reasoning_effort?, collections?, color?}`` -> 201 mit allen Feldern
  (``temperature``: Kreativität 0–2 oder ``null``, creativity.py;
  ``reasoning_effort``: Denktiefe oder ``""``/``null``, reasoning.py),
- ``GET|PATCH /api/projects/<pk>/`` (PATCH zusätzlich ``pinned``, ``archived``),
- ``DELETE /api/projects/<pk>/?chats=keep|delete``: Angabe ist Pflicht, damit
  niemand Chats versehentlich mitlöscht,
- ``POST /api/conversations/<pk>/project/`` ``{project: <id>|null}``: Chat
  einem Projekt zuordnen bzw. herausnehmen.

Fehler als ``{"error": "<Text>"}`` wie in api.py. Fremde Projekte -> 404 (auch
für Verwalter); fremde, nicht lesbare Chats -> 404, lesbare fremde Chats
(Freigabe) -> 403: Zuordnen darf nur der Besitzer. CSRF prüft Django wie bei
allen anderen Endpunkten (Header ``X-CSRFToken``).
"""

from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods, require_POST

from multigpt.accounts.permissions import Action, can

from . import creativity, projects, reasoning
from .api import _error, _json_body, api_login_required
from .models import Conversation, Project


def _apply(user, project: Project, data: dict, *, creating: bool):
    """Felder aus ``data`` prüfen und setzen. Liefert (Sammlungen|None, Fehler|None)."""
    if creating or "name" in data:
        name, err = projects.clean_name(data.get("name"))
        if err:
            return None, err
        project.name = name
    for key, limit, label in (
        ("description", projects.DESCRIPTION_MAX_LENGTH, "Beschreibung"),
        ("instructions", projects.INSTRUCTIONS_MAX_LENGTH, "Anweisungen"),
    ):
        if key in data:
            value, err = projects.clean_text(data[key], limit, label)
            if err:
                return None, err
            setattr(project, key, value)
    if "default_model" in data:
        model, err = projects.clean_default_model(user, data["default_model"])
        if err:
            return None, err
        project.default_model = model
    if "temperature" in data:
        value, err = creativity.clean(data["temperature"])
        if err:
            return None, err
        project.temperature = value
    if "reasoning_effort" in data:
        effort, err = reasoning.clean(data["reasoning_effort"])
        if err:
            return None, err
        project.reasoning_effort = effort
    if "color" in data:
        color, err = projects.clean_color(data["color"])
        if err:
            return None, err
        project.color = color
    for key in ("pinned", "archived"):
        if key in data and not creating:
            if not isinstance(data[key], bool):
                return None, "Ungültige Anfrage."
            setattr(project, key, data[key])
    collections = None
    if "collections" in data:
        collections, err = projects.clean_collections(user, data["collections"])
        if err:
            return None, err
    return collections, None


def _status_for(err: str) -> int:
    return 404 if err == "Sammlung nicht gefunden." else 400


@require_http_methods(["GET", "POST"])
@api_login_required
def project_list(request):
    user = request.user
    if request.method == "GET":
        archived = request.GET.get("archived") == "1"
        qs = projects.with_chat_count(projects.own_projects(user, archived=archived))
        return JsonResponse([projects.serialize_project(p, user) for p in qs], safe=False)

    if not can(user, Action.CHAT):
        return _error("Chatten ist für dieses Konto nicht freigegeben.", 403)
    data = _json_body(request)
    if data is None:
        return _error("Ungültige Anfrage.", 400)
    project = Project(owner=user)
    collections, err = _apply(user, project, data, creating=True)
    if err:
        return _error(err, _status_for(err))
    with transaction.atomic():
        project.save()
        if collections:
            project.collections.set(collections)
    return JsonResponse(projects.serialize_project(project, user, detail=True), status=201)


@require_http_methods(["GET", "PATCH", "DELETE"])
@api_login_required
def project_detail(request, pk: int):
    user = request.user
    project = projects.get_own_project(user, pk)
    if project is None:
        return _error(projects.MSG_NOT_FOUND, 404)

    if request.method == "GET":
        return JsonResponse(projects.serialize_project(project, user, detail=True))

    if request.method == "DELETE":
        mode = request.GET.get("chats")
        if mode not in ("keep", "delete"):
            return _error(
                "Bitte angeben, ob die Chats behalten (chats=keep) oder mitgelöscht "
                "(chats=delete) werden.",
                400,
            )
        deleted = projects.delete_project(project, delete_chats=mode == "delete")
        return JsonResponse({"deleted": True, "id": pk, "deleted_chats": deleted})

    data = _json_body(request)
    if data is None:
        return _error("Ungültige Anfrage.", 400)
    if not data:
        return _error("Keine Änderung angegeben.", 400)
    collections, err = _apply(user, project, data, creating=False)
    if err:
        return _error(err, _status_for(err))
    with transaction.atomic():
        project.save()
        if collections is not None:
            project.collections.set(collections)
    return JsonResponse(projects.serialize_project(project, user, detail=True))


@require_POST
@api_login_required
def conversation_project(request, pk: int):
    """Chat in ein eigenes Projekt verschieben (``project``: ID) oder herausnehmen (null)."""
    user = request.user
    conversation = Conversation.objects.filter(pk=pk).first()
    if conversation is None or not can(user, Action.READ, conversation):
        return _error("Chat nicht gefunden.", 404)
    if conversation.user_id != user.pk:
        return _error("Nur der Besitzer kann den Chat einem Projekt zuordnen.", 403)
    data = _json_body(request)
    if data is None or "project" not in data:
        return _error("Ungültige Anfrage.", 400)
    raw = data["project"]
    project = None
    if raw is not None:
        if isinstance(raw, bool) or not isinstance(raw, int):
            return _error("Ungültige Anfrage.", 400)
        project = projects.get_own_project(user, raw)
        if project is None:
            return _error(projects.MSG_NOT_FOUND, 404)
    projects.move_conversation(conversation, project)
    return JsonResponse(
        {
            "id": conversation.pk,
            "project": project.pk if project else None,
            "project_name": project.name if project else "",
            "project_archived": project.archived if project else False,
        }
    )
