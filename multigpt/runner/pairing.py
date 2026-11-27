"""Anleitung der Kopplungsseite (M14-02): natives Programm ``multigpt-runner`` mit
systemd-User-Unit. Der Token steht nie in einer Datei oder einem Befehl, er kommt
über die Eingabe von ``multigpt-runner pair``.
"""

from __future__ import annotations

import hashlib
import re
import ssl
from pathlib import Path

from django.conf import settings
from django.template.loader import render_to_string

UNIT_TEMPLATE = "runner/multigpt-runner.service"
UNIT_NAME = "multigpt-runner.service"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


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
