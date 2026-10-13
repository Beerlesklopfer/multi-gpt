//! MCP-Server des Agents (M14, Rust-Agent; Port von `runner_agent/tools.py`).
//!
//! Für MultiGPT ist der Runner ein MCP-Server (Transport `runner`). Je `pipe`
//! läuft hier ein Server des offiziellen SDK `rmcp`; die JSON-RPC-Nachrichten
//! kommen und gehen als Rahmen `mcp`. Der Server von MultiGPT bietet dem Modell
//! seinen eigenen, signierten Katalog an (`runner-rs/catalog.json`); `tools/list`
//! dient nur dem Abgleich und ist auf `welcome.services` beschränkt.
//!
//! Werkzeuge: `read`, `write`, `edit`, `glob`, `grep`, `bash`, `serve`,
//! `publish`, `telnet` und `setup_host` (Zielsystem einrichten; ersetzt `ssh`).
//! Alle Ergebnisse stammen aus dem Container und gelten als nicht
//! vertrauenswürdiges Material (`_meta["multigpt/untrusted"]`).
//!
//! Einbindung: Die Sitzung setzt `ToolHost` um; der Agent ruft für jeden
//! Rahmen `mcp` `McpRouter::on_frame` und beim Stopp `close_session`.

pub mod host;
pub mod server;
pub mod setup;
pub mod telnet;
pub mod tools;

#[cfg(test)]
mod tests;

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use serde_json::{Value, json};

pub use host::{ExecOutput, Relay, Target, ToolError, ToolHost, ToolLimits, ToolPty, ToolResult};
pub use server::{McpPipe, RunnerServer};
pub use tools::SessionTools;

/// Inhalt von `runner-rs/catalog.json` (Dienstkatalog, signiert von der CI).
pub const CATALOG_JSON: &str = include_str!("../../../catalog.json");

struct SessionEntry<H: ToolHost> {
    tools: Arc<SessionTools<H>>,
    pipes: HashMap<String, Arc<tokio::sync::Mutex<McpPipe>>>,
}

/// Alle MCP-pipes aller Sitzungen dieses Agents.
pub struct McpRouter<H: ToolHost> {
    sessions: Arc<Mutex<HashMap<String, SessionEntry<H>>>>,
}

impl<H: ToolHost> Default for McpRouter<H> {
    fn default() -> Self {
        Self {
            sessions: Arc::new(Mutex::new(HashMap::new())),
        }
    }
}

impl<H: ToolHost> McpRouter<H> {
    pub fn new() -> Self {
        Self::default()
    }

    /// Werkzeuge der Sitzung (legt sie beim ersten Aufruf an).
    pub fn tools(&self, host: &Arc<H>) -> Arc<SessionTools<H>> {
        let mut sessions = self.sessions.lock().expect("Sperre");
        sessions
            .entry(host.session_id())
            .or_insert_with(|| SessionEntry {
                tools: Arc::new(SessionTools::new(host.clone())),
                pipes: HashMap::new(),
            })
            .tools
            .clone()
    }

    /// Rahmen `mcp` des Servers: Nachricht an den Server der pipe (legt ihn an).
    pub async fn on_frame(&self, host: Arc<H>, pipe: &str, message: Value) {
        let sid = host.session_id();
        let tools = self.tools(&host);
        let handle = {
            let mut sessions = self.sessions.lock().expect("Sperre");
            let entry = sessions.get_mut(&sid).expect("eben angelegt");
            entry
                .pipes
                .retain(|_, p| p.try_lock().map_or(true, |p| !p.is_finished()));
            entry
                .pipes
                .entry(pipe.to_string())
                .or_insert_with(|| {
                    let registry = self.sessions.clone();
                    let (sid, name) = (sid.clone(), pipe.to_string());
                    let on_end = move || {
                        if let Some(entry) = registry.lock().expect("Sperre").get_mut(&sid) {
                            entry.pipes.remove(&name);
                        }
                    };
                    Arc::new(tokio::sync::Mutex::new(McpPipe::start(
                        tools.clone(),
                        pipe.to_string(),
                        on_end,
                    )))
                })
                .clone()
        };
        host.touch();
        let fed = handle.lock().await.feed(message).await;
        if let Err(Some(error)) = fed {
            let frame = json!({"session": sid, "pipe": pipe, "message": error});
            if let Err(err) = host.send("mcp", frame).await {
                tracing::warn!("MCP-Fehler nicht sendbar: {err}");
            }
        }
    }

    /// Ende einer Sitzung: pipes und Telnet-Verbindungen schließen.
    pub async fn close_session(&self, sid: &str) {
        let entry = self.sessions.lock().expect("Sperre").remove(sid);
        if let Some(entry) = entry {
            for pipe in entry.pipes.values() {
                pipe.lock().await.close();
            }
            entry.tools.close().await;
        }
    }

    /// Alle Sitzungen schließen (Verbindungsende des Agents).
    pub async fn close_all(&self) {
        let ids: Vec<String> = self
            .sessions
            .lock()
            .expect("Sperre")
            .keys()
            .cloned()
            .collect();
        for sid in ids {
            self.close_session(&sid).await;
        }
    }

    /// Zahl offener pipes einer Sitzung (Tests, Status).
    pub fn pipe_count(&self, sid: &str) -> usize {
        self.sessions
            .lock()
            .expect("Sperre")
            .get(sid)
            .map_or(0, |entry| entry.pipes.len())
    }
}
