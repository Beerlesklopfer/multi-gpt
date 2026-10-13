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


PROXY_URL = "http://10.89.3.2:3128"
PACKAGE_HOSTS = [
    "deb.debian.org",
    "security.debian.org",
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
    "github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
    "index.crates.io",
    "static.crates.io",
]


def _pkg_work(scratch=None, preview_port=18080, proxy_url=PROXY_URL):
    """Arbeitscontainer im Modus Paketquellen (gelockerte Vorlage)."""
    limits = _limits(network="packages")
    return T.work_body(
        "42",
        "7",
        IMAGE,
        limits,
        T.network_name("42"),
        preview_port,
        {"limits": limits.as_dict()},
        scratch,
        proxy_url,
    )


def _set_env(**values):
    def mutate(body):
        body["env"].update(values)

    return mutate


def _set_proxy(url):
    return _set_env(**{key: url for key in T.PROXY_ENV_KEYS})


def _drop_env(*keys):
    def mutate(body):
        for key in keys:
            body["env"].pop(key)

    return mutate


def packages_body_cases():
    """Modus Paketquellen (pkg-e2e): gelockerte Vorlage nur dort und nur so weit wie vorgesehen."""
    cfg = make_config()
    local_cfg = make_config(scratch_host_dir=HOST_DIR)
    work = _pkg_work()
    proxy = T.proxy_body("42", IMAGE, PACKAGE_HOSTS)
    yield "pkg_work", cfg, work, "work", "Paketquellen: Schicht beschreibbar, 6 Capabilities, Proxy"
    yield "pkg_work_no_port", cfg, _pkg_work(preview_port=None), "work", "ohne Vorschau-Port"
    yield (
        "pkg_work_scratch_local",
        local_cfg,
        _pkg_work(
            scratch=T.Scratch("local", 1024, f"{HOST_DIR}/{SCRATCH_UUID}"), preview_port=None
        ),
        "work",
        "Paketquellen mit lokalem Scratchpad",
    )
    yield "pkg_proxy", cfg, proxy, "relay", "Proxy der Paketquellen (Sitzungsnetz + Ausgang)"
    yield (
        "pkg_proxy_one_host",
        cfg,
        T.proxy_body("42", IMAGE, ["deb.debian.org"]),
        "relay",
        "ein Host",
    )
    yield (
        "pkg_proxy_max_hosts",
        cfg,
        T.proxy_body("42", IMAGE, [f"h{i}.example.org" for i in range(T.MAX_PACKAGE_HOSTS)]),
        "relay",
        "genau 50 Hosts",
    )

    # Alle Mutationen der festen Vorlage gelten auch hier (außer der beschreibbaren Schicht).
    for name, mutate in _mutations():
        if name != "rw_root":
            yield (
                f"pkg_work_{name}",
                cfg,
                _mutated(work, mutate),
                "reject",
                "Mutation (Paketquellen)",
            )

    caps = T.CAPS_PACKAGES
    work_mutations = {
        # Capabilities: genau die sechs, keine weitere, keine fehlt, feste Schreibweise
        "cap_sys_admin": lambda b: b["cap_add"].append("SYS_ADMIN"),
        "cap_net_admin": lambda b: b["cap_add"].append("NET_ADMIN"),
        "cap_net_raw": lambda b: b["cap_add"].append("NET_RAW"),
        "cap_sys_ptrace": lambda b: b["cap_add"].append("SYS_PTRACE"),
        "cap_mknod": lambda b: b["cap_add"].append("MKNOD"),
        "cap_all": lambda b: b.update(cap_add=["ALL"]),
        "cap_replaced": lambda b: b["cap_add"].__setitem__(0, "SYS_CHROOT"),
        "cap_missing": lambda b: b["cap_add"].pop(),
        "cap_empty": lambda b: b.update(cap_add=[]),
        "cap_absent": lambda b: b.pop("cap_add"),
        "cap_duplicate": lambda b: b["cap_add"].append("CHOWN"),
        "cap_order": lambda b: b.update(cap_add=list(reversed(caps))),
        "cap_prefix": lambda b: b.update(cap_add=[f"CAP_{c}" for c in caps]),
        "cap_lowercase": lambda b: b.update(cap_add=[c.lower() for c in caps]),
        "cap_drop_empty": lambda b: b.update(cap_drop=[]),
        "cap_drop_absent": lambda b: b.pop("cap_drop"),
        # Schicht: im Modus Paketquellen beschreibbar (schreibgeschützt passt nicht zum Soll)
        "readonly_root": lambda b: b.update(read_only_filesystem=True),
        "readonly_absent": lambda b: b.pop("read_only_filesystem"),
        # weitere Rechte und Namensräume
        "privileged_too": lambda b: b.update(privileged=True),
        "nnp_absent": lambda b: b.pop("no_new_privileges"),
        "uts_host": lambda b: b.update(utsns={"nsmode": "host"}),
        "cgroupns_host": lambda b: b.update(cgroupns={"nsmode": "host"}),
        "userns_keep_id": lambda b: b.update(userns=dict(T.KEEP_ID)),
        "volume_mount_var": lambda b: b["mounts"].append(
            {"destination": "/var", "type": "tmpfs", "source": "tmpfs", "options": ["size=1m"]}
        ),
        # Netze: nur das interne Sitzungsnetz, keine fremden Netze, nie direkt ins Internet
        "net_out": lambda b: b.update(Networks={T.network_name("42", "out"): {}}),
        "net_plus_out": lambda b: b["Networks"].update({T.network_name("42", "out"): {}}),
        "net_rel": lambda b: b.update(Networks={T.network_name("42", "rel"): {}}),
        "net_podman": lambda b: b.update(Networks={"podman": {}}),
        "net_other_session": lambda b: b.update(Networks={T.network_name("43"): {}}),
        "net_slirp": lambda b: b.update(netns={"nsmode": "slirp4netns"}),
        "net_pasta": lambda b: b.update(netns={"nsmode": "pasta"}),
        "net_static_ip": lambda b: b["Networks"][T.network_name("42")].update(
            static_ips=["192.168.1.10"]
        ),
        "host_dns_packages": lambda b: b.update(use_image_resolve_conf=False),
        "httpproxy_host": lambda b: b.update(httpproxy=True),
        # Proxy-Umgebung: genau die vorgesehenen Schlüssel, eine Adresse, Port 3128
        "proxy_env_missing": _drop_env("HTTPS_PROXY"),
        "proxy_env_lower_missing": _drop_env("https_proxy"),
        "proxy_env_all_missing": _drop_env(*T.PROXY_ENV_KEYS, "NO_PROXY", "no_proxy"),
        "proxy_env_mismatch": _set_env(HTTPS_PROXY="http://10.89.3.3:3128"),
        "proxy_hostname": _set_proxy("http://proxy:3128"),
        "proxy_port": _set_proxy("http://10.89.3.2:3129"),
        "proxy_https_scheme": _set_proxy("https://10.89.3.2:3128"),
        "proxy_credentials": _set_proxy("http://user:pw@10.89.3.2:3128"),
        "proxy_trailing_newline": _set_proxy("http://10.89.3.2:3128\n"),
        "proxy_path": _set_proxy("http://10.89.3.2:3128/x"),
        "proxy_not_string": _set_proxy(3128),
        "no_proxy_home_net": _set_env(NO_PROXY="localhost,127.0.0.1,::1,192.168.0.0/16"),
        "no_proxy_all": _set_env(NO_PROXY="*", no_proxy="*"),
        "no_proxy_missing": _drop_env("no_proxy"),
        "env_all_proxy": _set_env(ALL_PROXY="socks5://10.89.3.2:1080"),
        "env_pip_index": _set_env(PIP_INDEX_URL="http://evil.example/simple"),
        "env_ld_preload": _set_env(LD_PRELOAD="/tmp/x.so"),
        "env_path": _set_env(PATH="/tmp:/usr/bin"),
        # Kennzeichnung: gelockerte Vorlage nur mit Label packages
        "label_none": lambda b: b["labels"].update({T.LABEL_NETWORK: "none"}),
        "label_default": lambda b: b["labels"].update({T.LABEL_NETWORK: "default"}),
        "label_allowlist": lambda b: b["labels"].update({T.LABEL_NETWORK: "allowlist"}),
        "label_missing_network": lambda b: b["labels"].pop(T.LABEL_NETWORK),
        "label_uppercase": lambda b: b["labels"].update({T.LABEL_NETWORK: "PACKAGES"}),
        "role_relay": lambda b: b["labels"].update({T.LABEL_ROLE: "relay"}),
        "role_tool": lambda b: b["labels"].update({T.LABEL_ROLE: "tool"}),
    }
    for name, mutate in work_mutations.items():
        yield f"pkg_work_{name}", cfg, _mutated(work, mutate), "reject", "Paketquellen"

    # Ohne Modus Paketquellen bleibt alles fest: keine beschreibbare Schicht, keine Capabilities.
    plain = _work()
    for network in ("none", "allowlist", "default"):
        base = _work_with(network=network)
        yield (
            f"plain_{network}_rw_root",
            cfg,
            _mutated(base, lambda b: b.update(read_only_filesystem=False)),
            "reject",
            "beschreibbare Schicht nur mit Paketquellen",
        )
        yield (
            f"plain_{network}_caps",
            cfg,
            _mutated(base, lambda b: b.update(cap_add=list(caps))),
            "reject",
            "Capabilities nur mit Paketquellen",
        )
        yield (
            f"plain_{network}_relaxed",
            cfg,
            _mutated(base, lambda b: b.update(read_only_filesystem=False, cap_add=list(caps))),
            "reject",
            "ganze Lockerung ohne Label packages",
        )
    yield (
        "plain_cap_add_empty",
        cfg,
        _mutated(plain, lambda b: b.update(cap_add=[])),
        "reject",
        "cap_add nur im Modus Paketquellen (auch leer nicht)",
    )
    yield (
        "plain_proxy_env",
        cfg,
        _mutated(plain, _set_proxy(PROXY_URL)),
        "reject",
        "Proxy-Umgebung nur mit Paketquellen",
    )
    yield (
        "plain_readonly_absent",
        cfg,
        _mutated(plain, lambda b: b.pop("read_only_filesystem")),
        "reject",
        "Schreibschutz fehlt",
    )
    relay = T.relay_body("42", IMAGE, [("10.0.0.5", 22)])
    tool = T.tool_body("42", "c1", IMAGE)
    for prefix, body in (("relay", relay), ("tool", tool), ("pkg_proxy", proxy)):
        yield (
            f"{prefix}_rw_root",
            cfg,
            _mutated(body, lambda b: b.update(read_only_filesystem=False)),
            "reject",
            "Hilfscontainer immer schreibgeschützt",
        )
        yield (
            f"{prefix}_caps_packages",
            cfg,
            _mutated(body, lambda b: b.update(cap_add=list(caps))),
            "reject",
            "Hilfscontainer ohne Capabilities",
        )
        yield (
            f"{prefix}_label_packages_relaxed",
            cfg,
            _mutated(
                body,
                lambda b: (
                    b["labels"].update({T.LABEL_NETWORK: "packages"}),
                    b.update(read_only_filesystem=False, cap_add=list(caps)),
                ),
            ),
            "reject",
            "Label packages lockert nur den Arbeitscontainer",
        )

    def proxy_command(*hosts):
        return lambda b: b.update(command=["mgpt-relay", "--proxy", "3128", *hosts])

    proxy_mutations = {
        "ip_literal": proxy_command("10.0.0.5"),
        "ip_literal_public": proxy_command("93.184.215.14"),
        "ipv6_literal": proxy_command("[::1]"),
        "home_net": proxy_command("192.168.1.1"),
        "host_with_port": proxy_command("pypi.org:443"),
        "wildcard": proxy_command("*.debian.org"),
        "uppercase": proxy_command("PyPI.org"),
        "single_label": proxy_command("localhost"),
        "trailing_dot": proxy_command("pypi.org."),
        "trailing_newline": proxy_command("pypi.org\n"),
        "option_as_host": proxy_command("--allow-all"),
        "url_as_host": proxy_command("https://pypi.org"),
        "numeric_tld": proxy_command("host.123"),
        "label_too_long": proxy_command("a" * 64 + ".org"),
        "no_hosts": proxy_command(),
        "too_many_hosts": proxy_command(*[f"h{i}.example.org" for i in range(51)]),
        "port_other": lambda b: b["command"].__setitem__(2, "3129"),
        "relay_targets": proxy_command("10.0.0.5:22"),
        "shell": lambda b: b.update(command=["sh", "-c", "mgpt-relay --proxy 3128 pypi.org"]),
        "rel_network": lambda b: b.update(
            Networks={T.network_name("42", "rel"): {}, T.network_name("42", "out"): {}}
        ),
        "only_out": lambda b: b.update(Networks={T.network_name("42", "out"): {}}),
        "only_net": lambda b: b.update(Networks={T.network_name("42", "net"): {}}),
        "extra_net": lambda b: b["Networks"].update({T.network_name("42", "rel"): {}}),
        "other_session_net": lambda b: b.update(
            Networks={T.network_name("43", "net"): {}, T.network_name("42", "out"): {}}
        ),
        "podman_net": lambda b: b.update(Networks={T.network_name("42", "net"): {}, "podman": {}}),
        "port": lambda b: b.update(
            portmappings=[
                {
                    "host_ip": "127.0.0.1",
                    "host_port": 18080,
                    "container_port": 3128,
                    "protocol": "tcp",
                }
            ]
        ),
        "privileged": lambda b: b.update(privileged=True),
        "root_user": lambda b: b.update(user="0:0"),
        "env_extra": lambda b: b["env"].update(HTTP_PROXY=PROXY_URL),
    }
    for name, mutate in proxy_mutations.items():
        yield f"pkg_proxy_{name}", cfg, _mutated(proxy, mutate), "reject", "Proxy"
    yield (
        "work_net_other_session",
        cfg,
        _mutated(_work(), lambda b: b.update(Networks={T.network_name("43"): {}})),
        "reject",
        "Arbeitscontainer nur am eigenen Sitzungsnetz",
    )
    # Relay der Positivliste darf nicht am Sitzungsnetz des Arbeitscontainers hängen.
    yield (
        "relay_base_on_work_net",
        cfg,
        _mutated(
            relay,
            lambda b: b.update(
                Networks={T.network_name("42", "net"): {}, T.network_name("42", "out"): {}}
            ),
        ),
        "reject",
        "Relay mit --base nur an Relay-Netz und Ausgang",
    )


def packages_inspect_cases():
    expected = _limits(network="packages").as_dict()
    caps = [f"CAP_{c}" for c in T.CAPS_PACKAGES]
    rw = {"ReadonlyRootfs": False, "CapAdd": caps}
    yield "pkg_inspect_ok", _inspect(**rw), expected, "ok", "Paketquellen wie Soll"
    yield (
        "pkg_inspect_no_prefix",
        _inspect(ReadonlyRootfs=False, CapAdd=list(T.CAPS_PACKAGES)),
        (expected),
        "ok",
        "Capabilities ohne CAP_",
    )
    yield (
        "pkg_inspect_lowercase",
        _inspect(ReadonlyRootfs=False, CapAdd=[c.lower() for c in caps]),
        expected,
        "ok",
        "Schreibweise egal",
    )
    yield (
        "pkg_inspect_subset",
        _inspect(ReadonlyRootfs=False, CapAdd=caps[:2]),
        expected,
        ("ok"),
        "Teilmenge",
    )
    yield "pkg_inspect_readonly", _inspect(), expected, "ok", "schreibgeschützt ist strenger"
    bad = {
        "cap_sys_admin": {"CapAdd": [*caps, "CAP_SYS_ADMIN"]},
        "cap_net_admin": {"CapAdd": [*caps, "CAP_NET_ADMIN"]},
        "cap_net_raw": {"CapAdd": ["CAP_NET_RAW"]},
        "cap_all": {"CapAdd": ["ALL"]},
        "cap_not_list": {"CapAdd": "CAP_CHOWN"},
        "privileged": {"Privileged": True},
        "no_nnp": {"SecurityOpt": []},
        "pid_host": {"PidMode": "host"},
        "ipc_host": {"IpcMode": "host"},
        "uts_host": {"UTSMode": "host"},
        "net_host": {"NetworkMode": "host"},
        "devices": {"Devices": [{"PathOnHost": "/dev/fuse"}]},
        "binds": {"Binds": ["/var/lib/apt:/var/lib/apt"]},
        "tmpfs_extra": {
            "Tmpfs": {"/workspace": "size=128m", "/tmp": "size=256m", "/var": "size=1g"}
        },
        "port_all": {"PortBindings": {"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "18080"}]}},
        "proxy_port": {
            "PortBindings": {"3128/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18080"}]}
        },
        "memory": {"Memory": 1 << 30, "MemorySwap": 1 << 30},
    }
    for name, host in bad.items():
        yield f"pkg_inspect_{name}", _inspect(**{**rw, **host}), expected, "reject", "Abweichung"
    info = _inspect(**rw)
    info["Config"]["User"] = "0:0"
    yield "pkg_inspect_root_user", info, expected, "reject", "Nutzer root"
    # Ohne Modus Paketquellen ist die Lockerung eine Abweichung.
    for network in ("none", "allowlist", "default"):
        plain = _limits(network=network).as_dict()
        yield f"plain_{network}_inspect_relaxed", _inspect(**rw), plain, "reject", "ohne Modus"
    yield (
        "plain_inspect_cap_chown",
        _inspect(CapAdd=["CAP_CHOWN"]),
        (_limits().as_dict()),
        "reject",
        "eine Capability ohne Modus",
    )


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
    pkg = {T.LABEL_NETWORK: "packages"}
    yield "pkg_net_internal", nb(net, True, pkg), "ok", "Paketquellen: Sitzungsnetz intern"
    yield "pkg_net_external", nb(net, False, pkg), "reject", "Paketquellen: nie direkt nach außen"
    yield "pkg_net_dns", dict(nb(net, True, pkg), dns_enabled=True), "reject", "DNS nur im Proxy"


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
    for name, info, exp, expect, note in packages_inspect_cases():
        yield name, info, exp, 18080, None, expect, note


def limits_cases():
    cfg = make_config(max_cpus=2, max_memory_mb=1024)
    yield "limits_at_max", cfg, _limits(cpus=2, memory_mb=1024), "ok", "genau die Obergrenze"
    yield "limits_cpus_over", cfg, _limits(cpus=4), "reject", "CPU über lokaler Grenze"
    yield "limits_memory_over", cfg, _limits(memory_mb=4096), "reject", "RAM über lokaler Grenze"
    yield "limits_cpus_low", cfg, _limits(cpus=0.05), "reject", "CPU unter 0,1"
    yield "limits_pids_low", cfg, _limits(pids=8), "reject", "pids unter 16"
    yield "limits_tmpfs_over", make_config(), _limits(tmpfs_mb=8193), "reject", "tmpfs zu groß"
    yield "limits_packages", cfg, _limits(network="packages"), "ok", "Netzart Paketquellen"
    yield "limits_network_unknown", cfg, _limits(network="bridge"), "reject", "unbekannte Netzart"
    yield "limits_network_host", cfg, _limits(network="host"), "reject", "unbekannte Netzart"


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

    for name, cfg, body, expect, note in [*body_cases(), *packages_body_cases()]:
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
