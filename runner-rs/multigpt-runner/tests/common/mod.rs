//! Testhilfen: lokaler WebSocket-Server (wahlweise mit TLS und selbst
//! signiertem Zertifikat), der wie der Runner-Dienst signierte Rahmen schickt,
//! und ein Handler, der empfangene Rahmen aufzeichnet.
#![allow(dead_code)]
// Die Rückruf-Signatur von tungstenite (accept_hdr_async) ist vorgegeben.
#![allow(clippy::result_large_err)]

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use ed25519_dalek::SigningKey;
use futures_util::{SinkExt, StreamExt};
use multigpt_runner::backoff::Backoff;
use multigpt_runner::config::Config;
use multigpt_runner::connection::{Agent, AgentOptions};
use multigpt_runner::handler::{FrameHandler, HandlerError};
use multigpt_runner::outbox::Outbox;
use multigpt_runner::protocol::{
    self, EngineInfo, Frame, SessionInfo, Welcome, signed::sign_server_frame,
};
use serde_json::{Value, json};
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::net::TcpListener;
use tokio::sync::mpsc;
use tokio_tungstenite::WebSocketStream;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::tungstenite::handshake::server::{Request, Response};
use tokio_tungstenite::tungstenite::http::HeaderMap;
use tokio_tungstenite::tungstenite::protocol::CloseFrame;
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;

pub const TOKEN: &str = "mgrt_test_0123456789abcdefghijkl";

pub fn server_key() -> SigningKey {
    SigningKey::from_bytes(&[42; 32])
}

pub fn image() -> String {
    format!("ghcr.io/x/work:1@sha256:{}", "a".repeat(64))
}

pub trait Io: AsyncRead + AsyncWrite + Unpin + Send {}
impl<T: AsyncRead + AsyncWrite + Unpin + Send> Io for T {}

/// Eine angenommene WebSocket-Verbindung auf der Server-Seite.
pub struct Conn {
    pub ws: WebSocketStream<Box<dyn Io>>,
    pub headers: HeaderMap,
    pub nonce: Option<String>,
    pub key: SigningKey,
}

impl Conn {
    /// Nächster Textrahmen als JSON (Ping/Pong übersprungen); `None` bei Ende.
    pub async fn recv(&mut self) -> Option<Value> {
        loop {
            let msg = tokio::time::timeout(Duration::from_secs(10), self.ws.next())
                .await
                .expect("Zeitüberschreitung beim Lesen")?;
            match msg.ok()? {
                Message::Text(text) => return Some(serde_json::from_str(text.as_str()).unwrap()),
                Message::Close(_) => return None,
                _ => continue,
            }
        }
    }

    /// Bis zum Close lesen und dessen Code liefern.
    pub async fn close_code(&mut self) -> Option<u16> {
        loop {
            let msg = tokio::time::timeout(Duration::from_secs(10), self.ws.next())
                .await
                .expect("Zeitüberschreitung beim Lesen")?;
            if let Ok(Message::Close(frame)) = msg {
                return frame.map(|f| u16::from(f.code));
            }
            msg.ok()?;
        }
    }

    /// hello lesen, prüfen und die nonce merken.
    pub async fn hello(&mut self) -> Value {
        let hello = self.recv().await.expect("hello erwartet");
        assert_eq!(hello["type"], "hello");
        protocol::validate(&hello, Some(protocol::Sender::Agent)).expect("gültiges hello");
        self.nonce = Some(hello["nonce"].as_str().unwrap().to_string());
        hello
    }

    pub async fn send_text(&mut self, text: String) {
        self.ws.send(Message::text(text)).await.unwrap();
    }

    /// Rahmen signiert senden (wie der Runner-Dienst).
    pub async fn send_signed(&mut self, kind: &str, fields: Value) {
        let nonce = self.nonce.clone().expect("erst hello lesen");
        let text = sign_server_frame(kind, fields, &nonce, &self.key).unwrap();
        self.send_text(text).await;
    }

    pub async fn welcome(&mut self, heartbeat_seconds: u64) {
        self.send_signed("welcome", welcome_fields(heartbeat_seconds))
            .await;
    }

    pub async fn close(&mut self, code: u16) {
        let frame = CloseFrame {
            code: CloseCode::from(code),
            reason: "".into(),
        };
        let _ = self.ws.close(Some(frame)).await;
        // auf das Ende warten
        while let Ok(Some(Ok(_))) =
            tokio::time::timeout(Duration::from_secs(5), self.ws.next()).await
        {}
    }
}

pub fn welcome_fields(heartbeat_seconds: u64) -> Value {
    json!({
        "runner": 7,
        "protocol": 2,
        "server_version": "0.4.0",
        "heartbeat_seconds": heartbeat_seconds,
        "images": [image()],
        "max_frame_bytes": protocol::MAX_FRAME_BYTES,
        "chunk_bytes": protocol::CHUNK_BYTES,
        "services": ["read", "bash"],
        "catalog": "valid",
    })
}

/// Lokaler Testserver.
pub struct Server {
    pub url: String,
    /// SHA-256 des Zertifikats (nur TLS).
    pub fingerprint: Option<String>,
    /// TCP-Verbindungen (auch solche, deren TLS scheiterte).
    pub tcp_count: Arc<AtomicUsize>,
    conns: mpsc::Receiver<Conn>,
}

impl Server {
    pub async fn plain() -> Server {
        Self::start(false).await
    }

    pub async fn tls() -> Server {
        Self::start(true).await
    }

    async fn start(tls: bool) -> Server {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let (tx, rx) = mpsc::channel(8);
        let tcp_count = Arc::new(AtomicUsize::new(0));
        let mut fingerprint = None;
        let acceptor = if tls {
            let cert = rcgen::generate_simple_self_signed(vec!["localhost".into()]).unwrap();
            let der = cert.cert.der().clone();
            fingerprint = Some(multigpt_runner::tls::fingerprint(der.as_ref()));
            let key =
                rustls::pki_types::PrivateKeyDer::Pkcs8(cert.signing_key.serialize_der().into());
            let config = rustls::ServerConfig::builder_with_provider(Arc::new(
                rustls::crypto::ring::default_provider(),
            ))
            .with_safe_default_protocol_versions()
            .unwrap()
            .with_no_client_auth()
            .with_single_cert(vec![der], key)
            .unwrap();
            Some(tokio_rustls::TlsAcceptor::from(Arc::new(config)))
        } else {
            None
        };
        let count = tcp_count.clone();
        tokio::spawn(async move {
            loop {
                let Ok((tcp, _)) = listener.accept().await else {
                    return;
                };
                count.fetch_add(1, Ordering::SeqCst);
                let tx = tx.clone();
                let acceptor = acceptor.clone();
                tokio::spawn(async move {
                    let io: Box<dyn Io> = match acceptor {
                        Some(acceptor) => match acceptor.accept(tcp).await {
                            Ok(stream) => Box::new(stream),
                            Err(_) => return,
                        },
                        None => Box::new(tcp),
                    };
                    let headers = Arc::new(Mutex::new(HeaderMap::new()));
                    let seen = headers.clone();
                    let callback = move |req: &Request, resp: Response| {
                        *seen.lock().unwrap() = req.headers().clone();
                        Ok(resp)
                    };
                    let Ok(ws) = tokio_tungstenite::accept_hdr_async(io, callback).await else {
                        return;
                    };
                    let headers = headers.lock().unwrap().clone();
                    let _ = tx
                        .send(Conn {
                            ws,
                            headers,
                            nonce: None,
                            key: server_key(),
                        })
                        .await;
                });
            }
        });
        let scheme = if tls { "wss" } else { "ws" };
        Server {
            url: format!("{scheme}://localhost:{port}/runner/ws"),
            fingerprint,
            tcp_count,
            conns: rx,
        }
    }

    /// Nächste Verbindung (höchstens 10 s warten).
    pub async fn accept(&mut self) -> Conn {
        tokio::time::timeout(Duration::from_secs(10), self.conns.recv())
            .await
            .expect("keine Verbindung")
            .expect("Server beendet")
    }

    /// Kommt innerhalb von `wait` eine Verbindung?
    pub async fn try_accept(&mut self, wait: Duration) -> Option<Conn> {
        tokio::time::timeout(wait, self.conns.recv())
            .await
            .ok()
            .flatten()
    }
}

/// Handler, der Rahmen aufzeichnet; `pty_input` meldet `unknown_session`.
#[derive(Default)]
pub struct RecHandler {
    pub frames: Mutex<Vec<Frame>>,
    pub welcomes: Mutex<Vec<Welcome>>,
    pub ticks: AtomicUsize,
    pub disconnects: AtomicUsize,
    pub adopted: AtomicUsize,
    /// Engine nicht erreichbar (engine_info schlägt fehl).
    pub engine_down: std::sync::atomic::AtomicBool,
}

impl FrameHandler for RecHandler {
    async fn engine_info(&self) -> Result<EngineInfo, HandlerError> {
        if self.engine_down.load(Ordering::SeqCst) {
            return Err(HandlerError::new("engine", "Podman-Socket fehlt"));
        }
        Ok(EngineInfo {
            name: "fake".into(),
            version: "1.0".into(),
            rootless: true,
        })
    }

    async fn adopt(&self) {
        self.adopted.fetch_add(1, Ordering::SeqCst);
    }

    async fn sessions(&self) -> Vec<SessionInfo> {
        vec![SessionInfo {
            session: "s1".into(),
            workspace: Some("3".into()),
            state: "running".into(),
            preview_port: None,
        }]
    }

    async fn on_welcome(&self, welcome: &Welcome) {
        self.welcomes.lock().unwrap().push(welcome.clone());
    }

    async fn on_frame(&self, frame: Frame) -> Result<(), HandlerError> {
        let result = match &frame {
            Frame::PtyInput(_) => Err(HandlerError::new("unknown_session", "Sitzung unbekannt")),
            Frame::PreviewRequest(_) => panic!("Testpanik"),
            _ => Ok(()),
        };
        self.frames.lock().unwrap().push(frame);
        result
    }

    async fn tick(&self) {
        self.ticks.fetch_add(1, Ordering::SeqCst);
    }

    async fn disconnected(&self) {
        self.disconnects.fetch_add(1, Ordering::SeqCst);
    }
}

/// Einstellungen für den Testserver mit Token- und Schlüsseldatei in `dir`.
pub fn config(url: &str, dir: &std::path::Path) -> Config {
    let mut config = Config::new(url);
    config.insecure_dev = url.starts_with("ws://");
    config.hostname = "testpc".into();
    config.token_file = dir.join("token");
    config.server_key_file = dir.join("server.pub");
    config.images = [image()].into_iter().collect();
    std::fs::write(&config.token_file, format!("{TOKEN}\n")).unwrap();
    std::fs::write(
        &config.server_key_file,
        protocol::b64encode(server_key().verifying_key().as_bytes()),
    )
    .unwrap();
    config
}

/// Optionen ohne echtes Warten (Wartezeiten werden nur aufgezeichnet).
pub fn fast_options() -> AgentOptions {
    AgentOptions {
        backoff: Backoff::with_rand(Box::new(|| 0.5)),
        sleep: Arc::new(|_| Box::pin(async {})),
        close_timeout: Duration::from_secs(1),
        ..AgentOptions::default()
    }
}

pub fn agent(config: Config, opts: AgentOptions) -> Agent<RecHandler> {
    Agent::new(config, Arc::new(RecHandler::default()), Outbox::new(), opts)
}
