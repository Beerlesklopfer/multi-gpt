//! Schnittstelle zwischen den Werkzeugen und der Sitzung (`ToolHost`).
//!
//! Die Werkzeuge kennen weder Podman noch die Verbindung: alles, was sie
//! brauchen, liefert die Sitzung über diesen Trait (umgesetzt von
//! `session::SessionHandle`, in den Tests von einer Test-Sitzung). Er ist
//! generisch (RPITIT mit `Send`), damit kein `async-trait` nötig ist; nur das
//! PTY ist ein Objekt (`ToolPty`), weil Telnet es über Aufrufe hinweg hält.

use std::future::Future;
use std::sync::Arc;

use serde_json::{Map, Value};

/// Fehler eines Werkzeugs; der Text (deutsch) geht als Fehlerergebnis an das Modell.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ToolError {
    pub message: String,
}

impl ToolError {
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl std::fmt::Display for ToolError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for ToolError {}

pub type ToolResult<T> = Result<T, ToolError>;

/// Werte der Sitzung aus `session_start`, soweit die Werkzeuge sie brauchen.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ToolLimits {
    /// Zeitlimit je Befehl (Sekunden), Vorgabe 120.
    pub command_seconds: u64,
    /// Ausgabe je Befehl für das Modell (Byte), Vorgabe 64 KiB.
    pub output_bytes: usize,
    /// Größe des tmpfs (MB) – harte Dateigrenze der Vorlage.
    pub tmpfs_mb: u64,
}

impl Default for ToolLimits {
    fn default() -> Self {
        Self {
            command_seconds: 120,
            output_bytes: 64 * 1024,
            tmpfs_mb: 256,
        }
    }
}

/// Relay der Sitzung (Netz `allowlist`): Adresse im Relay-Netz und Ziele in
/// Reihenfolge der Positivliste (Port `RELAY_BASE_PORT + i` = i-tes Ziel).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Relay {
    pub ip: String,
    pub targets: Vec<(String, u16)>,
}

/// Ergebnis eines `exec` ohne PTY.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ExecOutput {
    pub code: Option<i32>,
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
    pub timed_out: bool,
}

/// Wo ein Befehl läuft.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Target<'a> {
    /// Arbeitscontainer der Sitzung (Arbeitsverzeichnis `/workspace`).
    Work,
    /// Hilfscontainer (Name aus `open_helper`, Arbeitsverzeichnis `/tmp`).
    Helper(&'a str),
}

/// PTY eines Befehls. Lesen und Schreiben dürfen gleichzeitig laufen (Telnet).
pub trait ToolPty: Send + Sync + 'static {
    /// Nächste Ausgabe; leer bzw. `None` = Ende.
    fn read(&self) -> impl Future<Output = Option<Vec<u8>>> + Send;
    fn write(&self, data: &[u8]) -> impl Future<Output = ToolResult<()>> + Send;
    /// Exit-Code nach dem Ende (`None` = unbekannt).
    fn wait(&self) -> impl Future<Output = Option<i32>> + Send;
    /// Beenden und bei der Sitzung abmelden (mehrfach aufrufbar).
    fn close(&self) -> impl Future<Output = ()> + Send;
}

/// Alles, was die Werkzeuge von ihrer Sitzung brauchen.
pub trait ToolHost: Send + Sync + 'static {
    type Pty: ToolPty;

    /// Kennung der Sitzung (Feld `session` der Rahmen).
    fn session_id(&self) -> String;
    fn limits(&self) -> ToolLimits;
    /// Sperre der Sitzung: ein Werkzeug zur Zeit (auch Maß für „beschäftigt“).
    fn lock(&self) -> Arc<tokio::sync::Mutex<()>>;
    /// Aktivität vermerken (Leerlauf-Stopp).
    fn touch(&self);

    /// `mgpt-fs` im Arbeitscontainer: Anfrage `{"op": op, ...fields}`, Antwort
    /// ohne Rohdaten. Bei `write`/`edit` setzt die Sitzung `scratch_limit` selbst.
    /// `{"ok": false}` wird zu `ToolError` mit der Meldung von `mgpt-fs`.
    fn fs(
        &self,
        op: &str,
        fields: Map<String, Value>,
    ) -> impl Future<Output = ToolResult<Value>> + Send;
    /// Wie `fs`, liefert zusätzlich die Rohdaten nach der Antwortzeile (`file_out`).
    fn fs_raw(
        &self,
        op: &str,
        fields: Map<String, Value>,
    ) -> impl Future<Output = ToolResult<(Value, Vec<u8>)>> + Send;
    /// Commit und neues Bundle (8r); Hinweis für das Ergebnis ("" ohne Änderung).
    fn save(&self, message: &str) -> impl Future<Output = String> + Send;

    /// Lokales Scratchpad über seiner Grenze?
    fn scratch_full(&self) -> impl Future<Output = bool> + Send;
    /// Hinweis, wenn das lokale Scratchpad über der Grenze liegt ("" sonst).
    fn scratch_note(&self) -> impl Future<Output = String> + Send;
    /// Grenze des lokalen Scratchpads (MB), `None` ohne lokales Scratchpad.
    fn scratch_size_mb(&self) -> Option<u64>;
    /// Restplatz des lokalen Scratchpads in Byte (`None` ohne lokales).
    fn scratch_rest(&self) -> impl Future<Output = Option<u64>> + Send;

    /// Befehl im PTY starten (120 × 40). Die Sitzung meldet das PTY unter
    /// `pty_id` an (Terminal, Leerlauf) und beim `close` wieder ab.
    fn exec_pty(
        &self,
        pty_id: &str,
        target: Target<'_>,
        argv: Vec<String>,
    ) -> impl Future<Output = ToolResult<Self::Pty>> + Send;
    /// Befehl ohne PTY mit stdin (Zugang ablegen, Binary übertragen).
    fn exec(
        &self,
        target: Target<'_>,
        argv: Vec<String>,
        stdin: Vec<u8>,
        timeout_s: u64,
        max_output: usize,
    ) -> impl Future<Output = ToolResult<ExecOutput>> + Send;

    /// Vorschau starten; liefert `{url, port, token}`.
    fn start_preview(&self, path: Option<&str>) -> impl Future<Output = ToolResult<Value>> + Send;

    /// Hilfscontainer für ssh/telnet (nur am Relay-Netz); liefert den Namen.
    fn open_helper(&self) -> impl Future<Output = ToolResult<String>> + Send;
    fn close_helper(&self, name: &str) -> impl Future<Output = ()> + Send;
    /// Relay der Sitzung; `None`, wenn das Netz nicht `allowlist` ist.
    fn relay(&self) -> Option<Relay>;

    /// Rahmen an den Server (wie `send(kind, **fields)` in Python; `null`-Felder fallen weg).
    fn send(&self, kind: &str, fields: Value) -> impl Future<Output = ToolResult<()>> + Send;
    /// Größe der Stücke für `file_chunk`.
    fn chunk_bytes(&self) -> usize;

    /// Adresse von MultiGPT (`MGPT_URL`, ohne `/` am Ende).
    fn server_url(&self) -> String;
    /// Datei von MultiGPT laden (gleiches TLS-Pinning wie die Verbindung,
    /// `Authorization: Bearer <bearer>`; den Token nie protokollieren).
    fn download(
        &self,
        url: &str,
        bearer: &str,
        max_bytes: usize,
    ) -> impl Future<Output = ToolResult<Vec<u8>>> + Send;

    /// Dienste, die der Server freigegeben hat (`welcome.services`); `None` = alle.
    fn allowed_services(&self) -> Option<Vec<String>>;
}
