"""Fertige Quadlet-Datei und Befehle für die Kopplungsseite (M14-02).

Die Vorlage ``runner/multigpt-runner.container`` (Inhalt: ``ops``) bekommt
Agent- und Arbeits-Image, die Adresse des Servers und – bei einem
selbstsignierten Zertifikat – dessen SHA-256-Fingerprint (8i). Der Token steht
nie in der Datei: Er liegt als Podman-Secret vor (``podman secret create``),
nicht in einer Umgebungsvariable, die ``inspect`` und ``ps`` zeigen würden.
"""

from __future__ import annotations

import hashlib
import re
import ssl
from pathlib import Path

from django.conf import settings
from django.template import TemplateDoesNotExist
from django.template.loader import render_to_string

TEMPLATE = "runner/multigpt-runner.container"
FILE_NAME = "multigpt-runner.container"
SECRET_NAME = "mgpt_runner_token"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")

# Ersatz, falls die Vorlage fehlt (gleiche Felder wie die Vorlage von ops).
_FALLBACK = """\
[Unit]
Description=MultiGPT-Runner (Agent)
Wants=network-online.target
After=network-online.target

[Container]
ContainerName=multigpt-runner
Image={agent_image}
Volume=%t/podman/podman.sock:/run/podman.sock
Secret={secret},type=mount,target=/run/secrets/token
Environment=MGPT_URL={mgpt_url}
{ca_line}Environment=MGPT_IMAGES={work_image}
NoNewPrivileges=true
DropCapability=ALL
ReadOnly=true

[Service]
Restart=always

[Install]
WantedBy=default.target
"""


def ca_fingerprint() -> str:
    """SHA-256 des Server-Zertifikats (hex, klein, ohne Doppelpunkte) oder ''.

    ``RUNNER_CA_SHA256`` geht vor; sonst aus der PEM-Datei ``RUNNER_TLS_CERT``.
    """
    explicit = (getattr(settings, "RUNNER_CA_SHA256", "") or "").replace(":", "").lower()
    if _HEX64.match(explicit):
        return explicit
    path = getattr(settings, "RUNNER_TLS_CERT", "") or ""
    if not path:
        return ""
    try:
        der = ssl.PEM_cert_to_DER_cert(Path(path).read_text(encoding="ascii"))
    except (OSError, ValueError, UnicodeDecodeError):
        return ""
    return hashlib.sha256(der).hexdigest()


def server_url(request) -> str:
    """``https://<host>`` aus der Anfrage (hinter nginx mit X-Forwarded-Proto)."""
    return f"{request.scheme}://{request.get_host()}"


def render(mgpt_url: str) -> str:
    context = {
        "agent_image": settings.RUNNER_AGENT_IMAGE,
        "work_image": settings.RUNNER_WORK_IMAGE,
        "mgpt_url": mgpt_url,
        "ca_sha256": ca_fingerprint(),
        "secret_name": SECRET_NAME,
    }
    try:
        return render_to_string(TEMPLATE, context)
    except TemplateDoesNotExist:
        ca = context["ca_sha256"]
        return _FALLBACK.format(
            agent_image=context["agent_image"],
            work_image=context["work_image"],
            mgpt_url=mgpt_url,
            secret=SECRET_NAME,
            ca_line=f"Environment=MGPT_CA_SHA256={ca}\n" if ca else "",
        )


def setup_commands() -> list[tuple[str, str]]:
    """Befehle in Reihenfolge: (Erklärung, Befehl). Der Token kommt über die Eingabe."""
    return [
        ("Podman-Socket des Nutzers einschalten", "systemctl --user enable --now podman.socket"),
        ("Dienste auch ohne Anmeldung laufen lassen", "loginctl enable-linger $USER"),
        (
            "Token als Secret ablegen (Token einfügen, dann Strg+D)",
            f"podman secret create {SECRET_NAME} -",
        ),
        ("Ordner für die Quadlet-Datei", "mkdir -p ~/.config/containers/systemd"),
        (
            f"Datei {FILE_NAME} dort speichern, dann laden und starten",
            "systemctl --user daemon-reload && systemctl --user start multigpt-runner",
        ),
    ]
