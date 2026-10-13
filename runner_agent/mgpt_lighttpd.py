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
  Ports 80/443 nicht binden; der Agent veröffentlicht sie als 10080/10443.
- Nur GET und HEAD, keine Symlinks, ``.git`` gesperrt, strenge Kopfzeilen und CSP
  mit ``sandbox`` (eigener Ursprung).
- Eine laufende Vorschau (anderes Verzeichnis) wird beendet und ersetzt: lighttpd
  ersetzt diesen Prozess per ``exec``, die PID bleibt gleich und steht in der PID-Datei.
"""

from __future__ import annotations

import argparse
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

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")

CSP = (
    "sandbox allow-scripts allow-forms allow-modals allow-popups; "
    "default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob:; "
    "connect-src 'self'; form-action 'none'; base-uri 'none'"
)

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
    token: str,
    *,
    bind: str = "0.0.0.0",
    port: int = 8080,
    tls_port: int | None = 8443,
    cert: str | None = None,
    key: str | None = None,
) -> str:
    """lighttpd-Konfiguration (reiner Text, ohne Seiteneffekte)."""
    if not TOKEN_RE.match(token):
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
        f"alias.url = ( {_q('/' + token + '/')} => {_q(root.rstrip('/') + '/')} )",
        f"setenv.add-response-header = (\n{header_lines}\n)",
    ]
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mgpt-serve")
    parser.add_argument("--token", required=True)
    parser.add_argument("--dir", default=".")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--tls-port", type=int, default=8443)
    parser.add_argument("--no-tls", action="store_true")
    parser.add_argument("--bind", default="0.0.0.0")
    args = parser.parse_args(argv)
    if not TOKEN_RE.match(args.token):
        print("Token ungültig.", file=sys.stderr)
        return 2
    root = os.path.realpath(os.path.join(WORKSPACE, args.dir.lstrip("/")))
    if not (root == WORKSPACE or root.startswith(WORKSPACE + os.sep)) or not os.path.isdir(root):
        print("Verzeichnis liegt nicht unter /workspace.", file=sys.stderr)
        return 2
    _replace_running()
    os.makedirs(RUN_DIR, mode=0o700, exist_ok=True)
    cert = key = None
    tls_port = None if args.no_tls else args.tls_port
    if tls_port is not None:
        try:
            cert, key = make_certificate(RUN_DIR)
        except (OSError, subprocess.SubprocessError):
            print("Zertifikat konnte nicht erzeugt werden; nur http.", file=sys.stderr)
            tls_port = None
    config = build_config(
        root, args.token, bind=args.bind, port=args.port, tls_port=tls_port, cert=cert, key=key
    )
    path = os.path.join(RUN_DIR, "lighttpd.conf")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(config)
    os.execv(LIGHTTPD, ["lighttpd", "-D", "-f", path])
    return 0  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
