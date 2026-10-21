# syntax=docker/dockerfile:1

ARG DEBIAN_RELEASE=trixie

# --- Stage 1: build the .deb (dh-virtualenv -> /usr/share/python/multi-gpt) ---
FROM debian:${DEBIAN_RELEASE} AS builder

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        debhelper \
        dh-virtualenv \
        python3 \
        python3-dev \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build/src
COPY . .

RUN dpkg-buildpackage -us -uc -b


# --- Stage 2: runtime ---
FROM debian:${DEBIAN_RELEASE}-slim AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DJANGO_SETTINGS_MODULE=multigpt.settings \
    STATIC_ROOT=/usr/share/python/multi-gpt/static \
    MEDIA_ROOT=/var/lib/multi-gpt/media \
    MULTI_GPT_BIND=0.0.0.0:8000

COPY --from=builder /build/multi-gpt_*.deb /tmp/

# Das postinst legt Nutzer und Verzeichnisse an und erzeugt
# /etc/multi-gpt/.env mit Schlüsseln. Die Datei wird sofort gelöscht:
# keine Secrets im Image. Im Container kommt die Konfiguration aus der
# Umgebung (compose.yaml: env_file .env). nginx kommt als Abhängigkeit des
# Pakets mit, wird im Container aber nicht benutzt (MULTI_GPT_SKIP_NGINX=1:
# Site nicht aktivieren); gunicorn lauscht hier über MULTI_GPT_BIND auf 0.0.0.0.
RUN apt-get update \
    && MULTI_GPT_SKIP_NGINX=1 apt-get install -y --no-install-recommends /tmp/multi-gpt_*.deb \
        bubblewrap \
    && bwrap --version \
    && rm -rf /var/lib/apt/lists/* /tmp/*.deb \
    && rm -f /etc/multi-gpt/.env \
    && install -d -m 0750 -o multi-gpt -g multi-gpt /var/lib/multi-gpt /var/lib/multi-gpt/media

# Sandbox für run_python: bubblewrap (/usr/bin/bwrap) kommt als Abhängigkeit des
# Pakets und steht oben noch einmal ausdrücklich, damit es nie fehlt. Ob die
# Sandbox läuft, hängt von der Container-Laufzeit ab: bwrap legt User-Namespaces
# und Mounts an. Rootless Podman erlaubt das; das Standardprofil von rootful
# Docker (seccomp, AppArmor) sperrt es, dort bleibt das Werkzeug ausgeschaltet
# (kein unsicherer Rückfall), siehe compose.yaml. Prüfen: Admin >
# Chat-Einstellungen > „Sandbox testen“.
ENV PATH="/usr/share/python/multi-gpt/bin:${PATH}"

USER multi-gpt
WORKDIR /var/lib/multi-gpt
EXPOSE 8000

# /healthz/ prüft auch die Datenbank und ist unabhängig von ALLOWED_HOSTS.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz/', timeout=4)"]

CMD ["gunicorn", "--config", "/etc/multi-gpt/gunicorn.conf.py", "multigpt.wsgi:application"]
