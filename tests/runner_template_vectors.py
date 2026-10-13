"""Gemeinsame Testvektoren der festen Vorlage (Python ↔ Rust, rs-e2e).

Orakel ist ``runner_agent/template.py``. Die Fälle stehen in
``runner-rs/testdata/template/<name>.json``::

    python -m tests.runner_template_vectors      # Dateien neu schreiben

Format je Datei::

    {"check": "body" | "network" | "inspect" | "limits" | "image",
     "config": {"images": [...], "scratch_host_dir": null | "...",
                "max_cpus": 8.0, "max_memory_mb": ..., "max_pids": ..., "max_tmpfs_mb": ...},
     "expect": "work" | "relay" | "tool" | "ok" | "reject",
     "note": "...",
     # je nach check:
     "body": {...}                                  # body, network (libpod-Körper)
     "info": {...}, "expected": {cpus, memory_mb, pids, tmpfs_mb, network},
     "preview_port": 18080 | null, "scratch_source": null | "..."   # inspect
     "limits": {...}                                # limits (Werte des Servers)
     "image": "...", "server_images": [...] | null} # image

``body``: ``check_body`` liefert die Rolle (``expect`` = work/relay/tool) oder
lehnt ab (``reject``). ``network``: ``check_network_body``. ``inspect``:
``check_inspect`` meldet keine (``ok``) bzw. mindestens eine Abweichung
(``reject``). ``limits``: ``check_limits``. ``image``: ``check_image`` gegen
``server_images`` und ``config.images``.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from runner_agent import template as T
from tests.test_runner_agent import (
    IMAGE,
    OTHER_IMAGE,
    SCRATCH_UUID,
    _inspect,
    _limits,
    _local_body,
    _mutations,
    _work,
    make_config,
)

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = ROOT / "runner-rs" / "testdata" / "template"
HOST_DIR = "/home/user/MultiGPT/Scratch"

# Fälle, die Python (noch) anders entscheidet als vorgesehen (xfail strict im
# Python-Test; Rust soll ablehnen).
KNOWN_PYTHON_DEVIATIONS = {
    "tmpfs_option_trailing_newline": "re.match mit $ lässt ein abschließendes \\n zu",
    "scratch_uuid_trailing_newline": "re.match mit $ lässt ein abschließendes \\n zu",
    "relay_target_trailing_newline": "re.match mit $ lässt ein abschließendes \\n zu",
}


def _config_dict(cfg) -> dict:
    return {
        "images": sorted(cfg.images),
        "scratch_host_dir": cfg.scratch_host_dir,
        "max_cpus": float(cfg.max_cpus),
        "max_memory_mb": cfg.max_memory_mb,
        "max_pids": cfg.max_pids,
        "max_tmpfs_mb": cfg.max_tmpfs_mb,
    }


def _mutated(body: dict, mutate) -> dict:
    body = copy.deepcopy(body)
    mutate(body)
    return body


def _work_with(**kw):
    """Arbeitscontainer mit anderer Netzart bzw. ohne Vorschau-Port."""
    port = kw.pop("preview_port", 18080)
    scratch = kw.pop("scratch", None)
    limits = _limits(**kw)
    return T.work_body(
        "42", "7", IMAGE, limits, T.network_name("42"), port, {"limits": limits.as_dict()}, scratch
    )


def body_cases():
    """(Name, Konfiguration, Körper, Erwartung, Zweck)."""
    cfg = make_config()
    local_cfg = make_config(scratch_host_dir=HOST_DIR)
    work = _work()
    relay = T.relay_body("42", IMAGE, [("10.0.0.5", 22), ("nas.local", 443)])
    tool = T.tool_body("42", "c1", IMAGE)
    local = _local_body(local_cfg)
    yield "work_none_preview", cfg, work, "work", "Arbeitscontainer, Netz aus, Vorschau-Port"
    yield "work_no_port", cfg, _work_with(preview_port=None), "work", "ohne Vorschau-Port"
    yield "work_allowlist", cfg, _work_with(network="allowlist"), "work", "Positivliste"
    yield "work_default_net", cfg, _work_with(network="default"), "work", "Netz an (eigene DNS)"
    yield (
        "work_scratch_container",
        cfg,
        _work_with(scratch=T.Scratch("container", 512)),
        "work",
        "Scratchpad als tmpfs",
    )
    yield "work_scratch_local", local_cfg, local, "work", "lokales Scratchpad (Bind)"
    yield "work_preview_last_port", cfg, _work_with(preview_port=18089), "work", "Port 18089"
    yield "relay", cfg, relay, "relay", "Relay der Positivliste"
    yield "tool", cfg, tool, "tool", "Hilfscontainer ssh/telnet"
    for name, mutate in _mutations():
        yield f"work_{name}", cfg, _mutated(work, mutate), "reject", "Mutation der Vorlage"
    local_mutations = {
        "local_source_home": lambda b: b["mounts"][-1].update(source="/home/user"),
        "local_source_dotdot": lambda b: b["mounts"][-1].update(source=f"{HOST_DIR}/../.ssh"),
        "local_options_exec": lambda b: b["mounts"][-1].update(options=["rbind", "rw"]),
        "local_dest_workspace": lambda b: b["mounts"][-1].update(destination="/workspace"),
        "local_no_userns": lambda b: b.pop("userns"),
        "local_userns_host": lambda b: b.update(userns={"nsmode": "host"}),
        "scratch_uuid_trailing_newline": lambda b: b["mounts"][-1].update(
            source=f"{HOST_DIR}/{SCRATCH_UUID}\n"
        ),
        "local_source_uppercase_uuid": lambda b: b["mounts"][-1].update(
            source=f"{HOST_DIR}/{SCRATCH_UUID.upper()}"
        ),
    }
    for name, mutate in local_mutations.items():
        yield name, local_cfg, _mutated(local, mutate), "reject", "lokales Scratchpad"
    yield "local_without_host_dir", cfg, local, "reject", "ohne eingerichteten Ordner kein Bind"
    yield (
        "userns_without_local",
        cfg,
        _mutated(work, lambda b: b.update(userns=dict(T.KEEP_ID))),
        "reject",
        "userns nur mit Bind",
    )
    extra = {
        "tmpfs_exec": lambda b: b["mounts"][0]["options"].append("exec"),
        "tmpfs_option_trailing_newline": lambda b: b["mounts"][0]["options"].__setitem__(
            0, b["mounts"][0]["options"][0] + "\n"
        ),
        "tmpfs_no_size": lambda b: b["mounts"][0].update(options=["mode=0700", "nosuid", "nodev"]),
        "mount_extra_field": lambda b: b["mounts"][0].update(propagation="shared"),
        "mount_duplicate_dest": lambda b: b["mounts"].append(copy.deepcopy(b["mounts"][1])),
        "no_mounts": lambda b: b.update(mounts=[]),
        "cpu_quota_over_max": lambda b: b["resource_limits"]["cpu"].update(quota=9 * 100_000),
        "cpu_period": lambda b: b["resource_limits"]["cpu"].update(period=50_000),
        "memory_over_max": lambda b: b["resource_limits"]["memory"].update(
            limit=(16384 + 1) << 20, swap=(16384 + 1) << 20
        ),
        "rlimit_rtprio": lambda b: b["r_limits"].append({"type": "rtprio", "soft": 1, "hard": 1}),
        "rlimit_no_fsize": lambda b: b.update(
            r_limits=[r for r in b["r_limits"] if r["type"] != "fsize"]
        ),
        "rlimit_soft_over_hard": lambda b: b["r_limits"][0].update(soft=10, hard=5),
        "port_udp": lambda b: b["portmappings"][0].update(protocol="udp"),
        "port_extra_field": lambda b: b["portmappings"][0].update(range=2),
        "network_options": lambda b: b["Networks"][T.network_name("42")].update(
            static_ips=["10.0.0.9"]
        ),
        "two_networks": lambda b: b["Networks"].update({T.network_name("42", "out"): {}}),
        "relay_network_for_work": lambda b: b.update(Networks={T.network_name("42", "rel"): {}}),
        "env_extra": lambda b: b["env"].update(LD_PRELOAD="/tmp/x.so"),
        "label_not_string": lambda b: b["labels"].update(x=1),
        "role_unknown": lambda b: b["labels"].update({T.LABEL_ROLE: "admin"}),
        "name_other_session": lambda b: b.update(name=T.container_name("43")),
        "init_false": lambda b: b.update(init=False),
        "httpproxy": lambda b: b.update(httpproxy=True),
        "hosts_from_host": lambda b: b.update(use_image_hosts=False),
        "netns_none": lambda b: b.update(netns={"nsmode": "none"}),
        "cgroupns_host": lambda b: b.update(cgroupns={"nsmode": "host"}),
        "utsns_host": lambda b: b.update(utsns={"nsmode": "host"}),
        "image_volume_bind": lambda b: b.update(image_volume_mode="bind"),
    }
    for name, mutate in extra.items():
        yield (
            f"work_{name}" if not name.endswith("newline") else name,
            cfg,
            _mutated(work, mutate),
            "reject",
            "weitere Mutation",
        )
    relay_mutations = {
        "relay_shell": lambda b: b.update(command=["sh", "-c", "nc -l 1"]),
        "relay_default_net": lambda b: b["Networks"].update(podman={}),
        "relay_port": lambda b: b.update(
            portmappings=[
                {
                    "host_ip": "127.0.0.1",
                    "host_port": 18080,
                    "container_port": 8080,
                    "protocol": "tcp",
                }
            ]
        ),
        "relay_no_targets": lambda b: b.update(command=["mgpt-relay", "--base", "20000"]),
        "relay_too_many_targets": lambda b: b.update(
            command=["mgpt-relay", "--base", "20000", *[f"h{i}:1" for i in range(51)]]
        ),
        "relay_base_port": lambda b: b["command"].__setitem__(2, "21000"),
        "relay_target_trailing_newline": lambda b: b["command"].__setitem__(3, "10.0.0.5:22\n"),
        "relay_target_option": lambda b: b["command"].__setitem__(3, "--exec=sh"),
        "relay_work_network": lambda b: b["Networks"].update({T.network_name("42"): {}}),
    }
    for name, mutate in relay_mutations.items():
        yield name, cfg, _mutated(relay, mutate), "reject", "Relay"
    tool_mutations = {
        "tool_work_network": lambda b: b["Networks"].update({T.network_name("42", "net"): {}}),
        "tool_workspace_mount": lambda b: b["mounts"].append(
            T._tmpfs("/workspace", 16, "0700", True)
        ),
        "tool_command": lambda b: b.update(command=["ssh", "x"]),
        "tool_name_other_call": lambda b: b.update(name=T.container_name("42", "tool", "other")),
        "tool_port": lambda b: b.update(
            portmappings=[
                {
                    "host_ip": "127.0.0.1",
                    "host_port": 18080,
                    "container_port": 8080,
                    "protocol": "tcp",
                }
            ]
        ),
        "tool_out_network": lambda b: b.update(Networks={T.network_name("42", "out"): {}}),
    }
    for name, mutate in tool_mutations.items():
        yield name, cfg, _mutated(tool, mutate), "reject", "Hilfscontainer"


def network_cases():
    def nb(name, internal, labels=None):
        return T.network_body(name, internal=internal, labels=labels or {})

    net, rel, out = (T.network_name("42", k) for k in ("net", "rel", "out"))
    yield "net_internal", nb(net, True), "ok", "Sitzungsnetz intern"
    yield "net_default", nb(net, False, {T.LABEL_NETWORK: "default"}), "ok", "Netz an"
    yield "rel_internal", nb(rel, True), "ok", "Relay-Netz intern"
    yield "out_external", nb(out, False), "ok", "Ausgang"
    yield "net_external", nb(net, False), "reject", "Netz aus muss intern sein"
    yield "rel_external", nb(rel, False), "reject", "Relay-Netz muss intern sein"
    yield "net_dns", dict(nb(net, True), dns_enabled=True), "reject", "kein DNS"
    yield "net_name_podman", nb("podman", True), "reject", "fremdes Netz"
    yield "net_driver_macvlan", dict(nb(net, True), driver="macvlan"), "reject", "nur bridge"
    yield "net_extra_option", dict(nb(net, True), options={"mtu": "9000"}), "reject", "Option"
    yield "net_no_label", dict(nb(net, True), labels={}), "reject", "Label fehlt"


def inspect_cases():
    expected = _limits().as_dict()
    yield "inspect_ok", _inspect(), expected, 18080, None, "ok", "wie Soll"
    bad = {
        "memory": {"Memory": 1 << 30, "MemorySwap": 1 << 30},
        "swap": {"MemorySwap": -1},
        "cpu": {"CpuQuota": 300000},
        "pids": {"PidsLimit": 4096},
        "privileged": {"Privileged": True},
        "rw_root": {"ReadonlyRootfs": False},
        "cap_add": {"CapAdd": ["NET_ADMIN"]},
        "no_nnp": {"SecurityOpt": []},
        "pid_host": {"PidMode": "host"},
        "net_host": {"NetworkMode": "host"},
        "devices": {"Devices": [{"PathOnHost": "/dev/kvm"}]},
        "binds": {"Binds": ["/home:/x"]},
        "tmpfs_extra": {
            "Tmpfs": {"/workspace": "size=128m", "/tmp": "size=256m", "/etc": "size=1m"}
        },
        "tmpfs_size": {"Tmpfs": {"/workspace": "size=256m", "/tmp": "size=256m"}},
        "port_all": {"PortBindings": {"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "18080"}]}},
        "port_other": {"PortBindings": {"22/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18080"}]}},
        "port_number": {
            "PortBindings": {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18081"}]}
        },
    }
    for name, host in bad.items():
        yield f"inspect_{name}", _inspect(**host), expected, 18080, None, "reject", "Abweichung"
    info = _inspect()
    info["Config"]["User"] = "0:0"
    yield "inspect_root_user", info, expected, 18080, None, "reject", "Nutzer root"
    info = _inspect()
    info["Mounts"] = [{"Type": "bind", "Source": "/home", "Destination": "/x", "Options": []}]
    yield "inspect_mount", info, expected, 18080, None, "reject", "fremder Mount"
    source = f"{HOST_DIR}/{SCRATCH_UUID}"
    info = _inspect(Binds=[f"{source}:/scratch:rw,noexec,nosuid,nodev"])
    info["Mounts"] = [
        {
            "Type": "bind",
            "Source": source,
            "Destination": "/scratch",
            "Options": ["rbind", "noexec", "nosuid", "nodev"],
        }
    ]
    yield "inspect_local_scratch", info, expected, 18080, source, "ok", "lokales Scratchpad"
    info2 = copy.deepcopy(info)
    info2["Mounts"][0]["Options"] = ["rbind"]
    yield "inspect_local_scratch_exec", info2, expected, 18080, source, "reject", "ohne noexec"
    yield "inspect_local_scratch_unexpected", info, expected, 18080, None, "reject", "ohne Soll"
    yield "inspect_no_port", _inspect(PortBindings={}), expected, None, None, "ok", "ohne Port"


def limits_cases():
    cfg = make_config(max_cpus=2, max_memory_mb=1024)
    yield "limits_at_max", cfg, _limits(cpus=2, memory_mb=1024), "ok", "genau die Obergrenze"
    yield "limits_cpus_over", cfg, _limits(cpus=4), "reject", "CPU über lokaler Grenze"
    yield "limits_memory_over", cfg, _limits(memory_mb=4096), "reject", "RAM über lokaler Grenze"
    yield "limits_cpus_low", cfg, _limits(cpus=0.05), "reject", "CPU unter 0,1"
    yield "limits_pids_low", cfg, _limits(pids=8), "reject", "pids unter 16"
    yield "limits_tmpfs_over", make_config(), _limits(tmpfs_mb=8193), "reject", "tmpfs zu groß"


def image_cases():
    cfg = make_config()
    yield "image_both_lists", cfg, IMAGE, [IMAGE], "ok", "lokal und Server"
    yield "image_server_none", cfg, IMAGE, None, "ok", "ohne Serverliste (Prüfung der Vorlage)"
    yield "image_not_local", cfg, OTHER_IMAGE, [OTHER_IMAGE], "reject", "nur Server erlaubt"
    yield "image_not_server", cfg, IMAGE, [OTHER_IMAGE], "reject", "nur lokal erlaubt"
    yield "image_tag", cfg, "ghcr.io/x/y:latest", ["ghcr.io/x/y:latest"], "reject", "ohne Digest"


def build() -> dict[str, dict]:
    """Alle Fälle als {Dateiname: Inhalt}; prüft dabei gegen template.py."""
    out: dict[str, dict] = {}

    def add(name, entry):
        assert f"{name}.json" not in out, name
        out[f"{name}.json"] = entry

    for name, cfg, body, expect, note in body_cases():
        add(
            name,
            {
                "check": "body",
                "config": _config_dict(cfg),
                "body": body,
                "expect": expect,
                "note": note,
            },
        )
    for name, body, expect, note in network_cases():
        add(
            name,
            {
                "check": "network",
                "config": _config_dict(make_config()),
                "body": body,
                "expect": expect,
                "note": note,
            },
        )
    for name, info, expected, port, source, expect, note in inspect_cases():
        add(
            name,
            {
                "check": "inspect",
                "config": _config_dict(make_config()),
                "info": info,
                "expected": expected,
                "preview_port": port,
                "scratch_source": source,
                "expect": expect,
                "note": note,
            },
        )
    for name, cfg, limits, expect, note in limits_cases():
        add(
            name,
            {
                "check": "limits",
                "config": _config_dict(cfg),
                "limits": limits.as_dict(),
                "expect": expect,
                "note": note,
            },
        )
    for name, cfg, image, server, expect, note in image_cases():
        add(
            name,
            {
                "check": "image",
                "config": _config_dict(cfg),
                "image": image,
                "server_images": server,
                "expect": expect,
                "note": note,
            },
        )
    return out


class _Cfg:
    """Konfiguration aus einem Vektor (nur die Felder, die template.py liest)."""

    def __init__(self, data: dict):
        self.images = frozenset(data["images"])
        self.scratch_host_dir = data["scratch_host_dir"]
        self.max_cpus = data["max_cpus"]
        self.max_memory_mb = data["max_memory_mb"]
        self.max_pids = data["max_pids"]
        self.max_tmpfs_mb = data["max_tmpfs_mb"]


def evaluate(entry: dict) -> str:
    """Python-Ergebnis eines Vektors: Rolle, ``ok`` oder ``reject``."""
    cfg = _Cfg(entry["config"])
    check = entry["check"]
    try:
        if check == "body":
            return T.check_body(copy.deepcopy(entry["body"]), cfg)
        if check == "network":
            T.check_network_body(copy.deepcopy(entry["body"]))
        elif check == "inspect":
            problems = T.check_inspect(
                entry["info"],
                T.Limits(**entry["expected"]),
                entry["preview_port"],
                entry["scratch_source"],
            )
            return "reject" if problems else "ok"
        elif check == "limits":
            T.check_limits(T.Limits(**entry["limits"]), cfg)
        elif check == "image":
            T.check_image(entry["image"], entry["server_images"], cfg.images)
        else:
            raise AssertionError(check)
    except T.TemplateError:
        return "reject"
    except (KeyError, TypeError, AttributeError):
        # Python stolpert über die Form (z. B. fehlender Schlüssel) – zählt als Ablehnung,
        # die Vorlage wird ja nicht angewandt.
        return "reject"
    return "ok"


def render(entry: dict) -> str:
    return json.dumps(entry, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def write(root: Path = TEMPLATE_DIR) -> int:
    root.mkdir(parents=True, exist_ok=True)
    for old in root.glob("*.json"):
        old.unlink()
    files = build()
    for name, entry in files.items():
        (root / name).write_text(render(entry), encoding="utf-8")
    return len(files)


if __name__ == "__main__":
    print(f"{write()} Vorlagen-Vektoren nach {TEMPLATE_DIR}")
