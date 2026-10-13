//! Befehlszeile: Agent starten, `--version`, `pair` (Kopplung auf einem Rechner
//! bzw. Zielsystem einrichten).
//!
//! Exit-Codes wie der Python-Agent: 0 normal beendet, 2 Einstellungen fehlen oder
//! sind ungültig, 3 Server lehnt ab (4401/4403/4409/4426/4498) – die User-Unit
//! verhindert dann mit `RestartPreventExitStatus=2 3` eine Neustart-Schleife.
//! `pair`: 0 gekoppelt, 1 Fehler beim Einrichten des Dienstes, 2 falsche Eingaben.

use std::collections::HashMap;
use std::io::{BufRead, Write as _};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use tracing::{error, info};

use crate::VERSION;
use crate::config::{self, Config, ConfigError};
use crate::connection::{Agent, AgentOptions};
use crate::handler::FrameHandler;
use crate::outbox::Outbox;
use crate::protocol::{self, CLOSE_NORMAL};

/// Vorlage der systemd-User-Unit.
pub const UNIT_TEMPLATE: &str = include_str!("../systemd/multigpt-runner.service");
pub const UNIT_NAME: &str = "multigpt-runner.service";

pub const EXIT_OK: i32 = 0;
pub const EXIT_FAILED: i32 = 1;
pub const EXIT_CONFIG: i32 = 2;
pub const EXIT_REJECTED: i32 = 3;

const USAGE: &str = "\
Aufruf:
  multigpt-runner                 Agent starten (Einstellungen aus der Umgebung, MGPT_*)
  multigpt-runner --version       Version ausgeben
  multigpt-runner pair --url <URL> --images \"<image@sha256:…> …\" [--ca-sha256 <hex>] [--no-start]
                                  Kopplung einrichten; stdin: Zeile 1 Token, Zeile 2 Server-Schlüssel (Base64)
";

/// Protokollierung nach stderr (journald), Stufe über `RUST_LOG` (Vorgabe info).
pub fn init_logging() {
    let filter = tracing_subscriber::EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info"));
    let _ = tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_writer(std::io::stderr)
        .with_ansi(std::io::IsTerminal::is_terminal(&std::io::stderr()))
        .with_target(false)
        .try_init();
}

/// Einstieg des Programms; `make` baut den Handler (Sitzungen, Werkzeuge).
pub fn main_with<H, F>(args: Vec<String>, make: F) -> i32
where
    H: FrameHandler,
    F: FnOnce(&Config, Outbox) -> Result<Arc<H>, String>,
{
    match args.get(1).map(String::as_str) {
        Some("--version" | "-V" | "version") => {
            println!("multigpt-runner {VERSION}");
            EXIT_OK
        }
        Some("--help" | "-h" | "help") => {
            print!("{USAGE}");
            EXIT_OK
        }
        Some("pair") => pair_main(&args[2..]),
        Some(other) => {
            eprintln!("Unbekannter Aufruf: {other}\n{USAGE}");
            EXIT_CONFIG
        }
        None => {
            init_logging();
            run_agent(make)
        }
    }
}

/// Agent mit Einstellungen aus der Umgebung starten.
pub fn run_agent<H, F>(make: F) -> i32
where
    H: FrameHandler,
    F: FnOnce(&Config, Outbox) -> Result<Arc<H>, String>,
{
    let config = match check_config() {
        Ok(config) => config,
        Err(e) => {
            error!("Einstellungen: {e}");
            return EXIT_CONFIG;
        }
    };
    if config.images.is_empty() {
        error!("MGPT_IMAGES ist leer: Ohne Positivliste startet kein Arbeitscontainer.");
        return EXIT_CONFIG;
    }
    let runtime = match tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
    {
        Ok(rt) => rt,
        Err(e) => {
            error!("Laufzeit nicht startbar: {e}");
            return EXIT_FAILED;
        }
    };
    runtime.block_on(async move {
        let outbox = Outbox::new();
        let handler = match make(&config, outbox.clone()) {
            Ok(handler) => handler,
            Err(e) => {
                error!("Einstellungen: {e}");
                return EXIT_CONFIG;
            }
        };
        info!("MultiGPT-Runner {VERSION} startet.");
        let mut agent = Agent::new(config, handler, outbox, AgentOptions::default());
        let stop = agent.stop_handle();
        tokio::spawn(async move {
            wait_for_signal().await;
            info!("Signal empfangen, Agent endet.");
            stop.stop();
        });
        let stopped = agent.stop_handle();
        let code = agent.run().await;
        exit_code(code, stopped.is_stopped())
    })
}

/// Exit-Code nach dem Ende von [`Agent::run`].
pub fn exit_code(code: Option<u16>, stopped: bool) -> i32 {
    if stopped || matches!(code, None | Some(CLOSE_NORMAL)) {
        EXIT_OK
    } else {
        EXIT_REJECTED
    }
}

/// Einstellungen lesen und Token, Server-Schlüssel und Katalog vorab prüfen.
pub fn check_config() -> Result<Config, ConfigError> {
    let config = Config::from_process_env()?;
    config.read_token()?;
    config.read_server_key()?;
    config.read_catalog()?;
    Ok(config)
}

async fn wait_for_signal() {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{SignalKind, signal};
        let mut term = match signal(SignalKind::terminate()) {
            Ok(term) => term,
            Err(_) => {
                let _ = tokio::signal::ctrl_c().await;
                return;
            }
        };
        tokio::select! {
            _ = term.recv() => {}
            _ = tokio::signal::ctrl_c() => {}
        }
    }
    #[cfg(not(unix))]
    {
        let _ = tokio::signal::ctrl_c().await;
    }
}

// --- Kopplung -------------------------------------------------------------------

/// Eingaben von `pair`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PairArgs {
    pub url: String,
    pub ca_sha256: Option<String>,
    pub images: Vec<String>,
    pub start: bool,
}

/// Argumente von `pair` prüfen.
pub fn parse_pair_args(args: &[String]) -> Result<PairArgs, ConfigError> {
    let mut url = None;
    let mut ca = None;
    let mut images = Vec::new();
    let mut start = true;
    let mut it = args.iter();
    while let Some(arg) = it.next() {
        let (name, inline) = match arg.split_once('=') {
            Some((n, v)) if n.starts_with("--") => (n, Some(v.to_string())),
            _ => (arg.as_str(), None),
        };
        let mut value = || {
            inline
                .clone()
                .or_else(|| it.next().cloned())
                .ok_or_else(|| ConfigError(format!("{name}: Wert fehlt")))
        };
        match name {
            "--url" => url = Some(value()?),
            "--ca-sha256" => ca = Some(value()?),
            "--images" => images.extend(value()?.split_whitespace().map(str::to_string)),
            "--no-start" => start = false,
            other => return Err(ConfigError(format!("pair: unbekannte Option {other}"))),
        }
    }
    let url = url.ok_or_else(|| ConfigError("pair: --url fehlt".into()))?;
    let url = config::normalize_url(&url, false)?;
    let ca_sha256 = config::normalize_fingerprint(ca.as_deref())?;
    if images.is_empty() {
        return Err(ConfigError(
            "pair: --images fehlt (Images mit Digest)".into(),
        ));
    }
    for image in &images {
        if !protocol::IMAGE_RE.is_match(image) {
            return Err(ConfigError(format!("--images: Image ohne Digest: {image}")));
        }
    }
    images.sort();
    images.dedup();
    Ok(PairArgs {
        url,
        ca_sha256,
        images,
        start,
    })
}

/// Token und Server-Schlüssel von stdin (Zeile 1, Zeile 2) prüfen.
pub fn read_pair_secrets(input: &mut dyn BufRead) -> Result<(String, String), ConfigError> {
    let mut line = |what: &str| -> Result<String, ConfigError> {
        let mut text = String::new();
        input
            .read_line(&mut text)
            .map_err(|_| ConfigError(format!("{what} nicht lesbar")))?;
        Ok(text.trim().to_string())
    };
    let token = line("Token")?;
    let key = line("Server-Schlüssel")?;
    if !protocol::is_token(&token) {
        return Err(ConfigError("Zeile 1 ist kein gültiger Token.".into()));
    }
    if protocol::signed::verifying_key_from_b64(&key).is_none() {
        return Err(ConfigError(
            "Zeile 2 ist kein Ed25519-Schlüssel (Base64, 32 Byte).".into(),
        ));
    }
    Ok((token, key))
}

/// Dateien der Kopplung schreiben: token (0600) und server.pub (0444).
pub fn write_pair_files(dir: &Path, token: &str, key_b64: &str) -> std::io::Result<()> {
    std::fs::create_dir_all(dir)?;
    set_mode(dir, 0o700)?;
    write_private(&dir.join("token"), &format!("{token}\n"), 0o600)?;
    // schreibgeschützt; vorhandene (schreibgeschützte) Datei vorher entfernen
    let key_file = dir.join("server.pub");
    let _ = std::fs::remove_file(&key_file);
    write_private(&key_file, &format!("{key_b64}\n"), 0o444)
}

fn write_private(path: &Path, content: &str, mode: u32) -> std::io::Result<()> {
    let tmp = path.with_extension("tmp");
    let _ = std::fs::remove_file(&tmp);
    {
        let mut options = std::fs::OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut file = options.open(&tmp)?;
        file.write_all(content.as_bytes())?;
        file.sync_all()?;
    }
    set_mode(&tmp, mode)?;
    std::fs::rename(&tmp, path)
}

fn set_mode(path: &Path, mode: u32) -> std::io::Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(mode))?;
    }
    #[cfg(not(unix))]
    let _ = (path, mode);
    Ok(())
}

/// Unit-Datei: Platzhalter einsetzen, Pfad des Programms und Ordner der Kopplung.
pub fn render_unit(binary: &Path, pair: &PairArgs, config_dir: &Path) -> String {
    let ca = match &pair.ca_sha256 {
        Some(ca) => format!(
            "# Selbstsigniertes Zertifikat des Servers: SHA-256-Fingerprint festgehalten.\n\
             Environment=MGPT_CA_SHA256={ca}"
        ),
        None => String::new(),
    };
    let mut unit = UNIT_TEMPLATE
        .replace("@MGPT_URL@", &pair.url)
        .replace("@MGPT_IMAGES@", &pair.images.join(" "))
        .replace(
            "ExecStart=%h/.local/bin/multigpt-runner",
            &format!("ExecStart={}", systemd_escape_path(binary)),
        );
    unit = if ca.is_empty() {
        unit.replace("#@MGPT_CA_SHA256@\n", "")
    } else {
        unit.replace("#@MGPT_CA_SHA256@", &ca)
    };
    let default_dir = "%h/.config/multigpt-runner/";
    let dir = format!("{}/", systemd_escape_path(config_dir));
    if !dir.starts_with('"') {
        unit = unit.replace(default_dir, &dir);
    }
    unit
}

fn systemd_escape_path(path: &Path) -> String {
    // Pfade mit Leerzeichen in Anführungszeichen; `%` verdoppeln.
    let text = path.to_string_lossy().replace('%', "%%");
    if text.contains(' ') {
        format!("\"{text}\"")
    } else {
        text
    }
}

fn user_unit_dir(env: &HashMap<String, String>) -> PathBuf {
    match env.get("XDG_CONFIG_HOME").filter(|v| v.starts_with('/')) {
        Some(xdg) => PathBuf::from(xdg).join("systemd/user"),
        None => {
            PathBuf::from(env.get("HOME").cloned().unwrap_or_default()).join(".config/systemd/user")
        }
    }
}

fn systemctl(args: &[&str]) -> Result<(), String> {
    let status = std::process::Command::new("systemctl")
        .arg("--user")
        .args(args)
        .status()
        .map_err(|e| format!("systemctl nicht ausführbar: {e}"))?;
    if status.success() {
        Ok(())
    } else {
        Err(format!(
            "systemctl --user {} fehlgeschlagen",
            args.join(" ")
        ))
    }
}

fn pair_main(args: &[String]) -> i32 {
    let pair = match parse_pair_args(args) {
        Ok(pair) => pair,
        Err(e) => {
            eprintln!("{e}\n{USAGE}");
            return EXIT_CONFIG;
        }
    };
    let (token, key) = match read_pair_secrets(&mut std::io::stdin().lock()) {
        Ok(secrets) => secrets,
        Err(e) => {
            eprintln!("{e}");
            return EXIT_CONFIG;
        }
    };
    let env: HashMap<String, String> = std::env::vars().collect();
    let dir = config::config_dir_from(&env);
    if let Err(e) = write_pair_files(&dir, &token, &key) {
        eprintln!("Kopplungsdateien nicht schreibbar ({}): {e}", dir.display());
        return EXIT_FAILED;
    }
    drop(token);
    let binary = std::env::current_exe()
        .and_then(|p| p.canonicalize())
        .unwrap_or_else(|_| PathBuf::from("multigpt-runner"));
    let unit_dir = user_unit_dir(&env);
    let unit = unit_dir.join(UNIT_NAME);
    let written = std::fs::create_dir_all(&unit_dir)
        .and_then(|_| std::fs::write(&unit, render_unit(&binary, &pair, &dir)));
    if let Err(e) = written {
        eprintln!("Unit nicht schreibbar ({}): {e}", unit.display());
        return EXIT_FAILED;
    }
    if pair.start {
        let started = systemctl(&["daemon-reload"]).and_then(|_| {
            systemctl(&["enable", "--now", "podman.socket"])?;
            systemctl(&["enable", UNIT_NAME])?;
            systemctl(&["restart", UNIT_NAME])
        });
        if let Err(e) = started {
            eprintln!("{e}");
            return EXIT_FAILED;
        }
        println!(
            "multigpt-runner gekoppelt mit {} (Dienst {UNIT_NAME} gestartet).",
            pair.url
        );
    } else {
        println!(
            "multigpt-runner gekoppelt mit {} (Dienst nicht gestartet).",
            pair.url
        );
    }
    EXIT_OK
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(list: &[&str]) -> Vec<String> {
        list.iter().map(|s| s.to_string()).collect()
    }

    fn image() -> String {
        format!("ghcr.io/x/work:1@sha256:{}", "b".repeat(64))
    }

    #[test]
    fn exit_codes() {
        assert_eq!(exit_code(None, false), 0);
        assert_eq!(exit_code(Some(1000), false), 0);
        assert_eq!(exit_code(Some(4401), false), 3);
        assert_eq!(exit_code(Some(4498), false), 3);
        assert_eq!(exit_code(Some(1006), true), 0);
    }

    #[test]
    fn pair_args() {
        let parsed = parse_pair_args(&args(&[
            "--url",
            "https://mgpt.local",
            &format!("--images={} {}", image(), image()),
            "--ca-sha256",
            &"AB".repeat(32),
            "--no-start",
        ]))
        .unwrap();
        assert_eq!(parsed.url, "wss://mgpt.local/runner/ws");
        assert_eq!(parsed.images, vec![image()]);
        assert_eq!(parsed.ca_sha256, Some("ab".repeat(32)));
        assert!(!parsed.start);
        assert!(parse_pair_args(&args(&["--url", "https://h"])).is_err());
        assert!(parse_pair_args(&args(&["--url", "http://h", "--images", &image()])).is_err());
        assert!(parse_pair_args(&args(&["--images", &image()])).is_err());
        assert!(parse_pair_args(&args(&["--url", "https://h", "--images", "x:1"])).is_err());
        assert!(parse_pair_args(&args(&["--bogus"])).is_err());
    }

    #[test]
    fn pair_secrets_and_files() {
        let key = protocol::b64encode(
            ed25519_dalek::SigningKey::from_bytes(&[2; 32])
                .verifying_key()
                .as_bytes(),
        );
        let token = "t".repeat(32);
        let input = format!("{token}\n{key}\n");
        let (t, k) = read_pair_secrets(&mut input.as_bytes()).unwrap();
        assert_eq!((t.as_str(), k.as_str()), (token.as_str(), key.as_str()));
        assert!(read_pair_secrets(&mut "kurz\nx\n".as_bytes()).is_err());
        assert!(read_pair_secrets(&mut format!("{token}\nQUJD\n").as_bytes()).is_err());

        let dir = tempfile::tempdir().unwrap();
        let cfg = dir.path().join("multigpt-runner");
        write_pair_files(&cfg, &token, &key).unwrap();
        write_pair_files(&cfg, &token, &key).unwrap(); // zweimal (server.pub schreibgeschützt)
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = |p: &Path| std::fs::metadata(p).unwrap().permissions().mode() & 0o777;
            assert_eq!(mode(&cfg.join("token")), 0o600);
            assert_eq!(mode(&cfg.join("server.pub")), 0o444);
            assert_eq!(mode(&cfg), 0o700);
        }
        let mut config = Config::new("wss://h/runner/ws");
        config.token_file = cfg.join("token");
        config.server_key_file = cfg.join("server.pub");
        assert_eq!(config.read_token().unwrap(), token);
        assert!(config.read_server_key().is_ok());
    }

    #[test]
    fn unit_template() {
        let pair = PairArgs {
            url: "wss://mgpt.local/runner/ws".into(),
            ca_sha256: Some("ab".repeat(32)),
            images: vec![image(), format!("x/y@sha256:{}", "c".repeat(64))],
            start: false,
        };
        let unit = render_unit(
            Path::new("/opt/my bin/multigpt-runner"),
            &pair,
            Path::new("/home/u/.config/multigpt-runner"),
        );
        assert!(unit.contains("ExecStart=\"/opt/my bin/multigpt-runner\""));
        assert!(unit.contains("Environment=MGPT_URL=wss://mgpt.local/runner/ws\n"));
        assert!(unit.contains(&format!("Environment=MGPT_CA_SHA256={}\n", "ab".repeat(32))));
        assert!(unit.contains(&format!(
            "Environment=\"MGPT_IMAGES={} x/y@sha256:",
            image()
        )));
        assert!(unit.contains("MGPT_TOKEN_FILE=/home/u/.config/multigpt-runner/token"));
        assert!(unit.contains("RestartPreventExitStatus=2 3"));
        assert!(!unit.contains('@') || !unit.contains("@MGPT"));
        let plain = render_unit(
            Path::new("/x"),
            &PairArgs {
                ca_sha256: None,
                ..pair
            },
            Path::new("/h/.config/multigpt-runner"),
        );
        assert!(!plain.contains("MGPT_CA_SHA256"));
        assert!(!plain.contains("%h/.local/bin"));
    }
}
