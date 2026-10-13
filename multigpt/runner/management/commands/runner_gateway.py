"""Runner-Dienst starten (M14-04): ``manage.py runner_gateway``.

Startet uvicorn mit der ASGI-App aus ``multigpt/runner/asgi.py``. Adresse aus
``MULTI_GPT_RUNNER_BIND`` (Standard ``127.0.0.1:8001``, davor nginx mit
``location = /runner/ws``); ``unix:/pfad`` nimmt einen Unix-Socket. Ein Prozess
genügt für eine Familie; weil die Zustellung über PostgreSQL läuft, gingen auch
mehrere (je eigener Dienst bzw. Port).
"""

from __future__ import annotations

import os

from django.core.management.base import BaseCommand, CommandError

DEFAULT_BIND = "127.0.0.1:8001"


def parse_bind(value: str) -> dict:
    """``host:port``, ``[::1]:port`` oder ``unix:/pfad`` → uvicorn-Argumente."""
    value = (value or "").strip() or DEFAULT_BIND
    if value.startswith("unix:"):
        path = value[5:]
        if not path:
            raise CommandError("MULTI_GPT_RUNNER_BIND: Pfad des Unix-Sockets fehlt.")
        return {"uds": path}
    host, sep, port = value.rpartition(":")
    if not sep or not port.isdigit() or not 0 < int(port) < 65536:
        raise CommandError(f"MULTI_GPT_RUNNER_BIND ungültig: {value!r} (erwartet host:port)")
    host = host.strip("[]") or "127.0.0.1"
    return {"host": host, "port": int(port)}


def uvicorn_options(bind: str) -> dict:
    """Einstellungen laut Plan (Implementierung.md, M14 „Runner-Dienst“)."""
    return {
        **parse_bind(bind),
        "workers": 1,
        "ws": "websockets-sansio",  # ohne die veraltete legacy-Umsetzung
        "ws_max_size": 8 * 1024 * 1024,
        "ws_ping_interval": 20.0,
        "ws_ping_timeout": 20.0,
        "proxy_headers": True,
        "forwarded_allow_ips": "127.0.0.1,::1",
        "limit_concurrency": 200,
        "server_header": False,
        "date_header": False,
        "lifespan": "on",
        "log_config": None,  # Logging von Django (journald)
        "access_log": False,
    }


class Command(BaseCommand):
    help = "Runner-Dienst (WebSocket /runner/ws) mit uvicorn starten."

    def add_arguments(self, parser):
        parser.add_argument(
            "--bind",
            default=None,
            help="Adresse (host:port oder unix:/pfad); Standard MULTI_GPT_RUNNER_BIND "
            f"bzw. {DEFAULT_BIND}.",
        )

    def handle(self, *args, **options):
        import uvicorn

        bind = options["bind"] or os.environ.get("MULTI_GPT_RUNNER_BIND", "")
        config = uvicorn_options(bind)
        where = config.get("uds") or f"{config['host']}:{config['port']}"
        self.stdout.write(f"Runner-Dienst auf {where} (/runner/ws)")
        uvicorn.run("multigpt.runner.asgi:app", **config)
