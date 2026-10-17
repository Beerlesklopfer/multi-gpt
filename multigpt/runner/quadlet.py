"""Anleitung der Kopplungsseite (M14-02): natives Programm mit systemd-User-Unit
(Hauptweg, Rust-Agent) und – als Ausweg – die Quadlet-Datei des Python-Agents.

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
UNIT_TEMPLATE = "runner/multigpt-runner.service"
UNIT_NAME = "multigpt-runner.service"
SECRET_NAME = "mgpt_runner_token"
KEY_SECRET_NAME = "mgpt_runner_server_key"
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
Secret={key_secret},type=mount,target=/run/secrets/server_key
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
        "key_secret_name": KEY_SECRET_NAME,
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
            key_secret=KEY_SECRET_NAME,
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
        (
            "Schlüssel des Servers als Secret ablegen (Schlüssel einfügen, dann Strg+D)",
            f"podman secret create {KEY_SECRET_NAME} -",
        ),
        ("Ordner für die Quadlet-Datei", "mkdir -p ~/.config/containers/systemd"),
        (
            f"Datei {FILE_NAME} dort speichern, dann laden und starten",
            "systemctl --user daemon-reload && systemctl --user start multigpt-runner",
        ),
    ]


# --- Natives Programm (Hauptweg) -------------------------------------------------------


def render_unit(mgpt_url: str) -> str:
    """systemd-User-Unit des nativen Runners (``multigpt-runner pair`` legt sie an)."""
    return render_to_string(
        UNIT_TEMPLATE,
        {
            "mgpt_url": mgpt_url,
            "ca_sha256": ca_fingerprint(),
            "work_image": settings.RUNNER_WORK_IMAGE,
        },
    )


def config_setup_command() -> str:
    """Persönliche Konfiguration auf dem Runner-Rechner einmalig anlegen."""
    template = render_to_string("runner/config.env.example")
    return (
        'runner_config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/multigpt-runner"\n'
        'mkdir -p "$runner_config_dir" && (\n'
        "  umask 077\n"
        '  if [ ! -e "$runner_config_dir/config.env" ]; then\n'
        "    set -C\n"
        "    cat > \"$runner_config_dir/config.env\" <<'MGPT_CONFIG_EOF'\n"
        + template.rstrip("\n")
        + "\nMGPT_CONFIG_EOF\n  fi\n)"
    )


def native_commands(mgpt_url: str, binaries: dict) -> list[tuple[str, str]]:
    """Befehle in Reihenfolge: (Erklärung, Befehl). ``binaries``: arch → {name,
    sha256} aus ``trust.dist_info``. Der Token steht nie in einem Befehl
    (Shell-Verlauf), sondern kommt über die Eingabe von ``pair``."""
    ca = ca_fingerprint()
    pin = f" --ca-sha256 {ca}" if ca else ""
    sums = "\n".join(f"{item['sha256']}  {item['name']}" for item in binaries.values())
    return [
        ("Podman-Socket des Nutzers einschalten", "systemctl --user enable --now podman.socket"),
        ("Dienste auch ohne Anmeldung laufen lassen", "loginctl enable-linger $USER"),
        (
            "Im Ordner mit dem heruntergeladenen Programm: Architektur bestimmen",
            "cd ~/Downloads && ARCH=$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/')",
        ),
        (
            "Prüfsumme vergleichen (muss „OK“ melden)",
            f"sha256sum --ignore-missing -c <<'EOF'\n{sums}\nEOF",
        ),
        (
            "Programm installieren",
            "install -D -m 0755 multigpt-runner-linux-$ARCH ~/.local/bin/multigpt-runner",
        ),
        (
            "Persönliche Konfiguration anlegen (vorhandene Datei bleibt erhalten)",
            config_setup_command(),
        ),
        (
            "Koppeln und als Dienst starten (danach die Eingabe unten einfügen, dann Strg+D)",
            f"~/.local/bin/multigpt-runner pair --url {mgpt_url}{pin} "
            f"--images {settings.RUNNER_WORK_IMAGE}",
        ),
    ]
