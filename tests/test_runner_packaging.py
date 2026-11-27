"""Paketierung des Runners (M14-07/13): Unit, debian/rules, Images.

Die nginx- und postinst-Logik prüft ``make test-packaging`` (tests/packaging/);
hier nur, was ohne nginx und ohne Datenbank geht.
"""

import configparser
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UNIT = ROOT / "debian" / "multi-gpt.multi-gpt-runner.service"


def _unit() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read(UNIT, encoding="utf-8")
    return parser


def test_unit_starts_runner_gateway_hardened():
    service = _unit()["Service"]
    assert service["ExecStart"] == (
        "/usr/share/python/multi-gpt/bin/python -P -m django runner_gateway"
    )
    assert service["Type"] == "simple"
    assert service["User"] == "multi-gpt"
    assert service["EnvironmentFile"] == "/etc/multi-gpt/.env"
    assert service["KillMode"] == "mixed"
    assert service["TimeoutStopSec"] == "15"
    assert service["Restart"] == "on-failure"
    assert service["RestrictAddressFamilies"] == "AF_UNIX AF_INET AF_INET6"
    for key in ("NoNewPrivileges", "PrivateTmp", "ProtectHome", "ProtectKernelTunables"):
        assert service[key] == "yes", key
    assert service["ProtectSystem"] == "strict"
    assert _unit()["Install"]["WantedBy"] == "multi-user.target"


def test_worker_hardening_is_subset_of_runner():
    """Was der Worker an Härtung hat, hat der Runner-Dienst auch."""
    worker = configparser.ConfigParser(strict=False, interpolation=None)
    worker.optionxform = str
    worker.read(ROOT / "debian" / "multi-gpt.multi-gpt-worker.service", encoding="utf-8")
    runner = _unit()["Service"]
    for key, value in worker["Service"].items():
        if key.startswith(("Protect", "Restrict", "Private", "NoNew", "Lock", "SystemCall")):
            assert runner.get(key) == value, key


def test_rules_install_runner_unit():
    rules = (ROOT / "debian" / "rules").read_text(encoding="utf-8")
    assert "dh_installsystemd --name=multi-gpt-runner" in rules
    assert "usr/share/multi-gpt/runner-images.env" in rules


def test_pyproject_runner_dependencies():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = {d.split(">")[0].split("=")[0].split("[")[0] for d in data["project"]["dependencies"]}
    assert {"websockets", "uvicorn", "starlette"} <= deps
    assert "podman" in " ".join(data["tool"]["pytest"]["ini_options"]["markers"])


def test_containerfiles_copy_agent_helpers():
    work = (ROOT / "containers" / "work" / "Containerfile").read_text(encoding="utf-8")
    # mgpt-serve im Image ist die lighttpd-Hülle (mgpt_serve.py bleibt für die FakeEngine).
    for name in ("mgpt_fs", "mgpt_lighttpd", "mgpt_relay"):
        assert f"containers/work/{name}.py" in work
        assert (ROOT / "containers" / "work" / f"{name}.py").is_file()
    assert "containers/work/mgpt_lighttpd.py /usr/local/bin/mgpt-serve" in work
    for package in ("lighttpd", "lighttpd-mod-openssl", "curl", "jq", "ripgrep", "wget"):
        assert f"\n        {package} \\" in work
    assert "build-essential" not in work  # Compiler nur über apt_install
    assert "EXPOSE 8080 8443" in work
    assert "USER 1000:1000" in work
    assert not (ROOT / "containers" / "agent").exists()  # Python-Agent entfernt
