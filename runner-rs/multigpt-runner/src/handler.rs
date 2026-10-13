//! Schnittstelle zwischen Verbindungskern und Sitzungen/Werkzeugen.
//!
//! Der Kern ([`crate::connection`]) prüft und entpackt die Rahmen des Servers
//! und ruft dafür einen [`FrameHandler`] auf. Rahmen der Reihenfolge-Gruppe
//! ([`is_ordered`]) laufen nacheinander in Empfangsreihenfolge, alle übrigen als
//! eigene Aufgabe (wie `ORDERED` in `runner_agent/agent.py`). Liefert der
//! Handler einen Fehler, sendet der Kern einen Rahmen `error` mit `session` und
//! `ref` (pipe, pty, request bzw. transfer).

use std::fmt;
use std::future::Future;

use crate::outbox::SendError;
use crate::protocol::{EngineInfo, Frame, SessionInfo, Welcome};

/// Rahmentypen, die der Kern der Reihe nach abarbeitet (kurz, reihenfolgekritisch).
pub const ORDERED: &[&str] = &[
    "mcp",
    "bundle_chunk",
    "bundle_ack",
    "pty_input",
    "pty_resize",
    "pty_close",
    "close",
    "error",
];

pub fn is_ordered(kind: &str) -> bool {
    ORDERED.contains(&kind)
}

/// Fehler bei der Verarbeitung eines Rahmens.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HandlerError {
    /// Wird dem Server als `error{code, message}` gemeldet; `code` aus `ERROR_CODES`.
    Frame { code: String, message: String },
    /// Verbindung weg – nichts mehr zu melden.
    Disconnected,
}

impl HandlerError {
    pub fn new(code: &str, message: impl Into<String>) -> Self {
        HandlerError::Frame {
            code: code.to_string(),
            message: message.into(),
        }
    }

    pub fn internal() -> Self {
        Self::new("internal", "Interner Fehler im Agent.")
    }
}

impl fmt::Display for HandlerError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            HandlerError::Frame { code, message } => write!(f, "{code}: {message}"),
            HandlerError::Disconnected => f.write_str("Verbindung getrennt"),
        }
    }
}

impl std::error::Error for HandlerError {}

impl From<SendError> for HandlerError {
    fn from(e: SendError) -> Self {
        match e {
            SendError::NotConnected => HandlerError::Disconnected,
            SendError::Protocol(p) => HandlerError::new("internal", p.message),
        }
    }
}

/// Sitzungen und Werkzeuge des Agents, aus Sicht des Verbindungskerns.
///
/// Senden an den Server über das [`crate::outbox::Outbox`], das der Handler beim
/// Bau bekommt. Alle Methoden dürfen gleichzeitig laufen (`&self`).
pub trait FrameHandler: Send + Sync + 'static {
    /// Zustand der Container-Engine für `hello`. Fehler: Kern schließt mit 1013
    /// und versucht es später erneut.
    fn engine_info(&self) -> impl Future<Output = Result<EngineInfo, HandlerError>> + Send;

    /// Einmal beim Start: laufende Arbeitscontainer übernehmen.
    fn adopt(&self) -> impl Future<Output = ()> + Send;

    /// Zustand der Sitzungen für `hello` und `heartbeat`.
    fn sessions(&self) -> impl Future<Output = Vec<SessionInfo>> + Send;

    /// Nach jedem `welcome` (Images, Stückgröße, freigegebene Dienste).
    fn on_welcome(&self, welcome: &Welcome) -> impl Future<Output = ()> + Send;

    /// Ein Rahmen des Servers (nie `welcome`, `close` oder `error` – die behandelt der Kern).
    fn on_frame(&self, frame: Frame) -> impl Future<Output = Result<(), HandlerError>> + Send;

    /// Vor jedem Heartbeat (Leerlauf prüfen).
    fn tick(&self) -> impl Future<Output = ()> + Send;

    /// Verbindung weg: MCP-Pipes und Terminals schließen; Container bleiben.
    fn disconnected(&self) -> impl Future<Output = ()> + Send;
}

/// Handler ohne Sitzungen (Gerüst und Tests des Kerns): meldet jede Anfrage
/// als `not_allowed`.
#[derive(Debug, Default, Clone)]
pub struct NullHandler;

impl FrameHandler for NullHandler {
    async fn engine_info(&self) -> Result<EngineInfo, HandlerError> {
        Ok(EngineInfo {
            name: "fake".into(),
            version: "0".into(),
            rootless: true,
        })
    }

    async fn adopt(&self) {}

    async fn sessions(&self) -> Vec<SessionInfo> {
        Vec::new()
    }

    async fn on_welcome(&self, _welcome: &Welcome) {}

    async fn on_frame(&self, frame: Frame) -> Result<(), HandlerError> {
        Err(HandlerError::new(
            "not_allowed",
            format!("Rahmen {} wird nicht unterstützt.", frame.kind()),
        ))
    }

    async fn tick(&self) {}

    async fn disconnected(&self) {}
}
