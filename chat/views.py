from django.contrib.auth.decorators import login_required
from django.db import DatabaseError, connection
from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe


@login_required
def index(request):
    return render(request, "chat/index.html")


@never_cache
@require_safe
def healthz(request):
    """Lebenszeichen für systemd, Docker und Monitoring. Prüft nur die DB-Verbindung."""
    try:
        connection.ensure_connection()
    except DatabaseError:
        return HttpResponse("db unavailable", status=503, content_type="text/plain")
    return HttpResponse("ok", content_type="text/plain")
