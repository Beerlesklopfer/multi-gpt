#!/usr/bin/python3 -I
"""mgpt-fs: Datei- und git-Hilfsprogramm im Arbeitscontainer (M14-09).

Liegt im Arbeits-Image als ``/usr/local/bin/mgpt-fs`` (nur Standardbibliothek).
Der Runner-Agent ruft es per ``exec`` auf; das Modell sieht es nicht direkt.

**Aufruf:** ``mgpt-fs`` liest von stdin eine Zeile JSON ``{"op": …, …}``. Bei
``bundle_in`` folgen danach genau ``size`` Byte Rohdaten. Die Antwort ist eine
Zeile JSON auf stdout (``{"ok": true, …}`` bzw. ``{"ok": false, "error": …}``);
bei ``bundle_out`` folgen ihr ``size`` Byte Rohdaten. Der Exit-Code ist immer 0,
außer bei kaputter Eingabe (2).

**Pfade:** nur unter ``/workspace`` (bzw. ``MGPT_WORKSPACE`` im Test). Relative
Pfade gelten ab dort, absolute müssen darunter liegen. Symlinks werden aufgelöst
und dürfen nicht hinausführen; die letzte Komponente wird mit ``O_NOFOLLOW``
geöffnet. ``.git`` ist für Schreibzugriffe gesperrt.
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import os
import re
import stat
import subprocess
import sys

ROOT = os.path.realpath(os.environ.get("MGPT_WORKSPACE") or "/workspace")
SCRATCH = os.path.realpath(os.environ.get("MGPT_SCRATCH") or "/scratch")
TMP = os.environ.get("MGPT_TMP") or "/tmp"
MAX_READ_BYTES = 2 * 1024 * 1024  # größere Dateien liest ``read`` nur teilweise
MAX_LINE = 2000  # Zeichen je Zeile in ``read``/``grep``
MAX_FILE_BYTES = 16 * 1024 * 1024  # Obergrenze für write/edit/fetch
MAX_RESULTS = 1000
SKIP_DIRS = {".git"}
GIT_IDENTITY = ["-c", "user.name=MultiGPT Runner", "-c", "user.email=runner@multigpt.invalid"]
GIT_SAFE = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]

MIME = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".wasm": "application/wasm",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
}


class FsError(Exception):
    pass


# --- Pfade -------------------------------------------------------------------


def _inside(path: str, root: str = ROOT) -> bool:
    return path == root or path.startswith(root + os.sep)


def resolve(path: str | None, *, for_write: bool = False) -> str:
    """Pfad unter ``/workspace`` (bzw. ``/scratch``) auflösen.

    Relative Pfade gelten ab ``/workspace``; ``/scratch/…`` meint das Scratchpad
    der Sitzung. Symlinks dürfen nicht aus der jeweiligen Wurzel hinausführen.
    """
    if not isinstance(path, str) or not path.strip():
        path = "."
    if "\x00" in path:
        raise FsError("Ungültiger Pfad.")
    root, label = ROOT, "/workspace"
    if path == "/scratch" or path.startswith("/scratch/"):
        if not os.path.isdir(SCRATCH):
            raise FsError("Diese Sitzung hat kein Scratchpad.")
        root, label = SCRATCH, "/scratch"
        path = os.path.relpath(path, "/scratch")
    elif path.startswith("/workspace/") or path == "/workspace":
        path = os.path.relpath(path, "/workspace")
    if os.path.isabs(path):
        raise FsError("Pfad liegt nicht unter /workspace oder /scratch.")
    joined = os.path.normpath(os.path.join(root, path))
    if not _inside(joined, root):
        raise FsError(f"Pfad liegt nicht unter {label}.")
    real = os.path.realpath(joined)
    if not _inside(real, root):
        raise FsError(f"Pfad führt aus {label} hinaus (Symlink).")
    if for_write and root == ROOT:
        rel = os.path.relpath(real, ROOT)
        if rel.split(os.sep)[0] in SKIP_DIRS:
            raise FsError("Das Verzeichnis .git ist gesperrt.")
    return real


def _root_of(path: str) -> str:
    return SCRATCH if _inside(path, SCRATCH) else ROOT


def display(path: str) -> str:
    if _inside(path, SCRATCH):
        rel = os.path.relpath(path, SCRATCH)
        return "/scratch" if rel == "." else "/scratch/" + rel
    rel = os.path.relpath(path, ROOT)
    return "." if rel == "." else rel


def _open_read(path: str) -> int:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise FsError("Keine reguläre Datei.")
    return fd


def _read_bytes(path: str, limit: int) -> tuple[bytes, int]:
    try:
        fd = _open_read(path)
    except FileNotFoundError:
        raise FsError(f"Datei nicht gefunden: {display(path)}") from None
    except OSError as exc:
        raise FsError(f"Datei nicht lesbar: {display(path)} ({exc.strerror})") from None
    with os.fdopen(fd, "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        return fh.read(limit), size


def _write_bytes(path: str, data: bytes) -> bool:
    """Datei schreiben (anlegen oder ersetzen); liefert True, wenn neu."""
    parent = os.path.dirname(path)
    root = _root_of(path)
    rel_parent = os.path.relpath(parent, root)
    if rel_parent != ".":
        current = root
        for part in rel_parent.split(os.sep):
            current = os.path.join(current, part)
            if os.path.islink(current):
                raise FsError("Pfad führt über einen Symlink.")
            if not os.path.exists(current):
                os.mkdir(current, 0o755)
            elif not os.path.isdir(current):
                raise FsError(f"Kein Verzeichnis: {display(current)}")
    created = not os.path.lexists(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(path, flags, 0o644)
    except OSError as exc:
        raise FsError(f"Datei nicht schreibbar: {display(path)} ({exc.strerror})") from None
    with os.fdopen(fd, "wb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise FsError("Keine reguläre Datei.")
        fh.write(data)
    return created


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def _walk(base: str):
    """Alle Dateien unter base (ohne .git, ohne Symlinks auf Verzeichnisse)."""
    if os.path.isfile(base):
        yield base
        return
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            yield os.path.join(dirpath, name)


# --- Dateiwerkzeuge ------------------------------------------------------------


def op_read(req: dict) -> dict:
    path = resolve(req.get("path"))
    data, size = _read_bytes(path, MAX_READ_BYTES)
    if _is_binary(data):
        raise FsError(f"Binärdatei ({size} Byte): {display(path)}")
    lines = data.decode("utf-8", errors="replace").splitlines()
    offset = max(int(req.get("offset") or 1), 1)
    limit = max(int(req.get("limit") or 2000), 1)
    chosen = lines[offset - 1 : offset - 1 + limit]
    out = []
    for number, line in enumerate(chosen, start=offset):
        if len(line) > MAX_LINE:
            line = line[:MAX_LINE] + " …"
        out.append(f"{number:6}\t{line}")
    return {
        "content": "\n".join(out),
        "lines": len(lines),
        "from": offset,
        "to": offset + len(chosen) - 1,
        "truncated": size > len(data),
    }


def _du(base: str) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(base, followlinks=False):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_blocks * 512
            except OSError:
                pass
    return total


def _check_scratch(req: dict, path: str, new_size: int) -> None:
    """Lokales Scratchpad (Bind-Mount ohne Quota): Grenze vor dem Schreiben prüfen."""
    limit = req.get("scratch_limit")
    if not limit or not _inside(path, SCRATCH):
        return
    try:
        old = os.lstat(path).st_blocks * 512
    except OSError:
        old = 0
    if _du(SCRATCH) - old + new_size > int(limit):
        raise FsError(f"Scratchpad voll (Grenze {int(limit) >> 20} MB).")


def op_write(req: dict) -> dict:
    content = req.get("content")
    if not isinstance(content, str):
        raise FsError("content fehlt.")
    data = content.encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise FsError("Inhalt zu groß.")
    path = resolve(req.get("path"), for_write=True)
    _check_scratch(req, path, len(data))
    if os.path.isdir(path):
        raise FsError("Pfad ist ein Verzeichnis.")
    created = _write_bytes(path, data)
    return {"path": display(path), "bytes": len(data), "created": created}


def op_edit(req: dict) -> dict:
    old, new = req.get("old_string"), req.get("new_string")
    if not isinstance(old, str) or not isinstance(new, str):
        raise FsError("old_string und new_string fehlen.")
    if old == "":
        raise FsError("old_string darf nicht leer sein.")
    if old == new:
        raise FsError("old_string und new_string sind gleich.")
    path = resolve(req.get("path"), for_write=True)
    data, size = _read_bytes(path, MAX_FILE_BYTES + 1)
    if size > MAX_FILE_BYTES or _is_binary(data):
        raise FsError("Datei zu groß oder binär.")
    text = data.decode("utf-8")
    count = text.count(old)
    if count == 0:
        raise FsError("old_string kommt in der Datei nicht vor.")
    if count > 1 and not req.get("replace_all"):
        raise FsError(
            f"old_string kommt {count}-mal vor; mehr Kontext angeben oder replace_all setzen."
        )
    text = text.replace(old, new) if req.get("replace_all") else text.replace(old, new, 1)
    _check_scratch(req, path, len(text.encode("utf-8")))
    _write_bytes(path, text.encode("utf-8"))
    return {"path": display(path), "replacements": count if req.get("replace_all") else 1}


def _glob_regex(pattern: str) -> re.Pattern:
    """Glob mit ``**`` (beliebig viele Verzeichnisse) in einen Regex übersetzen."""
    out, i = [], 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif ch == "*":
            out.append("[^/]*")
            i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        elif ch == "[":
            end = pattern.find("]", i + 1)
            if end == -1:
                out.append(re.escape(ch))
                i += 1
            else:
                body = pattern[i + 1 : end].replace("\\", "\\\\")
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = end + 1
        else:
            out.append(re.escape(ch))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def op_glob(req: dict) -> dict:
    pattern = req.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise FsError("pattern fehlt.")
    base = resolve(req.get("path"))
    regex = _glob_regex(pattern.lstrip("/"))
    found = []
    for path in _walk(base):
        rel = os.path.relpath(path, base)
        if regex.match(rel):
            try:
                found.append((os.lstat(path).st_mtime, display(path)))
            except OSError:
                continue
    found.sort(key=lambda item: (-item[0], item[1]))
    paths = [p for _, p in found]
    return {"paths": paths[:MAX_RESULTS], "total": len(paths)}


def op_grep(req: dict) -> dict:
    pattern = req.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise FsError("pattern fehlt.")
    try:
        regex = re.compile(pattern, re.IGNORECASE if req.get("ignore_case") else 0)
    except re.error as exc:
        raise FsError(f"Ungültiger regulärer Ausdruck: {exc}") from None
    base = resolve(req.get("path"))
    file_glob = req.get("glob") or None
    limit = min(max(int(req.get("max_results") or 200), 1), MAX_RESULTS)
    matches, total = [], 0
    for path in _walk(base):
        if file_glob and not fnmatch.fnmatch(os.path.basename(path), file_glob):
            continue
        if os.path.islink(path):
            continue
        try:
            data, size = _read_bytes(path, MAX_READ_BYTES)
        except FsError:
            continue
        if _is_binary(data):
            continue
        for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
            if regex.search(line):
                total += 1
                if len(matches) < limit:
                    if len(line) > MAX_LINE:
                        line = line[:MAX_LINE] + " …"
                    matches.append(f"{display(path)}:{number}:{line}")
    return {"matches": matches, "total": total}


def op_fetch(req: dict) -> dict:
    """Datei für die Vorschau (8p): nur reguläre Dateien unter ``dir``, keine Listen."""
    base = resolve(req.get("dir"))
    raw = str(req.get("path") or "").split("?", 1)[0].split("#", 1)[0]
    rel = raw.lstrip("/")
    if rel == "" or rel.endswith("/"):
        rel += "index.html"
    try:
        path = resolve(os.path.join(os.path.relpath(base, ROOT), rel))
    except FsError:
        return {"status": 404}
    if path != base and not path.startswith(base + os.sep):
        return {"status": 404}
    if os.path.isdir(path):
        path = os.path.join(path, "index.html")
    if display(path).split(os.sep)[0] in SKIP_DIRS:
        return {"status": 404}
    try:
        data, size = _read_bytes(path, MAX_FILE_BYTES + 1)
    except FsError:
        return {"status": 404}
    if size > MAX_FILE_BYTES:
        return {"status": 413}
    ext = os.path.splitext(path)[1].lower()
    return {
        "status": 200,
        "content_type": MIME.get(ext, "application/octet-stream"),
        "data": base64.b64encode(data).decode("ascii"),
    }


def op_usage(req: dict) -> dict:
    total = 0
    base = SCRATCH if req.get("scratch") else ROOT
    if not os.path.isdir(base):
        return {"bytes": 0}
    for dirpath, _dirnames, filenames in os.walk(base, followlinks=False):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return {"bytes": total}


def op_file_out(req: dict) -> tuple[dict, bytes]:
    """Datei für ``publish``: Kopf (Name, Größe, Prüfsumme, MIME) und Rohdaten."""
    path = resolve(req.get("path"))
    limit = int(req.get("max_bytes") or MAX_FILE_BYTES)
    data, size = _read_bytes(path, limit + 1)
    if size > limit or len(data) > limit:
        raise FsError(f"Datei größer als {limit} Byte.")
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    mime = MIME.get(ext, "application/octet-stream").split(";")[0]
    head = {"name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    return {**head, "mime": mime, "path": display(path)}, data


# --- git ---------------------------------------------------------------------


def _git(*args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *GIT_SAFE, *args],
        cwd=ROOT,
        capture_output=capture,
        stdin=subprocess.DEVNULL,
        timeout=300,
    )
    if check and proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()[-500:]
        raise FsError(f"git {args[0]} fehlgeschlagen: {err}")
    return proc


def _head() -> str | None:
    proc = _git("rev-parse", "--verify", "-q", "HEAD", check=False)
    head = proc.stdout.decode().strip()
    return head or None


def op_init(req: dict) -> dict:
    if os.listdir(ROOT):
        raise FsError("/workspace ist nicht leer.")
    _git("init", "-q", "-b", "main", ".")
    return {"head": None}


def op_bundle_in(req: dict, payload: bytes) -> dict:
    if os.listdir(ROOT):
        raise FsError("/workspace ist nicht leer.")
    path = os.path.join(TMP, "mgpt-in.bundle")
    with open(path, "wb") as fh:
        fh.write(payload)
    try:
        # clone prüft das Bundle selbst (verify braucht ein Repository).
        _git("clone", "-q", "--no-hardlinks", path, ".")
        # Herkunft vergessen: der Arbeitsstand kennt keinen entfernten Rechner.
        _git("remote", "remove", "origin", check=False)
    finally:
        os.unlink(path)
    return {"head": _head()}


def op_commit(req: dict) -> dict:
    message = str(req.get("message") or "Änderung durch MultiGPT")[:500]
    _git("add", "-A")
    status = _git("status", "--porcelain").stdout
    if not status.strip():
        return {"changed": False, "head": _head()}
    _git(*GIT_IDENTITY, "commit", "-q", "--no-verify", "-m", message)
    return {"changed": True, "head": _head()}


def op_bundle_out(req: dict) -> tuple[dict, bytes]:
    head = _head()
    if head is None:
        return {"size": 0, "head": None, "sha256": hashlib.sha256(b"").hexdigest()}, b""
    path = os.path.join(TMP, "mgpt-out.bundle")
    try:
        _git("bundle", "create", "-q", path, "--all")
        with open(path, "rb") as fh:
            data = fh.read()
    finally:
        if os.path.exists(path):
            os.unlink(path)
    return {"size": len(data), "head": head, "sha256": hashlib.sha256(data).hexdigest()}, data


OPS = {
    "read": op_read,
    "write": op_write,
    "edit": op_edit,
    "glob": op_glob,
    "grep": op_grep,
    "fetch": op_fetch,
    "usage": op_usage,
    "init": op_init,
    "commit": op_commit,
}


def _reply(obj: dict, payload: bytes = b"") -> None:
    out = sys.stdout.buffer
    out.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
    if payload:
        out.write(payload)
    out.flush()


def _read_exact(stream, size: int) -> bytes:
    parts, remaining = [], size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise FsError("Eingabe zu kurz.")
        parts.append(chunk)
        remaining -= len(chunk)
    return b"".join(parts)


def main() -> int:
    stdin = sys.stdin.buffer
    line = stdin.readline(MAX_FILE_BYTES * 2)
    try:
        req = json.loads(line)
        if not isinstance(req, dict):
            raise ValueError
    except ValueError:
        _reply({"ok": False, "error": "Ungültige Anfrage."})
        return 2
    op = req.get("op")
    try:
        if op == "bundle_in":
            size = int(req.get("size") or 0)
            result = op_bundle_in(req, _read_exact(stdin, size))
            _reply({"ok": True, **result})
        elif op in ("bundle_out", "file_out"):
            result, data = op_bundle_out(req) if op == "bundle_out" else op_file_out(req)
            _reply({"ok": True, **result}, data)
        elif op in OPS:
            _reply({"ok": True, **OPS[op](req)})
        else:
            _reply({"ok": False, "error": f"Unbekannte Operation: {op}"})
    except FsError as exc:
        _reply({"ok": False, "error": str(exc)})
    except (OSError, UnicodeDecodeError, subprocess.SubprocessError) as exc:
        _reply({"ok": False, "error": f"Fehler: {exc.__class__.__name__}: {exc}"[:500]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
