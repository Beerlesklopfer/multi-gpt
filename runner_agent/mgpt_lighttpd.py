#!/usr/bin/python3 -I
"""mgpt-serve (Arbeits-Image): statischer Webserver lighttpd für die Vorschau.

Im Arbeits-Image liegt diese Datei als ``/usr/local/bin/mgpt-serve``. Aufruf durch
den Agent wie beim Python-Server (``mgpt_serve.py``, nur für die FakeEngine in
Tests): ``mgpt-serve --token T [--dir SUB] [--port 8080] [--tls-port 8443]``.

Entscheidungen (Nutzer, 2026-10-14):

- **lighttpd** statt eines eigenen Python-Servers; nur statische Dateien: kein
  CGI, keine Templates, keine Verzeichnislisten (Verzeichnis → index.html).
- Docroot ist ``/workspace`` bzw. das Unterverzeichnis ``--dir``. Der Inhalt
  ist unter ``/`` und unter ``/<token>/`` erreichbar (die Vorschau über MultiGPT
  nutzt den Pfad mit Token).
- Port 8080 (http) und 8443 (https, selbst signiertes Zertifikat, beim Start
  im tmpfs erzeugt und nie gespeichert). Nutzer 1000 ohne Capabilities kann die
  Ports 80/443 nicht binden; der Agent veröffentlicht sie als 24080/24443.
- Nur GET und HEAD, keine Symlinks, ``.git`` gesperrt, strenge Kopfzeilen und CSP.
  Die native Seite behält ihren Ursprung; die MultiGPT-Vorschau setzt ihre eigene Sandbox.
- Eine laufende Vorschau (anderes Verzeichnis) wird beendet und ersetzt: lighttpd
  ersetzt diesen Prozess per ``exec``, die PID bleibt gleich und steht in der PID-Datei.
- Die erzeugte Konfiguration liegt in /workspace/lighttpd.conf und wird mit -f übergeben.
"""

from __future__ import annotations

import argparse
import http.client
import os
import re
import signal
import subprocess
import sys
import time

WORKSPACE = os.path.realpath(os.environ.get("MGPT_WORKSPACE") or "/workspace")
TMP = os.environ.get("MGPT_TMP") or "/tmp"
RUN_DIR = os.path.join(TMP, "mgpt-lighttpd")
PIDFILE = os.path.join(TMP, "mgpt-serve.pid")
LIGHTTPD = os.environ.get("MGPT_LIGHTTPD") or "/usr/sbin/lighttpd"
TEMPLATE = "/etc/lighttpd/lighttpd.conf"

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")

# Native Webseiten behalten ihren HTTP-Ursprung, damit CSS, Module und fetch
# von derselben Adresse zusammen mit CORP same-origin funktionieren.
CSP = (
    "default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob:; "
    "connect-src 'self'; form-action 'none'; base-uri 'none'"
)

LEGACY_CSP = "sandbox allow-scripts allow-forms allow-modals allow-popups; " + CSP

MIME = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".map": "application/json",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".xml": "application/xml",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".ico": "image/x-icon",
    ".wasm": "application/wasm",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
}


def _q(text: str) -> str:
    """Zeichenkette für die lighttpd-Konfiguration (Anführungszeichen/Backslash sperren)."""
    if any(c in text for c in '"\\\n\r\x00'):
        raise ValueError("Ungültiges Zeichen in der Konfiguration.")
    return f'"{text}"'


def build_config(
    root: str,
    token: str | None,
    *,
    bind: str = "0.0.0.0",
    port: int = 8080,
    tls_port: int | None = 8443,
    cert: str | None = None,
    key: str | None = None,
) -> str:
    """lighttpd-Konfiguration (reiner Text, ohne Seiteneffekte)."""
    if token is not None and not TOKEN_RE.match(token):
        raise ValueError("Token ungültig.")
    mime = ",\n".join(f"  {_q(ext)} => {_q(ctype)}" for ext, ctype in sorted(MIME.items()))
    headers = {
        "Content-Security-Policy": CSP,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
        "Cross-Origin-Resource-Policy": "same-origin",
    }
    header_lines = ",\n".join(f"  {_q(k)} => {_q(v)}" for k, v in headers.items())
    modules = ["mod_access", "mod_alias", "mod_setenv"]
    if tls_port is not None:
        modules.append("mod_openssl")
    lines = [
        "# Von mgpt-serve erzeugt (nur statische Dateien).",
        f"server.modules = ( {', '.join(_q(m) for m in modules)} )",
        f"server.document-root = {_q(root)}",
        f"server.bind = {_q(bind)}",
        f"server.port = {int(port)}",
        'server.tag = "mgpt-serve"',
        'server.follow-symlink = "disable"',
        'server.errorlog = "/dev/stderr"',
        "server.max-fds = 256",
        'index-file.names = ( "index.html", "index.htm" )',
        f"mimetype.assign = (\n{mime}\n)",
        "# Nur lesende Anfragen; .git bleibt gesperrt.",
        '$HTTP["request-method"] !~ "^(GET|HEAD)$" {',
        '  url.access-deny = ( "" )',
        "}",
        r'$HTTP["url"] =~ "(^|/)\.git(/|$)" {',
        '  url.access-deny = ( "" )',
        "}",
        f"setenv.add-response-header = (\n{header_lines}\n)",
    ]
    if token is not None:  # zusätzlich unter /<token>/ (Vorschau über MultiGPT)
        lines.append(f"alias.url = ( {_q('/' + token + '/')} => {_q(root.rstrip('/') + '/')} )")
    if tls_port is not None:
        if not cert or not key:
            raise ValueError("Zertifikat und Schlüssel fehlen.")
        lines += [
            f'$SERVER["socket"] == {_q(f"{bind}:{int(tls_port)}")} {{',
            '  ssl.engine = "enable"',
            f"  ssl.privkey = {_q(key)}",
            f"  ssl.pemfile = {_q(cert)}",
            "}",
        ]
    return "\n".join(lines) + "\n"


def config_from_template(root: str, token: str | None, **options) -> str:
    """Image-Vorlage übernehmen und die Werte der Sitzung einsetzen."""
    generated = build_config(root, token, **options)
    try:
        with open(TEMPLATE, encoding="utf-8") as fh:
            config = fh.read()
    except FileNotFoundError:
        return generated
    config = config.replace(_q(LEGACY_CSP), _q(CSP))
    replacements = {
        "server.document-root": _q(root),
        "server.bind": _q(options["bind"]),
        "server.port": str(options["port"]),
    }
    for name, value in replacements.items():
        config, count = re.subn(
            rf"^{re.escape(name)}[ \t]*=.*$",
            lambda _, name=name, value=value: f"{name} = {value}",
            config,
            count=1,
            flags=re.MULTILINE,
        )
        if count != 1:
            raise ValueError(f"Image-Vorlage enthält {name} nicht.")
    if token is not None:
        config += f"\nalias.url = ( {_q('/' + token + '/')} => {_q(root.rstrip('/') + '/')} )\n"
    if options.get("tls_port") is not None:
        config += '\nserver.modules += ( "mod_openssl" )\n'
        config += generated[generated.index('$SERVER["socket"]') :]
    return config


def make_certificate(directory: str) -> tuple[str, str]:
    """Selbst signiertes Wegwerf-Zertifikat im tmpfs (nie im Arbeitsstand)."""
    cert = os.path.join(directory, "cert.pem")
    key = os.path.join(directory, "key.pem")
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-nodes",
            "-days",
            "2",
            "-subj",
            "/CN=multigpt-runner",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
            "-keyout",
            key,
            "-out",
            cert,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )
    os.chmod(key, 0o600)
    return cert, key


def _replace_running() -> None:
    """Eine laufende Vorschau (anderes Verzeichnis) beenden und sich eintragen."""
    try:
        with open(PIDFILE) as fh:
            pid = int(fh.read().strip())
        if pid != os.getpid():
            os.kill(pid, signal.SIGTERM)
            for _ in range(50):
                os.kill(pid, 0)
                time.sleep(0.05)
    except (OSError, ValueError):
        pass
    with open(PIDFILE, "w") as fh:
        fh.write(str(os.getpid()))


def check_ready(token: str, port: int) -> int:
    """Erfolg erst melden, wenn der gestartete Server HTTP beantwortet."""
    for _ in range(50):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.2)
        try:
            connection.request("HEAD", "/")
            response = connection.getresponse()
            if response.status < 500:
                return 0
        except (OSError, http.client.HTTPException):
            pass
        finally:
            connection.close()
        time.sleep(0.2)
    print("Webserver wurde nicht erreichbar; Start und lighttpd.conf prüfen.", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mgpt-serve")
    parser.add_argument("--token", help="Pfad-Token; ohne Angabe nur unter / erreichbar")
    parser.add_argument("--print-config", action="store_true", help="Konfiguration ausgeben")
    parser.add_argument("--check-ready", action="store_true")
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--config", default="lighttpd.conf", help="Konfiguration unter /workspace")
    parser.add_argument("--dir", default=".")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--tls-port", type=int, default=8443)
    parser.add_argument("--no-tls", action="store_true")
    parser.add_argument("--bind", default="0.0.0.0")
    args = parser.parse_args(argv)
    if args.token is not None and not TOKEN_RE.match(args.token):
        print("Token ungültig.", file=sys.stderr)
        return 2
    directory = args.dir
    if directory == "/workspace" or directory.startswith("/workspace/"):
        directory = directory[len("/workspace") :].lstrip("/") or "."
    root = os.path.realpath(os.path.join(WORKSPACE, directory))
    if not (root == WORKSPACE or root.startswith(WORKSPACE + os.sep)) or not os.path.isdir(root):
        print("Verzeichnis liegt nicht unter /workspace.", file=sys.stderr)
        return 2
    if args.print_config:
        sys.stdout.write(
            build_config(root, args.token, bind=args.bind, port=args.port, tls_port=None)
        )
        return 0
    if args.check_ready:
        return check_ready(args.token or "", args.port)
    os.makedirs(RUN_DIR, mode=0o700, exist_ok=True)
    port_note = f"http :{args.port}" + ("" if args.no_tls else f", https :{args.tls_port}")
    print(f"mgpt-serve: {root} ({port_note})", file=sys.stderr)
    cert = key = None
    tls_port = None if args.no_tls else args.tls_port
    if tls_port is not None:
        try:
            cert, key = make_certificate(RUN_DIR)
        except (OSError, subprocess.SubprocessError):
            print("Zertifikat konnte nicht erzeugt werden; nur http.", file=sys.stderr)
            tls_port = None
    config = config_from_template(
        root, args.token, bind=args.bind, port=args.port, tls_port=tls_port, cert=cert, key=key
    )
    path = os.path.join(WORKSPACE, args.config)
    if os.path.dirname(os.path.realpath(path)) != WORKSPACE or os.path.islink(path):
        print("Konfiguration muss direkt unter /workspace liegen.", file=sys.stderr)
        return 2
    # Eine vom Modell angepasste Kopie der Image-Vorlage bleibt erhalten.
    if not os.path.exists(path):
        with open(path, "x", encoding="utf-8") as fh:
            fh.write(config)
    else:
        # Nur den früher von uns erzeugten, widersprüchlichen Header migrieren.
        with open(path, encoding="utf-8") as fh:
            existing = fh.read()
        migrated = existing.replace(_q(LEGACY_CSP), _q(CSP))
        if migrated != existing:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(migrated)
    subprocess.run([LIGHTTPD, "-tt", "-f", path], check=True, timeout=10)
    if args.prepare:
        return 0
    _replace_running()
    os.execv(LIGHTTPD, ["lighttpd", "-D", "-f", path])
    return 0  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
