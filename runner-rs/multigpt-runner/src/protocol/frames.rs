//! Typisierte Rahmen (serde). Feldnamen exakt wie in `protocol.py`; optionale
//! Felder sind `Option<_>` und entfallen beim Senden, wenn sie `None` sind.
//!
//! Prüfung (Längen, Muster, Bereiche, Richtung) macht [`super::schema`]; ein
//! [`Frame`] entsteht beim Empfang nur aus einem bereits geprüften Rahmen.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::ProtocolError;

/// Zustand der Container-Engine (`hello.engine`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct EngineInfo {
    pub name: String,
    pub version: String,
    pub rootless: bool,
}

/// Zustand einer Sitzung (`hello.sessions`, `heartbeat.sessions`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SessionInfo {
    pub session: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub workspace: Option<String>,
    pub state: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub preview_port: Option<u16>,
}

/// Freigegebenes Ziel bei `network = allowlist`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Allow {
    pub host: String,
    pub port: u16,
}

/// Tatsächliche Grenzen eines Arbeitscontainers (`session_state.limits`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Limits {
    pub cpus: f64,
    pub memory_mb: u64,
    pub pids: u64,
    pub tmpfs_mb: u64,
    pub network: String,
}

/// Bundle des Arbeitsstands (`session_start.bundle`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BundleInfo {
    pub size: u64,
    pub sha256: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub head: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Hello {
    pub token: String,
    pub protocol: u32,
    pub agent_version: String,
    pub engine: EngineInfo,
    pub hostname: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sessions: Option<Vec<SessionInfo>>,
    /// Zufall je Verbindung (64 hex); bindet die Signaturen des Servers.
    pub nonce: String,
    /// Text von catalog.json (exakte Bytes).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub catalog: Option<String>,
    /// Ed25519-Signatur eines Release-Schlüssels über `catalog` (Base64).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub catalog_sig: Option<String>,
    /// key_id des Release-Schlüssels.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub catalog_key: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Welcome {
    pub runner: u64,
    pub protocol: u32,
    pub server_version: String,
    pub heartbeat_seconds: u64,
    pub images: Vec<String>,
    pub max_frame_bytes: u64,
    pub chunk_bytes: u64,
    /// Freigegebene Dienste (nur diese bietet und führt der Agent aus).
    pub services: Vec<String>,
    /// Ergebnis der Prüfung des Dienstkatalogs.
    pub catalog: String,
}

/// Umschlag für jeden Rahmen des Servers (Protokoll 2).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Signed {
    /// key_id des Installationsschlüssels.
    pub key: String,
    /// JSON-Text des inneren Rahmens als Base64.
    pub payload: String,
    /// Ed25519-Signatur (Base64).
    pub sig: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Heartbeat {
    pub sessions: Vec<SessionInfo>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SessionStart {
    pub session: String,
    pub workspace: String,
    pub image: String,
    pub cpus: f64,
    pub memory_mb: u64,
    pub pids: u64,
    pub tmpfs_mb: u64,
    pub network: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub allow: Option<Vec<Allow>>,
    pub command_seconds: u64,
    pub output_bytes: u64,
    pub idle_minutes: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub bundle: Option<BundleInfo>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub scratch_mode: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub scratch_mb: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub workspace_uuid: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SessionState {
    pub session: String,
    pub state: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub limits: Option<Limits>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub preview_port: Option<u16>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SessionStop {
    pub session: String,
    pub reason: String,
}

/// MCP-Nachricht (JSON-RPC 2.0) je `pipe`, in beide Richtungen.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Mcp {
    pub session: String,
    pub pipe: String,
    pub message: Value,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BundleGet {
    pub session: String,
    pub transfer: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BundleChunk {
    pub session: String,
    pub transfer: String,
    pub seq: u64,
    /// Base64
    pub data: String,
    pub last: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub size: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sha256: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub head: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub message: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FileChunk {
    pub session: String,
    pub transfer: String,
    pub seq: u64,
    /// Base64
    pub data: String,
    pub last: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub size: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sha256: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub mime: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BundleAck {
    pub session: String,
    pub transfer: String,
    pub ok: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub head: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PtyOpen {
    pub session: String,
    pub pty: String,
    pub cols: u16,
    pub rows: u16,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PtyInput {
    pub session: String,
    pub pty: String,
    /// Base64
    pub data: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PtyResize {
    pub session: String,
    pub pty: String,
    pub cols: u16,
    pub rows: u16,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PtyClose {
    pub session: String,
    pub pty: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PtyData {
    pub session: String,
    pub pty: String,
    pub seq: u64,
    /// Base64
    pub data: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PtyExit {
    pub session: String,
    pub pty: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub code: Option<i32>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Preview {
    pub session: String,
    pub running: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub port: Option<u16>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub token: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PreviewRequest {
    pub session: String,
    pub request: String,
    pub method: String,
    pub path: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub headers: Option<BTreeMap<String, String>>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PreviewResponse {
    pub session: String,
    pub request: String,
    pub seq: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub status: Option<u16>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub headers: Option<BTreeMap<String, String>>,
    /// Base64
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub data: Option<String>,
    pub last: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ErrorFrame {
    pub code: String,
    pub message: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub session: Option<String>,
    /// pipe, transfer, pty bzw. request
    #[serde(default, rename = "ref", skip_serializing_if = "Option::is_none")]
    pub ref_: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Close {
    pub reason: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub message: Option<String>,
}

/// Ein Rahmen des Protokolls (beide Richtungen).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Frame {
    Hello(Hello),
    Welcome(Welcome),
    Signed(Signed),
    Heartbeat(Heartbeat),
    SessionStart(SessionStart),
    SessionState(SessionState),
    SessionStop(SessionStop),
    Mcp(Mcp),
    BundleGet(BundleGet),
    BundleChunk(BundleChunk),
    FileChunk(FileChunk),
    BundleAck(BundleAck),
    PtyOpen(PtyOpen),
    PtyInput(PtyInput),
    PtyResize(PtyResize),
    PtyClose(PtyClose),
    PtyData(PtyData),
    PtyExit(PtyExit),
    Preview(Preview),
    PreviewRequest(PreviewRequest),
    PreviewResponse(PreviewResponse),
    Error(ErrorFrame),
    Close(Close),
}

impl Frame {
    /// Typname wie im Feld `type`.
    pub fn kind(&self) -> &'static str {
        match self {
            Frame::Hello(_) => "hello",
            Frame::Welcome(_) => "welcome",
            Frame::Signed(_) => "signed",
            Frame::Heartbeat(_) => "heartbeat",
            Frame::SessionStart(_) => "session_start",
            Frame::SessionState(_) => "session_state",
            Frame::SessionStop(_) => "session_stop",
            Frame::Mcp(_) => "mcp",
            Frame::BundleGet(_) => "bundle_get",
            Frame::BundleChunk(_) => "bundle_chunk",
            Frame::FileChunk(_) => "file_chunk",
            Frame::BundleAck(_) => "bundle_ack",
            Frame::PtyOpen(_) => "pty_open",
            Frame::PtyInput(_) => "pty_input",
            Frame::PtyResize(_) => "pty_resize",
            Frame::PtyClose(_) => "pty_close",
            Frame::PtyData(_) => "pty_data",
            Frame::PtyExit(_) => "pty_exit",
            Frame::Preview(_) => "preview",
            Frame::PreviewRequest(_) => "preview_request",
            Frame::PreviewResponse(_) => "preview_response",
            Frame::Error(_) => "error",
            Frame::Close(_) => "close",
        }
    }

    /// Sitzung des Rahmens, falls er eine hat.
    pub fn session(&self) -> Option<&str> {
        match self {
            Frame::SessionStart(f) => Some(&f.session),
            Frame::SessionState(f) => Some(&f.session),
            Frame::SessionStop(f) => Some(&f.session),
            Frame::Mcp(f) => Some(&f.session),
            Frame::BundleGet(f) => Some(&f.session),
            Frame::BundleChunk(f) => Some(&f.session),
            Frame::FileChunk(f) => Some(&f.session),
            Frame::BundleAck(f) => Some(&f.session),
            Frame::PtyOpen(f) => Some(&f.session),
            Frame::PtyInput(f) => Some(&f.session),
            Frame::PtyResize(f) => Some(&f.session),
            Frame::PtyClose(f) => Some(&f.session),
            Frame::PtyData(f) => Some(&f.session),
            Frame::PtyExit(f) => Some(&f.session),
            Frame::Preview(f) => Some(&f.session),
            Frame::PreviewRequest(f) => Some(&f.session),
            Frame::PreviewResponse(f) => Some(&f.session),
            Frame::Error(f) => f.session.as_deref(),
            Frame::Hello(_)
            | Frame::Welcome(_)
            | Frame::Signed(_)
            | Frame::Heartbeat(_)
            | Frame::Close(_) => None,
        }
    }

    /// Bezug für einen Fehlerrahmen: pipe, pty, request bzw. transfer
    /// (Reihenfolge wie `Agent._error` in Python).
    pub fn reference(&self) -> Option<&str> {
        match self {
            Frame::Mcp(f) => Some(&f.pipe),
            Frame::PtyOpen(f) => Some(&f.pty),
            Frame::PtyInput(f) => Some(&f.pty),
            Frame::PtyResize(f) => Some(&f.pty),
            Frame::PtyClose(f) => Some(&f.pty),
            Frame::PtyData(f) => Some(&f.pty),
            Frame::PtyExit(f) => Some(&f.pty),
            Frame::PreviewRequest(f) => Some(&f.request),
            Frame::PreviewResponse(f) => Some(&f.request),
            Frame::BundleGet(f) => Some(&f.transfer),
            Frame::BundleChunk(f) => Some(&f.transfer),
            Frame::FileChunk(f) => Some(&f.transfer),
            Frame::BundleAck(f) => Some(&f.transfer),
            Frame::Error(f) => f.ref_.as_deref(),
            _ => None,
        }
    }

    /// Aus einem geprüften, normalisierten Rahmen (`schema::validate`).
    pub fn from_validated(value: Value) -> Result<Frame, ProtocolError> {
        serde_json::from_value(value)
            .map_err(|e| ProtocolError::new(format!("Rahmen nicht lesbar: {e}")))
    }
}

macro_rules! frame_from {
    ($($variant:ident($ty:ty)),* $(,)?) => {
        $(impl From<$ty> for Frame {
            fn from(value: $ty) -> Frame {
                Frame::$variant(value)
            }
        })*
    };
}

frame_from!(
    Hello(Hello),
    Welcome(Welcome),
    Signed(Signed),
    Heartbeat(Heartbeat),
    SessionStart(SessionStart),
    SessionState(SessionState),
    SessionStop(SessionStop),
    Mcp(Mcp),
    BundleGet(BundleGet),
    BundleChunk(BundleChunk),
    FileChunk(FileChunk),
    BundleAck(BundleAck),
    PtyOpen(PtyOpen),
    PtyInput(PtyInput),
    PtyResize(PtyResize),
    PtyClose(PtyClose),
    PtyData(PtyData),
    PtyExit(PtyExit),
    Preview(Preview),
    PreviewRequest(PreviewRequest),
    PreviewResponse(PreviewResponse),
    Error(ErrorFrame),
    Close(Close),
);

#[cfg(test)]
mod tests {
    use super::super::{Sender, encode, parse};
    use super::*;

    #[test]
    fn roundtrip_typed() {
        let frame = Frame::Error(ErrorFrame {
            code: "internal".into(),
            message: "x".into(),
            session: Some("s1".into()),
            ref_: Some("p1".into()),
        });
        let text = encode(&frame, Some(Sender::Agent)).unwrap();
        assert!(text.contains(r#""ref":"p1""#));
        assert_eq!(parse(&text, Some(Sender::Server)).unwrap(), frame);
    }

    #[test]
    fn session_start_with_nulls() {
        let text = format!(
            r#"{{"type":"session_start","session":"s","workspace":"w","image":"x@sha256:{}","cpus":2,"memory_mb":512,"pids":256,"tmpfs_mb":256,"network":"none","allow":null,"command_seconds":60,"output_bytes":65536,"idle_minutes":15}}"#,
            "0".repeat(64)
        );
        let Frame::SessionStart(start) = parse(&text, Some(Sender::Server)).unwrap() else {
            panic!("falscher Typ");
        };
        assert_eq!(start.cpus, 2.0);
        assert!(start.allow.is_none() && start.bundle.is_none());
    }
}
