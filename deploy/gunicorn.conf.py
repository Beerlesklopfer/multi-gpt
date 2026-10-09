"""Gunicorn-Konfiguration für MultiGPT.

Wird verwendet von
- systemd (Debian-Paket): /etc/multi-gpt/gunicorn.conf.py
- Docker:                 /etc/multi-gpt/gunicorn.conf.py
- Entwicklung (make run): deploy/gunicorn.conf.py

Überschreibbar über Umgebungsvariablen (im Betrieb in /etc/multi-gpt/.env):
  MULTI_GPT_BIND     Adresse, Standard 127.0.0.1:8000
  MULTI_GPT_WORKERS  Anzahl Worker-Prozesse, Standard 2
  MULTI_GPT_THREADS  Threads je Worker (gthread), Standard 8
  MULTI_GPT_TIMEOUT  Sekunden bis ein hängender Worker neu gestartet wird, Standard 300
  MULTI_GPT_FORWARDED_ALLOW_IPS  Proxys, deren X-Forwarded-Proto gunicorn glaubt,
                     Standard 127.0.0.1,::1 (nginx auf demselben Rechner)

Im Debian-Paket lauscht gunicorn nur auf 127.0.0.1; davor steht nginx mit TLS.

gthread, weil Antworten per SSE gestreamt werden (lange offene Requests).
"""

import os
import sys


def _int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


bind = os.environ.get("MULTI_GPT_BIND", "").strip() or "127.0.0.1:8000"
worker_class = "gthread"
workers = _int("MULTI_GPT_WORKERS", 2)
threads = _int("MULTI_GPT_THREADS", 8)
timeout = _int("MULTI_GPT_TIMEOUT", 300)
graceful_timeout = 30
keepalive = 5

# Nur der lokale nginx darf wsgi.url_scheme per X-Forwarded-Proto setzen
# (Django wertet den Header zusätzlich über SECURE_PROXY_SSL_HEADER aus).
# Docker hinter einem Proxy in einem anderen Container: Variable setzen.
forwarded_allow_ips = os.environ.get("MULTI_GPT_FORWARDED_ALLOW_IPS", "").strip() or "127.0.0.1,::1"

# Heartbeat-Datei der Worker im RAM statt auf der Platte (siehe Gunicorn-Doku
# "blocking os.fchmod"); fällt auf das Standard-Tempverzeichnis zurück.
worker_tmp_dir = "/dev/shm" if os.access("/dev/shm", os.W_OK) else None

# Logs nach stdout/stderr (journald bzw. docker logs). Standard-Access-Format:
# Request-Zeile, Status, Größe, Referer, User-Agent – keine Request-Bodies.
accesslog = "-"
errorlog = "-"
_level = os.environ.get("LOG_LEVEL", "").strip().lower()
loglevel = _level if _level in {"debug", "info", "warning", "error", "critical"} else "info"

# Gunicorn >= 25.1 legt sonst eine Steuer-Socket unter $HOME/.gunicorn an.
# Wird nicht gebraucht; ältere Versionen ignorieren die unbekannte Einstellung.
control_socket_disable = True


def worker_exit(server, worker):
    """MCP-Sitzungen schließen und stdio-Kindprozesse beenden (Plan 8g, M4a-01).

    Nur wenn der Worker die MCP-Brücke überhaupt benutzt hat; zusätzlich
    räumt ``atexit`` auf.
    """
    bridge = sys.modules.get("multigpt.chat.mcp.bridge")
    if bridge is not None:
        bridge.shutdown()
