//! Offene Telnet-Sitzung im PTY eines Hilfscontainers (nie im Arbeitscontainer).
//!
//! Die Verbindung bleibt über Aufrufe hinweg offen, bis sie endet oder die
//! Sitzung stoppt. Ausgaben gehen live als `pty_data` an den Server und werden
//! für das Modell gesammelt; ein Passwort-Echo wird maskiert.

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use regex::Regex;
use serde_json::{Map, Value, json};
use tokio::sync::{Mutex, Notify};
use tokio::task::JoinHandle;
use tokio::time::Instant;

use super::host::{Target, ToolError, ToolHost, ToolPty, ToolResult};
use super::tools::{SessionTools, b64};
use crate::protocol;

/// Sekunden ohne Ausgabe = Antwort vollständig.
pub const TELNET_QUIET: Duration = Duration::from_secs(1);
pub const TELNET_MAX_WAIT: Duration = Duration::from_secs(20);

struct Shared {
    buffer: Mutex<Vec<u8>>,
    event: Notify,
    closed: AtomicBool,
}

pub struct TelnetConnection<H: ToolHost> {
    host: Arc<H>,
    pty: Arc<H::Pty>,
    helper: String,
    shared: Arc<Shared>,
    reader: Mutex<Option<JoinHandle<()>>>,
    output_bytes: usize,
}

impl<H: ToolHost> TelnetConnection<H> {
    /// Hilfscontainer starten (nur am Relay-Netz) und `telnet` im PTY öffnen.
    pub async fn open(tools: &SessionTools<H>, relay_port: u16) -> ToolResult<Arc<Self>> {
        let host = tools.host.clone();
        let relay = host
            .relay()
            .ok_or_else(|| ToolError::new("Für diese Sitzung ist kein Relay eingerichtet."))?;
        let pty_id = tools.next_pty_id("telnet");
        let helper = host.open_helper().await?;
        let argv = vec!["telnet".to_string(), relay.ip, relay_port.to_string()];
        let pty = match host.exec_pty(&pty_id, Target::Helper(&helper), argv).await {
            Ok(pty) => Arc::new(pty),
            Err(err) => {
                host.close_helper(&helper).await;
                return Err(err);
            }
        };
        let shared = Arc::new(Shared {
            buffer: Mutex::new(Vec::new()),
            event: Notify::new(),
            closed: AtomicBool::new(false),
        });
        let conn = Arc::new(Self {
            output_bytes: host.limits().output_bytes,
            host: host.clone(),
            pty: pty.clone(),
            helper: helper.clone(),
            shared: shared.clone(),
            reader: Mutex::new(None),
        });
        let task = tokio::spawn(read_loop(host, pty, pty_id, helper, shared));
        *conn.reader.lock().await = Some(task);
        Ok(conn)
    }

    pub fn is_closed(&self) -> bool {
        self.shared.closed.load(Ordering::SeqCst)
    }

    /// Ausgabe sammeln, bis `until` passt bzw. `TELNET_QUIET` lang Ruhe ist.
    async fn collect(&self, until: Option<&Regex>, max_wait: Duration) -> String {
        let deadline = Instant::now() + max_wait;
        while Instant::now() < deadline && !self.is_closed() {
            let notified = self.shared.event.notified();
            if let Some(pattern) = until {
                let text = String::from_utf8_lossy(&self.shared.buffer.lock().await).into_owned();
                if pattern.is_match(&text) {
                    break;
                }
            }
            let wait = TELNET_QUIET.min(deadline.saturating_duration_since(Instant::now()));
            if tokio::time::timeout(wait, notified).await.is_err() && until.is_none() {
                break;
            }
        }
        let data = std::mem::take(&mut *self.shared.buffer.lock().await);
        let text = String::from_utf8_lossy(&data).into_owned();
        // Nur das Ende (Grenze in Zeichen wie in Python).
        let count = text.chars().count();
        if count > self.output_bytes {
            text.chars().skip(count - self.output_bytes).collect()
        } else {
            text
        }
    }

    /// Anmelden mit Benutzer und ggf. Passwort (Passwort-Echo wird maskiert).
    pub async fn login(&self, host: &Map<String, Value>) -> ToolResult<String> {
        let text = |key: &str| {
            host.get(key)
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string()
        };
        let (user, secret) = (text("user"), text("secret"));
        if user.is_empty() {
            return Ok(self.collect(None, TELNET_MAX_WAIT).await);
        }
        let login_re = Regex::new(r"(?im)(login|user(name)?)\s*:\s*$").expect("Muster");
        let mut out = self.collect(Some(&login_re), Duration::from_secs(15)).await;
        self.pty.write(format!("{user}\r\n").as_bytes()).await?;
        if text("secret_type") == "password" && !secret.is_empty() {
            let pass_re = Regex::new(r"(?im)pass(word)?\s*:\s*$").expect("Muster");
            out += &self.collect(Some(&pass_re), Duration::from_secs(15)).await;
            self.pty.write(format!("{secret}\r\n").as_bytes()).await?;
            // Passwort-Echo nie zurückgeben
            out += &self
                .collect(None, TELNET_MAX_WAIT)
                .await
                .replace(&secret, "****");
        }
        Ok(out)
    }

    pub async fn send(&self, data: &str) -> ToolResult<String> {
        if self.is_closed() {
            return Ok(String::new());
        }
        self.pty.write(format!("{data}\r\n").as_bytes()).await?;
        Ok(self.collect(None, TELNET_MAX_WAIT).await)
    }

    pub async fn close(&self) {
        if let Some(task) = self.reader.lock().await.take() {
            task.abort();
        }
        self.pty.close().await;
        self.host.close_helper(&self.helper).await;
        self.shared.closed.store(true, Ordering::SeqCst);
        self.shared.event.notify_waiters();
    }
}

async fn read_loop<H: ToolHost>(
    host: Arc<H>,
    pty: Arc<H::Pty>,
    pty_id: String,
    helper: String,
    shared: Arc<Shared>,
) {
    let session = host.session_id();
    let mut seq = 0u64;
    while let Some(data) = pty.read().await {
        if data.is_empty() {
            break;
        }
        shared.buffer.lock().await.extend_from_slice(&data);
        shared.event.notify_waiters();
        let part = &data[..data.len().min(protocol::PTY_BYTES)];
        let frame = json!({"session": session, "pty": pty_id, "seq": seq, "data": b64(part)});
        if let Err(err) = host.send("pty_data", frame).await {
            tracing::debug!("pty_data (telnet) nicht gesendet: {err}");
        }
        seq += 1;
    }
    shared.closed.store(true, Ordering::SeqCst);
    shared.event.notify_waiters();
    pty.close().await;
    host.close_helper(&helper).await;
    let _ = host
        .send("pty_exit", json!({"session": session, "pty": pty_id}))
        .await;
}
