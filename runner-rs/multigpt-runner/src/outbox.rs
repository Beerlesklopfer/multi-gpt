//! Senden an den Server: ein klonbarer Griff auf die aktuelle Verbindung.
//!
//! Wie `Agent.send(kind, **fields)` in Python: Der Rahmen wird gebaut, mit
//! Richtung `agent` geprüft (null-Felder entfallen) und an die Schreibaufgabe
//! der Verbindung übergeben. Ohne Verbindung schlägt das Senden fehl.

use std::fmt;
use std::sync::{Arc, RwLock};

use serde_json::Value;
use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::Message;

use crate::protocol::{self, Frame, ProtocolError, Sender};

/// Senden fehlgeschlagen.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SendError {
    /// Keine Verbindung zum Server (bzw. gerade getrennt).
    NotConnected,
    /// Rahmen verletzt das Protokoll (Programmfehler im Agent).
    Protocol(ProtocolError),
}

impl fmt::Display for SendError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            SendError::NotConnected => f.write_str("Keine Verbindung zum Server."),
            SendError::Protocol(e) => write!(f, "Ungültiger Rahmen: {e}"),
        }
    }
}

impl std::error::Error for SendError {}

impl From<ProtocolError> for SendError {
    fn from(e: ProtocolError) -> Self {
        SendError::Protocol(e)
    }
}

/// Klonbarer Griff zum Senden; überlebt Neuverbindungen.
#[derive(Clone, Default)]
pub struct Outbox {
    inner: Arc<RwLock<Option<mpsc::Sender<Message>>>>,
}

impl fmt::Debug for Outbox {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Outbox")
            .field("connected", &self.is_connected())
            .finish()
    }
}

impl Outbox {
    pub fn new() -> Self {
        Self::default()
    }

    /// Ein Outbox, dessen Rahmen in einem Kanal landen (Tests der Handler).
    ///
    /// Liefert die Textrahmen in Sendereihenfolge.
    pub fn channel(capacity: usize) -> (Outbox, mpsc::Receiver<Message>) {
        let (tx, rx) = mpsc::channel(capacity);
        let outbox = Outbox::new();
        outbox.attach(tx);
        (outbox, rx)
    }

    /// Verbindung anhängen (nur der Verbindungskern).
    pub fn attach(&self, tx: mpsc::Sender<Message>) {
        *self.inner.write().unwrap_or_else(|e| e.into_inner()) = Some(tx);
    }

    /// Verbindung lösen (nur der Verbindungskern).
    pub fn detach(&self) {
        *self.inner.write().unwrap_or_else(|e| e.into_inner()) = None;
    }

    pub fn is_connected(&self) -> bool {
        self.sender().is_some_and(|tx| !tx.is_closed())
    }

    fn sender(&self) -> Option<mpsc::Sender<Message>> {
        self.inner.read().unwrap_or_else(|e| e.into_inner()).clone()
    }

    /// Rahmen `kind` mit den Feldern `fields` (JSON-Objekt) senden.
    pub async fn send(&self, kind: &str, fields: Value) -> Result<(), SendError> {
        let text = protocol::encode_value(kind, Some(Sender::Agent), fields)?;
        self.send_text(text).await
    }

    /// Typisierten Rahmen senden.
    pub async fn send_frame(&self, frame: impl Into<Frame>) -> Result<(), SendError> {
        let text = protocol::encode(&frame.into(), Some(Sender::Agent))?;
        self.send_text(text).await
    }

    /// Bereits geprüften Text senden.
    async fn send_text(&self, text: String) -> Result<(), SendError> {
        let tx = self.sender().ok_or(SendError::NotConnected)?;
        tx.send(Message::text(text))
            .await
            .map_err(|_| SendError::NotConnected)
    }

    /// Steuerrahmen (Ping, Close) an die Verbindung (nur der Verbindungskern).
    pub(crate) async fn send_raw(&self, message: Message) -> Result<(), SendError> {
        let tx = self.sender().ok_or(SendError::NotConnected)?;
        tx.send(message).await.map_err(|_| SendError::NotConnected)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[tokio::test]
    async fn send_validates_and_drops_nulls() {
        let outbox = Outbox::new();
        assert_eq!(
            outbox.send("heartbeat", json!({"sessions": []})).await,
            Err(SendError::NotConnected)
        );
        let (outbox, mut rx) = Outbox::channel(4);
        outbox
            .send(
                "pty_exit",
                json!({"session": "s", "pty": "p", "code": null}),
            )
            .await
            .unwrap();
        let Message::Text(text) = rx.recv().await.unwrap() else {
            panic!("Text erwartet");
        };
        assert_eq!(
            text.as_str(),
            r#"{"pty":"p","session":"s","type":"pty_exit"}"#
        );
        // Richtung: welcome darf der Agent nicht senden
        assert!(matches!(
            outbox.send("close", json!({"reason": "quota"})).await,
            Err(SendError::Protocol(_))
        ));
        outbox.detach();
        assert!(!outbox.is_connected());
    }
}
