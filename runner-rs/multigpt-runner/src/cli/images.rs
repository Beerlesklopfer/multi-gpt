//! `multigpt-runner images list|add|remove`: lokale Positivliste der Arbeits-Images
//! (`MGPT_IMAGES`) pflegen, ohne Unit oder `config.env` von Hand zu bearbeiten.
//!
//! Maßgeblich ist, was der Dienst tatsächlich bekommt: eine aktive Zeile
//! `MGPT_IMAGES=…` in `config.env` überschreibt die Vorgabe der Unit
//! (`Environment="MGPT_IMAGES=…"`). Geändert wird deshalb genau diese Quelle.
//! Danach lädt der Befehl systemd neu (nur bei geänderter Unit) und startet den
//! Dienst neu, außer mit `--no-restart`.
//!
//! Exit-Codes: 0 erledigt, 1 Datei bzw. Dienst nicht änderbar, 2 falsche Eingaben.

use std::collections::{BTreeSet, HashMap};
use std::path::{Path, PathBuf};

use super::{
    EXIT_CONFIG, EXIT_FAILED, EXIT_OK, UNIT_NAME, systemctl, user_unit_dir, write_private,
};
use crate::config;
use crate::protocol;

pub const USAGE: &str = "  multigpt-runner images list     freigegebene Arbeits-Images anzeigen
  multigpt-runner images add <image@sha256:…> … [--no-restart]
  multigpt-runner images remove <image@sha256:…> … [--no-restart]
                                  lokale Freigabe ändern (MGPT_IMAGES) und den Dienst neu starten
";

const KEY: &str = "MGPT_IMAGES";

/// Wo die wirksame Liste steht.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Source {
    /// aktive Zeile in `config.env`
    ConfigEnv,
    /// `Environment="MGPT_IMAGES=…"` in der Unit
    Unit,
}

/// Liste aus einem Wert (durch Leerraum getrennt).
fn split(value: &str) -> BTreeSet<String> {
    value.split_whitespace().map(str::to_string).collect()
}

/// Wert einer aktiven Zeile `MGPT_IMAGES=…` in `config.env` (letzte gewinnt, wie bei systemd).
pub fn env_file_value(text: &str) -> Option<String> {
    let mut found = None;
    for line in text.lines() {
        let line = line.trim();
        if let Some(rest) = line.strip_prefix(KEY).and_then(|r| r.strip_prefix('=')) {
            found = Some(unquote(rest.trim()).to_string());
        }
    }
    found
}

/// Wert von `Environment="MGPT_IMAGES=…"` bzw. `Environment=MGPT_IMAGES=…` in der Unit.
pub fn unit_value(text: &str) -> Option<String> {
    let mut found = None;
    for line in text.lines() {
        let Some(rest) = line.trim().strip_prefix("Environment=") else {
            continue;
        };
        let inner = unquote(rest.trim());
        if let Some(value) = inner.strip_prefix(KEY).and_then(|r| r.strip_prefix('=')) {
            found = Some(value.to_string());
        }
    }
    found
}

fn unquote(value: &str) -> &str {
    value
        .strip_prefix('"')
        .and_then(|v| v.strip_suffix('"'))
        .unwrap_or(value)
}

/// Wirksame Liste und ihre Quelle.
pub fn effective(unit: Option<&str>, env_file: Option<&str>) -> Option<(Source, BTreeSet<String>)> {
    if let Some(value) = env_file.and_then(env_file_value) {
        return Some((Source::ConfigEnv, split(&value)));
    }
    unit.and_then(unit_value)
        .map(|value| (Source::Unit, split(&value)))
}

fn joined(images: &BTreeSet<String>) -> String {
    images.iter().cloned().collect::<Vec<_>>().join(" ")
}

/// `config.env` mit neuer Liste: aktive Zeilen ersetzen (nur die letzte bleibt).
pub fn rewrite_env_file(text: &str, images: &BTreeSet<String>) -> String {
    let new_line = format!("{KEY}=\"{}\"", joined(images));
    let active: Vec<usize> = text
        .lines()
        .enumerate()
        .filter(|(_, l)| {
            l.trim()
                .strip_prefix(KEY)
                .is_some_and(|r| r.starts_with('='))
        })
        .map(|(i, _)| i)
        .collect();
    let mut out = Vec::new();
    for (i, line) in text.lines().enumerate() {
        if active.last() == Some(&i) {
            out.push(new_line.clone());
        } else if !active.contains(&i) {
            out.push(line.to_string());
        }
    }
    if active.is_empty() {
        out.push(new_line);
    }
    let mut result = out.join("\n");
    result.push('\n');
    result
}

/// Unit mit neuer Liste; `None`, wenn die Unit keine Zeile `MGPT_IMAGES` hat.
pub fn rewrite_unit(text: &str, images: &BTreeSet<String>) -> Option<String> {
    let mut replaced = false;
    let mut out = Vec::new();
    for line in text.lines() {
        let is_images = line
            .trim()
            .strip_prefix("Environment=")
            .map(|rest| unquote(rest.trim()).starts_with(&format!("{KEY}=")))
            .unwrap_or(false);
        if is_images {
            out.push(format!("Environment=\"{KEY}={}\"", joined(images)));
            replaced = true;
        } else {
            out.push(line.to_string());
        }
    }
    if !replaced {
        return None;
    }
    let mut result = out.join("\n");
    if text.ends_with('\n') {
        result.push('\n');
    }
    Some(result)
}

/// Eingaben von `images add|remove`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChangeArgs {
    pub images: Vec<String>,
    pub restart: bool,
}

pub fn parse_change_args(args: &[String]) -> Result<ChangeArgs, String> {
    let mut images = Vec::new();
    let mut restart = true;
    for arg in args {
        match arg.as_str() {
            "--no-restart" => restart = false,
            other if other.starts_with("--") => {
                return Err(format!("images: unbekannte Option {other}"));
            }
            image => {
                if !protocol::is_image(image) {
                    return Err(format!(
                        "Image ohne Digest: {image} (erwartet: name@sha256:<64 Hex-Zeichen>)"
                    ));
                }
                images.push(image.to_string());
            }
        }
    }
    if images.is_empty() {
        return Err("images: kein Image angegeben".into());
    }
    Ok(ChangeArgs { images, restart })
}

struct Paths {
    unit: PathBuf,
    env_file: PathBuf,
}

fn paths(env: &HashMap<String, String>) -> Paths {
    Paths {
        unit: user_unit_dir(env).join(UNIT_NAME),
        env_file: config::config_dir_from(env).join("config.env"),
    }
}

fn read_optional(path: &Path) -> Result<Option<String>, String> {
    match std::fs::read_to_string(path) {
        Ok(text) => Ok(Some(text)),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(format!("{} nicht lesbar: {e}", path.display())),
    }
}

pub fn main(args: &[String]) -> i32 {
    let env: HashMap<String, String> = std::env::vars().collect();
    let paths = paths(&env);
    let (unit, env_file) = match (read_optional(&paths.unit), read_optional(&paths.env_file)) {
        (Ok(unit), Ok(env_file)) => (unit, env_file),
        (Err(e), _) | (_, Err(e)) => {
            eprintln!("{e}");
            return EXIT_FAILED;
        }
    };
    let current = effective(unit.as_deref(), env_file.as_deref());
    match args.first().map(String::as_str) {
        Some("list") if args.len() == 1 => {
            match current {
                Some((source, images)) => {
                    let from = match source {
                        Source::ConfigEnv => paths.env_file.display().to_string(),
                        Source::Unit => paths.unit.display().to_string(),
                    };
                    if images.is_empty() {
                        println!("Keine Images freigegeben ({from}).");
                    }
                    for image in &images {
                        println!("{image}");
                    }
                    eprintln!("Quelle: {from}");
                }
                None => println!("Keine Images freigegeben (weder Unit noch config.env)."),
            }
            EXIT_OK
        }
        Some(cmd @ ("add" | "remove")) => {
            let change = match parse_change_args(&args[1..]) {
                Ok(change) => change,
                Err(e) => {
                    eprintln!("{e}\n{USAGE}");
                    return EXIT_CONFIG;
                }
            };
            let (source, mut images) = current.unwrap_or((Source::ConfigEnv, BTreeSet::new()));
            let before = images.clone();
            for image in &change.images {
                if cmd == "add" {
                    images.insert(image.clone());
                } else if !images.remove(image) {
                    eprintln!("Nicht freigegeben, übersprungen: {image}");
                }
            }
            if images == before {
                println!("Keine Änderung.");
                return EXIT_OK;
            }
            if images.is_empty() {
                eprintln!(
                    "Danach wäre kein Image mehr freigegeben; der Agent startet so nicht. \
                     Erst ein anderes Image hinzufügen."
                );
                return EXIT_CONFIG;
            }
            let written = match source {
                Source::ConfigEnv => {
                    if let Some(dir) = paths.env_file.parent()
                        && std::fs::create_dir_all(dir).is_ok()
                    {
                        let _ = super::set_mode(dir, 0o700);
                    }
                    let text = rewrite_env_file(env_file.as_deref().unwrap_or(""), &images);
                    write_private(&paths.env_file, &text, 0o600)
                        .map_err(|e| format!("{} nicht schreibbar: {e}", paths.env_file.display()))
                }
                Source::Unit => match rewrite_unit(unit.as_deref().unwrap_or(""), &images) {
                    Some(text) => std::fs::write(&paths.unit, text)
                        .map_err(|e| format!("{} nicht schreibbar: {e}", paths.unit.display())),
                    None => Err("Unit ohne MGPT_IMAGES".into()),
                },
            };
            if let Err(e) = written {
                eprintln!("{e}");
                return EXIT_FAILED;
            }
            for image in &images {
                println!("{image}");
            }
            if !change.restart {
                println!("Gespeichert. Wirksam nach: systemctl --user restart {UNIT_NAME}");
                return EXIT_OK;
            }
            let restarted = if source == Source::Unit {
                systemctl(&["daemon-reload"])
            } else {
                Ok(())
            }
            .and_then(|_| systemctl(&["try-restart", UNIT_NAME]));
            match restarted {
                Ok(()) => {
                    println!("Gespeichert, Dienst {UNIT_NAME} neu gestartet (falls er lief).");
                    EXIT_OK
                }
                Err(e) => {
                    eprintln!("Gespeichert, aber {e}");
                    EXIT_FAILED
                }
            }
        }
        _ => {
            eprintln!("Aufruf:\n{USAGE}");
            EXIT_CONFIG
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const A: &str =
        "ghcr.io/x/work@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    const B: &str =
        "ghcr.io/x/work@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

    fn set(items: &[&str]) -> BTreeSet<String> {
        items.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn unit_value_is_read_from_quoted_environment() {
        let unit = format!(
            "[Service]\nEnvironment=MGPT_URL=https://h\nEnvironment=\"MGPT_IMAGES={A} {B}\"\n"
        );
        assert_eq!(
            effective(Some(&unit), None),
            Some((Source::Unit, set(&[A, B])))
        );
    }

    #[test]
    fn active_config_env_line_wins_over_unit() {
        let unit = format!("Environment=\"MGPT_IMAGES={A}\"\n");
        let env = format!("# MGPT_IMAGES=\"ignored@sha256:x\"\nMGPT_IMAGES=\"{B}\"\n");
        assert_eq!(
            effective(Some(&unit), Some(&env)),
            Some((Source::ConfigEnv, set(&[B])))
        );
        // nur auskommentiert: Unit gilt
        let commented = "# MGPT_IMAGES=\"x\"\n";
        assert_eq!(
            effective(Some(&unit), Some(commented)).unwrap().0,
            Source::Unit
        );
    }

    #[test]
    fn rewrite_unit_replaces_only_the_images_line() {
        let unit = format!(
            "[Service]\nEnvironment=MGPT_URL=https://h\nEnvironment=\"MGPT_IMAGES={A}\"\nRestart=on-failure\n"
        );
        let new = rewrite_unit(&unit, &set(&[A, B])).unwrap();
        assert!(new.contains(&format!("Environment=\"MGPT_IMAGES={A} {B}\"\n")));
        assert!(new.contains("Environment=MGPT_URL=https://h\n"));
        assert!(new.ends_with("Restart=on-failure\n"));
        assert_eq!(rewrite_unit("[Service]\n", &set(&[A])), None);
    }

    #[test]
    fn rewrite_env_file_keeps_comments_and_collapses_duplicates() {
        let env = format!("# Kommentar\nMGPT_IMAGES={A}\nMGPT_HOSTNAME=WS\nMGPT_IMAGES=\"{A}\"\n");
        let new = rewrite_env_file(&env, &set(&[A, B]));
        assert_eq!(
            new,
            format!("# Kommentar\nMGPT_HOSTNAME=WS\nMGPT_IMAGES=\"{A} {B}\"\n")
        );
        assert_eq!(env_file_value(&new).map(|v| split(&v)), Some(set(&[A, B])));
        let appended = rewrite_env_file("# leer\n", &set(&[B]));
        assert_eq!(appended, format!("# leer\nMGPT_IMAGES=\"{B}\"\n"));
    }

    #[test]
    fn change_args_require_digest() {
        let args = |v: &[&str]| v.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        assert_eq!(
            parse_change_args(&args(&[A, "--no-restart"])),
            Ok(ChangeArgs {
                images: vec![A.into()],
                restart: false
            })
        );
        assert!(
            parse_change_args(&args(&["ghcr.io/x/work:latest"]))
                .unwrap_err()
                .contains("Digest")
        );
        assert!(parse_change_args(&args(&[])).is_err());
        assert!(parse_change_args(&args(&["--force", A])).is_err());
    }
}
