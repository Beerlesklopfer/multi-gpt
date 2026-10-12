"""Seite „Mitteilungen“ (A15, ``chat/notices.py``): nur die eigenen.

Ohne JavaScript: Liste per GET, „als gelesen markieren“ und „alle gelesen“ als
POST-Formulare mit Rückkehr zur Liste. Fremde Mitteilungen sind nicht
erreichbar (Filter auf das angemeldete Konto, sonst 404).
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from . import notices
from .models import Notice

PAGE_LIMIT = 200


@require_GET
@login_required
def notice_list(request):
    items = list(
        Notice.objects.filter(recipient=request.user).select_related("conversation")[:PAGE_LIMIT]
    )
    return render(
        request,
        "chat/notices.html",
        {
            "notices": items,
            "unread": sum(1 for n in items if n.read is None),
            "retention_days": notices.RETENTION_DAYS,
        },
    )


@require_POST
@login_required
def notice_read(request, pk: int):
    if not Notice.objects.filter(pk=pk, recipient=request.user).exists():
        raise Http404
    if notices.mark_read(request.user, pk):
        messages.success(request, "Mitteilung als gelesen markiert.")
    return redirect("chat:notices")


@require_POST
@login_required
def notice_read_all(request):
    count = notices.mark_read(request.user)
    if count:
        messages.success(request, "Alle Mitteilungen als gelesen markiert.")
    return redirect("chat:notices")
