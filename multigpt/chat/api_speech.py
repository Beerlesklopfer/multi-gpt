"""Sprache (M10): Endpunkte für Mikrofon, Vorlesen und Stimmenliste (Logik in ``speech.py``).

- ``POST /api/speech/transcribe/`` (multipart ``file``, ``duration`` in Sekunden, optional
  ``language``) -> 200 ``{"text", "language", "seconds", "model"}``.
- ``POST /api/messages/<id>/speak/`` (optional JSON ``{"force": true}``) -> 200
  ``{"attachment": {...}, "created", "truncated"}``; die Datei liefert ``/anhang/<id>/``.
- ``GET /api/speech/voices/?model=<id>`` -> ``{"voices": [...]}`` (nur Modelle des Kontos).

Fehler als ``{"error": "<deutscher Text>"}`` mit 400/403/404/409/413/415/422/502/503. Alle
Endpunkte brauchen das Recht ``VOICE``; Vorlesen prüft außerdem das Leserecht auf den Chat.
"""

from __future__ import annotations

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_POST

from multigpt.accounts.permissions import Action, can

from . import attachments as chat_attachments
from . import speech
from .api import _error, _json_body, api_login_required
from .models import AIModel, Message


@require_POST
@api_login_required
def transcribe(request):
    user = request.user
    if not can(user, Action.VOICE):
        return _error(speech.MSG_NOT_ALLOWED, 403)
    language = request.POST.get("language")
    if language is None:
        pref = speech.preference(user)
        language = pref.stt_language if pref else "de"
    try:
        result = speech.transcribe(
            user, request.FILES.get("file"), request.POST.get("duration"), language
        )
    except speech.SpeechError as exc:
        return _error(exc.message, exc.status)
    return JsonResponse(result)


@require_POST
@api_login_required
def speak(request, pk: int):
    user = request.user
    if not can(user, Action.VOICE):
        return _error(speech.MSG_NOT_ALLOWED, 403)
    message = get_object_or_404(Message.objects.select_related("conversation"), pk=pk)
    if not can(user, Action.READ, message.conversation):
        return _error("Nachricht nicht gefunden.", 404)
    data = _json_body(request)
    if data is None:
        return _error("Ungültige Anfrage.", 400)
    try:
        attachment, created, truncated = speech.speak(
            user, message, force=data.get("force") is True
        )
    except speech.SpeechError as exc:
        return _error(exc.message, exc.status)
    return JsonResponse(
        {
            "attachment": chat_attachments.serialize(attachment),
            "created": created,
            "truncated": truncated,
        }
    )


@require_GET
@api_login_required
def voices(request):
    user = request.user
    if not can(user, Action.VOICE):
        return _error(speech.MSG_NOT_ALLOWED, 403)
    try:
        pk = int(request.GET.get("model", ""))
    except ValueError:
        return _error("Ungültige Anfrage.", 400)
    candidates = speech.candidate_models(user, AIModel.Capability.TTS)
    ai_model = next((m for m in candidates if m.pk == pk), None)
    if ai_model is None:
        return _error(speech.MSG_NOT_FOUND, 404)
    refresh = request.GET.get("refresh") == "1"
    return JsonResponse({"voices": speech.voices(ai_model, refresh=refresh)})
