"""Health-Check unter /healthz/ für systemd, Docker und Monitoring.

Als erste Middleware beantwortet sie die Anfrage selbst, bevor Django den
Host-Header prüft (CommonMiddleware, SecurityMiddleware). Deshalb muss
ALLOWED_HOSTS weder 127.0.0.1 noch den Container-Namen enthalten. Geprüft wird
nur die Datenbankverbindung.
"""

import logging

from django.db import DatabaseError, connection
from django.http import HttpResponse, HttpResponseNotAllowed

HEALTH_PATH = "/healthz/"

logger = logging.getLogger("multigpt.health")


class HealthCheckMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path_info != HEALTH_PATH:
            return self.get_response(request)

        if request.method not in ("GET", "HEAD"):
            response = HttpResponseNotAllowed(["GET", "HEAD"])
        else:
            try:
                connection.ensure_connection()
            except DatabaseError as exc:
                # Nur eine Zeile, kein Stacktrace: Ein Ausfall der DB ist ein
                # Betriebszustand, kein Programmfehler.
                logger.warning("Datenbank nicht erreichbar: %s", type(exc).__name__)
                response = HttpResponse("db unavailable", status=503, content_type="text/plain")
                # Verhindert den zusätzlichen ERROR-Eintrag von django.request.
                response._has_been_logged = True
            else:
                response = HttpResponse("ok", content_type="text/plain")
            if request.method == "HEAD":
                response.content = b""
        response["Cache-Control"] = "no-store"
        return response
