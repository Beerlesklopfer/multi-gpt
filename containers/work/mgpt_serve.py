#!/usr/bin/python3 -I
"""mgpt-serve: Vorschau-Server im Arbeitscontainer (Entscheidungen 8p, 8x).

Liegt im Arbeits-Image als ``/usr/local/bin/mgpt-serve`` (nur Standardbibliothek).
Aufruf durch den Agent: ``mgpt-serve --token T [--dir SUB] [--port 8080]``.

- Nur statische Dateien unter ``/workspace/SUB``; keine Templates, keine
  serverseitige Ausführung, keine Verzeichnislisten (Verzeichnis → index.html).
- Jeder Pfad muss mit ``/<token>/`` beginnen: Jede Webseite im Browser kann
  ``localhost`` abrufen, das zufällige Sitzungs-Token verhindert das Erraten.
- Symlinks dürfen nicht aus dem Verzeichnis hinausführen (``O_NOFOLLOW``).
- Strenge CSP mit ``sandbox`` (eigener Ursprung, kein Zugriff auf andere Seiten).
"""

from __future__ import annotations

import argparse
import os
import signal
import stat
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

WORKSPACE = os.path.realpath(os.environ.get("MGPT_WORKSPACE") or "/workspace")
PIDFILE = os.path.join(os.environ.get("MGPT_TMP") or "/tmp", "mgpt-serve.pid")
MAX_FILE_BYTES = 16 * 1024 * 1024

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
    ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".wasm": "application/wasm",
    ".woff2": "font/woff2",
}


def resolve_file(root: str, rel: str) -> str | None:
    """Datei unter root zu rel finden; None, wenn unzulässig oder nicht vorhanden."""
    if "\x00" in rel:
        return None
    rel = rel.lstrip("/")
    if rel == "" or rel.endswith("/"):
        rel += "index.html"
    joined = os.path.normpath(os.path.join(root, rel))
    if not (joined == root or joined.startswith(root + os.sep)):
        return None
    real = os.path.realpath(joined)
    if os.path.isdir(real):
        real = os.path.join(real, "index.html")
    if not real.startswith(root + os.sep):
        return None
    if os.path.relpath(real, WORKSPACE).split(os.sep)[0] == ".git":
        return None
    return real


class Handler(BaseHTTPRequestHandler):
    server_version = "mgpt-serve"
    sys_version = ""
    root = WORKSPACE
    prefix = "/"

    def log_message(self, format, *args):  # keine Zugriffsprotokolle
        pass

    def _headers(self, status: int, ctype: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.end_headers()

    def _error(self, status: int, head: bool) -> None:
        body = f"{status}\n".encode()
        self._headers(status, "text/plain; charset=utf-8", len(body))
        if not head:
            self.wfile.write(body)

    def _serve(self, head: bool) -> None:
        path = unquote(urlsplit(self.path).path)
        if not path.startswith(self.prefix):
            return self._error(404, head)
        target = resolve_file(self.root, path[len(self.prefix) :])
        if target is None:
            return self._error(404, head)
        try:
            fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            return self._error(404, head)
        with os.fdopen(fd, "rb") as fh:
            info = os.fstat(fh.fileno())
            if not stat.S_ISREG(info.st_mode):
                return self._error(404, head)
            if info.st_size > MAX_FILE_BYTES:
                return self._error(413, head)
            data = fh.read(MAX_FILE_BYTES)
        ctype = MIME.get(os.path.splitext(target)[1].lower(), "application/octet-stream")
        self._headers(200, ctype, len(data))
        if not head:
            self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        self._serve(False)

    def do_HEAD(self):  # noqa: N802
        self._serve(True)

    def _forbidden(self):
        self._error(405, False)

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _forbidden


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
    parser.add_argument("--check-ready", action="store_true")
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--config", default="lighttpd.conf")
    parser.add_argument("--dir", default=".")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--bind", default="0.0.0.0")
    args = parser.parse_args(argv)
    if not args.token.replace("-", "").replace("_", "").isalnum() or len(args.token) < 16:
        print("Token ungültig.", file=sys.stderr)
        return 2
    directory = args.dir
    if directory == "/workspace" or directory.startswith("/workspace/"):
        directory = directory[len("/workspace") :].lstrip("/") or "."
    root = os.path.realpath(os.path.join(WORKSPACE, directory))
    if not (root == WORKSPACE or root.startswith(WORKSPACE + os.sep)) or not os.path.isdir(root):
        print("Verzeichnis liegt nicht unter /workspace.", file=sys.stderr)
        return 2
    if args.check_ready or args.prepare:
        return 0  # FakeEngine zeichnet Hintergrunddienste nur auf.
    _replace_running()
    Handler.root = root
    Handler.prefix = f"/{args.token}/"
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    server.daemon_threads = True
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
