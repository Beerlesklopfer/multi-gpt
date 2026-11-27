"""Hilfsprogramme des Arbeits-Images (``containers/work/mgpt_*.py``): fs, serve, relay.

Ohne Datenbank und ohne Container: Die Programme laufen als lokale Prozesse bzw.
im Thread mit eigenem Arbeitsverzeichnis. Der Rust-Agent ruft sie im Container
auf (``mgpt-fs`` auch in seinen Tests, ``MGPT_FS_PY``).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

WORK_DIR = Path(__file__).resolve().parent.parent / "containers" / "work"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"mgpt_work_{name}", WORK_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mgpt_serve = _load("mgpt_serve")
mgpt_relay = _load("mgpt_relay")


# --- mgpt-fs -----------------------------------------------------------------------------


def run_fs(root: Path, request: dict, payload: bytes = b"") -> tuple[dict, bytes]:
    tmp = root.parent / (root.name + "-tmp")
    tmp.mkdir(exist_ok=True)
    env = {
        "PATH": os.environ["PATH"],
        "MGPT_WORKSPACE": str(root),
        "MGPT_TMP": str(tmp),
        "HOME": str(tmp),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "MGPT_SCRATCH": os.environ.get("MGPT_SCRATCH", str(root.parent / "kein-scratch")),
        "MGPT_DATA": os.environ.get("MGPT_DATA", str(root.parent / "kein-data")),
    }
    proc = subprocess.run(
        [sys.executable, str(WORK_DIR / "mgpt_fs.py")],
        input=json.dumps(request).encode() + b"\n" + payload,
        capture_output=True,
        env=env,
        cwd=root,
        timeout=60,
    )
    line, _, rest = proc.stdout.partition(b"\n")
    return json.loads(line), rest


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    return root


def test_fs_write_read_edit(ws):
    res, _ = run_fs(ws, {"op": "write", "path": "src/app.py", "content": "a = 1\nb = 2\n"})
    assert res == {"ok": True, "path": "src/app.py", "bytes": 12, "created": True}
    res, _ = run_fs(ws, {"op": "read", "path": "/workspace/src/app.py"})
    assert res["content"] == "     1\ta = 1\n     2\tb = 2"
    res, _ = run_fs(ws, {"op": "read", "path": "src/app.py", "offset": 2, "limit": 1})
    assert res["content"] == "     2\tb = 2" and res["lines"] == 2
    res, _ = run_fs(
        ws, {"op": "edit", "path": "src/app.py", "old_string": "b = 2", "new_string": "b = 3"}
    )
    assert res["ok"] and (ws / "src/app.py").read_text() == "a = 1\nb = 3\n"
    run_fs(ws, {"op": "write", "path": "dup.txt", "content": "x x"})
    res, _ = run_fs(ws, {"op": "edit", "path": "dup.txt", "old_string": "x", "new_string": "y"})
    assert not res["ok"] and "2-mal" in res["error"]
    res, _ = run_fs(
        ws,
        {
            "op": "edit",
            "path": "dup.txt",
            "old_string": "x",
            "new_string": "y",
            "replace_all": True,
        },
    )
    assert res["replacements"] == 2 and (ws / "dup.txt").read_text() == "y y"


@pytest.mark.parametrize(
    "path", ["/etc/passwd", "../outside.txt", "/workspace/../etc/x", "a/../../b", "/tmp/x"]
)
def test_fs_rejects_paths_outside_workspace(ws, path):
    res, _ = run_fs(ws, {"op": "write", "path": path, "content": "x"})
    assert res["ok"] is False and "/workspace" in res["error"]
    res, _ = run_fs(ws, {"op": "read", "path": path})
    assert res["ok"] is False


def test_fs_rejects_symlinks_leading_out(ws, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("geheim")
    (ws / "link").symlink_to(secret)
    (ws / "dirlink").symlink_to(tmp_path)
    for request in (
        {"op": "read", "path": "link"},
        {"op": "write", "path": "link", "content": "x"},
        {"op": "write", "path": "dirlink/new.txt", "content": "x"},
        {"op": "edit", "path": "link", "old_string": "geheim", "new_string": "x"},
        {"op": "grep", "pattern": "geheim", "path": "dirlink"},
    ):
        res, _ = run_fs(ws, request)
        assert res["ok"] is False, request
        assert "Symlink" in res["error"]
    assert secret.read_text() == "geheim"
    res, _ = run_fs(ws, {"op": "grep", "pattern": "geheim"})
    assert res["matches"] == []
    # Symlink innerhalb bleibt erlaubt
    (ws / "a.txt").write_text("inhalt")
    (ws / "b.txt").symlink_to(ws / "a.txt")
    res, _ = run_fs(ws, {"op": "read", "path": "b.txt"})
    assert res["ok"] and "inhalt" in res["content"]


def test_fs_git_dir_is_write_protected(ws):
    run_fs(ws, {"op": "init"})
    res, _ = run_fs(ws, {"op": "write", "path": ".git/config", "content": "[core]\n"})
    assert res["ok"] is False and ".git" in res["error"]
    res, _ = run_fs(ws, {"op": "write", "path": "./.git/hooks/pre-commit", "content": "x"})
    assert res["ok"] is False


def test_fs_glob_grep_and_binary(ws):
    run_fs(ws, {"op": "write", "path": "a.py", "content": "print('Hallo')\n"})
    run_fs(ws, {"op": "write", "path": "pkg/b.py", "content": "x = 'hallo welt'\n"})
    run_fs(ws, {"op": "write", "path": "pkg/c.js", "content": "// hallo\n"})
    (ws / "bin.dat").write_bytes(b"\x00\x01hallo")
    res, _ = run_fs(ws, {"op": "glob", "pattern": "**/*.py"})
    assert sorted(res["paths"]) == ["a.py", "pkg/b.py"]
    res, _ = run_fs(ws, {"op": "glob", "pattern": "*.py"})
    assert res["paths"] == ["a.py"]
    res, _ = run_fs(ws, {"op": "grep", "pattern": "hallo", "ignore_case": True})
    assert sorted(res["matches"]) == [
        "a.py:1:print('Hallo')",
        "pkg/b.py:1:x = 'hallo welt'",
        "pkg/c.js:1:// hallo",
    ]
    res, _ = run_fs(ws, {"op": "grep", "pattern": "hallo", "glob": "*.js"})
    assert res["matches"] == ["pkg/c.js:1:// hallo"]
    res, _ = run_fs(ws, {"op": "grep", "pattern": "("})
    assert res["ok"] is False
    res, _ = run_fs(ws, {"op": "read", "path": "bin.dat"})
    assert res["ok"] is False and "Binär" in res["error"]


def test_fs_fetch_for_preview(ws, tmp_path):
    run_fs(ws, {"op": "write", "path": "site/index.html", "content": "<h1>Hi</h1>"})
    run_fs(ws, {"op": "write", "path": "site/js/app.js", "content": "1"})
    (ws / "site/out").symlink_to(tmp_path)
    res, _ = run_fs(ws, {"op": "fetch", "dir": "site", "path": "/"})
    assert res["status"] == 200 and res["content_type"].startswith("text/html")
    res, _ = run_fs(ws, {"op": "fetch", "dir": "site", "path": "/js/app.js?v=1"})
    assert res["status"] == 200 and res["content_type"].startswith("text/javascript")
    for path in ("/js/", "/../secret", "/out/x", "/nope.html"):
        res, _ = run_fs(ws, {"op": "fetch", "dir": "site", "path": path})
        assert res["status"] == 404, path


def test_fs_bundle_roundtrip(ws, tmp_path):
    res, _ = run_fs(ws, {"op": "init"})
    assert res == {"ok": True, "head": None}
    res, data = run_fs(ws, {"op": "bundle_out"})
    assert res["size"] == 0 and data == b""
    run_fs(ws, {"op": "write", "path": "main.py", "content": "print(1)\n"})
    res, _ = run_fs(ws, {"op": "commit", "message": "write main.py"})
    assert res["changed"] is True and len(res["head"]) == 40
    head = res["head"]
    res, _ = run_fs(ws, {"op": "commit", "message": "nichts"})
    assert res == {"ok": True, "changed": False, "head": head}
    res, data = run_fs(ws, {"op": "bundle_out"})
    assert res["head"] == head and res["size"] == len(data) > 0
    import hashlib

    assert hashlib.sha256(data).hexdigest() == res["sha256"]
    other = tmp_path / "ws2"
    other.mkdir()
    res, _ = run_fs(other, {"op": "bundle_in", "size": len(data)}, data)
    assert res == {"ok": True, "head": head}
    assert (other / "main.py").read_text() == "print(1)\n"
    res, _ = run_fs(other, {"op": "bundle_in", "size": 5}, b"kaput")
    assert res["ok"] is False


# --- mgpt-serve ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_serve_requires_token_and_lists_nothing(ws, tmp_path, monkeypatch):
    (ws / "site").mkdir()
    (ws / "site/index.html").write_text("<p>ok</p>")
    (ws / "site/sub").mkdir()
    (ws / "site/sub/a.txt").write_text("a")
    (tmp_path / "geheim.txt").write_text("geheim")
    (ws / "site/leak.txt").symlink_to(tmp_path / "geheim.txt")
    monkeypatch.setattr(mgpt_serve, "WORKSPACE", str(ws))
    token = "T" * 24
    handler = type("H", (mgpt_serve.Handler,), {"root": str(ws / "site"), "prefix": f"/{token}/"})
    from http.server import ThreadingHTTPServer

    port = _free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"

    def get(path):
        try:
            with urllib.request.urlopen(base + path, timeout=5) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers

    try:
        status, body, headers = get(f"/{token}/")
        assert status == 200 and body == b"<p>ok</p>"
        assert "sandbox" in headers["Content-Security-Policy"]
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert get(f"/{token}/sub/a.txt")[0] == 200
        assert get("/index.html")[0] == 404
        assert get(f"/{'X' * 24}/index.html")[0] == 404
        assert get(f"/{token}/sub/")[0] == 404  # keine Verzeichnisliste
        assert get(f"/{token}/leak.txt")[0] == 404
        assert get(f"/{token}/../../geheim.txt")[0] == 404
    finally:
        server.shutdown()


# --- mgpt-relay ----------------------------------------------------------------------------


def test_relay_forwards_only_to_listed_target():
    async def scenario():
        async def echo(reader, writer):
            data = await reader.read(100)
            writer.write(b"echo:" + data)
            await writer.drain()
            writer.close()

        target = await asyncio.start_server(echo, "127.0.0.1", 0)
        target_port = target.sockets[0].getsockname()[1]
        base = _free_port()
        relay = asyncio.create_task(
            mgpt_relay.serve(base, [("127.0.0.1", target_port)], "127.0.0.1")
        )
        await asyncio.sleep(0.2)
        reader, writer = await asyncio.open_connection("127.0.0.1", base)
        writer.write(b"hallo")
        await writer.drain()
        answer = await asyncio.wait_for(reader.read(100), 5)
        writer.close()
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", base + 1)
        relay.cancel()
        target.close()
        return answer

    assert asyncio.run(scenario()) == b"echo:hallo"
    with pytest.raises(ValueError):
        mgpt_relay.parse_target("host")
    assert mgpt_relay.parse_target("[fe80::1]:22") == ("fe80::1", 22)


def test_fs_scratch_limit_before_write(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    ws.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("MGPT_SCRATCH", str(scratch))
    env_root = ws
    res, _ = run_fs(env_root, {"op": "write", "path": "/scratch/a", "content": "x" * 5000,
                               "scratch_limit": 4096})  # fmt: skip
    assert res["ok"] is False and "Scratchpad voll" in res["error"]
    res, _ = run_fs(env_root, {"op": "write", "path": "/scratch/a", "content": "x" * 100,
                               "scratch_limit": 1 << 20})  # fmt: skip
    assert res["ok"] is True and res["path"] == "/scratch/a"
    # /workspace zählt nicht zum Scratchpad
    res, _ = run_fs(env_root, {"op": "write", "path": "big", "content": "x" * 9000,
                               "scratch_limit": 4096})  # fmt: skip
    assert res["ok"] is True


def test_fs_data_volume_read_write_and_full(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    ws.mkdir()
    res, _ = run_fs(ws, {"op": "write", "path": "/data/a.txt", "content": "x"})
    assert res["ok"] is False and "kein Volumen /data" in res["error"]
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("MGPT_DATA", str(data))
    res, _ = run_fs(ws, {"op": "write", "path": "/data/cache/a.txt", "content": "eins\n"})
    assert res == {"ok": True, "path": "/data/cache/a.txt", "bytes": 5, "created": True}
    assert (data / "cache/a.txt").read_text() == "eins\n"
    res, _ = run_fs(ws, {"op": "read", "path": "/data/cache/a.txt"})
    assert res["ok"] is True and "eins" in res["content"]
    # Über der Grenze (vom Agent gemessen): /data gesperrt, /workspace nicht
    full = {"data_full": True}
    res, _ = run_fs(ws, {"op": "write", "path": "/data/b.txt", "content": "x", **full})
    assert res["ok"] is False and "Volumen /data ist voll" in res["error"]
    res, _ = run_fs(ws, {"op": "edit", "path": "/data/cache/a.txt", "old_string": "eins",
                         "new_string": "zwei", **full})  # fmt: skip
    assert res["ok"] is False and "voll" in res["error"]
    res, _ = run_fs(ws, {"op": "write", "path": "b.txt", "content": "x", **full})
    assert res["ok"] is True
    # Symlink aus /data hinaus
    (data / "raus").symlink_to(tmp_path)
    res, _ = run_fs(ws, {"op": "read", "path": "/data/raus/workspace/b.txt"})
    assert res["ok"] is False and "/data" in res["error"]
