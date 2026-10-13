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
pub const PREVIEW_CONTAINER_PORT: u64 = 8080;
pub const RELAY_BASE_PORT: u64 = 20000;
pub const CPU_PERIOD: u64 = 100_000;
/// Größe von `/tmp` (tmpfs) in MB, zusätzlich zu `/workspace`.
pub const TMP_MB: u64 = 256;
pub const NOFILE: u64 = 4096;
/// Vorschau-Ports am Rechner (Entscheidung 8x), nur an 127.0.0.1.
pub const PREVIEW_PORTS: std::ops::Range<u64> = 18080..18090;
pub const NETWORK_MODES: [&str; 3] = ["none", "allowlist", "default"];

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
static RELAY_TARGET_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Za-z0-9][A-Za-z0-9.:-]{0,252}:\d{1,5}$").unwrap());
pub static UUID_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$").unwrap()
});
/// Image nur mit Digest (wie `protocol.IMAGE_RE`).
static IMAGE_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[a-z0-9][a-z0-9._/:-]{0,254}@sha256:[0-9a-f]{64}$").unwrap());

const ALLOWED_KEYS: [&str; 30] = [
    "name",
    "image",
    "command",
    "labels",
    "user",
    "hostname",
    "read_only_filesystem",
    "cap_drop",
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
];

/// Feste Werte; jede Abweichung wird abgelehnt.
fn fixed() -> Vec<(&'static str, Value)> {
    vec![
        ("user", json!(USER)),
        ("read_only_filesystem", json!(true)),
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
    ]
}

/// `userns` beim lokalen Scratchpad: Nutzer 1000 im Container = Nutzer des Rechners.
pub fn keep_id() -> Value {
    json!({"nsmode": "keep-id", "value": "uid=1000,gid=1000"})
}

pub fn env() -> Value {
    Value::Object(ENV.iter().map(|(k, v)| (k.to_string(), json!(v))).collect())
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
}

impl Default for Bounds {
    fn default() -> Self {
        Self {
            images: BTreeSet::new(),
            max_cpus: 8.0,
            max_memory_mb: 16384,
            max_pids: 4096,
            max_tmpfs_mb: 8192,
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
}

impl Limits {
    pub fn as_value(&self) -> Value {
        json!({
            "cpus": self.cpus,
            "memory_mb": self.memory_mb,
            "pids": self.pids,
            "tmpfs_mb": self.tmpfs_mb,
            "network": self.network,
        })
    }

    pub fn from_value(value: &Value) -> Option<Self> {
        Some(Self {
            cpus: value.get("cpus")?.as_f64()?,
            memory_mb: value.get("memory_mb")?.as_u64()?,
            pids: value.get("pids")?.as_u64()?,
            tmpfs_mb: value.get("tmpfs_mb")?.as_u64()?,
            network: value.get("network")?.as_str()?.to_string(),
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
            {"type": "fsize", "soft": limits.tmpfs_mb << 20, "hard": limits.tmpfs_mb << 20},
            {"type": "core", "soft": 0, "hard": 0},
        ],
        "env": env(),
        "stop_timeout": 5,
    });
    let map = body.as_object_mut().unwrap();
    for (key, value) in fixed() {
        map.insert(key.to_string(), value);
    }
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
/// `network` (intern ohne Gateway bei „aus“ und Positivliste, mit Gateway bei „default“).
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
        map.insert(
            "portmappings".into(),
            json!([{
                "host_ip": "127.0.0.1",
                "host_port": port,
                "container_port": PREVIEW_CONTAINER_PORT,
                "protocol": "tcp",
            }]),
        );
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

fn helper_limits() -> Limits {
    Limits {
        cpus: 0.5,
        memory_mb: 128,
        pids: 64,
        tmpfs_mb: 16,
        network: "allowlist".into(),
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
    expect(
        map.get("env") == Some(&env()),
        "Umgebung weicht von der Vorlage ab.",
    )?;
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
                networks.keys().next().unwrap().starts_with("mgpt-net-"),
                "Arbeitscontainer nur am Sitzungsnetz.",
            )?;
            expect(
                port_list.is_some_and(|p| p.len() <= 1),
                "Höchstens ein Port.",
            )?;
            for port in port_list.unwrap() {
                let host_port = port.get("host_port").cloned().unwrap_or(Value::Null);
                expect(
                    *port
                        == json!({
                            "host_ip": "127.0.0.1",
                            "host_port": host_port,
                            "container_port": PREVIEW_CONTAINER_PORT,
                            "protocol": "tcp",
                        }),
                    "Port nur 8080 an 127.0.0.1.",
                )?;
                expect(
                    host_port
                        .as_u64()
                        .is_some_and(|p| PREVIEW_PORTS.contains(&p)),
                    "Port außerhalb 18080–18089.",
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
            let ok = command.is_some_and(|c| {
                c.len() >= 4
                    && c.len() <= 53
                    && c[..3]
                        == [
                            json!("mgpt-relay"),
                            json!("--base"),
                            json!(RELAY_BASE_PORT.to_string()),
                        ]
                    && c[3..]
                        .iter()
                        .all(|t| t.as_str().is_some_and(|s| RELAY_TARGET_RE.is_match(s)))
            });
            expect(ok, "Relay-Befehl weicht ab.")?;
            expect(port_list.is_some_and(Vec::is_empty), "Relay ohne Ports.")?;
            let rel = network_name(session, "rel");
            let out = network_name(session, "out");
            expect(
                net_names == BTreeSet::from([rel.as_str(), out.as_str()]),
                "Relay nur an Relay-Netz und Ausgang.",
            )?;
        }
    }
    Ok(role)
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
    if !truthy(&host["ReadonlyRootfs"]) {
        problems.push("Wurzeldateisystem beschreibbar".into());
    }
    if truthy(&host["CapAdd"]) {
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
    if let Some(bindings) = host["PortBindings"].as_object() {
        let expected_port = preview_port.map(|p| p.to_string()).unwrap_or("None".into());
        for (port, entries) in bindings {
            for entry in entries.as_array().into_iter().flatten() {
                let host_port = match &entry["HostPort"] {
                    Value::String(s) => s.clone(),
                    Value::Null => "None".into(),
                    other => other.to_string(),
                };
                if port != &format!("{PREVIEW_CONTAINER_PORT}/tcp")
                    || entry["HostIp"] != "127.0.0.1"
                    || host_port != expected_port
                {
                    problems.push(format!("Port {port} weicht ab"));
                }
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
