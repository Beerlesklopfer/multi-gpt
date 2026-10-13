//! Verbindung zum Runner-Dienst: Anmeldung, Signaturprüfung, Heartbeat, Rahmen,
//! Backoff (wie `runner_agent/agent.py`).
//!
//! Der Agent baut nur ausgehend eine WebSocket-Verbindung auf
//! (`wss://…/runner/ws`, ohne `Origin`-Header, ohne Kompression). TLS wird immer
//! geprüft – gegen die System-CAs oder gegen den festgehaltenen Fingerprint, und
//! zwar im Handshake, bevor der Token den Rechner verlässt.
//!
//! Protokoll 2: Jeder Rahmen des Servers kommt im Umschlag `signed`; der Agent
//! prüft ihn mit dem Installationsschlüssel gegen die eigene `nonce` und nimmt
//! sonst nichts an. Falsch signiert → Trennung mit 4498 ohne Neuversuch.
//!
//! Nach den Close-Codes 4401/4403/4409/4426/4498 verbindet sich der Agent nicht
//! neu: Ein Mensch muss handeln (Exit-Code 3, siehe [`crate::cli`]).

use std::future::Future;
use std::panic::AssertUnwindSafe;
use std::pin::Pin;
use std::sync::Arc;
use std::time::Duration;

use ed25519_dalek::VerifyingKey;
use futures_util::{FutureExt, SinkExt, StreamExt};
use rustls::pki_types::CertificateDer;
use serde_json::json;
use tokio::sync::{mpsc, watch};
use tokio::time::{Instant, timeout};
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::http::HeaderValue;
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;
use tokio_tungstenite::tungstenite::protocol::{CloseFrame, WebSocketConfig};
use tokio_tungstenite::tungstenite::{self, Message};
use tokio_tungstenite::{Connector, connect_async_tls_with_config};
use tracing::{error, info, warn};

use crate::VERSION;
use crate::backoff::Backoff;
use crate::config::Config;
use crate::handler::{FrameHandler, HandlerError, is_ordered};
use crate::outbox::Outbox;
use crate::protocol::{
    self, CLOSE_ABNORMAL, CLOSE_NORMAL, CLOSE_SIGNATURE, CLOSE_TOKEN, CLOSE_TRY_AGAIN, EngineInfo,
    Frame, MAX_FRAME_BYTES, MAX_TEXT, RECONNECT, Sender, Welcome,
};
use crate::tls;

/// TLS bzw. Fingerprint passt nicht (lokal, kein Close-Rahmen).
pub const CLOSE_TLS: u16 = 1015;
/// Close-Rahmen ohne Code.
pub const CLOSE_NO_STATUS: u16 = 1005;
/// Nachricht zu groß (lokal erkannt).
pub const CLOSE_TOO_LARGE: u16 = 1009;

/// Warten (im Test ersetzbar, etwa durch eine gefälschte Uhr).
pub type SleepFn = Arc<dyn Fn(Duration) -> Pin<Box<dyn Future<Output = ()> + Send>> + Send + Sync>;

/// Einstellbare Zeiten und Hilfen der Verbindung.
pub struct AgentOptions {
    pub backoff: Backoff,
    /// Wartezeit zwischen den Verbindungsversuchen.
    pub sleep: SleepFn,
    /// Zusätzliche vertrauenswürdige CAs (nur Tests).
    pub extra_roots: Vec<CertificateDer<'static>>,
    pub ping_interval: Duration,
    pub ping_timeout: Duration,
    pub open_timeout: Duration,
    pub close_timeout: Duration,
}

impl Default for AgentOptions {
    fn default() -> Self {
        Self {
            backoff: Backoff::default(),
            sleep: Arc::new(|d| Box::pin(tokio::time::sleep(d))),
            extra_roots: Vec::new(),
            ping_interval: Duration::from_secs(20),
            ping_timeout: Duration::from_secs(20),
            open_timeout: Duration::from_secs(15),
            close_timeout: Duration::from_secs(5),
        }
    }
}

/// Griff zum Beenden eines laufenden [`Agent`] (klonbar).
#[derive(Clone)]
pub struct StopHandle(Arc<watch::Sender<bool>>);

impl StopHandle {
    pub fn stop(&self) {
        self.0.send_replace(true);
    }

    pub fn is_stopped(&self) -> bool {
        *self.0.borrow()
    }
}

/// Der Agent: verbindet sich, meldet sich an und verteilt die Rahmen an den Handler.
pub struct Agent<H: FrameHandler> {
    pub config: Config,
    handler: Arc<H>,
    outbox: Outbox,
    opts: AgentOptions,
    stop_tx: Arc<watch::Sender<bool>>,
    stop_rx: watch::Receiver<bool>,
    /// Letztes `welcome` der aktuellen Verbindung.
    pub welcome: Option<Welcome>,
    /// Grund aus dem letzten Rahmen `close` des Servers.
    pub close_reason: Option<String>,
    /// Letzter Close-Code.
    pub last_close: Option<u16>,
    /// Wartezeiten zwischen den Versuchen (für Tests und Log).
    pub delays: Vec<f64>,
    /// Zahl der Verbindungsversuche.
    pub attempts: u32,
}

impl<H: FrameHandler> Agent<H> {
    /// `outbox` muss dasselbe sein, das der Handler zum Senden benutzt.
    pub fn new(config: Config, handler: Arc<H>, outbox: Outbox, opts: AgentOptions) -> Self {
        let (stop_tx, stop_rx) = watch::channel(false);
        Self {
            config,
            handler,
            outbox,
            opts,
            stop_tx: Arc::new(stop_tx),
            stop_rx,
            welcome: None,
            close_reason: None,
            last_close: None,
            delays: Vec::new(),
            attempts: 0,
        }
    }

    pub fn outbox(&self) -> &Outbox {
        &self.outbox
    }

    pub fn handler(&self) -> &Arc<H> {
        &self.handler
    }

    pub fn stop_handle(&self) -> StopHandle {
        StopHandle(self.stop_tx.clone())
    }

    fn stopping(&self) -> bool {
        *self.stop_rx.borrow()
    }

    /// Verbinden, bei Abbruch mit Backoff neu verbinden; liefert den letzten Code.
    pub async fn run(&mut self) -> Option<u16> {
        self.handler.adopt().await;
        while !self.stopping() {
            let code = self.run_once().await;
            self.last_close = Some(code);
            if self.stopping() {
                break;
            }
            if !protocol::should_reconnect(Some(code)) {
                error!(
                    "Server hat die Verbindung abgelehnt ({code}: {}); kein Neuversuch.",
                    protocol::close_reason(code).unwrap_or("?")
                );
                break;
            }
            let longest = !RECONNECT.contains(&code);
            let delay = self.opts.backoff.next_secs(longest);
            self.delays.push(delay);
            info!("Verbindung beendet ({code}), neuer Versuch in {delay:.1} s.");
            let pause = (self.opts.sleep)(Duration::from_secs_f64(delay));
            let mut stop = self.stop_rx.clone();
            tokio::select! {
                _ = pause => {}
                _ = wait_stopped(&mut stop) => {}
            }
        }
        self.last_close
    }

    /// Eine Verbindung: liefert den Close-Code (1006 ohne Close-Rahmen).
    pub async fn run_once(&mut self) -> u16 {
        self.attempts += 1;
        let (token, key) = match self.config.read_token().and_then(|t| {
            let key = self.config.read_server_key()?;
            Ok((t, key))
        }) {
            Ok(pair) => pair,
            Err(e) => {
                error!("{e}");
                return CLOSE_TOKEN;
            }
        };
        // Zustand der Engine VOR dem Verbindungsaufbau holen: Podman kann dafür
        // viele Sekunden brauchen, das hello muss aber binnen HELLO_TIMEOUT kommen.
        let info = match self.handler.engine_info().await {
            Ok(info) => info,
            Err(e) => {
                error!("Engine nicht erreichbar: {e}");
                return CLOSE_TRY_AGAIN;
            }
        };
        let secure = self.config.url.starts_with("wss://");
        let mut request = match self.config.url.as_str().into_client_request() {
            Ok(request) => request,
            Err(e) => {
                warn!("Verbindung fehlgeschlagen: {e}");
                return CLOSE_ABNORMAL;
            }
        };
        if let Ok(agent) = HeaderValue::from_str(&format!("multigpt-runner/{VERSION}")) {
            request.headers_mut().insert("User-Agent", agent);
        }
        let ws_config = WebSocketConfig::default()
            .max_message_size(Some(MAX_FRAME_BYTES))
            .max_frame_size(Some(MAX_FRAME_BYTES));
        let tls = secure
            .then(|| tls::client_config(self.config.ca_sha256.as_deref(), &self.opts.extra_roots));
        let connector = match &tls {
            Some(setup) => Connector::Rustls(setup.config.clone()),
            None => Connector::Plain,
        };
        let connect =
            connect_async_tls_with_config(request, Some(ws_config), true, Some(connector));
        let ws = match timeout(self.opts.open_timeout, connect).await {
            Ok(Ok((ws, _response))) => ws,
            Ok(Err(e)) => {
                if tls.as_ref().is_some_and(|t| t.pin_failed()) {
                    error!("Zertifikat des Servers passt nicht zum festgehaltenen Fingerprint.");
                    return CLOSE_TLS;
                }
                warn!("Verbindung fehlgeschlagen: {}", short_error(&e));
                return CLOSE_ABNORMAL;
            }
            Err(_) => {
                warn!("Verbindung fehlgeschlagen: Zeitüberschreitung");
                return CLOSE_ABNORMAL;
            }
        };
        self.welcome = None;
        self.close_reason = None;
        let (sink, stream) = ws.split();
        let (tx, rx) = mpsc::channel::<Message>(64);
        self.outbox.attach(tx);
        let writer = tokio::spawn(write_loop(sink, rx));
        let code = self.session(stream, token, key, info).await;
        self.outbox.detach();
        let _ = timeout(self.opts.close_timeout, writer).await;
        self.handler.disconnected().await;
        code
    }

    async fn session<S>(
        &mut self,
        mut stream: S,
        token: String,
        key: VerifyingKey,
        info: EngineInfo,
    ) -> u16
    where
        S: futures_util::Stream<Item = Result<Message, tungstenite::Error>> + Unpin,
    {
        let nonce = hex::encode(rand::random::<[u8; 32]>());
        let catalog = match self.config.read_catalog() {
            Ok(catalog) => catalog,
            Err(e) => {
                error!("{e}");
                self.close(CLOSE_NORMAL).await;
                return CLOSE_TOKEN;
            }
        };
        let hostname = if self.config.hostname.is_empty() {
            "runner".to_string()
        } else {
            self.config.hostname.clone()
        };
        let hello = json!({
            "token": token,
            "protocol": protocol::PROTOCOL_VERSION,
            "agent_version": VERSION,
            "engine": info,
            "hostname": hostname,
            "sessions": self.handler.sessions().await,
            "nonce": nonce,
            "catalog": catalog.text,
            "catalog_sig": catalog.sig,
            "catalog_key": catalog.key,
        });
        drop(token);
        if let Err(e) = self.outbox.send("hello", hello).await {
            warn!("hello nicht gesendet: {e}");
            return CLOSE_ABNORMAL;
        }

        let mut heartbeat: Option<tokio::task::JoinHandle<()>> = None;
        let mut received: Option<u16> = None;
        let mut stop = self.stop_rx.clone();
        let mut pings = tokio::time::interval_at(
            Instant::now() + self.opts.ping_interval,
            self.opts.ping_interval,
        );
        let mut awaiting_pong: Option<Instant> = None;
        // Nach dem Close des Servers höchstens close_timeout auf das Ende der TCP-Verbindung warten.
        let mut close_deadline: Option<Instant> = None;
        let code = loop {
            let deadline =
                close_deadline.unwrap_or_else(|| Instant::now() + Duration::from_secs(3600));
            tokio::select! {
                _ = tokio::time::sleep_until(deadline), if close_deadline.is_some() => {
                    break received.unwrap_or(CLOSE_ABNORMAL);
                }
                _ = wait_stopped(&mut stop) => {
                    self.close(CLOSE_NORMAL).await;
                    // weiterlesen, bis der Server das Close bestätigt (höchstens close_timeout)
                    let deadline = Instant::now() + self.opts.close_timeout;
                    while let Ok(Some(msg)) = tokio::time::timeout_at(deadline, stream.next()).await {
                        match msg {
                            Ok(Message::Close(frame)) => received = Some(close_code_of(frame.as_ref())),
                            Ok(_) => {}
                            Err(_) => break,
                        }
                    }
                    break received.unwrap_or(CLOSE_NORMAL);
                }
                _ = pings.tick() => {
                    if let Some(since) = awaiting_pong {
                        if since.elapsed() >= self.opts.ping_timeout {
                            warn!("Keine Antwort auf Ping, Verbindung gilt als getrennt.");
                            break CLOSE_ABNORMAL;
                        }
                    } else {
                        awaiting_pong = Some(Instant::now());
                        let payload = rand::random::<[u8; 4]>().to_vec();
                        if self.outbox.send_raw(Message::Ping(payload.into())).await.is_err() {
                            break received.unwrap_or(CLOSE_ABNORMAL);
                        }
                    }
                }
                msg = stream.next() => {
                    let text = match msg {
                        None => break received.unwrap_or(CLOSE_ABNORMAL),
                        Some(Err(tungstenite::Error::Capacity(e))) => {
                            warn!("Rahmen vom Server zu groß: {e}");
                            self.close(CLOSE_TOO_LARGE).await;
                            break CLOSE_TOO_LARGE;
                        }
                        Some(Err(e)) => {
                            if received.is_none() {
                                warn!("Verbindung abgebrochen: {}", short_error(&e));
                            }
                            break received.unwrap_or(CLOSE_ABNORMAL);
                        }
                        Some(Ok(Message::Text(text))) => text.as_str().to_string(),
                        Some(Ok(Message::Binary(data))) => match String::from_utf8(data.to_vec()) {
                            Ok(text) => text,
                            Err(_) => "\u{0}".to_string(), // wird als „kein gültiges JSON“ abgewiesen
                        },
                        Some(Ok(Message::Pong(_))) => {
                            awaiting_pong = None;
                            continue;
                        }
                        Some(Ok(Message::Close(frame))) => {
                            received = Some(close_code_of(frame.as_ref()));
                            close_deadline = Some(Instant::now() + self.opts.close_timeout);
                            continue;
                        }
                        Some(Ok(_)) => continue,
                    };
                    match self.on_text(&text, &nonce, &key, &mut heartbeat).await {
                        Ok(()) => {}
                        Err(code) => break code,
                    }
                }
            }
        };
        if let Some(task) = heartbeat {
            task.abort();
        }
        code
    }

    /// Ein Textrahmen des Servers; `Err(code)` beendet die Verbindung.
    async fn on_text(
        &mut self,
        text: &str,
        nonce: &str,
        key: &VerifyingKey,
        heartbeat: &mut Option<tokio::task::JoinHandle<()>>,
    ) -> Result<(), u16> {
        let frame = match open_frame(text, nonce, key) {
            Ok(frame) => frame,
            Err(e) if e.close_code == CLOSE_SIGNATURE => {
                error!("Rahmen des Servers abgewiesen: {}", e.message);
                self.close(CLOSE_SIGNATURE).await;
                return Err(CLOSE_SIGNATURE);
            }
            Err(e) => {
                warn!("Ungültiger Rahmen vom Server: {}", e.message);
                let message: String = e.message.chars().take(MAX_TEXT).collect();
                let _ = self
                    .outbox
                    .send(
                        "error",
                        json!({"code": "invalid_frame", "message": message}),
                    )
                    .await;
                return Ok(());
            }
        };
        match frame {
            Frame::Welcome(welcome) => {
                self.on_welcome(&welcome).await;
                if heartbeat.is_none() {
                    *heartbeat = Some(self.spawn_heartbeat(welcome.heartbeat_seconds));
                }
                self.welcome = Some(welcome);
            }
            frame if self.welcome.is_none() => {
                warn!("Rahmen {} vor welcome ignoriert.", frame.kind());
            }
            Frame::Close(close) => {
                warn!("Server beendet die Verbindung: {}", close.reason);
                self.close_reason = Some(close.reason);
            }
            Frame::Error(e) => {
                warn!("Fehler vom Server ({}): {}", e.code, e.message);
            }
            frame if is_ordered(frame.kind()) => {
                dispatch(self.handler.clone(), self.outbox.clone(), frame).await;
            }
            frame => {
                tokio::spawn(dispatch(self.handler.clone(), self.outbox.clone(), frame));
            }
        }
        Ok(())
    }

    async fn on_welcome(&mut self, welcome: &Welcome) {
        if welcome.catalog != "valid" && welcome.catalog != "dev" {
            warn!(
                "Server bietet keine Werkzeuge an: Dienstkatalog {} (Programm aktualisieren?).",
                welcome.catalog
            );
        }
        self.handler.on_welcome(welcome).await;
        self.opts.backoff.reset();
        info!("Angemeldet als Runner {}.", welcome.runner);
    }

    fn spawn_heartbeat(&self, seconds: u64) -> tokio::task::JoinHandle<()> {
        let handler = self.handler.clone();
        let outbox = self.outbox.clone();
        let interval = Duration::from_secs(seconds);
        tokio::spawn(async move {
            loop {
                tokio::time::sleep(interval).await;
                let _ = AssertUnwindSafe(handler.tick()).catch_unwind().await;
                let sessions = handler.sessions().await;
                if outbox
                    .send("heartbeat", json!({"sessions": sessions}))
                    .await
                    .is_err()
                {
                    return;
                }
            }
        })
    }

    async fn close(&self, code: u16) {
        let frame = CloseFrame {
            code: CloseCode::from(code),
            reason: "".into(),
        };
        let _ = self.outbox.send_raw(Message::Close(Some(frame))).await;
    }
}

/// Warten, bis der Agent gestoppt wird (ohne den Lese-Griff zu halten).
async fn wait_stopped(stop: &mut watch::Receiver<bool>) {
    let _ = stop.wait_for(|s| *s).await.map(|_| ());
}

/// Rahmen des Servers: äußeren Umschlag prüfen, Signatur prüfen, inneren Rahmen liefern.
pub fn open_frame(
    text: &str,
    nonce: &str,
    key: &VerifyingKey,
) -> Result<Frame, protocol::ProtocolError> {
    let outer = protocol::parse(text, Some(Sender::Server))?;
    protocol::open_signed(&outer, nonce, key)
}

/// Einen Rahmen an den Handler geben; Fehler als `error` an den Server melden.
async fn dispatch<H: FrameHandler>(handler: Arc<H>, outbox: Outbox, frame: Frame) {
    let kind = frame.kind();
    let session = frame.session().map(str::to_string);
    let reference = frame.reference().map(str::to_string);
    let result = AssertUnwindSafe(handler.on_frame(frame))
        .catch_unwind()
        .await;
    let (code, message) = match result {
        Ok(Ok(())) | Ok(Err(HandlerError::Disconnected)) => return,
        Ok(Err(HandlerError::Frame { code, message })) => (code, message),
        Err(_) => {
            error!("Fehler bei Rahmen {kind}");
            (
                "internal".to_string(),
                "Interner Fehler im Agent.".to_string(),
            )
        }
    };
    let code = if protocol::ERROR_CODES.contains(&code.as_str()) {
        code
    } else {
        "internal".to_string()
    };
    let message: String = message.chars().take(1000).collect();
    let _ = outbox
        .send(
            "error",
            json!({"code": code, "message": message, "session": session, "ref": reference}),
        )
        .await;
}

async fn write_loop<K>(mut sink: K, mut rx: mpsc::Receiver<Message>)
where
    K: futures_util::Sink<Message, Error = tungstenite::Error> + Unpin,
{
    while let Some(message) = rx.recv().await {
        let closing = matches!(message, Message::Close(_));
        if sink.send(message).await.is_err() || closing {
            break;
        }
    }
    let _ = sink.flush().await;
}

fn close_code_of(frame: Option<&CloseFrame>) -> u16 {
    frame.map_or(CLOSE_NO_STATUS, |f| u16::from(f.code))
}

/// Kurze Fehlerbeschreibung ohne Inhalte (keine Token, keine Rahmen).
fn short_error(e: &tungstenite::Error) -> String {
    match e {
        tungstenite::Error::Io(io) => format!("E/A ({})", io.kind()),
        tungstenite::Error::Tls(_) => "TLS".into(),
        tungstenite::Error::Http(response) => format!("HTTP {}", response.status()),
        tungstenite::Error::Url(_) => "URL".into(),
        tungstenite::Error::Protocol(p) => format!("Protokoll ({p})"),
        other => other.to_string(),
    }
}
