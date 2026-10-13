//! Der Handler des Agents: verbindet den Verbindungskern mit den Sitzungen
//! (`session::SessionManager`, rs-podman) und den MCP-Werkzeugen
//! (`mcp::McpRouter`, rs-mcp) – wie `Agent._dispatch` in `runner_agent/agent.py`.

use std::collections::BTreeSet;
use std::sync::Arc;

use async_trait::async_trait;

use crate::config::Config;
use crate::engine::Engine;
use crate::fetch;
use crate::handler::{FrameHandler, HandlerError};
use crate::mcp::McpRouter;
use crate::outbox::Outbox;
use crate::podman::PodmanEngine;
use crate::protocol::{EngineInfo, Frame, SessionInfo, Welcome};
use crate::session::{Downloader, SessionHandle, SessionManager};
use crate::template::Bounds;

/// Sitzungen und Werkzeuge eines Agents.
pub struct AgentHandler {
    pub manager: Arc<SessionManager>,
    pub router: McpRouter<SessionHandle>,
    engine: Arc<dyn Engine>,
}

impl AgentHandler {
    pub fn new(engine: Arc<dyn Engine>, config: &Config, outbox: Outbox) -> Self {
        let manager = Arc::new(SessionManager::new(engine.clone(), config, outbox));
        manager.set_downloader(Arc::new(ServerDownloader {
            config: config.clone(),
        }));
        Self {
            manager,
            router: McpRouter::new(),
            engine,
        }
    }

    /// Handler mit der Engine aus den Einstellungen (`MGPT_ENGINE`).
    pub fn from_config(config: &Config, outbox: Outbox) -> Result<Arc<Self>, String> {
        let engine: Arc<dyn Engine> = match config.engine.as_str() {
            "podman" => Arc::new(PodmanEngine::new(
                Bounds::from(config),
                config.podman_socket.clone(),
            )),
            #[cfg(feature = "fake-engine")]
            "fake" => Arc::new(crate::engine::FakeEngine::new(Bounds::from(config))),
            other => return Err(format!("MGPT_ENGINE nicht verfügbar: {other}")),
        };
        Ok(Arc::new(Self::new(engine, config, outbox)))
    }

    fn session_ids(&self) -> BTreeSet<String> {
        self.manager
            .infos()
            .into_iter()
            .map(|info| info.session)
            .collect()
    }

    /// MCP-Pipes von Sitzungen schließen, die es nicht mehr gibt.
    async fn close_gone(&self, before: &BTreeSet<String>) {
        let now = self.session_ids();
        for sid in before.difference(&now) {
            self.router.close_session(sid).await;
        }
    }
}

impl FrameHandler for AgentHandler {
    async fn engine_info(&self) -> Result<EngineInfo, HandlerError> {
        let info = self
            .engine
            .info()
            .await
            .map_err(|e| HandlerError::new("engine", e.to_string()))?;
        Ok(EngineInfo {
            name: info.name,
            version: info.version,
            rootless: info.rootless,
        })
    }

    async fn adopt(&self) {
        self.manager.adopt().await;
    }

    async fn sessions(&self) -> Vec<SessionInfo> {
        self.manager.infos()
    }

    async fn on_welcome(&self, welcome: &Welcome) {
        self.manager.on_welcome(welcome);
        self.manager.set_services(Some(welcome.services.clone()));
    }

    async fn on_frame(&self, frame: Frame) -> Result<(), HandlerError> {
        match frame {
            Frame::SessionStart(start) => {
                let before = self.session_ids();
                self.manager.start(&start).await;
                self.close_gone(&before).await;
            }
            Frame::SessionStop(stop) => {
                self.router.close_session(&stop.session).await;
                self.manager.stop(&stop.session, &stop.reason).await;
            }
            Frame::Mcp(mcp) => {
                let handle = self.manager.handle(&mcp.session)?;
                self.router
                    .on_frame(Arc::new(handle), &mcp.pipe, mcp.message)
                    .await;
            }
            Frame::BundleChunk(chunk) => self.manager.on_bundle_chunk(&chunk).await?,
            Frame::BundleAck(ack) => self.manager.on_bundle_ack(&ack),
            Frame::PtyOpen(open) => self.manager.pty_open(&open).await?,
            Frame::PtyInput(input) => self.manager.pty_input(&input).await?,
            Frame::PtyResize(resize) => self.manager.pty_resize(&resize).await?,
            Frame::PtyClose(close) => self.manager.pty_close(&close).await?,
            Frame::PreviewRequest(request) => self.manager.preview_request(&request).await?,
            // close/error/welcome behandelt der Kern; Rahmen des Agents kommen nicht vom Server.
            _ => {}
        }
        Ok(())
    }

    async fn tick(&self) {
        let before = self.session_ids();
        self.manager.check_idle().await;
        self.close_gone(&before).await;
    }

    async fn disconnected(&self) {
        self.router.close_all().await;
        self.manager.close_all_io().await;
    }
}

/// Download vom eigenen Server (Einrichtungswerkzeug), mit TLS wie die Verbindung.
struct ServerDownloader {
    config: Config,
}

#[async_trait]
impl Downloader for ServerDownloader {
    async fn download(&self, url: &str, bearer: &str, max_bytes: usize) -> Result<Vec<u8>, String> {
        fetch::https_get(&self.config, url, Some(bearer), max_bytes as u64)
            .await
            .map_err(|e| e.to_string())
    }
}
