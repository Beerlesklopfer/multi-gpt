"""ASGI-Einstieg des Runner-Dienstes (M14-04): ``multigpt.runner.asgi:app``.

Eigener Dienst (``multi-gpt-runner.service``, Verwaltungsbefehl
``runner_gateway``) mit uvicorn und Starlette, nicht unter gunicorn: Er hält je
Runner eine lang offene WebSocket-Verbindung (``/runner/ws``). Die übrige
Anwendung bleibt WSGI.
"""

import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "multigpt.settings")
django.setup()

from multigpt.runner.gateway import build_app  # noqa: E402 - erst nach django.setup()

app = build_app()
