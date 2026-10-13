//! Einrichtungswerkzeug `setup_host`: Zielsystem per SSH-Schlüssel einrichten.
//!
//! Ersetzt das Alltags-`ssh` (Entscheidung vom 2026-10-13). Der Server setzt
//! nach einer Rückfrage (immer) den Zugang und die Kopplungsdaten ein; das
//! Modell nennt nur den Hostnamen. Ablauf:
//!
//! 1. Zugang (`known_hosts` mit dem festgehaltenen Host-Schlüssel, privater
//!    Schlüssel) nur im Hilfscontainer ablegen – nie im Arbeitscontainer.
//! 2. `uname -m` auf dem Ziel → `amd64` bzw. `arm64`.
//! 3. Das Programm lädt der Agent selbst von MultiGPT
//!    (`<MGPT_URL>/runner/download/<arch>`, gleiches TLS-Pinning wie die
//!    Verbindung, `Authorization: Bearer <Kopplungs-Token>`) und prüft die
//!    SHA-256 aus den Kopplungsdaten.
//! 4. Übertragung per SSH-stdin nach `~/.local/bin/multigpt-runner.new`; die
//!    SHA-256 auf dem Ziel muss übereinstimmen, erst dann `chmod`/`mv`.
//! 5. Kopplung: `multigpt-runner pair --url … --images … [--ca-sha256 …]`, Token und
//!    Server-Schlüssel nur über stdin (nie in Argumenten, Ausgaben maskiert).
//!
//! Auf dem Ziel laufen nur feste Befehle; alle eingesetzten Werte sind vorher
//! gegen enge Muster geprüft (keine Anführungszeichen, kein `$`).

use std::sync::LazyLock;

use base64::Engine as _;
use regex::Regex;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use super::host::{Target, ToolError, ToolHost, ToolResult};
use super::tools::{Outcome, SessionTools, truncate_chars};

pub const NAME: &str = "setup_host";
/// Größte zulässige Programmdatei.
pub const MAX_BINARY_BYTES: usize = 128 * 1024 * 1024;
/// Verzeichnis des Zugangs im Hilfscontainer.
pub const SSH_DIR: &str = "/tmp/.mgpt-ssh";
const STEP_TIMEOUT: u64 = 60;
const UPLOAD_TIMEOUT: u64 = 600;

static KEY_LINE_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[a-z0-9@.-]+ [A-Za-z0-9+/=]+$").expect("Muster"));
static USER_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Za-z0-9._-]{1,64}$").expect("Muster"));
static URL_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$").expect("Muster")
});
static TOKEN_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^mgrt_[a-z0-9]{10}_[A-Za-z0-9_-]{40,64}$").expect("Muster"));
static IMAGE_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^[a-z0-9][a-z0-9._/:-]{0,250}@sha256:[0-9a-f]{64}$").expect("Muster")
});
static SHA256_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[0-9a-f]{64}$").expect("Muster"));

/// Schema der Kopplungsdaten (setzt der Server ein).
pub fn pairing_schema() -> Value {
    let download = json!({
        "type": "object",
        "properties": {"url": {"type": "string"}, "sha256": {"type": "string"}},
        "required": ["url", "sha256"],
    });
    json!({
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "token": {"type": "string"},
            "ca_sha256": {"type": "string"},
            "server_key": {"type": "string"},
            "download": {
                "type": "object",
                "properties": {"amd64": download, "arm64": download},
            },
        },
        "required": ["url", "token", "server_key", "download"],
    })
}

/// Architektur aus `uname -m`.
pub fn arch_of(machine: &str) -> Option<&'static str> {
    match machine.trim() {
        "x86_64" | "amd64" => Some("amd64"),
        "aarch64" | "arm64" | "armv8l" => Some("arm64"),
        _ => None,
    }
}

/// Geprüfte Kopplungsdaten.
#[derive(Debug, Clone)]
pub struct Pairing {
    pub url: String,
    pub token: String,
    pub ca_sha256: Option<String>,
    pub server_key: String,
    /// Arbeits-Images mit Digest (`pair --images`).
    pub images: Vec<String>,
    /// (arch, url, sha256); die URL liegt immer unter `MGPT_URL` dieses Agents.
    pub download: Vec<(String, String, String)>,
}

fn text<'a>(map: &'a Map<String, Value>, key: &str) -> Option<&'a str> {
    map.get(key)
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
}

/// Kopplungsdaten prüfen; `server_url` ist `MGPT_URL` dieses Agents.
pub fn parse_pairing(args: &Map<String, Value>, server_url: &str) -> ToolResult<Pairing> {
    let Some(Value::Object(pairing)) = args.get("pairing") else {
        return Err(ToolError::new(
            "Kopplungsdaten fehlen (der Server setzt sie nach der Rückfrage ein).",
        ));
    };
    let server_url = server_url.trim_end_matches('/');
    let url = text(pairing, "url")
        .map(|u| u.trim_end_matches('/'))
        .filter(|u| URL_RE.is_match(u))
        .ok_or_else(|| ToolError::new("Adresse von MultiGPT in den Kopplungsdaten ungültig."))?;
    if url.starts_with("http://") && !server_url.starts_with("http://") {
        return Err(ToolError::new("Kopplung nur über https."));
    }
    let token = text(pairing, "token")
        .filter(|t| TOKEN_RE.is_match(t))
        .ok_or_else(|| ToolError::new("Kopplungs-Token fehlt oder ist ungültig."))?;
    let ca_sha256 = match pairing.get("ca_sha256") {
        None | Some(Value::Null) => None,
        Some(Value::String(s)) if s.is_empty() => None,
        Some(Value::String(s)) if SHA256_RE.is_match(&s.to_lowercase().replace(':', "")) => {
            Some(s.to_lowercase().replace(':', ""))
        }
        _ => return Err(ToolError::new("Fingerprint des Zertifikats ungültig.")),
    };
    let server_key = text(pairing, "server_key")
        .filter(|k| {
            base64::engine::general_purpose::STANDARD
                .decode(k)
                .is_ok_and(|raw| raw.len() == 32)
        })
        .ok_or_else(|| ToolError::new("Schlüssel des Servers fehlt oder ist ungültig."))?;
    let images: Vec<String> = match pairing.get("images") {
        Some(Value::String(list)) => list.split_whitespace().map(str::to_string).collect(),
        Some(Value::Array(list)) => list
            .iter()
            .map(|v| v.as_str().unwrap_or("").to_string())
            .collect(),
        _ => Vec::new(),
    };
    if images.is_empty() || images.iter().any(|image| !IMAGE_RE.is_match(image)) {
        return Err(ToolError::new(
            "Arbeits-Image (mit Digest) fehlt in den Kopplungsdaten.",
        ));
    }
    let Some(Value::Object(downloads)) = pairing.get("download") else {
        return Err(ToolError::new("Download-Angaben fehlen."));
    };
    let mut download = Vec::new();
    for arch in ["amd64", "arm64"] {
        let Some(Value::Object(entry)) = downloads.get(arch) else {
            continue;
        };
        // Geladen wird immer von MGPT_URL dieses Agents (gleiches Pinning wie die
        // Verbindung); die Adresse des Servers darf nur im Host abweichen.
        let path = format!("/runner/download/{arch}");
        let given = text(entry, "url").unwrap_or("");
        if !URL_RE.is_match(given) || !given.ends_with(&path) {
            return Err(ToolError::new(format!(
                "Download-Adresse für {arch} ist nicht …{path} von MultiGPT."
            )));
        }
        let file_url = format!("{server_url}{path}");
        let sha = text(entry, "sha256")
            .filter(|s| SHA256_RE.is_match(s))
            .ok_or_else(|| ToolError::new(format!("Prüfsumme für {arch} fehlt.")))?;
        download.push((arch.to_string(), file_url.to_string(), sha.to_string()));
    }
    if download.is_empty() {
        return Err(ToolError::new("Download-Angaben fehlen."));
    }
    Ok(Pairing {
        url: url.to_string(),
        token: token.to_string(),
        ca_sha256,
        server_key: server_key.to_string(),
        images,
        download,
    })
}

/// Feste SSH-Optionen (wie das frühere `ssh`-Werkzeug), nur Schlüssel.
pub fn ssh_argv(relay_ip: &str, relay_port: u16, user: &str, remote: &str) -> Vec<String> {
    let mut argv: Vec<String> = vec!["ssh".into(), "-F".into(), "/dev/null".into()];
    for option in [
        "StrictHostKeyChecking=yes".to_string(),
        format!("UserKnownHostsFile={SSH_DIR}/known_hosts"),
        "GlobalKnownHostsFile=/dev/null".into(),
        "HostKeyAlias=mgpt-host".into(),
        "UpdateHostKeys=no".into(),
        "ConnectTimeout=15".into(),
        "ServerAliveInterval=15".into(),
        "LogLevel=ERROR".into(),
        "ForwardAgent=no".into(),
        "ForwardX11=no".into(),
        "ClearAllForwardings=yes".into(),
        "IdentitiesOnly=yes".into(),
        format!("IdentityFile={SSH_DIR}/key"),
        "PasswordAuthentication=no".into(),
        "KbdInteractiveAuthentication=no".into(),
        "BatchMode=yes".into(),
    ] {
        argv.push("-o".into());
        argv.push(option);
    }
    argv.extend([
        "-p".into(),
        relay_port.to_string(),
        "-l".into(),
        user.into(),
        relay_ip.into(),
        "--".into(),
        remote.into(),
    ]);
    argv
}

/// Skript für `sh -c '…'` auf dem Ziel (ohne einfache Anführungszeichen, damit
/// es in jeder Login-Shell gleich ankommt).
fn remote_sh(script: &str) -> String {
    debug_assert!(!script.contains('\''));
    format!("sh -c '{script}'")
}

const BIN_DIR: &str = "\"$HOME/.local/bin\"";

fn upload_script() -> String {
    remote_sh(&format!(
        "umask 077; mkdir -p {BIN_DIR} && cat > {BIN_DIR}/multigpt-runner.new && \
         (sha256sum {BIN_DIR}/multigpt-runner.new 2>/dev/null || \
         shasum -a 256 {BIN_DIR}/multigpt-runner.new)"
    ))
}

fn install_script(pairing: &Pairing) -> String {
    let mut pair = format!(
        "{BIN_DIR}/multigpt-runner pair --url \"{}\" --images \"{}\"",
        pairing.url,
        pairing.images.join(" ")
    );
    if let Some(ca) = &pairing.ca_sha256 {
        pair += &format!(" --ca-sha256 {ca}");
    }
    remote_sh(&format!(
        "chmod 755 {BIN_DIR}/multigpt-runner.new && mv -f {BIN_DIR}/multigpt-runner.new \
         {BIN_DIR}/multigpt-runner && {pair}"
    ))
}

fn cleanup_script() -> String {
    remote_sh(&format!("rm -f {BIN_DIR}/multigpt-runner.new"))
}

/// Ausgabe für das Modell: Geheimnisse maskiert, gekürzt.
fn masked(data: &[u8], secrets: &[&str], limit: usize) -> String {
    let mut text = String::from_utf8_lossy(data).into_owned();
    for secret in secrets.iter().filter(|s| !s.is_empty()) {
        text = text.replace(secret, "****");
    }
    let text = text.trim().to_string();
    let count = text.chars().count();
    if count > limit {
        text.chars().skip(count - limit).collect()
    } else {
        text
    }
}

/// `setup_host` ausführen.
pub async fn run<H: ToolHost>(
    tools: &SessionTools<H>,
    args: &Map<String, Value>,
) -> ToolResult<Outcome> {
    let (host, relay_port) = tools.target(args)?;
    let host_key = text(host, "host_key").unwrap_or("").trim();
    if !KEY_LINE_RE.is_match(host_key) {
        return Err(ToolError::new(
            "Host-Schlüssel fehlt; ohne festgehaltenen Schlüssel keine Einrichtung (8v).",
        ));
    }
    let user = text(host, "user").unwrap_or("");
    if !USER_RE.is_match(user) {
        return Err(ToolError::new("Benutzername ungültig."));
    }
    if text(host, "secret_type") != Some("key") {
        return Err(ToolError::new(
            "Einrichtung nur mit SSH-Schlüssel (kein Passwort).",
        ));
    }
    let secret = text(host, "secret")
        .ok_or_else(|| ToolError::new("SSH-Schlüssel fehlt (der Server setzt ihn ein)."))?;
    let pairing = parse_pairing(args, &tools.host.server_url())?;
    let relay = tools
        .host
        .relay()
        .ok_or_else(|| ToolError::new("Für diese Sitzung ist kein Relay eingerichtet."))?;
    let name = text(host, "name").unwrap_or("Ziel").to_string();
    let limit = tools.host.limits().output_bytes;
    let hidden = [secret, pairing.token.as_str(), pairing.server_key.as_str()];

    let mut key = secret.as_bytes().to_vec();
    if !secret.ends_with('\n') {
        key.push(b'\n');
    }
    let files = [
        (
            "known_hosts",
            format!("mgpt-host {host_key}\n").into_bytes(),
        ),
        ("key", key),
    ];

    let helper = tools.host.open_helper().await?;
    let ssh = |remote: &str| ssh_argv(&relay.ip, relay_port, user, remote);
    let result = async {
        tools.put_secret_files(&helper, SSH_DIR, &files).await?;
        let exec = |argv: Vec<String>, stdin: Vec<u8>, timeout: u64| {
            let helper = helper.clone();
            async move {
                let mut wrapped = vec![
                    "timeout".to_string(),
                    "-k".into(),
                    "5".into(),
                    timeout.to_string(),
                ];
                wrapped.extend(argv);
                tools
                    .host
                    .exec(
                        Target::Helper(&helper),
                        wrapped,
                        stdin,
                        timeout + 15,
                        1024 * 1024,
                    )
                    .await
            }
        };
        let failed = |step: &str, out: &super::host::ExecOutput| {
            let mut message = format!("Einrichtung von {name}: {step} fehlgeschlagen");
            if out.code == Some(255) {
                message += " (SSH-Verbindung: Host-Schlüssel, Zugang oder Netz)";
            } else if out.code == Some(124) || out.timed_out {
                message += " (Zeitlimit)";
            }
            let detail = masked(&[&out.stderr[..], &out.stdout[..]].concat(), &hidden, 1000);
            if !detail.is_empty() {
                message += &format!(":\n{detail}");
            }
            ToolError::new(message)
        };

        // 1. Architektur
        let out = exec(ssh("uname -m"), Vec::new(), STEP_TIMEOUT).await?;
        if out.code != Some(0) {
            return Err(failed("Abfrage der Architektur", &out));
        }
        let machine = String::from_utf8_lossy(&out.stdout).trim().to_string();
        let arch = arch_of(&machine).ok_or_else(|| {
            ToolError::new(format!(
                "Architektur „{}“ wird nicht unterstützt (nur amd64, arm64).",
                truncate_chars(&machine, 40)
            ))
        })?;
        let (_, url, sha256) = pairing
            .download
            .iter()
            .find(|(a, _, _)| a == arch)
            .ok_or_else(|| ToolError::new(format!("Kein Programm für {arch} angeboten.")))?;

        // 2. Programm laden und prüfen
        let binary = tools
            .host
            .download(url, &pairing.token, MAX_BINARY_BYTES)
            .await?;
        let local = hex_sha256(&binary);
        if &local != sha256 {
            return Err(ToolError::new(
                "Prüfsumme des geladenen Programms stimmt nicht – Abbruch.",
            ));
        }

        // 3. Übertragen und auf dem Ziel prüfen
        let out = exec(ssh(&upload_script()), binary.clone(), UPLOAD_TIMEOUT).await?;
        let remote = String::from_utf8_lossy(&out.stdout)
            .split_whitespace()
            .next()
            .unwrap_or("")
            .to_lowercase();
        if out.code != Some(0) || &remote != sha256 {
            let _ = exec(ssh(&cleanup_script()), Vec::new(), STEP_TIMEOUT).await;
            if out.code != Some(0) {
                return Err(failed("Übertragung", &out));
            }
            return Err(ToolError::new(
                "Prüfsumme auf dem Zielsystem stimmt nicht – Programm entfernt, Abbruch.",
            ));
        }

        // 4. Installieren und koppeln (Token/Schlüssel nur über stdin)
        let stdin = format!("{}\n{}\n", pairing.token, pairing.server_key).into_bytes();
        let out = exec(ssh(&install_script(&pairing)), stdin, STEP_TIMEOUT * 2).await?;
        if out.code != Some(0) {
            return Err(failed("Kopplung", &out));
        }
        let report = masked(&out.stdout, &hidden, limit);
        let mut text = format!(
            "{name} eingerichtet: multigpt-runner ({arch}, SHA-256 {}) installiert und gekoppelt.",
            &sha256[..16]
        );
        if !report.is_empty() {
            text += &format!("\n{report}");
        }
        Ok((
            text,
            Some(json!({"arch": arch, "sha256": sha256, "paired": true})),
        ))
    }
    .await;
    tools.host.close_helper(&helper).await;
    result.map_err(|err| ToolError::new(masked(err.message.as_bytes(), &hidden, 4000)))
}

pub fn hex_sha256(data: &[u8]) -> String {
    Sha256::digest(data)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn arch_mapping() {
        assert_eq!(arch_of("x86_64\n"), Some("amd64"));
        assert_eq!(arch_of("aarch64"), Some("arm64"));
        assert_eq!(arch_of("armv7l"), None);
    }

    #[test]
    fn scripts_have_no_single_quotes_inside() {
        let pairing = Pairing {
            url: "https://mgpt.example/x".into(),
            token: "mgrt_geheimertoken".into(),
            ca_sha256: Some("ab".repeat(32)),
            server_key: "k".into(),
            images: vec![format!("localhost/w@sha256:{}", "0".repeat(64))],
            download: vec![],
        };
        for script in [upload_script(), install_script(&pairing), cleanup_script()] {
            let inner = &script["sh -c '".len()..script.len() - 1];
            assert!(!inner.contains('\''), "{script}");
            assert!(!script.contains(&pairing.token));
        }
        assert!(install_script(&pairing).contains("--ca-sha256 abab"));
    }
}
