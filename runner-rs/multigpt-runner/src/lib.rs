//! Runner-Agent von MultiGPT (M14, Rust).
//!
//! Läuft als Programm auf dem Rechner (systemd-User-Unit) bzw. auf einem
//! Zielsystem und steuert über den Podman-Socket des Nutzers wegwerfbare
//! Arbeitscontainer nach einer festen Vorlage. Zum Server baut er nur eine
//! ausgehende WebSocket-Verbindung auf (`wss://<host>/runner/ws`) und spricht
//! das Rahmenprotokoll von `multigpt/runner/protocol.py`.

pub mod agent_handler;
pub mod backoff;
pub mod catalog;
pub mod cli;
pub mod config;
pub mod connection;
pub mod engine;
pub mod fetch;
pub mod handler;
pub mod mcp;
pub mod outbox;
pub mod podman;
pub mod protocol;
pub mod session;
pub mod template;
pub mod tls;

/// Version des Agents (im `hello` und im User-Agent).
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
