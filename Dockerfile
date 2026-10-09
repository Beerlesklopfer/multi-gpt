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
    MULTI_GPT_BIND=0.0.0.0:8000 \
    MULTI_GPT_WORKERS=2

COPY --from=builder /build/multi-gpt_*.deb /tmp/

RUN apt-get update \
    && apt-get install -y --no-install-recommends /tmp/multi-gpt_*.deb \
    && rm -rf /var/lib/apt/lists/* /tmp/*.deb \
    && install -d -o multi-gpt -g multi-gpt /var/lib/multi-gpt

ENV PATH="/usr/share/python/multi-gpt/bin:${PATH}"

USER multi-gpt
WORKDIR /var/lib/multi-gpt
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=3)" || exit 1

CMD ["gunicorn", "--config", "/etc/multi-gpt/gunicorn.conf.py", "multi_gpt.wsgi:app"]
