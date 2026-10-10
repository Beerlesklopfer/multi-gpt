"""Chats teilen (RWUD): Freigaben verwalten, austragen, Kopie, Stand.

Endpunkte (Namespace ``chat``), JSON, Fehler ``{"error": "<Text>"}``, CSRF wie
alle API-Aufrufe:

- ``GET /api/conversations/<pk>/shares/`` – Freigaben und Auswahl (Konten,
  Gruppen); nur der Besitzer,
- ``POST …/shares/`` ``{kind: "user"|"group", target, can_write, can_update,
  can_delete}`` – freigeben bzw. Rechte ändern (gleicher Empfänger),
- ``PATCH …/shares/<id>/`` ``{can_write?, can_update?, can_delete?}`` – ändern,
- ``DELETE …/shares/<id>/`` – widerrufen (greift sofort),
- ``POST /api/conversations/<pk>/leave/`` – Empfänger: „Aus meiner Liste entfernen“,
- ``POST /api/conversations/<pk>/copy/`` – „Als eigene Kopie fortsetzen“ (R genügt),
- ``GET /api/conversations/<pk>/state/`` – Stand für den Hinweis „Neue Nachrichten“.

Rechte immer über ``can()``: kein READ -> 404 (keine Existenz-Lecks), lesbar,
aber nicht Besitzer -> 403. Teilen braucht zusätzlich das Recht SHARE der Rolle.
"""

from django.contrib.auth import get_user_model
from django.db import transaction
from django.http import JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from multigpt.accounts.models import UserGroup
from multigpt.accounts.permissions import Action, applicable_shares, can

from . import sharing
from .api import _error, _json_body, api_login_required
from .models import Conversation, Share

MSG_NOT_FOUND = "Chat nicht gefunden."
FLAGS = ("can_write", "can_update", "can_delete")


def _readable(request, pk: int) -> Conversation | None:
    conversation = Conversation.objects.select_related("user").filter(pk=pk).first()
    if conversation is None or not can(request.user, Action.READ, conversation):
        return None
    return conversation


def _owned(request, pk: int) -> tuple[Conversation | None, JsonResponse | None]:
    conversation = _readable(request, pk)
    if conversation is None:
        return None, _error(MSG_NOT_FOUND, 404)
    if conversation.user_id != request.user.pk:
        return None, _error("Nur wer den Chat angelegt hat, kann ihn teilen.", 403)
    if not can(request.user, Action.SHARE):
        return None, _error("Du darfst keine Chats teilen.", 403)
    return conversation, None


def _flags(data: dict, *, partial: bool) -> tuple[dict | None, str | None]:
    flags = {}
    for key in FLAGS:
        if key not in data:
            if not partial:
                flags[key] = False
            continue
        if not isinstance(data[key], bool):
            return None, "Ungültige Angabe der Rechte."
        flags[key] = data[key]
    return flags, None


def _listing(conversation: Conversation, owner) -> JsonResponse:
    return JsonResponse(
        {"shares": sharing.conversation_shares(conversation), **sharing.recipients(owner)}
    )


@require_http_methods(["GET", "POST"])
@api_login_required
def shares(request, pk: int):
    conversation, err = _owned(request, pk)
    if err:
        return err
    if request.method == "GET":
        return _listing(conversation, request.user)

    data = _json_body(request)
    if data is None:
        return _error("Ungültige Anfrage.", 400)
    kind = data.get("kind")
    target = data.get("target")
    if kind not in ("user", "group") or isinstance(target, bool) or not isinstance(target, int):
        return _error("Bitte ein Konto oder eine Gruppe wählen.", 400)
    flags, msg = _flags(data, partial=False)
    if msg:
        return _error(msg, 400)
    if kind == "user":
        recipient = get_user_model().objects.filter(pk=target, is_active=True).first()
        if recipient is None:
            return _error("Konto nicht gefunden.", 400)
        if recipient.pk == request.user.pk:
            return _error("Mit dir selbst musst du nicht teilen.", 400)
        lookup = {"user": recipient}
    else:
        recipient = UserGroup.objects.filter(pk=target).first()
        if recipient is None:
            return _error("Gruppe nicht gefunden.", 400)
        lookup = {"group": recipient}
    with transaction.atomic():
        share, created = Share.objects.update_or_create(
            conversation=conversation, **lookup, defaults=flags
        )
        if not created:
            # Erneut freigegeben: wer sich ausgetragen hatte, sieht den Chat wieder.
            share.left_by.clear()
    response = _listing(conversation, request.user)
    response.status_code = 201 if created else 200
    return response


@require_http_methods(["PATCH", "DELETE"])
@api_login_required
def share_detail(request, pk: int, share_id: int):
    conversation, err = _owned(request, pk)
    if err:
        return err
    share = Share.objects.filter(pk=share_id, conversation=conversation).first()
    if share is None:
        return _error("Freigabe nicht gefunden.", 404)
    if request.method == "DELETE":
        share.delete()  # Widerruf: jede weitere Anfrage prüft neu (can)
        return _listing(conversation, request.user)
    data = _json_body(request)
    if data is None:
        return _error("Ungültige Anfrage.", 400)
    flags, msg = _flags(data, partial=True)
    if msg:
        return _error(msg, 400)
    if not flags:
        return _error("Keine Änderung angegeben.", 400)
    Share.objects.filter(pk=share.pk).update(**flags)
    return _listing(conversation, request.user)


@require_POST
@api_login_required
def leave(request, pk: int):
    conversation = _readable(request, pk)
    if conversation is None:
        return _error(MSG_NOT_FOUND, 404)
    if conversation.user_id == request.user.pk:
        return _error("Eigene Chats lassen sich archivieren oder löschen.", 400)
    if not sharing.leave(request.user, conversation):
        return _error("Dieser Chat ist nicht mit dir geteilt.", 400)
    return JsonResponse({"left": True, "id": pk, "url": reverse("chat:index")})


@require_POST
@api_login_required
def copy(request, pk: int):
    conversation = _readable(request, pk)
    if conversation is None:
        return _error(MSG_NOT_FOUND, 404)
    if not can(request.user, Action.CHAT):
        return _error("Chatten ist für dieses Konto nicht freigegeben.", 403)
    if conversation.user_id == request.user.pk:
        return _error("Das ist schon dein eigener Chat.", 400)
    if not applicable_shares(request.user, conversation).exists():
        # z. B. Einsicht durch Verwalter: lesen ja, kopieren nein
        return _error("Nur geteilte Chats lassen sich kopieren.", 403)
    copied = sharing.copy_conversation(request.user, conversation)
    return JsonResponse(
        {
            "id": copied.pk,
            "title": copied.title,
            "url": reverse("chat:conversation", args=[copied.pk]),
        },
        status=201,
    )


@require_GET
@api_login_required
def state(request, pk: int):
    conversation = _readable(request, pk)
    if conversation is None:
        return _error(MSG_NOT_FOUND, 404)
    access = sharing.access_for(request.user, conversation)
    response = JsonResponse(
        {
            "stamp": sharing.state_stamp(conversation),
            "rights": access.label,
            "can_write": access.write,
        }
    )
    response["Cache-Control"] = "private, no-store"
    return response
