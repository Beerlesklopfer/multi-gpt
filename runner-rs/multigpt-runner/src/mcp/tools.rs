//! Werkzeuge im Arbeitscontainer (Port von `runner_agent/tools.py`).
//!
//! Dateien laufen über `mgpt-fs` (Pfade nur unter `/workspace` bzw. `/scratch`,
//! kein Symlink hinaus), Befehle in einem PTY mit Zeitlimit und Ausgabegrenze.
//! Nach Werkzeugen, die Dateien ändern können, folgt ein Commit mit neuem
//! Bundle (8r). `ssh` gibt es im Alltag nicht mehr; an seine Stelle tritt
//! `setup_host` (Zielsystem einrichten, siehe `setup.rs`).

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, LazyLock};
use std::time::Duration;

use base64::Engine as _;
use regex::Regex;
use serde_json::{Map, Value, json};

use super::host::{Target, ToolError, ToolHost, ToolPty, ToolResult};
use super::setup;
use super::telnet::TelnetConnection;
use crate::protocol;

pub const BASH_TIMEOUT_DEFAULT: u64 = 120;
pub const BASH_TIMEOUT_MAX: u64 = 600;
pub const PUBLISH_MAX_BYTES: u64 = 20 * 1024 * 1024;
/// Port im Relay für das i-te freigegebene Ziel (`template.RELAY_BASE_PORT`).
pub const RELAY_BASE_PORT: u16 = 20000;
/// Größe des PTY der Werkzeuge (Spalten, Zeilen).
pub const PTY_COLS: u16 = 120;
pub const PTY_ROWS: u16 = 40;

/// Werkzeuge, die (möglicherweise) Dateien im Arbeitsstand ändern.
pub const CHANGING: [&str; 3] = ["write", "edit", "bash"];

static ANSI_RE: LazyLock<regex::bytes::Regex> = LazyLock::new(|| {
    regex::bytes::Regex::new(
        r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]",
    )
    .expect("ANSI-Muster")
});
static MIME_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$").expect("MIME-Muster")
});

fn schema(properties: Value, required: &[&str]) -> Value {
    json!({
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": false,
    })
}

fn host_schema() -> Value {
    json!({
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "address": {"type": "string"},
            "port": {"type": "integer"},
            "user": {"type": "string"},
            "host_key": {"type": "string"},
            "secret_type": {"type": "string", "enum": ["key", "password", "none"]},
            "secret": {"type": "string"},
        },
        "required": ["name", "address", "port"],
    })
}

/// Werkzeug, wie der Agent es in `tools/list` meldet (nur zum Abgleich; dem
/// Modell bietet der Server seinen eigenen Katalog an).
#[derive(Debug, Clone)]
pub struct ToolDef {
    pub name: &'static str,
    pub description: &'static str,
    pub input_schema: Value,
}

/// Alle Werkzeuge des Agents.
pub fn tool_defs() -> Vec<ToolDef> {
    let path = json!({"type": "string"});
    vec![
        ToolDef {
            name: "read",
            description: "Datei lesen (mit Zeilennummern).",
            input_schema: schema(
                json!({"path": path, "offset": {"type": "integer"}, "limit": {"type": "integer"}}),
                &["path"],
            ),
        },
        ToolDef {
            name: "write",
            description: "Datei anlegen oder überschreiben.",
            input_schema: schema(
                json!({"path": path, "content": {"type": "string"}}),
                &["path", "content"],
            ),
        },
        ToolDef {
            name: "edit",
            description: "Eindeutige Textstelle ersetzen.",
            input_schema: schema(
                json!({
                    "path": path,
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                }),
                &["path", "old_string", "new_string"],
            ),
        },
        ToolDef {
            name: "glob",
            description: "Dateien nach Muster finden.",
            input_schema: schema(
                json!({"pattern": {"type": "string"}, "path": path}),
                &["pattern"],
            ),
        },
        ToolDef {
            name: "grep",
            description: "Volltextsuche mit regulärem Ausdruck.",
            input_schema: schema(
                json!({
                    "pattern": {"type": "string"},
                    "path": path,
                    "glob": {"type": "string"},
                    "ignore_case": {"type": "boolean"},
                    "max_results": {"type": "integer"},
                }),
                &["pattern"],
            ),
        },
        ToolDef {
            name: "bash",
            description: "Shell-Befehl im PTY ausführen (Zeitlimit, Ausgabe gekürzt).",
            input_schema: schema(
                json!({"command": {"type": "string"}, "timeout_s": {"type": "integer"}}),
                &["command"],
            ),
        },
        ToolDef {
            name: "serve",
            description: "Vorschau-Server für ein Verzeichnis starten.",
            input_schema: schema(json!({"path": path}), &[]),
        },
        ToolDef {
            name: "publish",
            description: "Datei aus /scratch oder /workspace als Anhang an MultiGPT schicken.",
            input_schema: schema(
                json!({
                    "path": path,
                    "name": {"type": "string"},
                    "transfer": {"type": "string"},
                    "max_bytes": {"type": "integer"},
                }),
                &["path", "transfer"],
            ),
        },
        ToolDef {
            name: "telnet",
            description: "Eingabe per Telnet an einen freigegebenen Host (Zugang vom Server).",
            input_schema: schema(
                json!({"host": host_schema(), "input": {"type": "string"}}),
                &["host", "input"],
            ),
        },
        ToolDef {
            name: setup::NAME,
            description: "Zielsystem per SSH-Schlüssel einrichten: Runner-Programm übertragen, \
                          Prüfsumme prüfen und mit MultiGPT koppeln (Zugang vom Server).",
            input_schema: schema(
                json!({"host": host_schema(), "pairing": setup::pairing_schema()}),
                &["host", "pairing"],
            ),
        },
    ]
}

/// Namen aller Werkzeuge des Agents.
pub fn tool_names() -> Vec<&'static str> {
    tool_defs().iter().map(|tool| tool.name).collect()
}

// --- Argumente -------------------------------------------------------------------------

/// Text-Argument (wie `_str` in Python): fehlt bzw. leer → Fehler.
pub fn arg_str<'a>(args: &'a Map<String, Value>, key: &str, empty: bool) -> ToolResult<&'a str> {
    match args.get(key) {
        Some(Value::String(value)) if empty || !value.is_empty() => Ok(value),
        _ => Err(ToolError::new(format!(
            "Argument „{key}“ fehlt oder ist kein Text."
        ))),
    }
}

/// Optionales Text-Argument (`null` = fehlt).
pub fn arg_opt_str<'a>(args: &'a Map<String, Value>, key: &str) -> ToolResult<Option<&'a str>> {
    match args.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(value)) if !value.is_empty() => Ok(Some(value)),
        _ => Err(ToolError::new(format!(
            "Argument „{key}“ fehlt oder ist kein Text."
        ))),
    }
}

/// Ganze Zahl in Grenzen (wie `_int` in Python: bool und 1.0 sind keine Zahlen).
pub fn arg_int(
    args: &Map<String, Value>,
    key: &str,
    low: u64,
    high: u64,
) -> ToolResult<Option<u64>> {
    match args.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Number(n)) if n.as_u64().is_some_and(|v| (low..=high).contains(&v)) => {
            Ok(n.as_u64())
        }
        _ => Err(ToolError::new(format!(
            "Argument „{key}“ muss eine Zahl zwischen {low} und {high} sein."
        ))),
    }
}

fn arg_bool(args: &Map<String, Value>, key: &str) -> bool {
    // Python: bool(args.get(key)) – nur echte Wahrheitswerte zählen hier.
    match args.get(key) {
        Some(Value::Bool(value)) => *value,
        Some(Value::Null) | None => false,
        Some(Value::Number(n)) => n.as_f64().is_some_and(|v| v != 0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

fn fields(pairs: Vec<(&str, Value)>) -> Map<String, Value> {
    pairs
        .into_iter()
        .filter(|(_, value)| !value.is_null())
        .map(|(key, value)| (key.to_string(), value))
        .collect()
}

fn opt<T: Into<Value>>(value: Option<T>) -> Value {
    value.map_or(Value::Null, Into::into)
}

/// Commit-Nachricht (kurz, ohne Inhalte).
pub fn summary(name: &str, args: &Map<String, Value>) -> String {
    match name {
        "write" | "edit" => {
            let path = args.get("path").and_then(Value::as_str).unwrap_or("");
            truncate_chars(&format!("{name} {path}"), 200)
        }
        "bash" => {
            let command = match args.get("command") {
                Some(Value::String(s)) => s.clone(),
                Some(other) => other.to_string(),
                None => String::new(),
            };
            let joined = command.split_whitespace().collect::<Vec<_>>().join(" ");
            format!("bash: {}", truncate_chars(&joined, 150))
        }
        _ => name.to_string(),
    }
}

/// Höchstens `max` Zeichen (wie Python-Slicing auf `str`).
pub fn truncate_chars(text: &str, max: usize) -> String {
    text.chars().take(max).collect()
}

/// PTY-Ausgabe für das Modell: ohne Steuerzeichen, Zeilenenden vereinheitlicht.
pub fn clean_output(data: &[u8]) -> String {
    let stripped = ANSI_RE.replace_all(data, &b""[..]);
    let mut out = Vec::with_capacity(stripped.len());
    let mut i = 0;
    while i < stripped.len() {
        match stripped[i] {
            b'\r' if stripped.get(i + 1) == Some(&b'\n') => {
                out.push(b'\n');
                i += 2;
                continue;
            }
            b'\r' => out.push(b'\n'),
            byte => out.push(byte),
        }
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// Exit-Code für `pty_exit` (wie `_exit` in Python).
pub fn clamp_exit(code: Option<i32>) -> Option<i32> {
    code.map(|c| c.clamp(-255, 255))
}

/// Base64 (Standard, mit Padding) wie `protocol.b64encode`.
pub fn b64(data: &[u8]) -> String {
    base64::engine::general_purpose::STANDARD.encode(data)
}

/// Datei in Stücke teilen: (seq, Daten, last); leere Datei = ein leeres Stück.
pub fn split_chunks(data: &[u8], chunk: usize) -> Vec<(u64, &[u8], bool)> {
    if data.is_empty() {
        return vec![(0, &data[..0], true)];
    }
    let parts: Vec<&[u8]> = data.chunks(chunk.max(1)).collect();
    let count = parts.len();
    parts
        .into_iter()
        .enumerate()
        .map(|(i, part)| (i as u64, part, i + 1 == count))
        .collect()
}

// --- Werkzeuge einer Sitzung -------------------------------------------------------------

/// Ergebnis eines Werkzeugs: Text und `structuredContent`.
pub type Outcome = (String, Option<Value>);

/// Werkzeuge einer Sitzung (gemeinsam für alle pipes der Sitzung).
pub struct SessionTools<H: ToolHost> {
    pub host: Arc<H>,
    run_counter: AtomicU64,
    pub(crate) telnet: tokio::sync::Mutex<HashMap<String, Arc<TelnetConnection<H>>>>,
}

impl<H: ToolHost> SessionTools<H> {
    pub fn new(host: Arc<H>) -> Self {
        Self {
            host,
            run_counter: AtomicU64::new(0),
            telnet: tokio::sync::Mutex::new(HashMap::new()),
        }
    }

    /// Nächste agent-eigene PTY-ID (`run-<n>`, `telnet-<n>`, `setup-<n>`).
    pub fn next_pty_id(&self, label: &str) -> String {
        let n = self.run_counter.fetch_add(1, Ordering::SeqCst) + 1;
        format!("{label}-{n}")
    }

    /// Werkzeug ausführen; liefert (Text, structuredContent).
    pub async fn call(&self, name: &str, args: &Value) -> ToolResult<Outcome> {
        if !tool_names().contains(&name) {
            return Err(ToolError::new(format!("Unbekanntes Werkzeug: {name}")));
        }
        if let Some(allowed) = self.host.allowed_services()
            && !allowed.iter().any(|service| service == name)
        {
            return Err(ToolError::new(format!(
                "Werkzeug „{name}“ ist vom Server nicht freigegeben."
            )));
        }
        let Value::Object(args) = args else {
            return Err(ToolError::new("Argumente müssen ein Objekt sein."));
        };
        let lock = self.host.lock();
        let _guard = lock.lock().await;
        self.host.touch();
        if matches!(name, "write" | "edit") && self.host.scratch_full().await {
            let size = self.host.scratch_size_mb().unwrap_or(0);
            return Err(ToolError::new(format!(
                "Scratchpad über der Grenze von {size} MB: Schreiben gesperrt. Bitte zuerst \
                 Dateien in /scratch löschen (bash: rm)."
            )));
        }
        let result = self.dispatch(name, args).await;
        self.host.touch();
        let (mut text, structured) = result?;
        if CHANGING.contains(&name) {
            text += &self.host.save(&summary(name, args)).await;
            text += &self.host.scratch_note().await;
        }
        Ok((text, structured))
    }

    async fn dispatch(&self, name: &str, args: &Map<String, Value>) -> ToolResult<Outcome> {
        match name {
            "read" => self.tool_read(args).await,
            "write" => self.tool_write(args).await,
            "edit" => self.tool_edit(args).await,
            "glob" => self.tool_glob(args).await,
            "grep" => self.tool_grep(args).await,
            "bash" => self.tool_bash(args).await,
            "serve" => self.tool_serve(args).await,
            "publish" => self.tool_publish(args).await,
            "telnet" => self.tool_telnet(args).await,
            setup::NAME => setup::run(self, args).await,
            _ => Err(ToolError::new(format!("Unbekanntes Werkzeug: {name}"))),
        }
    }

    // --- Dateien -----------------------------------------------------------------------

    async fn tool_read(&self, args: &Map<String, Value>) -> ToolResult<Outcome> {
        let request = fields(vec![
            ("path", arg_str(args, "path", false)?.into()),
            ("offset", opt(arg_int(args, "offset", 1, 1_000_000_000)?)),
            ("limit", opt(arg_int(args, "limit", 1, 100_000)?)),
        ]);
        let result = self.host.fs("read", request).await?;
        let content = result.get("content").and_then(Value::as_str).unwrap_or("");
        let mut text = if content.is_empty() {
            "(leere Datei bzw. keine Zeilen in diesem Bereich)".to_string()
        } else {
            content.to_string()
        };
        let number = |key: &str| result.get(key).and_then(Value::as_i64).unwrap_or(0);
        let (from, to, lines) = (number("from"), number("to"), number("lines"));
        if to < lines || from > 1 {
            text += &format!("\n[Zeilen {from}–{to} von {lines}]");
        }
        if result.get("truncated").and_then(Value::as_bool) == Some(true) {
            text += "\n[Datei nur teilweise gelesen (über 2 MB).]";
        }
        Ok((text, None))
    }

    async fn tool_write(&self, args: &Map<String, Value>) -> ToolResult<Outcome> {
        let request = fields(vec![
            ("path", arg_str(args, "path", false)?.into()),
            ("content", arg_str(args, "content", true)?.into()),
        ]);
        let result = self.host.fs("write", request).await?;
        let verb = if result.get("created").and_then(Value::as_bool) == Some(true) {
            "angelegt"
        } else {
            "überschrieben"
        };
        Ok((
            format!(
                "{} {verb} ({} Byte).",
                result.get("path").and_then(Value::as_str).unwrap_or(""),
                result.get("bytes").and_then(Value::as_u64).unwrap_or(0)
            ),
            None,
        ))
    }

    async fn tool_edit(&self, args: &Map<String, Value>) -> ToolResult<Outcome> {
        let request = fields(vec![
            ("path", arg_str(args, "path", false)?.into()),
            ("old_string", arg_str(args, "old_string", false)?.into()),
            ("new_string", arg_str(args, "new_string", true)?.into()),
            ("replace_all", arg_bool(args, "replace_all").into()),
        ]);
        let result = self.host.fs("edit", request).await?;
        Ok((
            format!(
                "{}: {} Stelle(n) ersetzt.",
                result.get("path").and_then(Value::as_str).unwrap_or(""),
                result
                    .get("replacements")
                    .and_then(Value::as_u64)
                    .unwrap_or(0)
            ),
            None,
        ))
    }

    async fn tool_glob(&self, args: &Map<String, Value>) -> ToolResult<Outcome> {
        let request = fields(vec![
            ("pattern", arg_str(args, "pattern", false)?.into()),
            ("path", opt(arg_opt_str(args, "path")?)),
        ]);
        let result = self.host.fs("glob", request).await?;
        Ok((listing(&result, "paths"), None))
    }

    async fn tool_grep(&self, args: &Map<String, Value>) -> ToolResult<Outcome> {
        let request = fields(vec![
            ("pattern", arg_str(args, "pattern", false)?.into()),
            ("path", opt(arg_opt_str(args, "path")?)),
            ("glob", opt(arg_opt_str(args, "glob")?)),
            ("ignore_case", arg_bool(args, "ignore_case").into()),
            ("max_results", opt(arg_int(args, "max_results", 1, 1000)?)),
        ]);
        let result = self.host.fs("grep", request).await?;
        Ok((listing(&result, "matches"), None))
    }

    // --- Befehle -----------------------------------------------------------------------

    /// Befehl im PTY ausführen (Arbeits- oder Hilfscontainer), streamen, gekürzt liefern.
    /// Liefert (bereinigte Ausgabe, Exit-Code, gekürzt).
    pub async fn run_pty(
        &self,
        argv: Vec<String>,
        timeout: u64,
        label: &str,
        env_prefix: &[String],
        target: Target<'_>,
    ) -> ToolResult<(String, Option<i32>, bool)> {
        let pty_id = self.next_pty_id(label);
        let mut wrapped = vec![
            "timeout".to_string(),
            "-k".to_string(),
            "5".to_string(),
            timeout.to_string(),
        ];
        wrapped.extend(env_prefix.iter().cloned());
        wrapped.extend(argv);
        let pty = self.host.exec_pty(&pty_id, target, wrapped).await?;
        let limit = self.host.limits().output_bytes;
        let mut collected = Vec::new();
        let pumped = tokio::time::timeout(
            Duration::from_secs(timeout + 15),
            pump(&*self.host, &pty_id, &pty, Some((&mut collected, limit))),
        )
        .await;
        pty.close().await;
        let (code, truncated) = pumped.unwrap_or((Some(124), false));
        Ok((clean_output(&collected), code, truncated))
    }

    async fn tool_bash(&self, args: &Map<String, Value>) -> ToolResult<Outcome> {
        let mut command = arg_str(args, "command", false)?.to_string();
        let limits = self.host.limits();
        let limit = BASH_TIMEOUT_MAX.min(limits.command_seconds);
        let timeout = arg_int(args, "timeout_s", 1, BASH_TIMEOUT_MAX)?
            .unwrap_or(BASH_TIMEOUT_DEFAULT)
            .min(limit);
        if let Some(rest) = self.host.scratch_rest().await {
            // Lokales Scratchpad ohne Quota: Dateigröße je Prozess auf den Rest begrenzen
            // (über der Grenze 0 – Löschen geht weiter, Schreiben nicht), höchstens die
            // harte Grenze der Vorlage (fsize = tmpfs-Größe).
            let rest = rest.min(limits.tmpfs_mb << 20);
            command = format!("ulimit -f {}; {command}", rest / 1024);
        }
        let argv = ["bash", "--noprofile", "--norc", "-c", &command]
            .iter()
            .map(|s| s.to_string())
            .collect();
        let (output, code, truncated) = self
            .run_pty(argv, timeout, "run", &[], Target::Work)
            .await?;
        let mut text = output.trim_end_matches('\n').to_string();
        if text.is_empty() {
            text = "(keine Ausgabe)".to_string();
        }
        if truncated {
            text += &format!("\n[Ausgabe nach {} Byte gekürzt]", limits.output_bytes);
        }
        if code == Some(124) {
            text += &format!("\n[Zeitlimit von {timeout} s erreicht, Befehl abgebrochen]");
        }
        text += &format!("\n[Exit-Code {}]", exit_text(code));
        Ok((
            text,
            Some(json!({"exit_code": code, "truncated": truncated})),
        ))
    }

    async fn tool_serve(&self, args: &Map<String, Value>) -> ToolResult<Outcome> {
        let info = self.host.start_preview(arg_opt_str(args, "path")?).await?;
        let url = info.get("url").and_then(Value::as_str).unwrap_or("");
        let text = format!(
            "Vorschau läuft: {url}\nNur statische Dateien; Verzeichnisse liefern index.html, \
             keine Listen."
        );
        Ok((text, Some(info)))
    }

    /// Datei per `file_chunk` an den Server (Übertragungs-ID gibt der Server vor).
    async fn tool_publish(&self, args: &Map<String, Value>) -> ToolResult<Outcome> {
        let transfer = arg_str(args, "transfer", false)?;
        if !protocol::ID_RE.is_match(transfer) {
            return Err(ToolError::new("Übertragungs-ID ungültig."));
        }
        let max_bytes =
            arg_int(args, "max_bytes", 1, protocol::MAX_BUNDLE_BYTES)?.unwrap_or(PUBLISH_MAX_BYTES);
        let request = fields(vec![
            ("path", arg_str(args, "path", false)?.into()),
            ("max_bytes", max_bytes.into()),
        ]);
        let (meta, data) = self.host.fs_raw("file_out", request).await?;
        let base = meta.get("name").and_then(Value::as_str).unwrap_or("");
        let name = arg_opt_str(args, "name")?.unwrap_or(base);
        let name = truncate_chars(name.replace(['/', '\\'], "_").trim(), 255);
        let name = if name.is_empty() {
            "datei".to_string()
        } else {
            name
        };
        let mime = meta
            .get("mime")
            .and_then(Value::as_str)
            .filter(|mime| MIME_RE.is_match(mime));
        let sha256 = meta.get("sha256").and_then(Value::as_str).unwrap_or("");
        let session = self.host.session_id();
        for (seq, part, last) in split_chunks(&data, self.host.chunk_bytes()) {
            let mut frame = json!({
                "session": session,
                "transfer": transfer,
                "seq": seq,
                "data": b64(part),
                "last": last,
            });
            if last {
                frame["size"] = data.len().into();
                frame["sha256"] = sha256.into();
                frame["name"] = name.clone().into();
                if let Some(mime) = mime {
                    frame["mime"] = mime.into();
                }
            }
            self.host.send("file_chunk", frame).await?;
        }
        let info =
            json!({"transfer": transfer, "size": data.len(), "sha256": sha256, "name": name});
        let path = meta.get("path").and_then(Value::as_str).unwrap_or("");
        Ok((
            format!("{path} als „{name}“ übertragen ({} Byte).", data.len()),
            Some(info),
        ))
    }

    // --- Zugang zu freigegebenen Hosts (8v, 8w) ------------------------------------------

    /// Host aus den Argumenten (vom Server eingesetzt) und Port im Relay.
    pub fn target<'a>(
        &self,
        args: &'a Map<String, Value>,
    ) -> ToolResult<(&'a Map<String, Value>, u16)> {
        let Some(Value::Object(host)) = args.get("host") else {
            return Err(ToolError::new(
                "Host fehlt (der Server setzt den Zugang ein).",
            ));
        };
        let (Some(Value::String(address)), Some(port)) =
            (host.get("address"), host.get("port").and_then(port_value))
        else {
            return Err(ToolError::new("Host ohne Adresse oder Port."));
        };
        let Some(relay) = self.host.relay() else {
            return Err(ToolError::new(
                "Für diese Sitzung ist kein Netz zu freigegebenen Hosts eingerichtet.",
            ));
        };
        let index = relay
            .targets
            .iter()
            .position(|(a, p)| a == address && *p == port)
            .ok_or_else(|| {
                ToolError::new("Host steht nicht in der Positivliste dieser Sitzung.")
            })?;
        let index = u16::try_from(index).map_err(|_| ToolError::new("Positivliste zu lang."))?;
        Ok((host, RELAY_BASE_PORT + index))
    }

    /// Zugang nur im Hilfscontainer ablegen (dessen /tmp, 0600), nie im Arbeitscontainer.
    pub async fn put_secret_files(
        &self,
        helper: &str,
        dir: &str,
        files: &[(&str, Vec<u8>)],
    ) -> ToolResult<()> {
        let mut script = vec!["umask 077".to_string(), format!("mkdir -p {dir}")];
        let mut payload = Vec::new();
        for (name, data) in files {
            script.push(format!("head -c {} > {dir}/{name}", data.len()));
            payload.extend_from_slice(data);
        }
        if files.iter().any(|(name, _)| *name == "askpass") {
            script.push(format!("chmod 700 {dir}/askpass"));
        }
        let argv = vec!["sh".to_string(), "-c".to_string(), script.join(" && ")];
        let result = self
            .host
            .exec(Target::Helper(helper), argv, payload, 30, 64 * 1024)
            .await?;
        if result.code != Some(0) {
            return Err(ToolError::new(
                "Zugang konnte im Hilfscontainer nicht abgelegt werden.",
            ));
        }
        Ok(())
    }

    async fn tool_telnet(&self, args: &Map<String, Value>) -> ToolResult<Outcome> {
        let (host, relay_port) = self.target(args)?;
        let data = arg_str(args, "input", true)?;
        let name = host
            .get("name")
            .and_then(Value::as_str)
            .or_else(|| host.get("address").and_then(Value::as_str))
            .unwrap_or("")
            .to_string();
        let existing = self.telnet.lock().await.get(&name).cloned();
        let (conn, mut output) = match existing {
            Some(conn) if !conn.is_closed() => (conn, String::new()),
            _ => {
                let conn = TelnetConnection::open(self, relay_port).await?;
                self.telnet.lock().await.insert(name, conn.clone());
                let output = conn.login(host).await?;
                (conn, output)
            }
        };
        output += &conn.send(data).await?;
        let mut text = clean_output(output.as_bytes())
            .trim_matches('\n')
            .to_string();
        if text.is_empty() {
            text = "(keine Ausgabe)".to_string();
        }
        if conn.is_closed() {
            text += "\n[Telnet-Verbindung beendet]";
        }
        Ok((text, None))
    }

    /// Alle offenen Telnet-Verbindungen schließen (Ende der Sitzung).
    pub async fn close(&self) {
        let connections: Vec<_> = self.telnet.lock().await.drain().map(|(_, c)| c).collect();
        for conn in connections {
            conn.close().await;
        }
    }
}

fn port_value(value: &Value) -> Option<u16> {
    value.as_u64().and_then(|p| u16::try_from(p).ok())
}

fn exit_text(code: Option<i32>) -> String {
    code.map_or_else(|| "unbekannt".to_string(), |c| c.to_string())
}

/// Ergebnisliste von glob/grep mit Hinweis auf weitere Treffer.
fn listing(result: &Value, key: &str) -> String {
    let items: Vec<&str> = result
        .get(key)
        .and_then(Value::as_array)
        .map(|items| items.iter().filter_map(Value::as_str).collect())
        .unwrap_or_default();
    if items.is_empty() {
        return "Keine Treffer.".to_string();
    }
    let mut text = items.join("\n");
    let total = result.get("total").and_then(Value::as_u64).unwrap_or(0);
    if total > items.len() as u64 {
        text += &format!("\n[{total} Treffer, gezeigt {}]", items.len());
    }
    text
}

/// PTY-Ausgabe als `pty_data` streamen (und für das Modell sammeln).
/// Liefert (Exit-Code, gekürzt); schickt am Ende `pty_exit`.
pub async fn pump<H: ToolHost>(
    host: &H,
    pty_id: &str,
    pty: &H::Pty,
    mut collect: Option<(&mut Vec<u8>, usize)>,
) -> (Option<i32>, bool) {
    let session = host.session_id();
    let (mut seq, mut truncated) = (0u64, false);
    while let Some(data) = pty.read().await {
        if data.is_empty() {
            break;
        }
        host.touch();
        if let Some((buffer, limit)) = collect.as_mut() {
            let room = limit.saturating_sub(buffer.len());
            if data.len() > room {
                truncated = true;
            }
            buffer.extend_from_slice(&data[..data.len().min(room)]);
        }
        for part in data.chunks(protocol::PTY_BYTES) {
            let frame = json!({"session": session, "pty": pty_id, "seq": seq, "data": b64(part)});
            if let Err(err) = host.send("pty_data", frame).await {
                tracing::debug!("pty_data nicht gesendet: {err}");
            }
            seq += 1;
        }
    }
    let code = pty.wait().await;
    let frame = json!({"session": session, "pty": pty_id, "code": clamp_exit(code)});
    if let Err(err) = host.send("pty_exit", frame).await {
        tracing::debug!("pty_exit nicht gesendet: {err}");
    }
    (code, truncated)
}
