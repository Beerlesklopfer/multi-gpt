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
# Umgebung (compose.yaml: env_file .env).
RUN apt-get update \
    && apt-get install -y --no-install-recommends /tmp/multi-gpt_*.deb \
    && rm -rf /var/lib/apt/lists/* /tmp/*.deb \
    && rm -f /etc/multi-gpt/.env \
    && install -d -m 0750 -o multi-gpt -g multi-gpt /var/lib/multi-gpt /var/lib/multi-gpt/media

ENV PATH="/usr/share/python/multi-gpt/bin:${PATH}"

USER multi-gpt
WORKDIR /var/lib/multi-gpt
EXPOSE 8000

# /healthz/ prüft auch die Datenbank und ist unabhängig von ALLOWED_HOSTS.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz/', timeout=4)"]

CMD ["gunicorn", "--config", "/etc/multi-gpt/gunicorn.conf.py", "multigpt.wsgi:application"]
