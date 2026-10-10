"""Werkzeug run_python und Sandbox (M4a-10).

Echte Sandbox-Läufe brauchen bubblewrap und User-Namespaces; ohne beides werden
sie übersprungen (``needs_sandbox``). Alles andere (Rechte, Angebot, Anhänge,
SVG-Bereinigung, Werkzeugschleife) läuft mit gemockter Sandbox überall.
"""

import io
import json
import os
import platform
import struct
import subprocess
import time

import httpx
import pytest
import respx
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.urls import reverse
from PIL import Image

from multigpt.accounts.models import Role
from multigpt.accounts.permissions import Action, can
from multigpt.chat import sandbox, svg_clean, tooling, tools_python
from multigpt.chat.models import (
    AIModel,
    Attachment,
    ChatSettings,
    Conversation,
    Message,
    Provider,
    ToolCall,
)
from multigpt.chat.services import append_message

PASSWORD = "Geheim-Test-1234"
BASE = "https://llm.example.invalid/v1"


def _sandbox_works() -> bool:
    if sandbox.bwrap_path() is None:
        return False
    try:
        return sandbox.status(refresh=True).available
    except Exception:  # noqa: BLE001
        return False


SANDBOX = _sandbox_works()
needs_sandbox = pytest.mark.skipif(not SANDBOX, reason="bubblewrap nicht nutzbar")


def run(code, **limits):
    return sandbox.run(code, sandbox.Limits(**limits))


# --- Echte Sandbox: Rechnen ------------------------------------------------------------


@needs_sandbox
def test_numpy_and_sympy():
    result = run(
        "import numpy as np, sympy as sp, mpmath\n"
        "x = sp.symbols('x')\n"
        "print(sp.integrate(sp.sin(x)**2, (x, 0, sp.pi)))\n"
        "print(sp.solve(x**2 - 5*x + 6, x))\n"
        "print(np.linalg.solve([[2, 1], [1, 3]], [3, 5]).round(6).tolist())\n"
        "mpmath.mp.dps = 30\n"
        "print(mpmath.pi)\n"
    )
    assert result.ok, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == "pi/2"
    assert lines[1] == "[2, 3]"
    assert lines[2] == "[0.8, 1.4]"
    assert lines[3] == "3.14159265358979323846264338328"


@needs_sandbox
def test_error_traceback_without_server_paths():
    result = run("def f():\n    return 1 / 0\nf()\n")
    assert result.exit_code == 1 and not result.ok
    assert 'File "<code>", line 2, in f' in result.stderr
    assert "ZeroDivisionError" in result.stderr
    assert "/sandbox/runner.py" not in result.stderr
    assert os.getcwd() not in result.stderr and "site-packages" not in result.stderr


@needs_sandbox
def test_sys_exit_code_passed_through():
    assert run("import sys; print('x'); sys.exit(3)").exit_code == 3


# --- Echte Sandbox: Grenzen --------------------------------------------------------------


@needs_sandbox
def test_endless_loop_hits_cpu_limit():
    started = time.monotonic()
    result = run("while True:\n    pass\n", cpu_seconds=1, wall_seconds=15)
    assert result.aborted == "cpu" and not result.ok
    assert time.monotonic() - started < 10


@needs_sandbox
def test_sleep_hits_wall_clock():
    started = time.monotonic()
    result = run("import time\ntime.sleep(60)\n", wall_seconds=2)
    assert result.aborted == "timeout"
    assert time.monotonic() - started < 8


@needs_sandbox
def test_memory_bomb_is_stopped():
    result = run("x = bytearray(3 * 1024**3)\nprint('zu viel')\n", memory_mb=256)
    assert not result.ok
    assert "zu viel" not in result.stdout
    assert "MemoryError" in result.stderr or result.aborted


def _sandbox_processes() -> int:
    out = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
    return sum("/sandbox/runner.py" in line for line in out.splitlines())


@needs_sandbox
def test_fork_bomb_is_limited_and_cleaned_up():
    code = (
        "import os, time\n"
        "made = 0\n"
        "for _ in range(200):\n"
        "    try:\n"
        "        pid = os.fork()\n"
        "    except OSError:\n"
        "        break\n"
        "    if pid == 0:\n"
        "        time.sleep(30)\n"
        "        os._exit(0)\n"
        "    made += 1\n"
        "print(made)\n"
    )
    started = time.monotonic()
    result = run(code, processes=4, wall_seconds=10)
    assert result.ok, result.stderr
    assert int(result.stdout) <= 2  # bwrap-Init und Python zählen mit
    assert time.monotonic() - started < 9
    time.sleep(0.5)
    assert _sandbox_processes() == 0


@needs_sandbox
def test_real_fork_bomb_ends_within_wall_clock():
    code = (
        "import os\nwhile True:\n    try:\n        os.fork()\n    except OSError:\n        pass\n"
    )
    started = time.monotonic()
    result = run(code, processes=8, wall_seconds=3, cpu_seconds=2)
    assert not result.ok
    assert time.monotonic() - started < 10
    time.sleep(0.5)
    assert _sandbox_processes() == 0


@needs_sandbox
def test_large_output_is_truncated():
    result = run("print('a' * 200_000)", output_kb=8)
    assert result.ok and result.truncated
    assert len(result.stdout) == 8 * 1024


@needs_sandbox
def test_huge_output_is_aborted():
    result = run("while True:\n    print('b' * 10_000)\n", output_kb=8, wall_seconds=15)
    assert result.aborted == "output" and result.truncated
    assert len(result.stdout) <= 8 * 1024


@needs_sandbox
def test_file_size_limit():
    result = run("open('gross.bin', 'wb').write(b'0' * (3 * 1024 * 1024))", file_mb=1)
    assert not result.ok
    assert "File too large" in result.stderr or result.aborted == "file_size"


# --- Echte Sandbox: Abschottung --------------------------------------------------------


@needs_sandbox
def test_no_network():
    result = run(
        "import socket\n"
        "for host in [('1.1.1.1', 53), ('127.0.0.1', 5432), ('::1', 8000)]:\n"
        "    try:\n"
        "        socket.create_connection(host, timeout=2)\n"
        "        print('offen', host)\n"
        "    except OSError as exc:\n"
        "        print('zu', type(exc).__name__)\n"
    )
    assert result.ok, result.stderr
    assert "offen" not in result.stdout
    assert result.stdout.count("zu") == 3


@needs_sandbox
def test_server_files_invisible(settings):
    paths = [
        "/etc/passwd",
        "/etc/multi-gpt/.env",
        "/var/lib/multi-gpt",
        "/home",
        "/run/postgresql/.s.PGSQL.5432",
        "/var/run/postgresql",
        "/proc/self/environ",
        str(settings.BASE_DIR),
        str(settings.BASE_DIR / ".env"),
        str(settings.BASE_DIR / "manage.py"),
        os.path.realpath(os.path.join(os.path.dirname(sandbox.__file__), "models.py")),
    ]
    code = (
        f"import os\nfor p in {paths!r}:\n"
        "    print(p, os.path.exists(p))\n"
        "for mod in ('django', 'multigpt', 'psycopg', 'cryptography'):\n"
        "    try:\n"
        "        __import__(mod)\n"
        "        print('import', mod)\n"
        "    except ImportError:\n"
        "        pass\n"
    )
    result = run(code)
    assert result.ok, result.stderr
    assert "True" not in result.stdout
    assert "import" not in result.stdout


@needs_sandbox
def test_writes_only_in_work_dir():
    result = run(
        "import os\n"
        "for p in ['/usr/x', '/x', '/sandbox/lib/x', '/sandbox/code.py']:\n"
        "    try:\n"
        "        open(p, 'w').write('x')\n"
        "        print('geschrieben', p)\n"
        "    except OSError:\n"
        "        pass\n"
        "open('ok.txt', 'w').write('x')\n"
        "open('/tmp/ok.txt', 'w').write('x')\n"
        "print(os.getcwd(), sorted(os.listdir('.')))\n"
    )
    assert result.ok, result.stderr
    assert "geschrieben" not in result.stdout
    assert "/work ['ok.txt']" in result.stdout


@needs_sandbox
def test_server_environment_invisible(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "streng-geheim-123")
    monkeypatch.setenv("DATABASE_URL", "postgres://geheim@db/x")
    result = run("import os\nprint(sorted(os.environ.items()))")
    assert result.ok
    assert "geheim" not in result.stdout
    assert "SECRET_KEY" not in result.stdout and "DATABASE_URL" not in result.stdout


@needs_sandbox
@pytest.mark.skipif(sandbox.seccomp_program() is None, reason="seccomp nur x86_64/aarch64")
def test_seccomp_blocks_namespaces_and_ptrace():
    result = run(
        "import ctypes, os\n"
        "try:\n"
        "    os.unshare(os.CLONE_NEWUSER)\n"
        "    print('unshare erlaubt')\n"
        "except OSError as exc:\n"
        "    print('unshare', exc.errno)\n"
        "libc = ctypes.CDLL(None, use_errno=True)\n"
        "print('ptrace', libc.ptrace(0, 0, None, None), ctypes.get_errno())\n"
    )
    assert result.ok, result.stderr
    assert "unshare 1" in result.stdout
    assert "ptrace -1 1" in result.stdout


@needs_sandbox
def test_plots_come_back_as_files():
    result = run(
        "import matplotlib.pyplot as plt\n"
        "plt.plot([1, 2, 3], [1, 4, 9])\n"
        "plt.savefig('quadrat.png')\n"
        "plt.savefig('quadrat.svg')\n"
        "open('notiz.txt', 'w').write('kein Bild')\n",
        wall_seconds=60,
    )
    assert result.ok, result.stderr
    names = sorted(f.name for f in result.files)
    assert names == ["quadrat.png", "quadrat.svg"]
    png = next(f for f in result.files if f.name.endswith(".png"))
    assert png.data.startswith(b"\x89PNG")


@needs_sandbox
def test_open_figure_without_savefig_is_saved():
    result = run("import matplotlib.pyplot as plt\nplt.bar([1, 2], [3, 4])\nplt.show()\n")
    assert result.ok, result.stderr
    assert [f.name for f in result.files] == ["abbildung-1.png"]


@needs_sandbox
def test_file_count_limit_in_sandbox():
    code = "for i in range(5):\n    open(f'b{i}.png', 'wb').write(b'x')\n"
    result = run(code, max_files=2)
    assert [f.name for f in result.files] == ["b0.png", "b1.png"]
    assert result.skipped_files == ["b2.png", "b3.png", "b4.png"]


@needs_sandbox
def test_selftest_all_green(settings):
    checks = sandbox.selftest()
    assert all(ok for ok, _ in checks), checks


# --- Ohne Sandbox: Bausteine ---------------------------------------------------------------


def test_limits_are_clamped():
    limits = sandbox.Limits(
        cpu_seconds=10_000, wall_seconds=0, memory_mb=1, processes=10_000, output_kb=-5
    ).clamped()
    assert limits.cpu_seconds == 60 and limits.wall_seconds == 2
    assert limits.memory_mb == 128 and limits.processes == 64 and limits.output_kb == 4


def test_limit_ranges_match_admin_fields():
    for name, field_name in [
        ("cpu_seconds", "python_cpu_seconds"),
        ("wall_seconds", "python_wall_seconds"),
        ("memory_mb", "python_memory_mb"),
        ("processes", "python_processes"),
        ("file_mb", "python_file_mb"),
        ("output_kb", "python_output_kb"),
    ]:
        validators = ChatSettings._meta.get_field(field_name).validators
        bounds = sorted(v.limit_value for v in validators)
        assert tuple(bounds) == sandbox.LIMIT_RANGES[name], name


@pytest.mark.parametrize("machine", ["x86_64", "aarch64"])
def test_seccomp_program_is_valid_bpf(machine):
    program = sandbox.seccomp_program(machine)
    assert program and len(program) % 8 == 0
    count = len(program) // 8
    for index in range(count):
        code, jt, jf, _ = struct.unpack("HBBI", program[index * 8 : index * 8 + 8])
        if code & 0x07 == 0x05:  # Sprung: Ziel innerhalb des Programms
            assert index + 1 + max(jt, jf) < count
    last_code = struct.unpack("HBBI", program[-8:])[0]
    assert last_code == 0x06  # endet mit RET


def test_seccomp_unknown_arch_is_none():
    assert sandbox.seccomp_program("sparc64") is None


def test_command_has_isolation_flags(monkeypatch):
    monkeypatch.setattr(sandbox, "bwrap_path", lambda: "/usr/bin/bwrap")
    args = sandbox.command(sandbox.Limits(), 10, 11, 12, 13)
    joined = " ".join(args)
    for flag in ("--unshare-all", "--die-with-parent", "--new-session", "--clearenv"):
        assert flag in args
    assert "--seccomp 13" in joined and "--remount-ro /" in joined
    assert "--ro-bind /usr /usr" in joined
    assert "--proc" not in args  # kein /proc (gesperrte Overmounts der Unit)
    assert "--bind" not in args and "--dev-bind" not in args  # nichts beschreibbar vom Host
    targets = [args[i + 2] for i, a in enumerate(args) if a == "--ro-bind"]
    assert targets[0] == "/usr"
    assert all(t.startswith("/sandbox/lib/") for t in targets[1:])
    assert "/sandbox/lib/numpy" in targets and "/sandbox/lib/sympy" in targets
    assert args[-5:-1] == ["-I", "-S", "-B", "/sandbox/runner.py"]


def test_command_without_bwrap(monkeypatch):
    monkeypatch.setattr(sandbox, "bwrap_path", lambda: None)
    with pytest.raises(sandbox.SandboxUnavailable, match="apt install bubblewrap"):
        sandbox.command(sandbox.Limits(), 10, 11, 12, None)


def test_status_without_bwrap(monkeypatch):
    monkeypatch.setattr(sandbox, "bwrap_path", lambda: None)
    state = sandbox.status(refresh=True)
    assert not state.available and "apt install bubblewrap" in state.reason
    assert not sandbox.available()


def test_failed_probe_is_unavailable(monkeypatch):
    def broken(code, limits=None):
        return sandbox.RunResult(
            exit_code=1, stderr="bwrap: No permissions to create new namespace"
        )

    monkeypatch.setattr(sandbox, "bwrap_path", lambda: "/usr/bin/bwrap")
    monkeypatch.setattr(sandbox, "run", broken)
    state = sandbox.status(refresh=True)
    assert not state.available and "No permissions" in state.reason


def test_parse_files_rejects_paths_and_garbage():
    limits = sandbox.Limits(max_files=3, max_file_bytes=100)
    raw = b"\n".join(
        [
            json.dumps({"name": "../../etc/x.png", "data": "QUJD"}).encode(),
            b"kein json",
            json.dumps({"name": "gross.png", "data": "QUJD" * 100}).encode(),
            json.dumps({"skipped": "/a/b.png", "reason": "size"}).encode(),
            json.dumps({"name": "c.png", "data": "!!!"}).encode(),
        ]
    )
    files, skipped = sandbox._parse_files(raw, limits)
    assert [(f.name, f.data) for f in files] == [("x.png", b"ABC")]
    assert skipped == ["gross.png", "b.png"]


# --- SVG-Bereinigung ---------------------------------------------------------------------------

EVIL_SVG = b"""<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"
     width="100" height="50" onload="alert(1)">
  <script>alert(1)</script>
  <style>@import url(https://evil.example/a.css); .a { fill: url(https://evil.example/p) }
  .b { fill: url(#ok) }</style>
  <a href="https://evil.example/"><text>klick</text></a>
  <foreignObject><div xmlns="http://www.w3.org/1999/xhtml">x</div></foreignObject>
  <image xlink:href="https://evil.example/x.png" width="1" height="1"/>
  <image href="data:image/png;base64,iVBORw0KGgo=" width="1" height="1"/>
  <image href="data:text/html;base64,PHNjcmlwdD4=" width="1" height="1"/>
  <use href="https://evil.example/s.svg#a"/>
  <use xlink:href="#kreis"/>
  <circle id="kreis" r="5" onclick="x()" style="fill: url('https://evil.example/p')"
          fill="url(#ok)" xml:base="https://evil.example/"/>
  <iframe src="https://evil.example/"/>
</svg>"""


def test_svg_clean_removes_active_content():
    out, width, height = svg_clean.clean(EVIL_SVG)
    text = out.decode()
    assert (width, height) == (100, 50)
    for bad in ("script", "onload", "onclick", "foreignObject", "<a ", "iframe", "@import"):
        assert bad not in text
    assert "evil.example" not in text
    assert "data:text/html" not in text
    assert 'href="data:image/png;base64,iVBORw0KGgo="' in text
    assert 'xlink:href="#kreis"' in text and 'fill="url(#ok)"' in text
    assert ".b { fill: url(#ok) }" in text
    assert text.startswith('<?xml version="1.0" encoding="utf-8"?>\n<svg xmlns=')


@pytest.mark.parametrize(
    "data",
    [
        b'<!DOCTYPE svg [<!ENTITY a "aaaa">]><svg xmlns="http://www.w3.org/2000/svg">&a;</svg>',
        b'<!DOCTYPE svg SYSTEM "file:///etc/passwd"><svg xmlns="x">&ext;</svg>',
        b"<html><script>alert(1)</script></html>",
        b"<svg>ohne Namensraum</svg>",
        b"\xff\xfe kaputt",
        b"<svg xmlns='http://www.w3.org/2000/svg'>" + b"<g>" * 300 + b"</g>" * 300 + b"</svg>",
    ],
    ids=["entity", "external", "html", "no-ns", "encoding", "deep"],
)
def test_svg_clean_rejects(data):
    with pytest.raises(svg_clean.SvgError):
        svg_clean.clean(data)


def test_svg_clean_keeps_matplotlib_output():
    matplotlib = pytest.importorskip("matplotlib")
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots()
    ax.plot([0, 1, 2], [0, 1, 4])
    ax.set_title("Quadrat <äöü>")
    buf = io.BytesIO()
    fig.savefig(buf, format="svg")
    plt.close(fig)
    out, width, height = svg_clean.clean(buf.getvalue())
    assert (width, height) == (614, 461)  # 460,8 x 345,6 pt
    assert out.count(b"<path") == buf.getvalue().count(b"<path")
    assert out.count(b"<use") == buf.getvalue().count(b"<use")


# --- Rechte, Einstellungen, Angebot -------------------------------------------------------


@pytest.fixture(autouse=True)
def media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"


@pytest.fixture(autouse=True)
def _no_sandbox():
    """Überschreibt die Fixture aus conftest: hier gilt der echte Sandbox-Status."""


@pytest.fixture(autouse=True)
def fresh_status():
    """Zwischengespeicherten Sandbox-Status je Test verwerfen (Mocks bleiben lokal)."""
    sandbox._status_cache = None
    yield
    sandbox._status_cache = None


def make_user(role_key, username):
    return get_user_model().objects.create_user(
        username, password=PASSWORD, role=Role.objects.get(key=role_key)
    )


@pytest.fixture
def tool_model(db):
    provider = Provider.objects.create(name="LLM", kind=Provider.Kind.OPENAI_COMPAT, base_url=BASE)
    return AIModel.objects.create(
        provider=provider, model_id="gpt-test", display_name="G", supports_tools=True
    )


@pytest.fixture
def sandbox_ok(monkeypatch):
    monkeypatch.setattr(sandbox, "available", lambda: True)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "key,expected", [("admin", True), ("adult", True), ("teen", True), ("guest", False)]
)
def test_role_defaults_from_migration(key, expected):
    assert Role.objects.get(key=key).can_compute is expected
    assert can(make_user(key, f"u-{key}"), Action.COMPUTE) is expected


@pytest.mark.django_db
def test_new_role_has_no_compute():
    assert Role.objects.create(key="neu", name="Neu").can_compute is False


@pytest.mark.django_db
def test_offered_with_right_setting_and_sandbox(tool_model, sandbox_ok):
    adult = make_user("adult", "erwachsen")
    assert "run_python" in tooling.builtin_bindings(adult, tool_model)
    guest = make_user("guest", "gast")
    assert "run_python" not in tooling.builtin_bindings(guest, tool_model)


@pytest.mark.django_db
def test_not_offered_without_tools_support(tool_model, sandbox_ok):
    tool_model.supports_tools = False
    tool_model.save()
    adult = make_user("adult", "erwachsen")
    assert "run_python" not in tooling.builtin_bindings(adult, tool_model)


@pytest.mark.django_db
def test_not_offered_without_sandbox(tool_model, monkeypatch):
    monkeypatch.setattr(sandbox, "bwrap_path", lambda: None)
    sandbox.status(refresh=True)
    try:
        adult = make_user("adult", "erwachsen")
        assert "run_python" not in tooling.builtin_bindings(adult, tool_model)
    finally:
        monkeypatch.undo()
        sandbox.status(refresh=True)


@pytest.mark.django_db
def test_not_offered_when_disabled(tool_model, sandbox_ok):
    cfg = ChatSettings.load()
    cfg.python_enabled = False
    cfg.save()
    adult = make_user("adult", "erwachsen")
    assert "run_python" not in tooling.builtin_bindings(adult, tool_model)


@pytest.mark.django_db
def test_mcp_access_does_not_matter(tool_model, sandbox_ok):
    tool_model.mcp_access = AIModel.McpAccess.NONE
    tool_model.save()
    adult = make_user("adult", "erwachsen")
    assert "run_python" in tooling.builtin_bindings(adult, tool_model)


@pytest.mark.django_db
def test_confirmation_switch(db):
    assert not tooling.builtin_needs_confirmation("run_python")
    cfg = ChatSettings.load()
    cfg.python_confirm = True
    cfg.save()
    assert tooling.builtin_needs_confirmation("run_python")


@pytest.mark.django_db
def test_limits_from_settings_are_clamped():
    cfg = ChatSettings(python_memory_mb=999_999, python_cpu_seconds=3)
    limits = tools_python.limits_from(cfg)
    assert limits.memory_mb == 4096 and limits.cpu_seconds == 3


def test_description_mentions_libraries_and_plots():
    spec = tooling.get_builtin("run_python").spec
    for word in ("numpy", "sympy", "mpmath", "matplotlib", "print", "plt.savefig", "Kein Netz"):
        assert word in spec.description
    assert spec.parameters["required"] == ["code"]


# --- Ausführen mit gemockter Sandbox: Ergebnis und Anhänge ------------------------------------


def png_bytes(size=(40, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(buf, "PNG", pnginfo=_png_info())
    return buf.getvalue()


def _png_info():
    from PIL import PngImagePlugin

    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "geheime Metadaten")
    return info


@pytest.fixture
def answer(db):
    user = make_user("adult", "erwachsen")
    conversation = Conversation.objects.create(user=user)
    return user, append_message(conversation, role="assistant", author=user)


class Collector:
    def __init__(self, message):
        self.message = message
        self.changed = False


def fake_run(monkeypatch, **fields):
    calls = []

    def runner(code, limits=None):
        calls.append((code, limits))
        return sandbox.RunResult(**{"exit_code": 0, "duration": 0.25, **fields})

    monkeypatch.setattr(sandbox, "run", runner)
    return calls


@pytest.mark.django_db
def test_run_result_text_for_model(answer, monkeypatch):
    user, message = answer
    calls = fake_run(monkeypatch, stdout="42\n", stderr="Warnung\n")
    result = tools_python._tool_run(user, {"code": "print(42)"}, Collector(message))
    assert not result.is_error
    assert result.text.startswith("Exit-Status 0, Laufzeit 0,25 s.")
    assert (
        "Ausgabe (stdout):\n42" in result.text and "Fehlerausgabe (stderr):\nWarnung" in result.text
    )
    code, limits = calls[0]
    assert code == "print(42)" and limits.cpu_seconds == 10 and limits.wall_seconds == 20


@pytest.mark.django_db
@pytest.mark.parametrize(
    "aborted,text",
    [
        ("timeout", "Zeitlimit von 20 s"),
        ("cpu", "CPU-Zeit-Grenze von 10 s"),
        ("output", "viel zu viel Ausgabe"),
        ("file_size", "Datei größer als 10 MB"),
    ],
)
def test_aborts_are_errors(answer, monkeypatch, aborted, text):
    user, message = answer
    fake_run(monkeypatch, exit_code=None, aborted=aborted)
    result = tools_python._tool_run(user, {"code": "x"}, Collector(message))
    assert result.is_error and text in result.text


@pytest.mark.django_db
@pytest.mark.parametrize("args", [{}, {"code": ""}, {"code": 5}, {"code": "x" * 50_001}])
def test_bad_arguments(answer, monkeypatch, args):
    user, message = answer
    calls = fake_run(monkeypatch)
    result = tools_python._tool_run(user, args, Collector(message))
    assert result.is_error and not calls


@pytest.mark.django_db
def test_run_rechecks_permission(answer, monkeypatch):
    user, message = answer
    calls = fake_run(monkeypatch)
    user.role.can_compute = False
    user.role.save()
    result = tools_python._tool_run(user, {"code": "1"}, Collector(message))
    assert result.is_error and result.text == tooling.MSG_NOT_ALLOWED and not calls


@pytest.mark.django_db
def test_unavailable_and_busy(answer, monkeypatch):
    user, message = answer

    def unavailable(code, limits=None):
        raise sandbox.SandboxUnavailable("weg")

    monkeypatch.setattr(sandbox, "run", unavailable)
    result = tools_python._tool_run(user, {"code": "1"}, Collector(message))
    assert result.is_error and result.text == tools_python.MSG_UNAVAILABLE

    def busy(code, limits=None):
        raise sandbox.SandboxBusy

    monkeypatch.setattr(sandbox, "run", busy)
    assert tools_python._tool_run(user, {"code": "1"}, Collector(message)).text == (
        tools_python.MSG_BUSY
    )


@pytest.mark.django_db
def test_png_and_svg_become_attachments(answer, monkeypatch):
    user, message = answer
    files = [
        sandbox.OutputFile("kurve.png", png_bytes()),
        sandbox.OutputFile("kurve.svg", EVIL_SVG),
    ]
    fake_run(monkeypatch, files=files)
    result = tools_python._tool_run(user, {"code": "plot"}, Collector(message))
    assert not result.is_error
    png, svg = Attachment.objects.filter(message=message).order_by("id")
    assert f"kurve.png (Anhang #{png.pk})" in result.text
    assert f"kurve.svg (Anhang #{svg.pk})" in result.text
    assert png.kind == svg.kind == Attachment.Kind.IMAGE
    assert png.owner is None and png.tool_call is None and png.generated_by_model is None
    assert png.mime_type == "image/png" and png.thumbnail and (png.width, png.height) == (40, 30)
    with png.file.open("rb") as fh:
        assert b"geheime Metadaten" not in fh.read()
    assert svg.mime_type == "image/svg+xml" and not svg.thumbnail
    assert (svg.width, svg.height) == (100, 50)
    with svg.file.open("rb") as fh:
        stored = fh.read()
    assert b"script" not in stored and b"evil.example" not in stored and b"onload" not in stored


@pytest.mark.django_db
def test_invalid_files_are_rejected(answer, monkeypatch):
    user, message = answer
    files = [
        sandbox.OutputFile("kaputt.png", b"\x89PNG kein Bild"),
        sandbox.OutputFile("boese.svg", b"<!DOCTYPE x [<!ENTITY a 'b'>]><svg/>"),
        sandbox.OutputFile("gross.svg", b"<svg>" + b" " * (svg_clean.MAX_BYTES + 1)),
        sandbox.OutputFile("text.txt", b"hallo"),
    ]
    fake_run(monkeypatch, files=files)
    result = tools_python._tool_run(user, {"code": "x"}, Collector(message))
    assert not Attachment.objects.exists()
    assert "Nicht übernommen: kaputt.png (kein gültiges Bild)" in result.text
    assert "boese.svg (kein gültiges Bild)" in result.text
    assert "gross.svg (Format oder Größe)" in result.text


@pytest.mark.django_db
def test_file_limit_per_answer(answer, monkeypatch):
    user, message = answer
    files = [sandbox.OutputFile(f"b{i}.png", png_bytes()) for i in range(5)]
    fake_run(monkeypatch, files=files)
    for _ in range(3):
        tools_python._tool_run(user, {"code": "x"}, Collector(message))
    assert Attachment.objects.filter(message=message).count() == tools_python.MAX_FILES_PER_ANSWER


@pytest.mark.django_db
def test_svg_served_as_download_with_csp(client, answer, monkeypatch):
    user, message = answer
    fake_run(monkeypatch, files=[sandbox.OutputFile("d.svg", EVIL_SVG)])
    tools_python._tool_run(user, {"code": "x"}, Collector(message))
    svg = Attachment.objects.get()
    client.force_login(user)
    response = client.get(svg.url)
    assert response.status_code == 200
    assert response["Content-Type"] == "image/svg+xml"
    assert "attachment" in response["Content-Disposition"]
    assert "sandbox" in response["Content-Security-Policy"]
    assert response["X-Content-Type-Options"] == "nosniff"
    other = make_user("adult", "fremd")
    client.force_login(other)
    assert client.get(svg.url).status_code == 404


@pytest.mark.django_db
def test_logs_contain_no_code_or_output(answer, monkeypatch, caplog):
    user, message = answer
    fake_run(monkeypatch, stdout="GEHEIMES-ERGEBNIS")
    caplog.set_level("INFO")
    tools_python._tool_run(user, {"code": "print('GEHEIMER-CODE')"}, Collector(message))
    assert str(message.pk) in caplog.text
    assert "GEHEIM" not in caplog.text


@needs_sandbox
@pytest.mark.django_db
def test_real_plot_becomes_attachments(answer):
    user, message = answer
    code = (
        "import numpy as np\nimport matplotlib.pyplot as plt\n"
        "x = np.linspace(0, 6.3, 50)\nplt.plot(x, np.sin(x))\n"
        "plt.savefig('sinus.png')\nplt.savefig('sinus.svg')\nprint('fertig')\n"
    )
    result = tools_python._tool_run(user, {"code": code}, Collector(message))
    assert not result.is_error, result.text
    kinds = sorted(a.mime_type for a in Attachment.objects.filter(message=message))
    assert kinds == ["image/png", "image/svg+xml"]


# --- Werkzeugschleife mit gemocktem Anbieter (respx) ----------------------------------------


def sse(*chunks):
    lines = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
    return "".join([*lines, "data: [DONE]\n\n"]).encode()


def tool_call_body(call_id, name, arguments):
    part = {
        "index": 0,
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }
    return sse(
        {"choices": [{"index": 0, "delta": {"tool_calls": [part]}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2}},
    )


def text_body(text):
    return sse(
        {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 5}},
    )


def events_of(response):
    assert response.status_code == 200, response.content
    raw = b"".join(response.streaming_content).decode()
    events = []
    for block in raw.split("\n\n"):
        if block.strip():
            lines = dict(line.split(": ", 1) for line in block.split("\n"))
            events.append((lines["event"], json.loads(lines["data"])))
    return events


INTEGRAL_CODE = (
    "import sympy as sp\nx = sp.symbols('x')\nprint(sp.integrate(sp.sin(x)**2, (x, 0, sp.pi)))"
)


def tool_loop(client, user, tool_model, code):
    bodies = [
        tool_call_body("c1", "run_python", {"code": code}),
        text_body("Das Integral ist sqrt(pi)."),
    ]
    requests = []

    def reply(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, content=bodies[len(requests) - 1], headers={"content-type": "text/event-stream"}
        )

    client.force_login(user)
    conversation = Conversation.objects.create(user=user)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{BASE}/chat/completions").mock(side_effect=reply)
        response = client.post(
            reverse("chat:api_messages", args=[conversation.pk]),
            json.dumps({"content": "Integral von exp(-x²)?", "model": tool_model.pk}),
            content_type="application/json",
        )
        events = events_of(response)
    return requests, events


@pytest.mark.django_db
def test_tool_loop_with_mocked_sandbox(client, tool_model, sandbox_ok, monkeypatch):
    calls = fake_run(
        monkeypatch, stdout="sqrt(pi)\n", files=[sandbox.OutputFile("f.png", png_bytes())]
    )
    user = make_user("adult", "erwachsen")
    code = INTEGRAL_CODE
    requests, events = tool_loop(client, user, tool_model, code)
    assert calls[0][0] == code
    offered = {t["function"]["name"] for t in requests[0]["tools"]}
    assert "run_python" in offered
    started = [d for n, d in events if n == "tool_call"]
    assert started[0]["tool"] == "run_python" and started[0]["server"] == "Berechnungen"
    assert started[0]["arguments"] == {"code": code}
    result = [d for n, d in events if n == "tool_result"][0]
    assert result["status"] == "ok" and "sqrt(pi)" in result["result"]
    tool_messages = [m for m in requests[1]["messages"] if m["role"] == "tool"]
    assert "Exit-Status 0" in tool_messages[0]["content"]
    assert "sqrt(pi)" in tool_messages[0]["content"]
    answer = Message.objects.get(role="assistant")
    assert answer.status == Message.Status.COMPLETE and "sqrt(pi)" in answer.content
    call = ToolCall.objects.get()
    assert call.tool == "run_python" and call.server is None and call.status == "ok"
    # Diagramm hängt an der Antwort (nicht am Werkzeugaufruf) und erscheint dort.
    attachment = Attachment.objects.get()
    assert attachment.message == answer and attachment.tool_call is None
    page = client.get(reverse("chat:api_messages", args=[answer.conversation_id]))
    shown = [a["id"] for m in page.json() for a in m.get("attachments", [])]
    assert attachment.pk in shown


@pytest.mark.django_db
def test_tool_loop_with_confirmation(client, tool_model, sandbox_ok, monkeypatch):
    calls = fake_run(monkeypatch, stdout="2\n")
    cfg = ChatSettings.load()
    cfg.python_confirm = True
    cfg.save()
    user = make_user("adult", "erwachsen")
    requests, events = tool_loop(client, user, tool_model, "print(1+1)")
    assert ("confirmation_required" in [n for n, _ in events]) and not calls
    assert ToolCall.objects.get().status == ToolCall.Status.AWAITING_CONFIRMATION


@needs_sandbox
@pytest.mark.django_db
def test_tool_loop_with_real_sandbox(client, tool_model):
    user = make_user("teen", "jugendlich")
    user.role.all_models = True
    user.role.save()
    code = INTEGRAL_CODE
    requests, events = tool_loop(client, user, tool_model, code)
    result = [d for n, d in events if n == "tool_result"][0]
    assert result["status"] == "ok", result
    tool_messages = [m for m in requests[1]["messages"] if m["role"] == "tool"]
    assert "pi/2" in tool_messages[0]["content"]


# --- Admin ------------------------------------------------------------------------------------


@pytest.fixture
def admin_client(client, db):
    admin = get_user_model().objects.create_superuser("chef", password=PASSWORD)
    client.force_login(admin)
    return client


@pytest.mark.django_db
def test_admin_shows_status_and_limits(admin_client, monkeypatch):
    monkeypatch.setattr(
        sandbox, "status", lambda refresh=False: sandbox.Status(False, "bwrap fehlt. Abhilfe X")
    )
    cfg = ChatSettings.load()
    response = admin_client.get(reverse("admin:chat_chatsettings_change", args=[cfg.pk]))
    text = response.content.decode()
    assert response.status_code == 200
    assert "Sandbox verfügbar" in text and "bwrap fehlt. Abhilfe X" in text
    assert "Sandbox testen" in text and "python_cpu_seconds" in text


@pytest.mark.django_db
def test_admin_sandbox_test_button(admin_client, monkeypatch):
    monkeypatch.setattr(
        sandbox, "selftest", lambda: [(True, "print(1+1) ergibt 2."), (False, "NETZ ERREICHBAR!")]
    )
    cfg = ChatSettings.load()
    url = reverse("admin:chat_chatsettings_sandbox_test", args=[cfg.pk])
    assert admin_client.get(url).status_code == 405
    response = admin_client.post(url)
    assert response.status_code == 302
    texts = [(m.level_tag, str(m)) for m in get_messages(response.wsgi_request)]
    assert texts == [("success", "print(1+1) ergibt 2."), ("error", "NETZ ERREICHBAR!")]


@pytest.mark.django_db
def test_admin_sandbox_test_needs_staff(client, monkeypatch):
    monkeypatch.setattr(sandbox, "selftest", lambda: pytest.fail("darf nicht laufen"))
    user = make_user("adult", "erwachsen")
    client.force_login(user)
    response = client.post(reverse("admin:chat_chatsettings_sandbox_test", args=[1]))
    assert response.status_code in (302, 403)


@pytest.mark.django_db
def test_admin_rejects_limits_above_maximum(admin_client, monkeypatch):
    monkeypatch.setattr(sandbox, "status", lambda refresh=False: sandbox.Status(True, "", True))
    cfg = ChatSettings.load()
    url = reverse("admin:chat_chatsettings_change", args=[cfg.pk])
    data = {
        "base_instructions": cfg.base_instructions,
        "default_image_model": "",
        "python_enabled": "on",
        "python_cpu_seconds": 10,
        "python_wall_seconds": 20,
        "python_memory_mb": 100_000,
        "python_processes": 4,
        "python_file_mb": 10,
        "python_output_kb": 64,
    }
    response = admin_client.post(url, data)
    assert response.status_code == 200  # Formular mit Fehler
    assert response.context["adminform"].form.errors.get("python_memory_mb")
    cfg.refresh_from_db()
    assert cfg.python_memory_mb == 512
    data["python_memory_mb"] = 1024
    assert admin_client.post(url, data).status_code == 302
    cfg.refresh_from_db()
    assert cfg.python_memory_mb == 1024


def test_architecture_note():
    # Nur zur Dokumentation im Testlauf: welche seccomp-Tabelle greift.
    assert platform.machine() in ("x86_64", "aarch64") or sandbox.seccomp_program() is None
