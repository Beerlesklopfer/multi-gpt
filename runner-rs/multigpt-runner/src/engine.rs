//! Container-Engine des Agents: Schnittstelle und `FakeEngine` für Tests
//! (Port von `runner_agent/engine.py`).
//!
//! Die echte Engine (libpod-REST über den Unix-Socket) steht in [`crate::podman`].
//! Jede Engine prüft Container- und Netzkörper vor dem Anlegen mit der festen
//! Vorlage ([`template::check_body`], [`template::check_network_body`]) und fasst
//! nur Objekte mit dem Label `multigpt.runner=1` an.

use std::collections::BTreeMap;
use std::fmt;
use std::time::Duration;

use async_trait::async_trait;
use serde_json::Value;

use crate::template::{self, TemplateError};

#[cfg(any(test, feature = "fake-engine"))]
pub use fake::FakeEngine;

/// Fehler der Container-Engine (deutsche Meldung, ohne Geheimnisse).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EngineError(pub String);

impl EngineError {
    pub fn new(message: impl Into<String>) -> Self {
        Self(message.into())
    }
}

impl fmt::Display for EngineError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for EngineError {}

impl From<TemplateError> for EngineError {
    fn from(err: TemplateError) -> Self {
        Self(err.0)
    }
}

pub type EngineResult<T> = Result<T, EngineError>;

/// Ergebnis eines `exec` ohne PTY.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ExecResult {
    pub code: Option<i64>,
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
    pub truncated: bool,
    pub timed_out: bool,
}

/// Name, Version und Betriebsart der Engine (`hello.engine`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EngineInfo {
    pub name: String,
    pub version: String,
    pub rootless: bool,
}

/// Container mit dem Label `multigpt.runner=1`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ContainerInfo {
    pub name: String,
    pub labels: BTreeMap<String, String>,
    pub running: bool,
}

/// Optionen eines `exec` ohne PTY.
#[derive(Debug, Clone)]
pub struct ExecOptions {
    pub stdin: Option<Vec<u8>>,
    pub timeout: Duration,
    pub max_output: usize,
    pub workdir: String,
}

impl Default for ExecOptions {
    fn default() -> Self {
        Self {
            stdin: None,
            timeout: Duration::from_secs(60),
            max_output: 64 * 1024,
            workdir: "/workspace".into(),
        }
    }
}

/// Laufender Prozess in einem Pseudo-Terminal des Containers. Lesen und
/// Schreiben dürfen gleichzeitig laufen.
#[async_trait]
pub trait Pty: Send + Sync {
    /// Nächste Ausgabe; leer am Ende.
    async fn read(&self) -> Vec<u8>;
    async fn write(&self, data: &[u8]) -> EngineResult<()>;
    async fn resize(&self, cols: u16, rows: u16) -> EngineResult<()>;
    /// Exit-Code nach dem Ende.
    async fn wait(&self) -> Option<i64>;
    /// Beenden (mehrfach aufrufbar); ein wartendes `read` liefert danach das Ende.
    async fn close(&self);
}

/// Schnittstelle der Container-Engine.
#[async_trait]
pub trait Engine: Send + Sync {
    async fn info(&self) -> EngineResult<EngineInfo>;
    async fn create_network(&self, body: &Value) -> EngineResult<()>;
    async fn remove_network(&self, name: &str) -> EngineResult<()>;
    async fn create_container(&self, body: &Value) -> EngineResult<()>;
    async fn start_container(&self, name: &str) -> EngineResult<()>;
    async fn remove_container(&self, name: &str) -> EngineResult<()>;
    async fn inspect_container(&self, name: &str) -> EngineResult<Value>;
    /// Alle Container mit dem Label `multigpt.runner=1`.
    async fn list_containers(&self) -> EngineResult<Vec<ContainerInfo>>;
    async fn exec(
        &self,
        name: &str,
        argv: &[String],
        opts: ExecOptions,
    ) -> EngineResult<ExecResult>;
    async fn exec_detached(&self, name: &str, argv: &[String], workdir: &str) -> EngineResult<()>;
    async fn exec_pty(
        &self,
        name: &str,
        argv: &[String],
        cols: u16,
        rows: u16,
        workdir: &str,
    ) -> EngineResult<Box<dyn Pty>>;
}

/// Labels als Map lesen (nur Textwerte).
pub fn labels_of(value: &Value) -> BTreeMap<String, String> {
    value
        .as_object()
        .map(|m| {
            m.iter()
                .filter_map(|(k, v)| v.as_str().map(|s| (k.clone(), s.to_string())))
                .collect()
        })
        .unwrap_or_default()
}

/// Gehört ein Objekt mit diesen Labels dem Agent?
pub fn is_own(labels: &Value) -> bool {
    labels
        .get(template::LABEL_RUNNER)
        .and_then(Value::as_str)
        .is_some_and(|v| v == "1")
}

#[cfg(any(test, feature = "fake-engine"))]
mod fake;
