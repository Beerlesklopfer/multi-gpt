"""Profilbild: ausliefern (alle angemeldeten Konten) und ändern (nur das eigene).

``GET  /konto/profilbild/<pk>/`` – WebP oder 404 (dann zeigt die Oberfläche
Initialen). Angemeldete Familienmitglieder sehen die Bilder der anderen (geteilte
Chats). Cache privat, Version über ``?v=<Zeitstempel>`` in der URL.
``POST /einstellungen/profilbild/`` – hochladen (Feld ``avatar``) bzw. entfernen
(``remove``); ändert nur das angemeldete Konto.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from . import avatar
from .models import Avatar


@require_GET
@login_required
def avatar_image(request, pk: int):
    data = Avatar.objects.filter(user_id=pk).values_list("data", flat=True).first()
    if not data:
        raise Http404
    response = HttpResponse(bytes(data), content_type="image/webp")
    response["Cache-Control"] = "private, max-age=86400"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'"
    return response


@require_POST
@login_required
def avatar_update(request):
    user = request.user
    if "remove" in request.POST:
        Avatar.objects.filter(user=user).delete()
        user.avatar_updated = None
        user.save(update_fields=["avatar_updated"])
        messages.success(request, "Profilbild entfernt.")
        return redirect("settings")
    upload = request.FILES.get("avatar")
    if upload is None:
        messages.error(request, "Bitte ein Bild auswählen.")
        return redirect("settings")
    try:
        data = avatar.process(upload, upload.size)
    except avatar.AvatarError as exc:
        messages.error(request, str(exc))
        return redirect("settings")
    Avatar.objects.update_or_create(user=user, defaults={"data": data})
    user.avatar_updated = timezone.now()
    user.save(update_fields=["avatar_updated"])
    messages.success(request, "Profilbild gespeichert.")
    return redirect("settings")
