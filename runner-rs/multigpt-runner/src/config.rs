//! Einstellungen des Agents aus der Umgebung (wie `runner_agent/config.py`).
//!
//! | Variable | Bedeutung |
//! |---|---|
//! | `MGPT_URL` | `https://host` bzw. `wss://host/runner/ws` (Pflicht) |
//! | `MGPT_TOKEN_FILE` | Secret-Datei mit dem Token (Vorgabe `/run/secrets/token`) |
//! | `MGPT_CA_SHA256` | festgehaltener SHA-256 des Server-Zertifikats (snakeoil, 8i) |
//! | `MGPT_PODMAN_SOCKET` | Podman-Socket (Vorgabe `/run/podman.sock`) |
//! | `MGPT_IMAGES` | lokale Positivliste der Images (mit Digest, durch Leerzeichen getrennt) |
//! | `MGPT_INSECURE_DEV` | `1` erlaubt `ws://` (nur Entwicklung) |
//! | `MGPT_HOSTNAME` | Name des Rechners im hello (Vorgabe: Rechnername) |
//! | `MGPT_SCRATCH_DIR`, `MGPT_SCRATCH_HOST_DIR` | lokales Scratchpad (`…/MultiGPT/Scratch`) |
//! | `MGPT_LABELS` | zusätzliche Labels `k=v k2=v2` (Tests: `multigpt.test=1`) |
//! | `MGPT_ENGINE` | `podman` (Vorgabe); `fake` nur mit Feature `fake-engine` und `MGPT_INSECURE_DEV=1` |
//! | `MGPT_SERVER_KEY_FILE` | öffentlicher Ed25519-Schlüssel der Installation (eine Zeile Base64) |
//! | `MGPT_CATALOG_FILE`, `MGPT_CATALOG_SIG_FILE`, `MGPT_CATALOG_KEY` | Dienstkatalog statt des eingebauten |
//!
//! Abweichend vom Python-Agent (Container mit `/run/secrets/…`) liegen Token und
//! Server-Schlüssel ohne Angabe im Konfigurationsordner des Nutzers:
//! `$XDG_CONFIG_HOME/multigpt-runner/` bzw. `~/.config/multigpt-runner/`
//! (`token`, `server.pub`), wie sie `multigpt-runner pair` anlegt.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fmt;
use std::path::{Component, Path, PathBuf};
use std::sync::LazyLock;

use regex::Regex;

use crate::protocol;

static FINGERPRINT_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[0-9a-f]{64}$").expect("festes Muster"));

/// Einstellung fehlt oder ist ungültig (Exit-Code 2).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConfigError(pub String);

impl fmt::Display for ConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for ConfigError {}

fn err(text: impl Into<String>) -> ConfigError {
    ConfigError(text.into())
}

#[derive(Debug, Clone, PartialEq)]
pub struct Config {
    /// `wss://host/runner/ws`
    pub url: String,
    pub token_file: PathBuf,
    /// Festgehaltener Fingerprint (Kleinbuchstaben-Hex, ohne Doppelpunkte).
    pub ca_sha256: Option<String>,
    pub podman_socket: PathBuf,
    /// Lokale Positivliste der Images.
    pub images: BTreeSet<String>,
    /// Nur Entwicklung: `ws://` erlaubt.
    pub insecure_dev: bool,
    pub hostname: String,
    /// Lokale Obergrenzen, egal was der Server schickt.
    pub max_cpus: f64,
    pub max_memory_mb: u64,
    pub max_pids: u64,
    pub max_tmpfs_mb: u64,
    /// Adresse des Port-Exports 8080/8443 → 10080/10443 (`0.0.0.0` Heimnetz, `127.0.0.1`
    /// nur der Rechner; Nutzerentscheidung 2026-10-14).
    pub expose_bind: String,
    /// Lokales Scratchpad: Ordner `~/MultiGPT/Scratch` – Sicht des Agents.
    pub scratch_dir: Option<PathBuf>,
    /// … und Sicht von Podman (Host).
    pub scratch_host_dir: Option<PathBuf>,
    /// Zusätzliche Labels an allen Podman-Objekten.
    pub labels: BTreeMap<String, String>,
    /// `podman` oder (nur Tests) `fake`.
    pub engine: String,
    /// Öffentlicher Schlüssel der Installation (signierte Befehle des Servers).
    pub server_key_file: PathBuf,
    /// Dienstkatalog als Datei (statt des eingebauten), mit Signatur und key_id.
    pub catalog_file: Option<PathBuf>,
    pub catalog_sig_file: Option<PathBuf>,
    pub catalog_key: Option<String>,
}

/// Konfigurationsordner des Agents: `$XDG_CONFIG_HOME/multigpt-runner` bzw.
/// `~/.config/multigpt-runner` (ohne HOME: `/etc/multigpt-runner`).
pub fn config_dir_from(env: &HashMap<String, String>) -> PathBuf {
    let get = |name: &str| env.get(name).filter(|v| v.starts_with('/'));
    if let Some(xdg) = get("XDG_CONFIG_HOME") {
        return PathBuf::from(xdg).join("multigpt-runner");
    }
    match get("HOME") {
        Some(home) => PathBuf::from(home).join(".config/multigpt-runner"),
        None => PathBuf::from("/etc/multigpt-runner"),
    }
}

/// Dienstkatalog fürs hello: (Text, Signatur Base64, key_id).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CatalogInfo {
    pub text: String,
    pub sig: Option<String>,
    pub key: Option<String>,
}

impl Config {
    /// Einstellungen mit Vorgaben (für Tests).
    pub fn new(url: impl Into<String>) -> Self {
        Self {
            url: url.into(),
            token_file: PathBuf::from("/run/secrets/token"),
            ca_sha256: None,
            podman_socket: PathBuf::from("/run/podman.sock"),
            images: BTreeSet::new(),
            insecure_dev: false,
            hostname: String::new(),
            max_cpus: 8.0,
            max_memory_mb: 16384,
            max_pids: 4096,
            max_tmpfs_mb: 8192,
            scratch_dir: None,
            expose_bind: "0.0.0.0".to_string(),
            scratch_host_dir: None,
            labels: BTreeMap::new(),
            engine: "podman".into(),
            server_key_file: PathBuf::from("/run/secrets/server_key"),
            catalog_file: None,
            catalog_sig_file: None,
            catalog_key: None,
        }
    }

    /// Öffentlicher Schlüssel der Installation (32 Byte roh, eine Zeile Base64).
    pub fn read_server_key(&self) -> Result<ed25519_dalek::VerifyingKey, ConfigError> {
        let text = std::fs::read(&self.server_key_file).map_err(|e| {
            err(format!(
                "Schlüsseldatei des Servers nicht lesbar: {} ({})",
                self.server_key_file.display(),
                io_reason(&e)
            ))
        })?;
        let text = String::from_utf8(text).map_err(|_| {
            err(format!(
                "Schlüsseldatei des Servers nicht lesbar: {} (nicht lesbar)",
                self.server_key_file.display()
            ))
        })?;
        protocol::signed::verifying_key_from_b64(&text)
            .ok_or_else(|| err("Schlüsseldatei des Servers enthält keinen Ed25519-Schlüssel."))
    }

    /// Dienstkatalog: Datei (MGPT_CATALOG_FILE) oder der eingebaute aus dem Build.
    pub fn read_catalog(&self) -> Result<CatalogInfo, ConfigError> {
        let Some(file) = &self.catalog_file else {
            return Ok(crate::catalog::embedded());
        };
        let read = |path: &Path| -> Result<String, ConfigError> {
            let bytes =
                std::fs::read(path).map_err(|_| err("Dienstkatalog nicht lesbar (OSError)."))?;
            String::from_utf8(bytes)
                .map_err(|_| err("Dienstkatalog nicht lesbar (UnicodeDecodeError)."))
        };
        let text = read(file)?;
        let sig = match &self.catalog_sig_file {
            Some(path) => Some(read(path)?.trim().to_string()).filter(|s| !s.is_empty()),
            None => None,
        };
        if sig.is_some()
            && !self
                .catalog_key
                .as_deref()
                .is_some_and(|k| protocol::KEY_ID_RE.is_match(k))
        {
            return Err(err("MGPT_CATALOG_KEY (key_id, 16 hex) fehlt zur Signatur."));
        }
        let key = sig.as_ref().and(self.catalog_key.clone());
        Ok(CatalogInfo { text, sig, key })
    }

    /// Token aus der Secret-Datei (nie aus der Umgebung, nie geloggt).
    pub fn read_token(&self) -> Result<String, ConfigError> {
        let raw = std::fs::read_to_string(&self.token_file).map_err(|e| {
            err(format!(
                "Token-Datei nicht lesbar: {} ({})",
                self.token_file.display(),
                io_reason(&e)
            ))
        })?;
        let token = raw.trim();
        if !protocol::is_token(token) {
            return Err(err("Token-Datei enthält keinen gültigen Token."));
        }
        Ok(token.to_string())
    }

    /// Einstellungen aus der Umgebung des Prozesses.
    pub fn from_process_env() -> Result<Self, ConfigError> {
        let env: HashMap<String, String> = std::env::vars().collect();
        Self::from_env(&env)
    }

    /// Einstellungen aus einer Umgebung (im Test eine eigene Tabelle).
    pub fn from_env(env: &HashMap<String, String>) -> Result<Self, ConfigError> {
        let get = |name: &str| env.get(name).map(String::as_str).filter(|v| !v.is_empty());
        let insecure = env.get("MGPT_INSECURE_DEV").map(String::as_str) == Some("1");
        let Some(url) = get("MGPT_URL") else {
            return Err(err("MGPT_URL fehlt."));
        };
        let mut images = BTreeSet::new();
        for image in get("MGPT_IMAGES").unwrap_or("").split_whitespace() {
            if !protocol::IMAGE_RE.is_match(image) {
                return Err(err(format!("MGPT_IMAGES: Image ohne Digest: {image}")));
            }
            images.insert(image.to_string());
        }
        let engine = get("MGPT_ENGINE").unwrap_or("podman").to_string();
        match engine.as_str() {
            "podman" => {}
            "fake" if cfg!(feature = "fake-engine") && insecure => {}
            "fake" => {
                return Err(err(
                    "MGPT_ENGINE=fake nur in Testbauten (Feature fake-engine) mit MGPT_INSECURE_DEV=1.",
                ));
            }
            other => return Err(err(format!("MGPT_ENGINE unbekannt: {other}"))),
        }
        let hostname = get("MGPT_HOSTNAME")
            .map(str::to_string)
            .unwrap_or_else(system_hostname);
        let mut config = Config::new(normalize_url(url, insecure)?);
        let dir = config_dir_from(env);
        config.token_file = get("MGPT_TOKEN_FILE").map_or_else(|| dir.join("token"), PathBuf::from);
        config.server_key_file =
            get("MGPT_SERVER_KEY_FILE").map_or_else(|| dir.join("server.pub"), PathBuf::from);
        config.catalog_file = get("MGPT_CATALOG_FILE").map(PathBuf::from);
        config.catalog_sig_file = get("MGPT_CATALOG_SIG_FILE").map(PathBuf::from);
        config.catalog_key = get("MGPT_CATALOG_KEY").map(str::to_string);
        config.ca_sha256 = normalize_fingerprint(get("MGPT_CA_SHA256"))?;
        config.podman_socket =
            PathBuf::from(get("MGPT_PODMAN_SOCKET").unwrap_or("/run/podman.sock"));
        config.images = images;
        config.insecure_dev = insecure;
        config.expose_bind = match get("MGPT_EXPOSE_BIND").map(str::trim) {
            None | Some("") => "0.0.0.0".to_string(),
            Some(value @ ("0.0.0.0" | "127.0.0.1")) => value.to_string(),
            Some(_) => {
                return Err(ConfigError(
                    "MGPT_EXPOSE_BIND: nur 0.0.0.0 oder 127.0.0.1.".into(),
                ));
            }
        };
        config.scratch_dir = get("MGPT_SCRATCH_DIR").map(PathBuf::from);
        config.scratch_host_dir = host_dir(get("MGPT_SCRATCH_HOST_DIR"))?;
        config.hostname = hostname.chars().take(253).collect();
        config.labels = parse_labels(get("MGPT_LABELS").unwrap_or(""))?;
        config.engine = engine;
        Ok(config)
    }
}

fn io_reason(e: &std::io::Error) -> String {
    // Wie Pythons strerror: ohne „(os error n)“.
    let text = e.to_string();
    match text.find(" (os error") {
        Some(pos) => text[..pos].to_string(),
        None => text,
    }
}

fn system_hostname() -> String {
    std::fs::read_to_string("/proc/sys/kernel/hostname")
        .or_else(|_| std::fs::read_to_string("/etc/hostname"))
        .map(|s| s.trim().to_string())
        .unwrap_or_default()
}

/// `https://host` → `wss://host/runner/ws`; `ws://` nur mit `insecure_dev`.
pub fn normalize_url(raw: &str, insecure_dev: bool) -> Result<String, ConfigError> {
    let raw = raw.trim();
    let (scheme, rest) = raw.split_once("://").unwrap_or(("", raw));
    let scheme = scheme.to_ascii_lowercase();
    let scheme = match scheme.as_str() {
        "https" | "wss" => "wss",
        "http" | "ws" => "ws",
        _ => return Err(err("MGPT_URL muss mit https:// oder wss:// beginnen.")),
    };
    if scheme == "ws" && !insecure_dev {
        return Err(err(
            "Unverschlüsselte Verbindung nur mit MGPT_INSECURE_DEV=1.",
        ));
    }
    // Abfrage und Fragment entfallen (wie urlunsplit mit leerem query/fragment).
    let rest = rest.split(['?', '#']).next().unwrap_or("");
    let (netloc, path) = match rest.find('/') {
        Some(pos) => (&rest[..pos], &rest[pos..]),
        None => (rest, ""),
    };
    if host_of(netloc).is_empty() {
        return Err(err("MGPT_URL ohne Rechnernamen."));
    }
    let mut path = path.trim_end_matches('/').to_string();
    if !path.ends_with("/runner/ws") {
        path.push_str("/runner/ws");
    }
    Ok(format!("{scheme}://{netloc}{path}"))
}

/// Rechnername aus `user:pass@host:port` bzw. `[::1]:port`.
fn host_of(netloc: &str) -> &str {
    let hostport = netloc.rsplit_once('@').map_or(netloc, |(_, h)| h);
    if let Some(rest) = hostport.strip_prefix('[') {
        return rest.split(']').next().unwrap_or("");
    }
    hostport.split(':').next().unwrap_or("")
}

/// Fingerprint vereinheitlichen: Kleinbuchstaben, ohne `:` und ohne `sha256=`.
pub fn normalize_fingerprint(raw: Option<&str>) -> Result<Option<String>, ConfigError> {
    let Some(raw) = raw.filter(|r| !r.is_empty()) else {
        return Ok(None);
    };
    let mut value = raw.trim().to_lowercase().replace(':', "");
    if let Some(rest) = value.strip_prefix("sha256=") {
        value = rest.to_string();
    }
    if !FINGERPRINT_RE.is_match(&value) {
        return Err(err("MGPT_CA_SHA256 ist kein SHA-256-Fingerprint."));
    }
    Ok(Some(value))
}

/// Pfad wie `os.path.normpath` (ohne Dateisystemzugriff).
fn normpath(raw: &str) -> PathBuf {
    let mut out: Vec<String> = Vec::new();
    let absolute = raw.starts_with('/');
    for part in Path::new(raw).components() {
        match part {
            Component::ParentDir => {
                if out.last().is_some_and(|p| p != "..") {
                    out.pop();
                } else if !absolute {
                    out.push("..".into());
                }
            }
            Component::Normal(name) => out.push(name.to_string_lossy().into_owned()),
            Component::RootDir | Component::CurDir | Component::Prefix(_) => {}
        }
    }
    let joined = out.join("/");
    match (absolute, joined.is_empty()) {
        (true, _) => PathBuf::from(format!("/{joined}")),
        (false, true) => PathBuf::from("."),
        (false, false) => PathBuf::from(joined),
    }
}

fn host_dir(raw: Option<&str>) -> Result<Option<PathBuf>, ConfigError> {
    let Some(raw) = raw else {
        return Ok(None);
    };
    let path = normpath(raw);
    let text = path.to_string_lossy();
    if !path.is_absolute() || !text.ends_with("/MultiGPT/Scratch") {
        return Err(err(
            "MGPT_SCRATCH_HOST_DIR muss auf …/MultiGPT/Scratch zeigen.",
        ));
    }
    Ok(Some(path))
}

/// `MGPT_LABELS`: `k=v k2=v2` (Schlüssel wie Podman-Labels, ohne Leerzeichen).
fn parse_labels(raw: &str) -> Result<BTreeMap<String, String>, ConfigError> {
    static KEY_RE: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$").expect("Muster"));
    let mut labels = BTreeMap::new();
    for item in raw.split_whitespace() {
        let (key, value) = item.split_once('=').unwrap_or((item, ""));
        if !KEY_RE.is_match(key) || value.len() > 256 {
            return Err(err(format!("MGPT_LABELS: ungültiges Label {item}")));
        }
        labels.insert(key.to_string(), value.to_string());
    }
    Ok(labels)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn env(pairs: &[(&str, &str)]) -> HashMap<String, String> {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    }

    #[test]
    fn url_forms() {
        assert_eq!(
            normalize_url("https://mgpt.local", false).unwrap(),
            "wss://mgpt.local/runner/ws"
        );
        assert_eq!(
            normalize_url(" https://mgpt.local:8443/base/ ", false).unwrap(),
            "wss://mgpt.local:8443/base/runner/ws"
        );
        assert_eq!(
            normalize_url("wss://h/runner/ws?x=1", false).unwrap(),
            "wss://h/runner/ws"
        );
        assert!(normalize_url("ws://h", false).is_err());
        assert_eq!(
            normalize_url("http://h:8000", true).unwrap(),
            "ws://h:8000/runner/ws"
        );
        assert!(normalize_url("ftp://h", true).is_err());
        assert!(normalize_url("https://", false).is_err());
        assert!(normalize_url("https://:443/", false).is_err());
        assert_eq!(
            normalize_url("https://[::1]:8443", false).unwrap(),
            "wss://[::1]:8443/runner/ws"
        );
    }

    #[test]
    fn fingerprint_forms() {
        let hex = "ab".repeat(32);
        let colon = hex
            .as_bytes()
            .chunks(2)
            .map(|c| std::str::from_utf8(c).unwrap().to_uppercase())
            .collect::<Vec<_>>()
            .join(":");
        assert_eq!(
            normalize_fingerprint(Some(&colon)).unwrap(),
            Some(hex.clone())
        );
        assert_eq!(
            normalize_fingerprint(Some(&format!("sha256={hex}"))).unwrap(),
            Some(hex)
        );
        assert!(normalize_fingerprint(Some("abc")).is_err());
        assert_eq!(normalize_fingerprint(None).unwrap(), None);
        assert_eq!(normalize_fingerprint(Some("")).unwrap(), None);
    }

    #[test]
    fn from_env_values() {
        let image = format!("ghcr.io/x/work:1@sha256:{}", "a".repeat(64));
        let config = Config::from_env(&env(&[
            ("MGPT_URL", "https://mgpt.local"),
            ("MGPT_IMAGES", &format!(" {image} ")),
            ("MGPT_HOSTNAME", "pc"),
            ("MGPT_SCRATCH_HOST_DIR", "/home/u/MultiGPT/Scratch/"),
            ("MGPT_LABELS", "multigpt.test=1 multigpt.test.run=r1"),
            ("HOME", "/home/u"),
        ]))
        .unwrap();
        assert_eq!(config.url, "wss://mgpt.local/runner/ws");
        assert!(config.images.contains(&image));
        assert_eq!(config.hostname, "pc");
        assert_eq!(
            config.token_file,
            PathBuf::from("/home/u/.config/multigpt-runner/token")
        );
        assert_eq!(
            config.server_key_file,
            PathBuf::from("/home/u/.config/multigpt-runner/server.pub")
        );
        assert_eq!(
            config.scratch_host_dir,
            Some(PathBuf::from("/home/u/MultiGPT/Scratch"))
        );
        assert_eq!(config.labels["multigpt.test.run"], "r1");
        assert_eq!(config.engine, "podman");
    }

    #[test]
    fn from_env_errors() {
        assert_eq!(
            Config::from_env(&env(&[])).unwrap_err().0,
            "MGPT_URL fehlt."
        );
        assert!(
            Config::from_env(&env(&[("MGPT_URL", "https://h"), ("MGPT_IMAGES", "x:1")])).is_err()
        );
        assert!(
            Config::from_env(&env(&[
                ("MGPT_URL", "https://h"),
                ("MGPT_SCRATCH_HOST_DIR", "/tmp/x")
            ]))
            .is_err()
        );
        assert!(
            Config::from_env(&env(&[
                ("MGPT_URL", "https://h"),
                (
                    "MGPT_SCRATCH_HOST_DIR",
                    "/home/u/MultiGPT/Scratch/../../etc"
                )
            ]))
            .is_err()
        );
        assert!(
            Config::from_env(&env(&[
                ("MGPT_URL", "https://h"),
                ("MGPT_ENGINE", "docker")
            ]))
            .is_err()
        );
        // fake nie ohne MGPT_INSECURE_DEV
        assert!(
            Config::from_env(&env(&[("MGPT_URL", "https://h"), ("MGPT_ENGINE", "fake")])).is_err()
        );
    }

    #[test]
    fn token_file() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("token");
        let mut config = Config::new("wss://h/runner/ws");
        config.token_file = path.clone();
        assert!(
            config
                .read_token()
                .unwrap_err()
                .0
                .starts_with("Token-Datei nicht lesbar")
        );
        std::fs::write(&path, "kurz\n").unwrap();
        assert_eq!(
            config.read_token().unwrap_err().0,
            "Token-Datei enthält keinen gültigen Token."
        );
        std::fs::write(&path, format!("{}\n", "t".repeat(40))).unwrap();
        assert_eq!(config.read_token().unwrap(), "t".repeat(40));
    }

    #[test]
    fn server_key_and_catalog() {
        let dir = tempfile::tempdir().unwrap();
        let mut config = Config::new("wss://h/runner/ws");
        config.server_key_file = dir.path().join("server.pub");
        assert!(
            config
                .read_server_key()
                .unwrap_err()
                .0
                .starts_with("Schlüsseldatei des Servers nicht lesbar")
        );
        std::fs::write(&config.server_key_file, "QUJD\n").unwrap();
        assert_eq!(
            config.read_server_key().unwrap_err().0,
            "Schlüsseldatei des Servers enthält keinen Ed25519-Schlüssel."
        );
        let sk = ed25519_dalek::SigningKey::from_bytes(&[5; 32]);
        std::fs::write(
            &config.server_key_file,
            format!("{}\n", protocol::b64encode(sk.verifying_key().as_bytes())),
        )
        .unwrap();
        assert_eq!(config.read_server_key().unwrap(), sk.verifying_key());
        // Katalog aus Datei; Signatur nur mit key_id
        config.catalog_file = Some(dir.path().join("catalog.json"));
        std::fs::write(dir.path().join("catalog.json"), "{}").unwrap();
        std::fs::write(dir.path().join("catalog.json.sig"), "c2ln\n").unwrap();
        assert_eq!(config.read_catalog().unwrap().sig, None);
        config.catalog_sig_file = Some(dir.path().join("catalog.json.sig"));
        assert!(config.read_catalog().is_err());
        config.catalog_key = Some("0123456789abcdef".into());
        let info = config.read_catalog().unwrap();
        assert_eq!(info.sig.as_deref(), Some("c2ln"));
        assert_eq!(info.key.as_deref(), Some("0123456789abcdef"));
    }
}
