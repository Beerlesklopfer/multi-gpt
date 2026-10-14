//! Feste Vorlage für Container (Entscheidung 8g, M14-09), Port von `runner_agent/template.py`.
//!
//! Der Podman-Socket ist so mächtig wie der Nutzer des Rechners. Der Agent baut die
//! Container-Optionen deshalb selbst aus einem festen Schema ([`work_body`],
//! [`relay_body`], [`tool_body`]); der Server liefert nur Werte, nie Optionen. Vor
//! jedem Anlegen prüft [`check_body`] den fertigen libpod-Körper noch einmal gegen
//! die Vorlage – auch ein Fehler im Agent kann so keine Mounts, Privilegien oder
//! Host-Namespaces durchreichen. Nach dem Start vergleicht [`check_inspect`] die
//! tatsächlichen Werte aus `inspect` mit dem Soll.
//!
//! Die Körper sind `serde_json::Value` im libpod-Format (1:1 wie im Python-Agent).

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

pub const LABEL_RUNNER: &str = "multigpt.runner";
pub const LABEL_SESSION: &str = "multigpt.session";
pub const LABEL_WORKSPACE: &str = "multigpt.workspace";
/// Rolle des Containers: work | relay | tool.
pub const LABEL_ROLE: &str = "multigpt.role";
/// Sollwerte der Sitzung (JSON), für den Abgleich nach dem Wiederverbinden.
pub const LABEL_SPEC: &str = "multigpt.spec";
pub const LABEL_PREVIEW: &str = "multigpt.preview_port";
/// Hilfscontainer: Kennung des Aufrufs.
pub const LABEL_CALL: &str = "multigpt.call";
/// Netzart der Sitzung: none | allowlist | default.
pub const LABEL_NETWORK: &str = "multigpt.network";

pub const USER: &str = "1000:1000";
/// http im Container; unprivilegiert, denn Port 80 kann Nutzer 1000 ohne Capabilities nicht binden.
pub const PREVIEW_CONTAINER_PORT: u64 = 8080;
/// https im Container (selbst signiertes Zertifikat, im tmpfs erzeugt).
pub const TLS_CONTAINER_PORT: u64 = 8443;
/// Host-Port https = http + 363 (443 → 10443, 10081 → 10444, …).
pub const TLS_PORT_OFFSET: u64 = 363;
/// Adresse des Port-Exports am Rechner (Nutzerentscheidung 2026-10-14: Heimnetz).
pub const DEFAULT_EXPOSE_BIND: &str = "0.0.0.0";
pub const EXPOSE_BINDS: [&str; 2] = ["0.0.0.0", "127.0.0.1"];
pub const RELAY_BASE_PORT: u64 = 20000;
pub const CPU_PERIOD: u64 = 100_000;
/// Größe von `/tmp` (tmpfs) in MB, zusätzlich zu `/workspace`.
pub const TMP_MB: u64 = 256;
pub const NOFILE: u64 = 4096;
/// Host-Ports (http) am Rechner: 8080 → 10080 … 10089, https jeweils +363 (Nutzer 2026-10-14).
pub const PREVIEW_PORTS: std::ops::Range<u64> = 10080..10090;
pub const NETWORK_MODES: [&str; 4] = ["none", "allowlist", "default", "packages"];
/// Port des Proxys im Relay-Container (Netzmodus „Paketquellen“).
pub const PROXY_PORT: u64 = 3128;
/// Capabilities des Arbeitscontainers nur im Modus `packages` (apt als root im
/// eigenen User-Namespace); Reihenfolge fest.
pub const CAPS_PACKAGES: [&str; 6] = [
    "CHOWN",
    "DAC_OVERRIDE",
    "FOWNER",
    "FSETID",
    "SETUID",
    "SETGID",
];
/// Ziele ohne Proxy im Modus `packages`.
pub const NO_PROXY: &str = "localhost,127.0.0.1,::1";
/// Proxy-Variablen im Arbeitscontainer (alle mit derselben Adresse).
pub const PROXY_ENV_KEYS: [&str; 4] = ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"];
pub const NO_PROXY_ENV_KEYS: [&str; 2] = ["NO_PROXY", "no_proxy"];
/// Höchstzahl der Paket-Hosts (wie `protocol::MAX_PACKAGE_HOSTS`).
pub const MAX_PACKAGE_HOSTS: usize = 50;

/// Feste Umgebung im Container (keine Geheimnisse des Rechners).
pub const ENV: [(&str, &str); 4] = [
    ("HOME", "/tmp"),
    (
        "PATH",
        "/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin",
    ),
    ("LANG", "C.UTF-8"),
    ("TERM", "xterm-256color"),
];

/// Optionen des Bind-Mounts für das lokale Scratchpad (einzige Mount-Ausnahme).
pub const BIND_OPTIONS: [&str; 5] = ["rbind", "rw", "noexec", "nosuid", "nodev"];

pub static NAME_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^mgpt-(ses|rly|tol)-[0-9a-f]{16}$").unwrap());
pub static NETWORK_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^mgpt-(net|out|rel)-[0-9a-f]{16}$").unwrap());
static TMPFS_OPTION_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^(size=\d+[kmg]?|mode=[0-7]{3,4}|nosuid|nodev|noexec|U)$").unwrap()
});
/// Paket-Host: DNS-Name ohne Port und ohne IP (Länge ≤ 253 extra geprüft).
pub static PACKAGE_HOST_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$").unwrap());
static PROXY_URL_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^http://\d{1,3}(\.\d{1,3}){3}:3128$").unwrap());
static RELAY_TARGET_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Za-z0-9][A-Za-z0-9.:-]{0,252}:\d{1,5}$").unwrap());
pub static UUID_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$").unwrap()
});
/// Image nur mit Digest (wie `protocol.IMAGE_RE`).
static IMAGE_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[a-z0-9][a-z0-9._/:-]{0,254}@sha256:[0-9a-f]{64}$").unwrap());

const ALLOWED_KEYS: [&str; 32] = [
    "name",
    "image",
    "command",
    "labels",
    "user",
    "hostname",
    "read_only_filesystem",
    "cap_drop",
    "cap_add",
    "no_new_privileges",
    "privileged",
    "init",
    "netns",
    "Networks",
    "mounts",
    "resource_limits",
    "r_limits",
    "env",
    "env_host",
    "httpproxy",
    "portmappings",
    "ipcns",
    "pidns",
    "utsns",
    "cgroupns",
    "image_volume_mode",
    "work_dir",
    "stop_timeout",
    "use_image_hosts",
    "use_image_resolve_conf",
    "userns",
    "remove",
];

/// Feste Werte; jede Abweichung wird abgelehnt.
fn fixed() -> Vec<(&'static str, Value)> {
    vec![
        ("user", json!(USER)),
        ("cap_drop", json!(["ALL"])),
        ("no_new_privileges", json!(true)),
        ("privileged", json!(false)),
        ("init", json!(true)),
        ("env_host", json!(false)),
        ("httpproxy", json!(false)),
        ("ipcns", json!({"nsmode": "private"})),
        ("pidns", json!({"nsmode": "private"})),
        ("utsns", json!({"nsmode": "private"})),
        ("cgroupns", json!({"nsmode": "private"})),
        ("image_volume_mode", json!("ignore")),
        ("use_image_hosts", json!(true)),
        // Wie `podman run --rm` (Nutzer, 2026-10-14).
        ("remove", json!(true)),
    ]
}

/// `userns` beim lokalen Scratchpad: Nutzer 1000 im Container = Nutzer des Rechners.
pub fn keep_id() -> Value {
    json!({"nsmode": "keep-id", "value": "uid=1000,gid=1000"})
}

pub fn env() -> Value {
    Value::Object(ENV.iter().map(|(k, v)| (k.to_string(), json!(v))).collect())
}

/// Umgebung im Modus `packages`: feste Umgebung plus Proxy des Relay-Containers.
pub fn env_packages(proxy_url: &str) -> Value {
    let mut env = env();
    let map = env.as_object_mut().unwrap();
    for key in PROXY_ENV_KEYS {
        map.insert(key.into(), json!(proxy_url));
    }
    for key in NO_PROXY_ENV_KEYS {
        map.insert(key.into(), json!(NO_PROXY));
    }
    env
}

/// Adresse des Proxys aus seiner IPv4 im Sitzungsnetz.
pub fn proxy_url(ip: &str) -> Option<String> {
    let url = format!("http://{ip}:{PROXY_PORT}");
    is_proxy_url(&url).then_some(url)
}

fn is_proxy_url(url: &str) -> bool {
    PROXY_URL_RE.is_match(url) && url[7..url.len() - 5].parse::<std::net::Ipv4Addr>().is_ok()
}

/// Paket-Host der signierten Liste (exakt, klein, ohne Port/IP).
pub fn is_package_host(host: &str) -> bool {
    host.len() <= 253 && PACKAGE_HOST_RE.is_match(host)
}

/// Befehl verletzt die feste Vorlage (wird nie ausgeführt).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TemplateError(pub String);

impl fmt::Display for TemplateError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for TemplateError {}

type Res<T = ()> = Result<T, TemplateError>;

fn expect(condition: bool, message: impl Into<String>) -> Res {
    if condition {
        Ok(())
    } else {
        Err(TemplateError(message.into()))
    }
}

/// Lokale Obergrenzen des Agents und Positivliste der Images (aus der Konfiguration).
#[derive(Debug, Clone)]
pub struct Bounds {
    pub images: BTreeSet<String>,
    pub max_cpus: f64,
    pub max_memory_mb: u64,
    pub max_pids: u64,
    pub max_tmpfs_mb: u64,
    /// Host-Sicht des Ordners `~/MultiGPT/Scratch` (nur mit lokalem Scratchpad).
    pub scratch_host_dir: Option<String>,
    /// Adresse des Port-Exports (`0.0.0.0` Heimnetz, `127.0.0.1` nur der Rechner).
    pub expose_bind: String,
}

impl Default for Bounds {
    fn default() -> Self {
        Self {
            images: BTreeSet::new(),
            max_cpus: 8.0,
            max_memory_mb: 16384,
            max_pids: 4096,
            max_tmpfs_mb: 8192,
            expose_bind: DEFAULT_EXPOSE_BIND.to_string(),
            scratch_host_dir: None,
        }
    }
}

impl From<&crate::config::Config> for Bounds {
    fn from(cfg: &crate::config::Config) -> Self {
        Self {
            images: cfg.images.clone(),
            max_cpus: cfg.max_cpus,
            max_memory_mb: cfg.max_memory_mb,
            max_pids: cfg.max_pids,
            max_tmpfs_mb: cfg.max_tmpfs_mb,
            expose_bind: cfg.expose_bind.clone(),
            scratch_host_dir: cfg
                .scratch_host_dir
                .as_ref()
                .map(|p| p.to_string_lossy().into_owned()),
        }
    }
}

/// Grenzen einer Sitzung (Werte des Servers bzw. Ist-Werte aus `inspect`).
#[derive(Debug, Clone, PartialEq)]
pub struct Limits {
    pub cpus: f64,
    pub memory_mb: u64,
    pub pids: u64,
    pub tmpfs_mb: u64,
    pub network: String,
    /// Grenze der beschreibbaren Schicht (MB), nur im Modus `packages`.
    pub rootfs_mb: Option<u64>,
}

impl Limits {
    pub fn as_value(&self) -> Value {
        let mut value = json!({
            "cpus": self.cpus,
            "memory_mb": self.memory_mb,
            "pids": self.pids,
            "tmpfs_mb": self.tmpfs_mb,
            "network": self.network,
        });
        if let Some(mb) = self.rootfs_mb {
            value["rootfs_mb"] = json!(mb);
        }
        value
    }

    pub fn packages(&self) -> bool {
        self.network == "packages"
    }

    pub fn from_value(value: &Value) -> Option<Self> {
        Some(Self {
            cpus: value.get("cpus")?.as_f64()?,
            memory_mb: value.get("memory_mb")?.as_u64()?,
            pids: value.get("pids")?.as_u64()?,
            tmpfs_mb: value.get("tmpfs_mb")?.as_u64()?,
            network: value.get("network")?.as_str()?.to_string(),
            rootfs_mb: match value.get("rootfs_mb") {
                None | Some(Value::Null) => None,
                Some(v) => Some(v.as_u64()?),
            },
        })
    }
}

/// Scratchpad `/scratch`: tmpfs (`container`) oder Ordner des Rechners (`local`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Scratch {
    pub mode: String,
    pub size_mb: u64,
    /// Host-Pfad bei `local`.
    pub source: Option<String>,
}

impl Scratch {
    pub fn is_local(&self) -> bool {
        self.mode == "local"
    }

    pub fn mount(&self) -> Value {
        if self.is_local() {
            return json!({
                "destination": "/scratch",
                "type": "bind",
                "source": self.source,
                "options": BIND_OPTIONS,
            });
        }
        let mut mount = tmpfs("/scratch", self.size_mb, "0700", true);
        mount["options"]
            .as_array_mut()
            .unwrap()
            .push(json!("noexec"));
        mount
    }
}

pub fn short_id(value: &str) -> String {
    let digest = Sha256::digest(value.as_bytes());
    hex::encode(digest)[..16].to_string()
}

/// Name je Rolle: Arbeitscontainer, Relay, Hilfscontainer je ssh/telnet-Aufruf.
pub fn container_name(session: &str, role: &str, call: Option<&str>) -> String {
    if role == "tool" {
        // Python: f"{session}#{call}" – ohne Kennung steht dort "None".
        return format!(
            "mgpt-tol-{}",
            short_id(&format!("{session}#{}", call.unwrap_or("None")))
        );
    }
    let kind = if role == "work" { "ses" } else { "rly" };
    format!("mgpt-{kind}-{}", short_id(session))
}

pub fn work_name(session: &str) -> String {
    container_name(session, "work", None)
}

pub fn network_name(session: &str, kind: &str) -> String {
    format!("mgpt-{kind}-{}", short_id(session))
}

pub fn is_image(image: &str) -> bool {
    IMAGE_RE.is_match(image)
}

/// Image nur mit Digest, aus der Liste des Servers UND der lokalen Positivliste.
pub fn check_image(
    image: Option<&str>,
    server_images: Option<&[String]>,
    local_images: &BTreeSet<String>,
) -> Res {
    let Some(image) = image.filter(|i| is_image(i)) else {
        return Err(TemplateError("Image ohne Digest.".into()));
    };
    expect(
        local_images.contains(image),
        "Image steht nicht in der lokalen Positivliste (MGPT_IMAGES).",
    )?;
    if let Some(server) = server_images {
        expect(
            server.iter().any(|i| i == image),
            "Image steht nicht in der Liste des Servers.",
        )?;
    }
    Ok(())
}

/// Werte des Servers gegen die lokalen Obergrenzen des Agents prüfen.
pub fn check_limits(limits: &Limits, bounds: &Bounds) -> Res {
    expect(
        (0.1..=bounds.max_cpus).contains(&limits.cpus),
        format!("CPU-Grenze außerhalb 0,1…{}.", bounds.max_cpus),
    )?;
    expect(
        (64..=bounds.max_memory_mb).contains(&limits.memory_mb),
        format!("RAM-Grenze außerhalb 64…{} MB.", bounds.max_memory_mb),
    )?;
    expect(
        (16..=bounds.max_pids).contains(&limits.pids),
        format!("Prozessgrenze außerhalb 16…{}.", bounds.max_pids),
    )?;
    expect(
        (16..=bounds.max_tmpfs_mb).contains(&limits.tmpfs_mb),
        format!("tmpfs-Grenze außerhalb 16…{} MB.", bounds.max_tmpfs_mb),
    )?;
    expect(
        NETWORK_MODES.contains(&limits.network.as_str()),
        "Unbekannte Netzart.",
    )?;
    expect(
        limits
            .rootfs_mb
            .is_none_or(|mb| (64..=1_048_576).contains(&mb)),
        "Grenze der Container-Schicht außerhalb 64…1048576 MB.",
    )
}

pub fn tmpfs(destination: &str, size_mb: u64, mode: &str, chown: bool) -> Value {
    let mut options = vec![
        format!("size={size_mb}m"),
        format!("mode={mode}"),
        "nosuid".to_string(),
        "nodev".to_string(),
    ];
    if chown {
        options.push("U".to_string());
    }
    json!({"destination": destination, "type": "tmpfs", "source": "tmpfs", "options": options})
}

fn base(name: &str, image: &str, labels: &BTreeMap<String, String>, limits: &Limits) -> Value {
    let memory = limits.memory_mb * 1024 * 1024;
    // Dateigröße je Prozess: tmpfs-Größe; im Modus `packages` mindestens die Grenze
    // der Schicht (große .deb-Dateien unter /var/cache/apt).
    let fsize = limits.tmpfs_mb.max(limits.rootfs_mb.unwrap_or(0)) << 20;
    let mut body = json!({
        "name": name,
        "image": image,
        "labels": labels,
        "hostname": "workspace",
        "resource_limits": {
            "cpu": {"quota": (limits.cpus * CPU_PERIOD as f64).round() as u64, "period": CPU_PERIOD},
            "memory": {"limit": memory, "swap": memory},
            "pids": {"limit": limits.pids},
        },
        "r_limits": [
            {"type": "nofile", "soft": NOFILE, "hard": NOFILE},
            {"type": "fsize", "soft": fsize, "hard": fsize},
            {"type": "core", "soft": 0, "hard": 0},
        ],
        "env": env(),
        "stop_timeout": 5,
    });
    let map = body.as_object_mut().unwrap();
    for (key, value) in fixed() {
        map.insert(key.to_string(), value);
    }
    map.insert("read_only_filesystem".into(), json!(true));
    body
}

fn labels_of(pairs: &[(&str, &str)]) -> BTreeMap<String, String> {
    pairs
        .iter()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect()
}

/// Sollwerte als JSON wie Python `json.dumps(spec, separators=(",", ":"), sort_keys=True)`.
pub fn spec_label(spec: &Value) -> String {
    fn sorted(value: &Value) -> Value {
        match value {
            Value::Object(map) => {
                let mut keys: Vec<&String> = map.keys().collect();
                keys.sort();
                let mut out = Map::new();
                for key in keys {
                    out.insert(key.clone(), sorted(&map[key]));
                }
                Value::Object(out)
            }
            Value::Array(items) => Value::Array(items.iter().map(sorted).collect()),
            other => other.clone(),
        }
    }
    // ensure_ascii wie Python: Nicht-ASCII als \uXXXX (nur innerhalb von Strings möglich).
    let text = serde_json::to_string(&sorted(spec)).unwrap_or_default();
    let mut out = String::with_capacity(text.len());
    for ch in text.chars() {
        if ch.is_ascii() {
            out.push(ch);
        } else {
            let mut buf = [0u16; 2];
            for unit in ch.encode_utf16(&mut buf) {
                out.push_str(&format!("\\u{unit:04x}"));
            }
        }
    }
    out
}

/// Arbeitscontainer: nur tmpfs `/workspace` und `/tmp` (plus `/scratch`), Netz nur über
/// `network` (intern ohne Gateway bei „aus“, Positivliste und Paketquellen, mit Gateway
/// bei „default“).
///
/// Im Modus `packages` (und nur dort) ist die Vorlage gezielt gelockert: Wurzel-
/// dateisystem beschreibbar (flüchtige Container-Schicht), Capabilities genau
/// [`CAPS_PACKAGES`] (für `apt_install` als root), Proxy-Umgebung mit `proxy_url`.
/// `no-new-privileges` und alles Übrige bleiben.
#[allow(clippy::too_many_arguments)]
/// Standardexport des Arbeitscontainers: 8080 → `preview_port` (http) und
/// 8443 → `preview_port + 363` (https), an `bind` des Rechners.
pub fn port_mappings(preview_port: u64, bind: &str) -> Value {
    json!([
        {
            "host_ip": bind,
            "host_port": preview_port,
            "container_port": PREVIEW_CONTAINER_PORT,
            "protocol": "tcp",
        },
        {
            "host_ip": bind,
            "host_port": preview_port + TLS_PORT_OFFSET,
            "container_port": TLS_CONTAINER_PORT,
            "protocol": "tcp",
        },
    ])
}

#[allow(clippy::too_many_arguments)]
pub fn work_body(
    session: &str,
    workspace: &str,
    image: &str,
    limits: &Limits,
    network: &str,
    preview_port: Option<u64>,
    spec: &Value,
    scratch: Option<&Scratch>,
    proxy_url: Option<&str>,
    expose_bind: &str,
) -> Value {
    let spec_text = spec_label(spec);
    let mut labels = labels_of(&[
        (LABEL_RUNNER, "1"),
        (LABEL_ROLE, "work"),
        (LABEL_SESSION, session),
        (LABEL_WORKSPACE, workspace),
        (LABEL_SPEC, &spec_text),
        (LABEL_NETWORK, &limits.network),
    ]);
    if let Some(port) = preview_port {
        labels.insert(LABEL_PREVIEW.into(), port.to_string());
    }
    let mut body = base(&work_name(session), image, &labels, limits);
    let map = body.as_object_mut().unwrap();
    // Ohne Netz keine DNS-Server des Rechners im Container (8w).
    map.insert(
        "use_image_resolve_conf".into(),
        json!(limits.network != "default"),
    );
    map.insert("command".into(), json!(["sleep", "infinity"]));
    map.insert("work_dir".into(), json!("/workspace"));
    map.insert("netns".into(), json!({"nsmode": "bridge"}));
    map.insert("Networks".into(), json!({ network: {} }));
    if limits.packages() {
        map.insert("read_only_filesystem".into(), json!(false));
        map.insert("cap_add".into(), json!(CAPS_PACKAGES));
        map.insert("env".into(), env_packages(proxy_url.unwrap_or("")));
    }
    let mut mounts = vec![
        tmpfs("/workspace", limits.tmpfs_mb, "0700", true),
        tmpfs("/tmp", TMP_MB, "1777", false),
    ];
    if let Some(scratch) = scratch {
        mounts.push(scratch.mount());
        if scratch.is_local() {
            map.insert("userns".into(), keep_id());
        }
    }
    map.insert("mounts".into(), Value::Array(mounts));
    if let Some(port) = preview_port {
        map.insert("portmappings".into(), port_mappings(port, expose_bind));
    }
    body
}

/// Relay-Container der Positivliste (8w): Relay-Netz (nur Hilfscontainer) + Ausgang.
pub fn relay_body(session: &str, image: &str, targets: &[(String, u64)]) -> Value {
    let labels = labels_of(&[
        (LABEL_RUNNER, "1"),
        (LABEL_ROLE, "relay"),
        (LABEL_SESSION, session),
    ]);
    let limits = helper_limits();
    let mut body = base(
        &container_name(session, "relay", None),
        image,
        &labels,
        &limits,
    );
    let mut command = vec![
        "mgpt-relay".to_string(),
        "--base".to_string(),
        RELAY_BASE_PORT.to_string(),
    ];
    command.extend(targets.iter().map(|(host, port)| format!("{host}:{port}")));
    let map = body.as_object_mut().unwrap();
    map.insert("command".into(), json!(command));
    map.insert("work_dir".into(), json!("/"));
    map.insert("use_image_resolve_conf".into(), json!(false));
    map.insert("netns".into(), json!({"nsmode": "bridge"}));
    map.insert(
        "Networks".into(),
        json!({ network_name(session, "rel"): {}, network_name(session, "out"): {} }),
    );
    map.insert("mounts".into(), json!([tmpfs("/tmp", 16, "1777", false)]));
    body
}

/// Proxy-Container für Paketquellen: Rolle `relay`, am Sitzungsnetz (dort erreicht
/// ihn der Arbeitscontainer) und am Ausgang. Nur die Hosts der signierten Liste.
pub fn proxy_body(session: &str, image: &str, hosts: &[String]) -> Value {
    let labels = labels_of(&[
        (LABEL_RUNNER, "1"),
        (LABEL_ROLE, "relay"),
        (LABEL_SESSION, session),
    ]);
    let limits = helper_limits();
    let mut body = base(
        &container_name(session, "relay", None),
        image,
        &labels,
        &limits,
    );
    let mut command = vec![
        "mgpt-relay".to_string(),
        "--proxy".to_string(),
        PROXY_PORT.to_string(),
    ];
    command.extend(hosts.iter().cloned());
    let map = body.as_object_mut().unwrap();
    map.insert("command".into(), json!(command));
    map.insert("work_dir".into(), json!("/"));
    map.insert("use_image_resolve_conf".into(), json!(false));
    map.insert("netns".into(), json!({"nsmode": "bridge"}));
    map.insert(
        "Networks".into(),
        json!({ network_name(session, "net"): {}, network_name(session, "out"): {} }),
    );
    map.insert("mounts".into(), json!([tmpfs("/tmp", 16, "1777", false)]));
    body
}

fn helper_limits() -> Limits {
    Limits {
        cpus: 0.5,
        memory_mb: 128,
        pids: 64,
        tmpfs_mb: 16,
        network: "allowlist".into(),
        rootfs_mb: None,
    }
}

/// Hilfscontainer für einen ssh/telnet-Aufruf (Grundsatz: das Modell sieht nie den Zugang).
///
/// Läuft nur am Relay-Netz der Sitzung; der Arbeitscontainer hängt dort nicht. Der
/// Zugang liegt nur in seinem `/tmp`, der Container wird nach dem Aufruf entfernt.
pub fn tool_body(session: &str, call: &str, image: &str) -> Value {
    let labels = labels_of(&[
        (LABEL_RUNNER, "1"),
        (LABEL_ROLE, "tool"),
        (LABEL_SESSION, session),
        (LABEL_CALL, call),
    ]);
    let limits = helper_limits();
    let mut body = base(
        &container_name(session, "tool", Some(call)),
        image,
        &labels,
        &limits,
    );
    let map = body.as_object_mut().unwrap();
    map.insert("command".into(), json!(["sleep", "infinity"]));
    map.insert("work_dir".into(), json!("/tmp"));
    map.insert("use_image_resolve_conf".into(), json!(true));
    map.insert("netns".into(), json!({"nsmode": "bridge"}));
    map.insert(
        "Networks".into(),
        json!({ network_name(session, "rel"): {} }),
    );
    map.insert("mounts".into(), json!([tmpfs("/tmp", 16, "0700", true)]));
    body
}

// --- Befehle als root (nur Modus `packages`) ------------------------------------------

/// Debian-Paketname (wie das Schema von `apt_install`).
pub static APT_PACKAGE_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[a-z0-9][a-z0-9+.-]{0,62}$").unwrap());
/// Höchstzahl der Pakete je `apt_install`.
pub const MAX_APT_PACKAGES: usize = 20;
/// Höchstzahl der Pakete beim Nachinstallieren aus `.mgpt/apt-packages`.
pub const MAX_APT_PACKAGES_TOTAL: usize = 200;

fn strings(items: &[&str]) -> Vec<String> {
    items.iter().map(|s| s.to_string()).collect()
}

/// `apt-get update` (Paketlisten über den Proxy).
pub fn apt_update_argv() -> Vec<String> {
    strings(&["apt-get", "-q", "update"])
}

const APT_INSTALL_HEAD: [&str; 9] = [
    "apt-get",
    "-q",
    "-y",
    "--no-install-recommends",
    "-o",
    "Dpkg::Options::=--force-confdef",
    "-o",
    "Dpkg::Options::=--force-confold",
    "install",
];

/// `apt-get install -y --no-install-recommends -- <pakete>` (Namen geprüft).
pub fn apt_install_argv(packages: &[String]) -> Res<Vec<String>> {
    expect(
        !packages.is_empty() && packages.len() <= MAX_APT_PACKAGES_TOTAL,
        "Paketliste leer oder zu lang.",
    )?;
    for name in packages {
        expect(
            APT_PACKAGE_RE.is_match(name),
            format!("Ungültiger Paketname: {name}"),
        )?;
    }
    let mut argv = strings(&APT_INSTALL_HEAD);
    argv.push("--".into());
    argv.extend(packages.iter().cloned());
    Ok(argv)
}

/// setuid-/setgid-Bits nach der Installation entfernen (keine solchen Programme
/// für Nutzer 1000; `no-new-privileges` wirkt zusätzlich).
pub fn strip_setuid_argv() -> Vec<String> {
    strings(&[
        "find", "/", "-xdev", "-type", "f", "-perm", "/6000", "-exec", "chmod", "a-s", "{}", "+",
    ])
}

/// Befehl für Nutzer 1000 ohne ambient- und vererbbare Capabilities (Podman setzt
/// `cap_add` sonst auch für Nicht-root-Nutzer als ambient; betrifft den Modus
/// `packages`, schadet ohne `cap_add` nicht). `setpriv` stammt aus util-linux.
pub fn drop_caps_argv(argv: &[String]) -> Vec<String> {
    let mut out = strings(&["setpriv", "--inh-caps=-all", "--ambient-caps=-all", "--"]);
    out.extend(argv.iter().cloned());
    out
}

/// Höchstes Zeitlimit eines root-Befehls (Sekunden).
pub const ROOT_TIMEOUT_MAX: u64 = 900;

/// Befehl als root mit `timeout` umschließen: endet das Zeitlimit des Agents, endet
/// auch der Prozess im Container (ein abgebrochener exec liefe sonst weiter).
pub fn with_timeout(argv: &[String], seconds: u64) -> Vec<String> {
    let mut out = strings(&["timeout", "-k", "5"]);
    out.push(seconds.clamp(1, ROOT_TIMEOUT_MAX).to_string());
    out.extend(argv.iter().cloned());
    out
}

/// Nur diese festen Befehle laufen als root (Prüfung in jeder Engine vor `exec`),
/// wahlweise mit vorangestelltem `timeout -k 5 <1…900>`.
pub fn check_root_argv(argv: &[String]) -> Res {
    if argv.len() > 4 && argv[..3] == strings(&["timeout", "-k", "5"]) {
        let ok = argv[3].len() <= 3
            && argv[3].bytes().all(|b| b.is_ascii_digit())
            && argv[3]
                .parse::<u64>()
                .is_ok_and(|s| (1..=ROOT_TIMEOUT_MAX).contains(&s));
        expect(ok, "Befehl als root nicht erlaubt.")?;
        return check_plain_root_argv(&argv[4..]);
    }
    check_plain_root_argv(argv)
}

fn check_plain_root_argv(argv: &[String]) -> Res {
    if argv == apt_update_argv().as_slice() || argv == strip_setuid_argv().as_slice() {
        return Ok(());
    }
    let head = APT_INSTALL_HEAD.len();
    let ok = argv.len() > head + 1
        && argv[..head]
            .iter()
            .zip(APT_INSTALL_HEAD)
            .all(|(a, b)| a == b)
        && argv[head] == "--"
        && apt_install_argv(&argv[head + 1..]).is_ok();
    expect(ok, "Befehl als root nicht erlaubt.")
}

// --- Prüfung ---------------------------------------------------------------------

fn key_set(map: &Map<String, Value>) -> BTreeSet<&str> {
    map.keys().map(String::as_str).collect()
}

fn keys_are(value: &Value, keys: &[&str]) -> bool {
    value
        .as_object()
        .is_some_and(|m| key_set(m) == keys.iter().copied().collect())
}

/// Ganzzahl wie in Python (`bool` zählt nicht; 1.0 wird wie 1 behandelt).
fn number(value: &Value) -> Option<f64> {
    match value {
        Value::Number(n) => n.as_f64(),
        _ => None,
    }
}

/// Nur tmpfs; einzige Ausnahme ist der Scratch-Ordner des Rechners. Liefert true bei Bind.
fn check_mounts(mounts: Option<&Value>, role: &str, bounds: &Bounds) -> Res<bool> {
    let allowed: &[&str] = if role == "work" {
        &["/workspace", "/tmp", "/scratch"]
    } else {
        &["/tmp"]
    };
    let list = mounts.and_then(Value::as_array);
    expect(list.is_some_and(|l| !l.is_empty()), "mounts fehlen.")?;
    let mut seen = BTreeSet::new();
    let mut bind = false;
    for mount in list.unwrap() {
        expect(mount.is_object(), "Mount ungültig.")?;
        expect(
            keys_are(mount, &["destination", "type", "source", "options"]),
            "Mount mit fremden Feldern.",
        )?;
        let dest = mount["destination"].as_str().unwrap_or("");
        expect(
            allowed.contains(&dest),
            format!(
                "Mount-Ziel nicht erlaubt: {}",
                display(&mount["destination"])
            ),
        )?;
        expect(seen.insert(dest.to_string()), "Mount-Ziel doppelt.")?;
        if mount["type"] == "bind" {
            expect(dest == "/scratch", "Bind-Mount nur für /scratch.")?;
            check_scratch_source(&mount["source"], bounds)?;
            expect(
                mount["options"] == json!(BIND_OPTIONS),
                "Bind-Mount ohne noexec/nosuid/nodev.",
            )?;
            bind = true;
            continue;
        }
        expect(
            mount["type"] == "tmpfs" && mount["source"] == "tmpfs",
            "Nur tmpfs erlaubt.",
        )?;
        let options = mount["options"].as_array();
        expect(options.is_some(), "Mount-Optionen ungültig.")?;
        let options = options.unwrap();
        for option in options {
            expect(
                option.as_str().is_some_and(|o| TMPFS_OPTION_RE.is_match(o)),
                format!("Mount-Option nicht erlaubt: {}", display(option)),
            )?;
        }
        let has = |o: &str| options.iter().any(|x| x == o);
        expect(has("nosuid") && has("nodev"), "tmpfs ohne nosuid/nodev.")?;
        expect(
            options
                .iter()
                .any(|o| o.as_str().is_some_and(|s| s.starts_with("size="))),
            "tmpfs ohne Größe.",
        )?;
    }
    Ok(bind)
}

fn display(value: &Value) -> String {
    match value {
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

fn check_scratch_source(source: &Value, bounds: &Bounds) -> Res {
    let host = bounds.scratch_host_dir.as_deref().unwrap_or("");
    expect(
        !host.is_empty(),
        "Lokales Scratchpad ist nicht eingerichtet.",
    )?;
    let source = source.as_str();
    expect(source.is_some(), "Scratch-Pfad ungültig.")?;
    let source = source.unwrap();
    let (parent, name) = source.rsplit_once('/').unwrap_or(("", source));
    expect(
        parent == host && UUID_RE.is_match(name),
        "Scratch-Pfad nicht erlaubt.",
    )
}

fn check_resources(body: &Value, bounds: &Bounds) -> Res {
    let res = body.get("resource_limits");
    expect(
        res.is_some_and(|r| keys_are(r, &["cpu", "memory", "pids"])),
        "cgroup-Grenzen fehlen.",
    )?;
    let res = res.unwrap();
    let (cpu, mem, pids) = (&res["cpu"], &res["memory"], &res["pids"]);
    expect(
        keys_are(cpu, &["quota", "period"]) && number(&cpu["period"]) == Some(CPU_PERIOD as f64),
        "CPU-Grenze ungültig.",
    )?;
    let quota = number(&cpu["quota"]).unwrap_or(0.0);
    expect(
        quota > 0.0 && quota <= bounds.max_cpus * CPU_PERIOD as f64,
        "CPU-Grenze zu hoch.",
    )?;
    expect(keys_are(mem, &["limit", "swap"]), "RAM-Grenze ungültig.")?;
    let limit = number(&mem["limit"]).unwrap_or(0.0);
    expect(
        limit > 0.0 && limit <= (bounds.max_memory_mb << 20) as f64,
        "RAM-Grenze zu hoch.",
    )?;
    expect(
        number(&mem["swap"]) == Some(limit),
        "Swap muss gleich RAM sein.",
    )?;
    let pid_limit = number(&pids["limit"]).unwrap_or(0.0);
    expect(
        keys_are(pids, &["limit"]) && pid_limit > 0.0 && pid_limit <= bounds.max_pids as f64,
        "pids ungültig.",
    )?;
    let rlimits = body.get("r_limits").and_then(Value::as_array);
    expect(rlimits.is_some(), "ulimits fehlen.")?;
    let mut kinds = BTreeSet::new();
    for item in rlimits.unwrap() {
        expect(
            keys_are(item, &["type", "soft", "hard"]),
            "ulimit ungültig.",
        )?;
        let kind = item["type"].as_str().unwrap_or("");
        expect(
            ["nofile", "fsize", "core", "nproc"].contains(&kind),
            "ulimit nicht erlaubt.",
        )?;
        let (soft, hard) = (number(&item["soft"]), number(&item["hard"]));
        expect(
            matches!((soft, hard), (Some(s), Some(h)) if 0.0 <= s && s <= h),
            "ulimit ungültig.",
        )?;
        kinds.insert(kind);
    }
    expect(
        kinds.contains("nofile") && kinds.contains("fsize"),
        "ulimit nofile/fsize fehlt.",
    )
}

/// Rolle eines geprüften Körpers.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Role {
    Work,
    Relay,
    Tool,
}

impl Role {
    pub fn as_str(self) -> &'static str {
        match self {
            Role::Work => "work",
            Role::Relay => "relay",
            Role::Tool => "tool",
        }
    }
}

/// libpod-Körper gegen die Vorlage prüfen; liefert die Rolle.
pub fn check_body(body: &Value, bounds: &Bounds) -> Res<Role> {
    let map = body.as_object();
    expect(map.is_some(), "Körper ungültig.")?;
    let map = map.unwrap();
    let unknown: Vec<&str> = map
        .keys()
        .map(String::as_str)
        .filter(|k| !ALLOWED_KEYS.contains(k))
        .collect();
    expect(
        unknown.is_empty(),
        format!("Option nicht erlaubt: {}", unknown.join(", ")),
    )?;
    for (key, value) in fixed() {
        expect(
            map.get(key) == Some(&value),
            format!("Option {key} weicht von der Vorlage ab."),
        )?;
    }
    let labels = map.get("labels").and_then(Value::as_object);
    expect(
        labels.is_some_and(|l| l.get(LABEL_RUNNER) == Some(&json!("1"))),
        "Label fehlt.",
    )?;
    let labels = labels.unwrap();
    expect(labels.values().all(Value::is_string), "Label ungültig.")?;
    let label = |key: &str| labels.get(key).and_then(Value::as_str);
    let role = match label(LABEL_ROLE) {
        Some("work") => Role::Work,
        Some("relay") => Role::Relay,
        Some("tool") => Role::Tool,
        _ => return Err(TemplateError("Rolle unbekannt.".into())),
    };
    let name = map.get("name").and_then(Value::as_str);
    expect(name.is_some_and(|n| NAME_RE.is_match(n)), "Name ungültig.")?;
    let session = label(LABEL_SESSION).unwrap_or("");
    expect(
        name == Some(container_name(session, role.as_str(), label(LABEL_CALL)).as_str()),
        "Name passt nicht zur Sitzung.",
    )?;
    check_image(
        map.get("image").and_then(Value::as_str),
        None,
        &bounds.images,
    )?;
    // Gelockerte Vorlage nur für den Arbeitscontainer im Modus `packages`.
    if role == Role::Work && label(LABEL_NETWORK) == Some("packages") {
        expect(
            map.get("read_only_filesystem") == Some(&json!(false)),
            "Option read_only_filesystem weicht von der Vorlage ab.",
        )?;
        expect(
            map.get("cap_add") == Some(&json!(CAPS_PACKAGES)),
            "Capabilities weichen von der Vorlage ab.",
        )?;
        check_packages_env(map.get("env"))?;
    } else {
        expect(
            map.get("read_only_filesystem") == Some(&json!(true)),
            "Option read_only_filesystem weicht von der Vorlage ab.",
        )?;
        expect(
            !map.contains_key("cap_add"),
            "Capabilities nur im Modus Paketquellen.",
        )?;
        expect(
            map.get("env") == Some(&env()),
            "Umgebung weicht von der Vorlage ab.",
        )?;
    }
    let resolv = map.get("use_image_resolve_conf").and_then(Value::as_bool);
    expect(resolv.is_some(), "use_image_resolve_conf fehlt.")?;
    if role == Role::Work && label(LABEL_NETWORK) != Some("default") {
        expect(
            resolv == Some(true),
            "Ohne Netz keine DNS-Server des Rechners.",
        )?;
    }
    expect(
        map.get("netns") == Some(&json!({"nsmode": "bridge"})),
        "Netz-Namespace nicht erlaubt.",
    )?;
    let networks = map.get("Networks").and_then(Value::as_object);
    expect(networks.is_some_and(|n| !n.is_empty()), "Netz fehlt.")?;
    let networks = networks.unwrap();
    for (net, opts) in networks {
        expect(
            NETWORK_RE.is_match(net) && *opts == json!({}),
            format!("Netz nicht erlaubt: {net}"),
        )?;
    }
    let bind = check_mounts(map.get("mounts"), role.as_str(), bounds)?;
    if bind {
        expect(
            map.get("userns") == Some(&keep_id()),
            "Lokales Scratchpad nur mit userns keep-id.",
        )?;
    } else {
        expect(
            !map.contains_key("userns"),
            "userns nur beim lokalen Scratchpad.",
        )?;
    }
    check_resources(body, bounds)?;
    let empty = json!([]);
    let ports = map.get("portmappings").unwrap_or(&empty);
    let port_list = ports.as_array();
    let net_names: BTreeSet<&str> = networks.keys().map(String::as_str).collect();
    match role {
        Role::Work => {
            expect(
                map.get("command") == Some(&json!(["sleep", "infinity"])),
                "Befehl weicht ab.",
            )?;
            expect(networks.len() == 1, "Arbeitscontainer nur an einem Netz.")?;
            expect(
                networks.keys().next().map(String::as_str)
                    == Some(network_name(session, "net").as_str()),
                "Arbeitscontainer nur am eigenen Sitzungsnetz.",
            )?;
            expect(
                port_list.is_some_and(|p| p.len() == 2 || p.is_empty()),
                "Genau http und https oder keine.",
            )?;
            let ports = port_list.unwrap();
            if !ports.is_empty() {
                let host_port = ports[0]
                    .get("host_port")
                    .and_then(Value::as_u64)
                    .filter(|p| PREVIEW_PORTS.contains(p));
                expect(host_port.is_some(), "Port außerhalb 10080–10089.")?;
                expect(
                    Value::Array(ports.clone())
                        == port_mappings(host_port.unwrap(), &bounds.expose_bind),
                    "Nur 8080 und 8443 an der vorgegebenen Adresse.",
                )?;
            }
        }
        Role::Tool => {
            expect(
                map.get("command") == Some(&json!(["sleep", "infinity"])),
                "Befehl weicht ab.",
            )?;
            expect(
                port_list.is_some_and(Vec::is_empty),
                "Hilfscontainer ohne Ports.",
            )?;
            let rel = network_name(session, "rel");
            expect(
                net_names == BTreeSet::from([rel.as_str()]),
                "Hilfscontainer nur am Relay-Netz.",
            )?;
        }
        Role::Relay => {
            let command = map.get("command").and_then(Value::as_array);
            let shape = |c: &[Value], flag: &str, port: u64, ok: &dyn Fn(&str) -> bool| {
                c.len() >= 4
                    && c.len() <= 53
                    && c[..3] == [json!("mgpt-relay"), json!(flag), json!(port.to_string())]
                    && c[3..].iter().all(|t| t.as_str().is_some_and(ok))
            };
            let relay = command.is_some_and(|c| {
                shape(c, "--base", RELAY_BASE_PORT, &|s| {
                    RELAY_TARGET_RE.is_match(s)
                })
            });
            let proxy = command.is_some_and(|c| shape(c, "--proxy", PROXY_PORT, &is_package_host));
            expect(relay || proxy, "Relay-Befehl weicht ab.")?;
            expect(port_list.is_some_and(Vec::is_empty), "Relay ohne Ports.")?;
            let inner = network_name(session, if proxy { "net" } else { "rel" });
            let out = network_name(session, "out");
            expect(
                net_names == BTreeSet::from([inner.as_str(), out.as_str()]),
                if proxy {
                    "Proxy nur an Sitzungsnetz und Ausgang."
                } else {
                    "Relay nur an Relay-Netz und Ausgang."
                },
            )?;
        }
    }
    Ok(role)
}

/// Umgebung des Arbeitscontainers im Modus `packages`: feste Umgebung plus genau
/// die Proxy-Variablen (alle dieselbe Adresse) und `NO_PROXY`.
fn check_packages_env(env_value: Option<&Value>) -> Res {
    let map = env_value.and_then(Value::as_object);
    expect(map.is_some(), "Umgebung weicht von der Vorlage ab.")?;
    let map = map.unwrap();
    let url = map.get("HTTP_PROXY").and_then(Value::as_str).unwrap_or("");
    expect(is_proxy_url(url), "Proxy-Adresse ungültig.")?;
    expect(
        Some(&env_packages(url)) == env_value,
        "Umgebung weicht von der Vorlage ab.",
    )
}

pub fn network_body(name: &str, internal: bool, labels: &BTreeMap<String, String>) -> Value {
    let mut all = labels.clone();
    all.insert(LABEL_RUNNER.into(), "1".into());
    json!({
        "name": name,
        "driver": "bridge",
        "internal": internal,
        "dns_enabled": false,
        "labels": all,
    })
}

pub fn check_network_body(body: &Value) -> Res {
    expect(
        keys_are(
            body,
            &["name", "driver", "internal", "dns_enabled", "labels"],
        ),
        "Netzoptionen ungültig.",
    )?;
    let name = body["name"].as_str().unwrap_or("");
    expect(NETWORK_RE.is_match(name), "Netzname ungültig.")?;
    expect(
        body["driver"] == "bridge" && body["dns_enabled"] == json!(false),
        "Netzart ungültig.",
    )?;
    expect(body["labels"][LABEL_RUNNER] == "1", "Label fehlt.")?;
    expect(body["internal"].is_boolean(), "Netzart ungültig.")?;
    let internal = body["internal"] == json!(true);
    if name.starts_with("mgpt-rel-") {
        expect(internal, "Relay-Netz muss intern sein.")?;
    }
    if name.starts_with("mgpt-net-") && body["labels"][LABEL_NETWORK] != "default" {
        expect(internal, "Sitzungsnetz muss intern sein.")?;
    }
    Ok(())
}

// --- Ist-Werte ---------------------------------------------------------------------

fn tmpfs_mb(options: &str) -> Option<u64> {
    for part in options.split(',') {
        if let Some(value) = part.strip_prefix("size=") {
            let last = value.chars().last()?;
            let (digits, unit) = if last.is_ascii_alphabetic() {
                (&value[..value.len() - 1], Some(last.to_ascii_lowercase()))
            } else {
                (value, None)
            };
            let number: u64 = digits.parse().ok()?;
            return Some(match unit {
                Some('k') => number / 1024,
                Some('m') => number,
                Some('g') => number * 1024,
                None => number >> 20,
                // Python: unbekannte Einheit → Zahl unverändert
                Some(_) => number,
            });
        }
    }
    None
}

fn int_of(value: Option<&Value>) -> i64 {
    match value {
        Some(Value::Number(n)) => n
            .as_i64()
            .or_else(|| n.as_f64().map(|f| f as i64))
            .unwrap_or(0),
        _ => 0,
    }
}

fn round3(value: f64) -> f64 {
    (value * 1000.0).round() / 1000.0
}

/// Tatsächliche Grenzen aus `inspect` (libpod-Format).
pub fn actual_limits(info: &Value, network: &str) -> Limits {
    let host = &info["HostConfig"];
    let quota = int_of(host.get("CpuQuota"));
    let period = int_of(host.get("CpuPeriod"));
    let nano = int_of(host.get("NanoCpus"));
    let cpus = if quota != 0 && period != 0 {
        round3(quota as f64 / period as f64)
    } else {
        round3(nano as f64 / 1e9)
    };
    let tmpfs = host["Tmpfs"]["/workspace"].as_str().unwrap_or("");
    Limits {
        cpus,
        memory_mb: (int_of(host.get("Memory")).max(0) as u64) >> 20,
        pids: int_of(host.get("PidsLimit")).max(0) as u64,
        tmpfs_mb: tmpfs_mb(tmpfs).unwrap_or(0),
        network: network.to_string(),
        rootfs_mb: None,
    }
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

fn fmt_cpus(value: f64) -> String {
    // Python: 1.5 → "1.5", 2 → "2.0"
    if value.fract() == 0.0 {
        format!("{value:.1}")
    } else {
        format!("{value}")
    }
}

/// Abweichungen der Ist-Werte von Soll und Vorlage (leer = in Ordnung).
pub fn check_inspect(
    info: &Value,
    expected: &Limits,
    preview_port: Option<u64>,
    scratch_source: Option<&str>,
    expose_bind: &str,
) -> Vec<String> {
    let mut problems = Vec::new();
    let null = Value::Null;
    let host = info.get("HostConfig").unwrap_or(&null);
    let actual = actual_limits(info, &expected.network);
    if (actual.cpus - expected.cpus).abs() > 0.01 {
        problems.push(format!(
            "CPU {} statt {}",
            fmt_cpus(actual.cpus),
            fmt_cpus(expected.cpus)
        ));
    }
    if actual.memory_mb != expected.memory_mb {
        problems.push(format!(
            "RAM {} MB statt {} MB",
            actual.memory_mb, expected.memory_mb
        ));
    }
    let memory = match host.get("Memory") {
        Some(v) if truthy(v) => int_of(Some(v)),
        _ => -1,
    };
    if int_of(host.get("MemorySwap")) != memory {
        problems.push("Swap ungleich RAM".into());
    }
    if actual.pids != expected.pids {
        problems.push(format!("pids {} statt {}", actual.pids, expected.pids));
    }
    if actual.tmpfs_mb != expected.tmpfs_mb {
        problems.push(format!(
            "tmpfs {} MB statt {} MB",
            actual.tmpfs_mb, expected.tmpfs_mb
        ));
    }
    if truthy(&host["Privileged"]) {
        problems.push("privilegiert".into());
    }
    let packages = expected.packages();
    if !packages && !truthy(&host["ReadonlyRootfs"]) {
        problems.push("Wurzeldateisystem beschreibbar".into());
    }
    if packages {
        // Nur die Capabilities der gelockerten Vorlage (Schreibweise wie inspect).
        let foreign = match &host["CapAdd"] {
            Value::Null => false,
            Value::Array(caps) => caps.iter().any(|cap| {
                let name = cap.as_str().unwrap_or("").to_ascii_uppercase();
                let name = name.strip_prefix("CAP_").unwrap_or(&name);
                !CAPS_PACKAGES.contains(&name)
            }),
            _ => true,
        };
        if foreign {
            problems.push("zusätzliche Capabilities".into());
        }
    } else if truthy(&host["CapAdd"]) {
        problems.push("zusätzliche Capabilities".into());
    }
    let secopt = host["SecurityOpt"].as_array();
    if !secopt.is_some_and(|s| s.iter().any(|o| o == "no-new-privileges")) {
        problems.push("no-new-privileges fehlt".into());
    }
    for key in ["PidMode", "IpcMode", "UTSMode", "NetworkMode"] {
        if host[key].as_str().is_some_and(|m| m.starts_with("host")) {
            problems.push(format!("Host-Namespace {key}"));
        }
    }
    if truthy(&host["Devices"]) {
        problems.push("Geräte eingebunden".into());
    }
    for mount in info["Mounts"].as_array().into_iter().flatten() {
        let options: BTreeSet<&str> = mount["Options"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .collect();
        let ok = scratch_source.is_some_and(|src| {
            mount["Type"] == "bind"
                && mount["Source"] == src
                && mount["Destination"] == "/scratch"
                && ["noexec", "nosuid", "nodev"]
                    .iter()
                    .all(|o| options.contains(o))
        });
        if !ok {
            let dest = match &mount["Destination"] {
                Value::String(s) => s.clone(),
                _ => "None".into(),
            };
            problems.push(format!("Mount {dest} nicht erlaubt"));
        }
    }
    for bind in host["Binds"].as_array().into_iter().flatten() {
        let ok = scratch_source.is_some_and(|src| {
            bind.as_str()
                .is_some_and(|b| b.starts_with(&format!("{src}:/scratch:")))
        });
        if !ok {
            problems.push("Bind-Mount nicht erlaubt".into());
        }
    }
    let foreign_tmpfs = host["Tmpfs"].as_object().is_some_and(|tmpfs| {
        tmpfs
            .keys()
            .any(|k| !["/workspace", "/tmp", "/scratch"].contains(&k.as_str()))
    });
    if foreign_tmpfs {
        problems.push("fremdes tmpfs".into());
    }
    let bindings = host["PortBindings"]
        .as_object()
        .cloned()
        .unwrap_or_default();
    let wanted: std::collections::BTreeMap<String, String> = preview_port
        .map(|p| {
            [
                (format!("{PREVIEW_CONTAINER_PORT}/tcp"), p.to_string()),
                (
                    format!("{TLS_CONTAINER_PORT}/tcp"),
                    (p + TLS_PORT_OFFSET).to_string(),
                ),
            ]
            .into_iter()
            .collect()
        })
        .unwrap_or_default();
    if !wanted.is_empty()
        && bindings.keys().collect::<std::collections::BTreeSet<_>>()
            != wanted.keys().collect::<std::collections::BTreeSet<_>>()
    {
        problems.push("Port-Export weicht ab (http und https erwartet)".into());
    }
    for (port, entries) in &bindings {
        for entry in entries.as_array().into_iter().flatten() {
            let host_port = match &entry["HostPort"] {
                Value::String(s) => s.clone(),
                Value::Null => "None".into(),
                other => other.to_string(),
            };
            if wanted.get(port) != Some(&host_port) || entry["HostIp"] != expose_bind {
                problems.push(format!("Port {port} weicht ab"));
            }
        }
    }
    if info["Config"]["User"] != USER {
        problems.push("Nutzer weicht ab".into());
    }
    problems
}

#[cfg(test)]
mod tests;
