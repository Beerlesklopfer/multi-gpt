"""Sandbox für Rechnungen (Werkzeug ``run_python``, M4a-10).

Der Code stammt vom Modell und gilt als **nicht vertrauenswürdig** (z. B. per
Prompt-Injection aus einer Webseite oder einem Dokument). Er läuft deshalb nie
im Server-Prozess, sondern in einem eigenen Prozess unter bubblewrap (``bwrap``).
Gibt es bwrap nicht oder scheitert die Probe, wird das Werkzeug nicht angeboten –
einen unsicheren Rückfall (``exec`` im Server) gibt es nicht, auch nicht unter DEBUG.

Aufbau (``command``):

- ``--unshare-all`` (User, PID, Netz, IPC, UTS, Cgroup): kein Netz außer einem
  leeren Loopback, eigene PIDs. ``--die-with-parent``, ``--new-session``
  (kein TIOCSTI auf ein Terminal), ``--cap-drop ALL``.
- Dateisystem: leeres Wurzel-tmpfs, nur lesend ``/usr`` (Interpreter und
  Standardbibliothek) und **einzeln** die Rechenpakete aus dem venv unter
  ``/sandbox/lib`` (numpy, sympy, mpmath, matplotlib und deren Abhängigkeiten).
  Django, MultiGPT selbst, Einstellungen, ``/etc``, ``/home``, ``/var/lib/multi-gpt``,
  DB-Sockets und ``/proc`` sind nicht sichtbar. ``/tmp`` und der Arbeitsordner
  ``/work`` sind eigene tmpfs mit fester Größe; danach wird die Wurzel
  schreibgeschützt (``--remount-ro /``).
- **Kein /proc:** Unter der gehärteten Unit (``ProtectKernelTunables``,
  ``ProtectKernelLogs``) liegen gesperrte Overmounts auf /proc; der Kernel
  verweigert dann ein neues procfs im User-Namespace. Ohne /proc ist die
  Sandbox ohnehin enger.
- ``--clearenv`` mit wenigen festen Variablen (PATH, HOME, MPLBACKEND=Agg,
  MPLCONFIGDIR im tmpfs, je ein BLAS-Thread). Umgebung des Servers (SECRET_KEY,
  DATABASE_URL …) kommt nicht hinein.
- Python mit ``-I -S -B``: keine Umgebungsvariablen, keine site-packages,
  kein Skriptordner im Pfad, keine .pyc.
- seccomp (``--seccomp``): selbst erzeugtes BPF-Programm (``seccomp_program``)
  ohne neue Abhängigkeit. Verboten u. a. ptrace, mount, unshare/setns, neue
  Namespaces per clone, clone3 (ENOSYS, glibc fällt auf clone zurück), bpf,
  perf_event_open, userfaultfd, keyctl, io_uring, Kernelmodule, kexec. Nur
  x86_64 und aarch64; sonst läuft die Sandbox ohne seccomp (Status nennt das).
  ``no_new_privs`` setzt bwrap selbst.

Grenzen (``Limits``, Werte aus den Chat-Einstellungen, hier zusätzlich auf
``MAX_LIMITS`` begrenzt) setzt das Startskript im Kindprozess per ``resource``,
bevor der Code läuft (weich = hart, also nicht anhebbar): CPU-Zeit, Adressraum,
Prozesse/Threads (RLIMIT_NPROC; erst innerhalb des User-Namespace gesetzt, weil
der Kernel sonst alle Prozesse des Dienstnutzers mitzählt), Dateigröße, offene
Dateien, keine Core-Dateien. Die Wanduhr überwacht der Server und beendet bei
Überschreitung die ganze Prozessgruppe (SIGKILL); mit bwrap stirbt dann der
PID-Namespace. Ausgabe wird beim Lesen begrenzt; läuft sie weit über, wird
abgebrochen.

Code, Startskript und seccomp-Programm gehen über memfd (``--ro-bind-data``,
``--seccomp``) hinein, Dateien (Diagramme) kommen über einen eigenen Pipe-
Deskriptor zurück (JSON-Zeilen, base64) – der Server liest dabei keine Pfade
aus der Sandbox. Alles Zurückgelieferte gilt weiter als nicht vertrauenswürdig
(Bilder werden neu kodiert, SVG bereinigt, siehe ``tools_python``).

Logs: nur Dauer, Exit-Status, Abbruchgrund – nie Code oder Ausgabe.
"""

from __future__ import annotations

import base64
import binascii
import importlib.util
import json
import logging
import os
import platform
import selectors
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Feste Suchpfade für bwrap (nicht PATH des Dienstes).
BWRAP_SEARCH_PATH = "/usr/bin:/bin:/usr/local/bin"
INSTALL_HINT = "Paket bubblewrap installieren: sudo apt install bubblewrap"

# Pakete, die in die Sandbox eingeblendet werden (Importname). Fehlende werden
# übersprungen; numpy, sympy und mpmath prüft der Status.
CALC_PACKAGES = (
    "numpy",
    "sympy",
    "mpmath",
    "matplotlib",
    "mpl_toolkits",
    "PIL",
    "contourpy",
    "kiwisolver",
    "fontTools",
    "cycler",
    "pyparsing",
    "dateutil",
    "six",
    "packaging",
)
REQUIRED_PACKAGES = ("numpy", "sympy", "mpmath")
# Mitgelieferte Bibliotheken der Wheels (auditwheel, RPATH $ORIGIN/../<name>.libs).
BUNDLED_LIBS = ("numpy.libs", "pillow.libs", "matplotlib.libs", "contourpy.libs", "kiwisolver.libs")

SANDBOX_LIB = "/sandbox/lib"
WORK_DIR = "/work"
TMP_SIZE = 64 * 1024 * 1024
# Arbeitsordner: Dateigrenze mal ein paar Dateien, mindestens 32 MB.
WORK_SIZE_FACTOR = 4

# Gleichzeitige Rechnungen je Server-Prozess; darüber wird kurz gewartet.
MAX_PARALLEL = 2


@dataclass(frozen=True)
class Limits:
    cpu_seconds: int = 10
    wall_seconds: int = 20
    memory_mb: int = 512
    processes: int = 4
    file_mb: int = 10
    output_kb: int = 64
    max_files: int = 6
    max_file_bytes: int = 5 * 1024 * 1024

    def clamped(self) -> Limits:
        """Auf sichere Mindest- und Höchstwerte begrenzen (auch bei manipulierter DB)."""
        values = {}
        for name, (low, high) in LIMIT_RANGES.items():
            values[name] = max(low, min(high, int(getattr(self, name))))
        return Limits(**values)


# Mindest- und Höchstwerte (die Admin-Felder nutzen dieselben Grenzen).
LIMIT_RANGES = {
    "cpu_seconds": (1, 60),
    "wall_seconds": (2, 120),
    "memory_mb": (128, 4096),
    # Bubblewrap-Init und Python zählen mit; darunter läuft nichts.
    "processes": (2, 64),
    "file_mb": (1, 100),
    "output_kb": (4, 1024),
    "max_files": (0, 20),
    "max_file_bytes": (64 * 1024, 20 * 1024 * 1024),
}


@dataclass
class OutputFile:
    name: str
    data: bytes


@dataclass
class RunResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    # Abbruchgrund: "" (normal beendet), "timeout", "output", "cpu", "file_size", "signal"
    aborted: str = ""
    signal: int | None = None
    truncated: bool = False
    duration: float = 0.0
    files: list[OutputFile] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.aborted


class SandboxUnavailable(Exception):
    """Sandbox nicht nutzbar; ``str(exc)`` ist die Ursache (deutsch, ohne Interna)."""


class SandboxBusy(Exception):
    """Zu viele gleichzeitige Rechnungen in diesem Prozess."""


# --- seccomp ------------------------------------------------------------------------

_BPF_LD_W_ABS = 0x00 | 0x00 | 0x20
_BPF_JEQ = 0x05 | 0x10
_BPF_JGE = 0x05 | 0x30
_BPF_JSET = 0x05 | 0x40
_BPF_RET = 0x06
_RET_ALLOW = 0x7FFF0000
_RET_ERRNO = 0x00050000
_EPERM = 1
_ENOSYS = 38
# Neue Namespaces per clone(): NEWNS, NEWCGROUP, NEWUTS, NEWIPC, NEWUSER, NEWPID, NEWNET.
_CLONE_NS_MASK = 0x00020000 | 0x02000000 | 0x04000000 | 0x08000000 | 0x10000000
_CLONE_NS_MASK |= 0x20000000 | 0x40000000

_DENY_NAMES = (
    "ptrace process_vm_readv process_vm_writev perf_event_open bpf userfaultfd "
    "keyctl add_key request_key mount umount2 pivot_root chroot unshare setns "
    "kexec_load kexec_file_load init_module finit_module delete_module reboot "
    "swapon swapoff acct syslog io_uring_setup io_uring_enter io_uring_register "
    "open_by_handle_at name_to_handle_at fsopen fsconfig fsmount fspick move_mount "
    "open_tree mount_setattr lookup_dcookie quotactl"
).split()

# Syscall-Nummern (asm/unistd_64.h bzw. asm-generic/unistd.h).
_SYSCALLS = {
    "x86_64": {
        "audit_arch": 0xC000003E,
        "x32": True,
        "clone": 56,
        "clone3": 435,
        "numbers": dict(
            zip(
                _DENY_NAMES,
                (101, 310, 311, 298, 321, 323, 250, 248, 249, 165, 166, 155, 161, 272, 308)
                + (246, 320, 175, 313, 176, 169, 167, 168, 163, 103, 425, 426, 427)
                + (304, 303, 430, 431, 432, 433, 429, 428, 442, 212, 179),
                strict=True,
            )
        ),
    },
    "aarch64": {
        "audit_arch": 0xC00000B7,
        "x32": False,
        "clone": 220,
        "clone3": 435,
        "numbers": dict(
            zip(
                _DENY_NAMES,
                (117, 270, 271, 241, 280, 282, 219, 217, 218, 40, 39, 41, 51, 97, 268)
                + (104, 294, 105, 273, 106, 142, 224, 225, 89, 116, 425, 426, 427)
                + (265, 264, 430, 431, 432, 433, 429, 428, 442, 18, 60),
                strict=True,
            )
        ),
    },
}


def seccomp_program(machine: str | None = None) -> bytes | None:
    """Klassisches BPF-Programm (Array von ``struct sock_filter``) für bwrap
    ``--seccomp``; None für nicht unterstützte Architekturen."""
    table = _SYSCALLS.get(machine or platform.machine())
    if table is None:
        return None
    ops: list[tuple] = [
        ("ld", 4),  # seccomp_data.arch
        ("jeq", table["audit_arch"], None, "deny"),  # fremde ABI (z. B. i386): verbieten
        ("ld", 0),  # seccomp_data.nr
    ]
    if table["x32"]:
        ops.append(("jge", 0x40000000, "deny", None))  # x32-ABI
    ops.append(("jeq", table["clone3"], "enosys", None))
    ops.append(("jeq", table["clone"], "clone", None))
    for number in sorted(table["numbers"].values()):
        ops.append(("jeq", number, "deny", None))
    ops += [
        ("ret", _RET_ALLOW),
        ("label", "clone"),
        ("ld", 16),  # args[0] (Flags), untere 32 Bit (Little Endian)
        ("jset", _CLONE_NS_MASK, "deny", None),
        ("ret", _RET_ALLOW),
        ("label", "deny"),
        ("ret", _RET_ERRNO | _EPERM),
        ("label", "enosys"),
        ("ret", _RET_ERRNO | _ENOSYS),
    ]
    labels, index = {}, 0
    for op in ops:
        if op[0] == "label":
            labels[op[1]] = index
        else:
            index += 1
    out, index = [], 0
    for op in ops:
        kind = op[0]
        if kind == "label":
            continue

        def offset(target, here=index):
            return 0 if target is None else labels[target] - here - 1

        if kind == "ld":
            out.append(struct.pack("HBBI", _BPF_LD_W_ABS, 0, 0, op[1]))
        elif kind == "ret":
            out.append(struct.pack("HBBI", _BPF_RET, 0, 0, op[1]))
        else:
            code = {"jeq": _BPF_JEQ, "jge": _BPF_JGE, "jset": _BPF_JSET}[kind]
            out.append(struct.pack("HBBI", code, offset(op[2]), offset(op[3]), op[1]))
        index += 1
    return b"".join(out)


# --- Startskript (läuft IN der Sandbox) --------------------------------------------

RUNNER = r"""
import json, os, resource, sys

cfg = json.loads(sys.argv[1])
rfd = int(cfg["result_fd"])
os.closerange(3, rfd)
os.closerange(rfd + 1, 4096)


def _limit(res, value):
    resource.setrlimit(res, (value, value))


resource.setrlimit(resource.RLIMIT_CPU, (cfg["cpu"], cfg["cpu"] + 1))
_limit(resource.RLIMIT_AS, cfg["memory"])
_limit(resource.RLIMIT_NPROC, cfg["processes"])
_limit(resource.RLIMIT_FSIZE, cfg["file_size"])
_limit(resource.RLIMIT_NOFILE, 256)
_limit(resource.RLIMIT_CORE, 0)
sys.path.append("/sandbox/lib")

import linecache, traceback

with open("/sandbox/code.py", encoding="utf-8") as fh:
    source = fh.read()
linecache.cache["<code>"] = (len(source), None, source.splitlines(True), "<code>")
status = 0
try:
    exec(compile(source, "<code>", "exec"), {"__name__": "__main__", "__builtins__": __builtins__})
except SystemExit as exc:
    if exc.code is None:
        status = 0
    elif isinstance(exc.code, int):
        status = exc.code
    else:
        print(exc.code, file=sys.stderr)
        status = 1
except BaseException as exc:
    status = 1
    try:
        frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename == "<code>"]
        text = []
        if frames:
            text = ["Traceback (most recent call last):\n"] + traceback.format_list(frames)
        text += traceback.format_exception_only(type(exc), exc)
        sys.stderr.write("".join(text))
    except BaseException:
        sys.stderr.write(type(exc).__name__ + "\n")
for stream in (sys.stdout, sys.stderr):
    try:
        stream.flush()
    except BaseException:
        pass

SUFFIXES = (".png", ".svg", ".jpg", ".jpeg", ".webp")


def _images():
    try:
        entries = sorted(os.scandir("/work"), key=lambda e: e.name)
    except OSError:
        return []
    return [
        e for e in entries
        if e.name.lower().endswith(SUFFIXES) and e.is_file(follow_symlinks=False)
    ]


try:
    plt = sys.modules.get("matplotlib.pyplot")
    if plt is not None and cfg["max_files"] and not _images():
        # Offene Diagramme ohne savefig: als PNG ablegen (plt.show() zeigt mit Agg nichts).
        for i, num in enumerate(plt.get_fignums()[: cfg["max_files"]], 1):
            plt.figure(num).savefig(f"/work/abbildung-{i}.png", dpi=100)
except BaseException:
    pass

try:
    import base64
    with os.fdopen(rfd, "wb") as out:
        for i, entry in enumerate(_images()):
            if i >= cfg["max_files"]:
                out.write((json.dumps({"skipped": entry.name, "reason": "count"}) + "\n").encode())
                continue
            size = entry.stat(follow_symlinks=False).st_size
            if size > cfg["max_file_bytes"]:
                out.write((json.dumps({"skipped": entry.name, "reason": "size"}) + "\n").encode())
                continue
            with open(entry.path, "rb") as fh:
                data = fh.read(cfg["max_file_bytes"] + 1)
            line = {"name": entry.name, "data": base64.b64encode(data).decode("ascii")}
            out.write((json.dumps(line) + "\n").encode())
except BaseException:
    pass
os._exit(status & 0xFF)
"""


# --- Umgebung -----------------------------------------------------------------------


def bwrap_path() -> str | None:
    return shutil.which("bwrap", path=BWRAP_SEARCH_PATH)


def python_path() -> str:
    """Interpreter für die Sandbox: die echte Binary hinter dem venv-Python."""
    return os.path.realpath(sys.executable)


def package_mounts() -> list[tuple[str, str]]:
    """(Quelle, Ziel) für die Rechenpakete; nur vorhandene. Suchen ohne Import."""
    mounts, seen_dirs = [], set()
    for name in CALC_PACKAGES:
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            spec = None
        if spec is None:
            continue
        if spec.submodule_search_locations:
            for location in spec.submodule_search_locations:
                src = os.path.realpath(location)
                if os.path.isdir(src):
                    mounts.append((src, f"{SANDBOX_LIB}/{name}"))
                    seen_dirs.add(os.path.dirname(src))
                    break
        elif spec.origin and os.path.isfile(spec.origin):
            src = os.path.realpath(spec.origin)
            mounts.append((src, f"{SANDBOX_LIB}/{os.path.basename(src)}"))
            seen_dirs.add(os.path.dirname(src))
    for base in sorted(seen_dirs):
        for libs in BUNDLED_LIBS:
            src = os.path.join(base, libs)
            if os.path.isdir(src) and all(m[0] != src for m in mounts):
                mounts.append((src, f"{SANDBOX_LIB}/{libs}"))
    return mounts


def _memfd(name: str, data: bytes) -> int:
    fd = os.memfd_create(name, os.MFD_CLOEXEC)
    os.write(fd, data)  # memfd: schreibt vollständig
    os.lseek(fd, 0, os.SEEK_SET)
    return fd


def command(
    limits: Limits,
    runner_fd: int,
    code_fd: int,
    result_fd: int,
    seccomp_fd: int | None,
    font_cache: tuple[str, int] | None = None,
) -> list[str]:
    """Aufruf von bwrap (ohne Netz, ohne Server-Dateien), siehe Modulkopf."""
    bwrap = bwrap_path()
    if bwrap is None:
        raise SandboxUnavailable(f"bubblewrap (bwrap) ist nicht installiert. {INSTALL_HINT}")
    work_size = max(32 * 1024 * 1024, limits.file_mb * 1024 * 1024 * WORK_SIZE_FACTOR)
    args = [
        bwrap,
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--clearenv",
        "--setenv", "PATH", "/usr/bin:/bin",
        "--setenv", "HOME", "/tmp",
        "--setenv", "LANG", "C.UTF-8",
        "--setenv", "MPLBACKEND", "Agg",
        "--setenv", "MPLCONFIGDIR", "/tmp/matplotlib",
        "--setenv", "OPENBLAS_NUM_THREADS", "1",
        "--setenv", "OMP_NUM_THREADS", "1",
        "--ro-bind", "/usr", "/usr",
        "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64",
        "--dev", "/dev",
        "--size", str(TMP_SIZE), "--tmpfs", "/tmp",
        "--size", str(work_size), "--tmpfs", WORK_DIR,
    ]  # fmt: skip
    if font_cache is not None:
        # Fertiger Font-Cache von matplotlib (``_font_cache``) im beschreibbaren tmpfs.
        name, fd = font_cache
        args += ["--dir", "/tmp/matplotlib", "--file", str(fd), f"/tmp/matplotlib/{name}"]
    for src, dest in package_mounts():
        args += ["--ro-bind", src, dest]
    args += [
        "--ro-bind-data", str(runner_fd), "/sandbox/runner.py",
        "--ro-bind-data", str(code_fd), "/sandbox/code.py",
    ]  # fmt: skip
    if seccomp_fd is not None:
        args += ["--seccomp", str(seccomp_fd)]
    config = {
        "result_fd": result_fd,
        "cpu": limits.cpu_seconds,
        "memory": limits.memory_mb * 1024 * 1024,
        "processes": limits.processes,
        "file_size": limits.file_mb * 1024 * 1024,
        "max_files": limits.max_files,
        "max_file_bytes": limits.max_file_bytes,
    }
    args += [
        "--remount-ro", "/",
        "--chdir", WORK_DIR,
        "--",
        python_path(), "-I", "-S", "-B", "/sandbox/runner.py", json.dumps(config),
    ]  # fmt: skip
    return args


# --- Ausführen ----------------------------------------------------------------------

_SLOTS = threading.BoundedSemaphore(MAX_PARALLEL)


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _decode(data: bytes, limit: int) -> tuple[str, bool]:
    truncated = len(data) > limit
    return data[:limit].decode("utf-8", errors="replace"), truncated


def _parse_files(raw: bytes, limits: Limits) -> tuple[list[OutputFile], list[str]]:
    """Antwortkanal streng lesen: JSON-Zeilen, Namen ohne Pfad, Größe begrenzt."""
    files, skipped = [], []
    for line in raw.splitlines()[: 2 * limits.max_files + 50]:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        name = item.get("name") if isinstance(item.get("name"), str) else ""
        name = os.path.basename(name)[:120]
        if isinstance(item.get("skipped"), str):
            skipped.append(os.path.basename(item["skipped"])[:120])
            continue
        if not name or not isinstance(item.get("data"), str):
            continue
        if len(files) >= limits.max_files:
            skipped.append(name)
            continue
        try:
            data = base64.b64decode(item["data"], validate=True)
        except (binascii.Error, ValueError):
            continue
        if not data or len(data) > limits.max_file_bytes:
            skipped.append(name)
            continue
        files.append(OutputFile(name, data))
    return files, skipped


def run(code: str, limits: Limits | None = None) -> RunResult:
    """``code`` in der Sandbox ausführen und das Ergebnis liefern.

    Blockiert höchstens etwa ``wall_seconds`` (plus Aufräumen). Fehlt bwrap,
    ``SandboxUnavailable``; zu viele parallele Läufe, ``SandboxBusy``.
    """
    limits = (limits or Limits()).clamped()
    if not _SLOTS.acquire(timeout=limits.wall_seconds):
        raise SandboxBusy
    try:
        return _run(code, limits)
    finally:
        _SLOTS.release()


def _run(code: str, limits: Limits, *, font_cache: bool = True) -> RunResult:
    program = seccomp_program()
    cache = _font_cache() if font_cache and "matplotlib" in code else None
    fds: list[int] = []
    read_end = None
    try:
        cache_arg = None
        if cache is not None:
            cache_arg = (cache[0], _memfd("fontcache", cache[1]))
            fds.append(cache_arg[1])
        runner_fd = _memfd("runner", RUNNER.encode())
        fds.append(runner_fd)
        code_fd = _memfd("code", code.encode("utf-8", errors="replace"))
        fds.append(code_fd)
        seccomp_fd = None
        if program is not None:
            seccomp_fd = _memfd("seccomp", program)
            fds.append(seccomp_fd)
        read_end, write_end = os.pipe()
        fds.append(write_end)
        args = command(limits, runner_fd, code_fd, write_end, seccomp_fd, cache_arg)
        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=tuple(fds),
                start_new_session=True,  # eigene Prozessgruppe für killpg
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            )
        except OSError as exc:
            raise SandboxUnavailable("bubblewrap konnte nicht gestartet werden.") from exc
    finally:
        for fd in fds:
            os.close(fd)
    try:
        return _collect(proc, read_end, limits, started)
    finally:
        _kill(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.error("Sandbox-Prozess %s reagiert nicht auf SIGKILL", proc.pid)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
        os.close(read_end)


def _collect(proc: subprocess.Popen, read_end: int, limits: Limits, started: float) -> RunResult:
    output_limit = limits.output_kb * 1024
    # Weit darüber hinaus: abbrechen statt endlos zu verwerfen.
    output_hard = max(4 * output_limit, 1024 * 1024)
    files_hard = limits.max_files * (limits.max_file_bytes * 4 // 3 + 1024) + 64 * 1024
    buffers = {"stdout": bytearray(), "stderr": bytearray(), "files": bytearray()}
    totals = dict.fromkeys(buffers, 0)
    caps = {"stdout": output_limit + 1, "stderr": output_limit + 1, "files": files_hard}
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ, "stdout")
    sel.register(proc.stderr, selectors.EVENT_READ, "stderr")
    sel.register(read_end, selectors.EVENT_READ, "files")
    deadline = started + limits.wall_seconds
    aborted = ""
    try:
        while sel.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                aborted = "timeout"
                break
            for key, _ in sel.select(timeout=min(remaining, 0.5)):
                name = key.data
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    sel.unregister(key.fileobj)
                    continue
                totals[name] += len(chunk)
                room = caps[name] - len(buffers[name])
                if room > 0:
                    buffers[name] += chunk[:room]
            if totals["stdout"] + totals["stderr"] > output_hard:
                aborted = "output"
                break
            if totals["files"] > files_hard:
                aborted = "output"
                buffers["files"].clear()
                break
    finally:
        sel.close()
    if aborted:
        _kill(proc)
    try:
        returncode = proc.wait(timeout=max(0.1, deadline - time.monotonic()) if not aborted else 5)
    except subprocess.TimeoutExpired:
        aborted = aborted or "timeout"
        _kill(proc)
        returncode = proc.wait(timeout=5)
    duration = time.monotonic() - started
    stdout, cut_out = _decode(bytes(buffers["stdout"]), output_limit)
    stderr, cut_err = _decode(bytes(buffers["stderr"]), output_limit)
    result = RunResult(
        stdout=stdout,
        stderr=stderr,
        truncated=cut_out or cut_err or aborted == "output",
        duration=duration,
    )
    if not aborted:
        # bwrap gibt den Status des Kindes weiter; Signal als 128 + Nummer.
        if returncode is not None and returncode > 128:
            result.signal = returncode - 128
            aborted = {
                signal.SIGXCPU: "cpu",
                signal.SIGXFSZ: "file_size",
            }.get(result.signal, "signal")
        elif returncode is not None and returncode < 0:
            result.signal = -returncode
            aborted = "signal"
        else:
            result.exit_code = returncode
    result.aborted = aborted
    if not aborted:
        result.files, result.skipped_files = _parse_files(bytes(buffers["files"]), limits)
    logger.info(
        "Sandbox-Lauf: %d ms, Exit %s, Abbruch %s, %d Datei(en)",
        int(duration * 1000),
        result.exit_code,
        aborted or "-",
        len(result.files),
    )
    return result


# --- Font-Cache von matplotlib -----------------------------------------------------
#
# Ohne Cache durchsucht matplotlib bei jedem Lauf alle Schriften (mehrere
# Sekunden). Der Cache wird einmal je Prozess in einer frischen Sandbox mit
# eigenem, festem Code erzeugt (nie aus einem Lauf mit fremdem Code) und
# danach jedem Lauf als Kopie ins tmpfs gelegt. Scheitert das, läuft es ohne.

FONT_CACHE_CODE = """
import base64, glob, json, os, zlib
import matplotlib.font_manager
path = sorted(glob.glob("/tmp/matplotlib/fontlist-*.json"))[0]
with open(path, "rb") as fh:
    data = zlib.compress(fh.read(), 9)
print(json.dumps({"name": os.path.basename(path), "data": base64.b64encode(data).decode()}))
"""
_font_cache_value: tuple[str, bytes] | None = None
_font_cache_done = False
_font_cache_lock = threading.Lock()


def _font_cache() -> tuple[str, bytes] | None:
    global _font_cache_value, _font_cache_done
    with _font_cache_lock:
        if _font_cache_done:
            return _font_cache_value
        _font_cache_done = True
        if not any(dest.endswith("/matplotlib") for _, dest in package_mounts()):
            return None
        try:
            limits = Limits(cpu_seconds=60, wall_seconds=90, output_kb=1024, max_files=0)
            result = _run(FONT_CACHE_CODE, limits.clamped(), font_cache=False)
            item = json.loads(result.stdout.strip().splitlines()[-1])
            name = os.path.basename(str(item["name"]))
            if not name.startswith("fontlist-") or not name.endswith(".json"):
                raise ValueError(name)
            _font_cache_value = (name, zlib.decompress(base64.b64decode(item["data"])))
        except Exception as exc:  # noqa: BLE001 - ohne Cache geht es auch, nur langsamer
            logger.warning("Font-Cache für die Sandbox fehlt (%s)", type(exc).__name__)
        return _font_cache_value


# --- Status und Selbsttest ----------------------------------------------------------


@dataclass(frozen=True)
class Status:
    available: bool
    reason: str = ""
    seccomp: bool = False
    packages: tuple[str, ...] = ()


_status_cache: tuple[float, Status] | None = None
_status_lock = threading.Lock()
STATUS_TTL_OK = 600.0
STATUS_TTL_FAILED = 60.0


def _probe() -> Status:
    if bwrap_path() is None:
        return Status(False, f"bubblewrap (bwrap) ist nicht installiert. {INSTALL_HINT}")
    if not python_path().startswith("/usr/"):
        return Status(False, "Der Python-Interpreter liegt nicht unter /usr.")
    found = {dest.rsplit("/", 1)[-1].removesuffix(".py") for _, dest in package_mounts()}
    missing = [name for name in REQUIRED_PACKAGES if name not in found]
    if missing:
        return Status(False, f"Rechenpakete fehlen: {', '.join(missing)}.")
    try:
        result = run("print(1 + 1)", Limits(wall_seconds=10, cpu_seconds=5))
    except SandboxBusy:
        return Status(False, "Sandbox ausgelastet, bitte erneut prüfen.")
    except (SandboxUnavailable, OSError) as exc:
        return Status(False, str(exc) or "bubblewrap konnte nicht gestartet werden.")
    if result.stdout.strip() != "2" or not result.ok:
        # stderr von bwrap nennt die Ursache (z. B. Namespaces gesperrt); ohne Pfade kürzen.
        detail = " ".join(result.stderr.split())[:300]
        return Status(False, f"Probelauf in bubblewrap fehlgeschlagen: {detail or 'keine Ausgabe'}")
    return Status(
        True,
        "",
        seccomp=seccomp_program() is not None,
        packages=tuple(sorted(found)),
    )


def status(*, refresh: bool = False) -> Status:
    """Sandbox nutzbar? Ergebnis der Probe wird zwischengespeichert
    (erfolgreich 10 min, fehlgeschlagen 1 min)."""
    global _status_cache
    with _status_lock:
        now = time.monotonic()
        if not refresh and _status_cache is not None and _status_cache[0] > now:
            return _status_cache[1]
        result = _probe()
        ttl = STATUS_TTL_OK if result.available else STATUS_TTL_FAILED
        _status_cache = (now + ttl, result)
        if not result.available:
            logger.warning("Sandbox nicht verfügbar")
        return result


def available() -> bool:
    return status().available


SELFTEST_CODE = """
import json, os, socket
r = {"calc": 1 + 1}
try:
    import numpy, sympy, mpmath
    r["libs"] = f"numpy {numpy.__version__}, sympy {sympy.__version__}, mpmath {mpmath.__version__}"
except Exception as exc:
    r["libs"] = None
try:
    import matplotlib
    r["mpl"] = matplotlib.__version__
except Exception:
    r["mpl"] = None
try:
    socket.create_connection(("1.1.1.1", 53), timeout=2).close()
    r["net"] = True
except OSError:
    r["net"] = False
seen = []
for path in PATHS:
    try:
        if os.path.isdir(path):
            os.listdir(path)
            seen.append(path)
        else:
            open(path, "rb").close()
            seen.append(path)
    except OSError:
        pass
r["seen"] = seen
try:
    open("/usr/sandbox-test", "w").close()
    r["write"] = True
except OSError:
    r["write"] = False
r["env"] = sorted(k for k in os.environ if k in SECRETS)
for mod in ("django", "multigpt"):
    try:
        __import__(mod)
        r.setdefault("imports", []).append(mod)
    except ImportError:
        pass
print(json.dumps(r))
"""

SECRET_ENV = ("SECRET_KEY", "DATABASE_URL", "FIELD_ENCRYPTION_KEY", "DJANGO_SETTINGS_MODULE")


def selftest() -> list[tuple[bool, str]]:
    """„Sandbox testen“ im Admin: Rechnung, Netz, Dateien, Umgebung.

    Liefert (ok, Text) je Prüfung; erzwingt eine neue Probe."""
    state = status(refresh=True)
    if not state.available:
        return [(False, f"Sandbox nicht verfügbar: {state.reason}")]
    from django.conf import settings

    paths = [
        "/etc/passwd",
        "/etc/multi-gpt/.env",
        "/var/lib/multi-gpt",
        "/home",
        "/proc/1/environ",
        "/run/postgresql",
        str(Path(settings.BASE_DIR)),
        str(Path(settings.BASE_DIR) / ".env"),
        str(settings.MEDIA_ROOT),
        sys.prefix,
    ]
    code = f"PATHS = {paths!r}\nSECRETS = {list(SECRET_ENV)!r}\n" + SELFTEST_CODE
    try:
        result = run(code, Limits(wall_seconds=20))
    except (SandboxBusy, SandboxUnavailable) as exc:
        return [(False, f"Sandbox-Test nicht möglich: {exc or 'ausgelastet'}")]
    try:
        data = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return [(False, "Sandbox-Test ohne verwertbares Ergebnis (Exit-Status "
                 f"{result.exit_code}, Abbruch {result.aborted or '-'}).")]  # fmt: skip
    checks = [
        (data.get("calc") == 2, f"print(1+1) ergibt {data.get('calc')} ({result.duration:.1f} s)."),
        (bool(data.get("libs")), data.get("libs") or "numpy/sympy/mpmath fehlen in der Sandbox."),
        (
            bool(data.get("mpl")),
            f"matplotlib {data['mpl']}"
            if data.get("mpl")
            else "matplotlib fehlt (keine Diagramme).",
        ),
        (not data.get("net"), "Netz gesperrt." if not data.get("net") else "NETZ ERREICHBAR!"),
        (
            not data.get("seen"),
            "Server-Dateien unsichtbar (/etc, /home, /var/lib/multi-gpt, Projekt, venv)."
            if not data.get("seen")
            else "SICHTBAR: " + ", ".join(data["seen"]),
        ),
        (not data.get("write"), "Schreiben außerhalb des Arbeitsordners verboten."),
        (
            not data.get("env"),
            "Keine Umgebungsvariablen des Servers."
            if not data.get("env")
            else "UMGEBUNG SICHTBAR: " + ", ".join(data["env"]),
        ),
        (
            not data.get("imports"),
            "Server-Code (Django, MultiGPT) nicht importierbar."
            if not data.get("imports")
            else "IMPORTIERBAR: " + ", ".join(data["imports"]),
        ),
        (
            True,
            "seccomp-Filter aktiv."
            if state.seccomp
            else f"Ohne seccomp-Filter (Architektur {platform.machine()} nicht unterstützt).",
        ),
    ]
    return checks
