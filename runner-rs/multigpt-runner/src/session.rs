//! Sitzungen: Arbeitscontainer, Netz, Arbeitsstand (git-Bundle), Terminal, Vorschau
//! (Port von `runner_agent/session.py`).
//!
//! Eine Sitzung ist ein wegwerfbarer Arbeitscontainer mit tmpfs `/workspace`.
//! Beim Start lädt der Agent das Bundle des Arbeitsstands vom Server und klont es
//! dort hinein; nach jeder Dateiänderung committet er und lädt ein neues Bundle
//! hoch (Entscheidungen vom 2026-10-13, 8r). Endet die Sitzung, bleibt auf dem
//! Rechner nichts übrig.
//!
//! **Netz (8w, Pflicht 4 aus dem Gutachten):** Jeder Arbeitscontainer hängt an
//! einem eigenen Podman-Netz. Bei „aus“ und „Positivliste“ ist es intern (kein
//! Gateway, keine Route, kein DNS); nur so lässt sich der Vorschau-Port an
//! 127.0.0.1 veröffentlichen, nach außen gibt es keinen Weg. Für die
//! Positivliste startet der Agent zusätzlich ein Relay (`mgpt-relay`) an einem
//! eigenen internen Relay-Netz und an einem Ausgang; es leitet nur zu den
//! freigegebenen Zielen weiter. Der Arbeitscontainer hängt nicht am Relay-Netz:
//! ssh und telnet laufen in einem kurzlebigen Hilfscontainer je Aufruf, der als
//! einziger dort hängt und den Zugang in seinem eigenen `/tmp` bekommt. So sieht
//! das Modell weder Adresse noch Zugang. Fällt das Relay aus, bleibt das Netz zu.
//!
//! **Scratchpad:** `container` = tmpfs `/scratch`; `local` = einziger Bind-Mount
//! `~/MultiGPT/Scratch/<uuid>` mit harter Grenze in drei Stufen (Prüfung in
//! `mgpt-fs` vor `write`/`edit`, `ulimit -f` für bash über [`ToolHost::scratch_rest`],
//! Messung bei jedem Heartbeat).

use std::collections::{BTreeMap, HashMap, HashSet};
use std::fmt;
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::{Duration, Instant};

use async_trait::async_trait;
use serde_json::{Map, Value, json};
use tokio::sync::oneshot;
use tokio::task::JoinHandle;

use crate::config::Config;
use crate::engine::{Engine, EngineError, ExecOptions, Pty};
use crate::mcp::host::{
    ExecOutput, Relay, Target, ToolError, ToolHost, ToolLimits, ToolPty, ToolResult,
};
use crate::outbox::{Outbox, SendError};
use crate::protocol::{
    self, BundleAck, BundleAssembler, BundleChunk, BundleInfo, PreviewRequest, PtyClose, PtyInput,
    PtyOpen, PtyResize, SessionInfo, SessionStart,
};
use crate::template::{self, Bounds, Limits, Scratch, TemplateError};

/// Sekunden für Download bzw. Bestätigung eines Uploads.
pub const BUNDLE_TIMEOUT: Duration = Duration::from_secs(120);
pub const FS_TIMEOUT: Duration = Duration::from_secs(120);
/// Größe der Werkzeug-PTYs (`bash`, `telnet`).
pub const TOOL_COLS: u16 = 120;
pub const TOOL_ROWS: u16 = 40;

/// Fehler mit deutscher Meldung und Fehlercode des Protokolls.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionError {
    pub message: String,
    pub code: String,
}

impl SessionError {
    pub fn new(message: impl Into<String>, code: &str) -> Self {
        Self {
            message: message.into(),
            code: code.to_string(),
        }
    }
}

impl fmt::Display for SessionError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for SessionError {}

impl From<EngineError> for SessionError {
    fn from(e: EngineError) -> Self {
        Self::new(e.0, "engine")
    }
}

impl From<TemplateError> for SessionError {
    fn from(e: TemplateError) -> Self {
        Self::new(e.0, "not_allowed")
    }
}

impl From<SendError> for SessionError {
    fn from(e: SendError) -> Self {
        match e {
            SendError::NotConnected => Self::new("Keine Verbindung zum Server.", "offline"),
            SendError::Protocol(p) => Self::new(p.message, "internal"),
        }
    }
}

impl From<SessionError> for crate::handler::HandlerError {
    fn from(e: SessionError) -> Self {
        crate::handler::HandlerError::new(&e.code, e.message)
    }
}

impl From<SessionError> for ToolError {
    fn from(e: SessionError) -> Self {
        ToolError::new(e.message)
    }
}

pub type SessionResult<T> = Result<T, SessionError>;

/// Datei von MultiGPT laden (Einrichtungswerkzeug); setzt der Verbindungskern.
#[async_trait]
pub trait Downloader: Send + Sync {
    /// `bearer` geht als `Authorization: Bearer …` mit und wird nie protokolliert.
    async fn download(&self, url: &str, bearer: &str, max_bytes: usize) -> Result<Vec<u8>, String>;
}

/// Veränderliche Werte einer Sitzung.
#[derive(Debug, Clone)]
pub struct SessionMut {
    pub state: String,
    pub limits: Limits,
    pub preview_port: Option<u16>,
    pub preview_token: Option<String>,
    pub preview_dir: Option<String>,
    pub relay_ip: Option<String>,
    pub relay_targets: Vec<(String, u16)>,
    pub head: Option<String>,
    pub last_activity: f64,
    pub scratch_used: u64,
}

pub struct Session {
    pub id: String,
    pub workspace: String,
    /// Werte aus `session_start` (ohne Bundle), auch im Label `multigpt.spec`.
    pub spec: Value,
    pub container: String,
    pub image: Option<String>,
    pub scratch: Option<Scratch>,
    /// Ordner des lokalen Scratchpads in Sicht des Agents.
    pub scratch_local: Option<PathBuf>,
    /// Ein Werkzeug zur Zeit (auch Maß für „beschäftigt“ beim Leerlauf).
    pub lock: Arc<tokio::sync::Mutex<()>>,
    mutable: Mutex<SessionMut>,
    ptys: Mutex<HashMap<String, Arc<dyn Pty>>>,
    pty_tasks: Mutex<HashMap<String, JoinHandle<()>>>,
    helpers: Mutex<HashSet<String>>,
}

fn guard<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

impl Session {
    pub fn state(&self) -> MutexGuard<'_, SessionMut> {
        guard(&self.mutable)
    }

    pub fn info(&self) -> SessionInfo {
        let st = self.state();
        SessionInfo {
            session: self.id.clone(),
            workspace: Some(self.workspace.clone()),
            state: st.state.clone(),
            preview_port: st.preview_port,
        }
    }

    pub fn command_seconds(&self) -> u64 {
        self.spec["command_seconds"]
            .as_u64()
            .filter(|v| *v > 0)
            .unwrap_or(120)
    }

    pub fn output_bytes(&self) -> usize {
        self.spec["output_bytes"]
            .as_u64()
            .filter(|v| *v > 0)
            .unwrap_or(64 * 1024) as usize
    }

    pub fn has_pty(&self, id: &str) -> bool {
        guard(&self.ptys).contains_key(id)
    }

    pub fn pty_count(&self) -> usize {
        guard(&self.ptys).len()
    }
}

/// Wert auf ±255 begrenzen (Exit-Code im Rahmen `pty_exit`).
fn exit_code(code: Option<i64>) -> Option<i32> {
    code.map(|c| c.clamp(-255, 255) as i32)
}

fn random_hex(bytes: usize) -> String {
    let data: Vec<u8> = (0..bytes).map(|_| rand::random::<u8>()).collect();
    hex::encode(data)
}

/// Wie Python `secrets.token_urlsafe(n)`.
fn token_urlsafe(bytes: usize) -> String {
    use base64::Engine as _;
    let data: Vec<u8> = (0..bytes).map(|_| rand::random::<u8>()).collect();
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(data)
}

/// Erste `n` Zeichen (wie Python `text[:n]`).
fn chars(text: &str, n: usize) -> String {
    text.chars().take(n).collect()
}

/// Letzte `n` Zeichen (wie Python `text[-n:]`).
fn tail(text: &str, n: usize) -> String {
    let count = text.chars().count();
    text.chars().skip(count.saturating_sub(n)).collect()
}

pub type Clock = Arc<dyn Fn() -> f64 + Send + Sync>;

/// Monotone Uhr in Sekunden.
pub fn monotonic() -> Clock {
    let start = Instant::now();
    Arc::new(move || start.elapsed().as_secs_f64() + 1000.0)
}

type Download = (
    BundleAssembler,
    Option<oneshot::Sender<SessionResult<Vec<u8>>>>,
);

/// Alle Sitzungen dieses Agents.
pub struct SessionManager {
    pub engine: Arc<dyn Engine>,
    pub bounds: Bounds,
    pub scratch_dir: Option<PathBuf>,
    pub scratch_host_dir: Option<String>,
    /// Zusätzliche Labels an allen Objekten (Tests: `multigpt.test=1`).
    pub labels: BTreeMap<String, String>,
    pub server_url: String,
    pub outbox: Outbox,
    pub clock: Clock,
    pub bundle_timeout: Duration,
    /// Image-Liste aus `welcome` (`None` vor dem ersten `welcome`).
    server_images: Mutex<Option<Vec<String>>>,
    chunk_bytes: AtomicUsize,
    services: Mutex<Option<Vec<String>>>,
    downloader: Mutex<Option<Arc<dyn Downloader>>>,
    sessions: Mutex<BTreeMap<String, Arc<Session>>>,
    downloads: Mutex<HashMap<String, Download>>,
    uploads: Mutex<HashMap<String, oneshot::Sender<BundleAck>>>,
    starting: Mutex<HashSet<String>>,
}

impl SessionManager {
    pub fn new(engine: Arc<dyn Engine>, config: &Config, outbox: Outbox) -> Self {
        Self {
            engine,
            bounds: Bounds::from(config),
            scratch_dir: config.scratch_dir.clone(),
            scratch_host_dir: config
                .scratch_host_dir
                .as_ref()
                .map(|p| p.to_string_lossy().into_owned()),
            labels: config.labels.clone(),
            server_url: config.url.clone(),
            outbox,
            clock: monotonic(),
            bundle_timeout: BUNDLE_TIMEOUT,
            server_images: Mutex::new(None),
            chunk_bytes: AtomicUsize::new(protocol::CHUNK_BYTES),
            services: Mutex::new(None),
            downloader: Mutex::new(None),
            sessions: Mutex::new(BTreeMap::new()),
            downloads: Mutex::new(HashMap::new()),
            uploads: Mutex::new(HashMap::new()),
            starting: Mutex::new(HashSet::new()),
        }
    }

    // --- Einstellungen aus welcome ---------------------------------------------------

    pub fn set_server_images(&self, images: Option<Vec<String>>) {
        *guard(&self.server_images) = images;
    }

    pub fn set_chunk_bytes(&self, bytes: usize) {
        self.chunk_bytes
            .store(bytes.clamp(1024, protocol::CHUNK_BYTES), Ordering::SeqCst);
    }

    pub fn chunk_bytes(&self) -> usize {
        self.chunk_bytes.load(Ordering::SeqCst)
    }

    pub fn set_services(&self, services: Option<Vec<String>>) {
        *guard(&self.services) = services;
    }

    pub fn services(&self) -> Option<Vec<String>> {
        guard(&self.services).clone()
    }

    pub fn set_downloader(&self, downloader: Arc<dyn Downloader>) {
        *guard(&self.downloader) = Some(downloader);
    }

    /// Werte aus `welcome` übernehmen.
    pub fn on_welcome(&self, welcome: &protocol::Welcome) {
        self.set_server_images(Some(welcome.images.clone()));
        self.set_chunk_bytes(welcome.chunk_bytes as usize);
    }

    async fn send(&self, kind: &str, fields: Value) -> SessionResult<()> {
        self.outbox.send(kind, fields).await.map_err(Into::into)
    }

    // --- Abgleich ----------------------------------------------------------------------

    /// Laufende Arbeitscontainer (z. B. nach Neustart des Agents) übernehmen.
    ///
    /// Container ohne lesbares Soll oder ohne laufenden Zustand werden entfernt;
    /// der Server gleicht beim `hello` ab und stoppt, was er nicht kennt.
    pub async fn adopt(&self) {
        let containers = match self.engine.list_containers().await {
            Ok(list) => list,
            Err(e) => {
                tracing::warn!("Abgleich nicht möglich: {e}");
                return;
            }
        };
        for item in containers {
            let labels = &item.labels;
            if labels.get(template::LABEL_ROLE).map(String::as_str) != Some("work") {
                continue;
            }
            let sid = labels
                .get(template::LABEL_SESSION)
                .cloned()
                .unwrap_or_default();
            if guard(&self.sessions).contains_key(&sid) {
                continue;
            }
            let parsed = (|| {
                let spec: Value = serde_json::from_str(labels.get(template::LABEL_SPEC)?).ok()?;
                let limits = Limits::from_value(&spec["limits"])?;
                if !item.running || item.name != template::work_name(&sid) {
                    return None;
                }
                let scratch = match spec.get("scratch") {
                    None | Some(Value::Null) => (
                        Scratch {
                            mode: "container".into(),
                            size_mb: 1024,
                            source: None,
                        },
                        None,
                    ),
                    Some(Value::Array(parts)) if parts.len() >= 3 => (
                        Scratch {
                            mode: parts[0].as_str()?.to_string(),
                            size_mb: parts[1].as_u64()?,
                            source: parts[2].as_str().map(str::to_string),
                        },
                        parts.get(3).and_then(Value::as_str).map(PathBuf::from),
                    ),
                    _ => return None,
                };
                Some((spec, limits, scratch))
            })();
            let Some((spec, limits, (scratch, scratch_local))) = parsed else {
                self.cleanup(&sid).await;
                continue;
            };
            let port = labels
                .get(template::LABEL_PREVIEW)
                .filter(|p| !p.is_empty() && p.bytes().all(|b| b.is_ascii_digit()))
                .and_then(|p| p.parse().ok());
            let mut relay_targets = Vec::new();
            let mut relay_ip = None;
            if limits.network == "allowlist" {
                relay_targets = spec["allow"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .filter_map(|item| {
                        Some((
                            item.get(0)?.as_str()?.to_string(),
                            u16::try_from(item.get(1)?.as_u64()?).ok()?,
                        ))
                    })
                    .collect();
                if let Ok(info) = self
                    .engine
                    .inspect_container(&template::container_name(&sid, "relay", None))
                    .await
                {
                    relay_ip = info["NetworkSettings"]["Networks"]
                        [&template::network_name(&sid, "rel")]["IPAddress"]
                        .as_str()
                        .filter(|s| !s.is_empty())
                        .map(str::to_string);
                }
            }
            let session = Session {
                id: sid.clone(),
                workspace: labels
                    .get(template::LABEL_WORKSPACE)
                    .cloned()
                    .unwrap_or_default(),
                image: spec["image"].as_str().map(str::to_string),
                spec,
                container: item.name.clone(),
                scratch: Some(scratch),
                scratch_local,
                lock: Arc::default(),
                mutable: Mutex::new(SessionMut {
                    state: "running".into(),
                    limits,
                    preview_port: port,
                    preview_token: None,
                    preview_dir: None,
                    relay_ip,
                    relay_targets,
                    head: None,
                    last_activity: (self.clock)(),
                    scratch_used: 0,
                }),
                ptys: Mutex::default(),
                pty_tasks: Mutex::default(),
                helpers: Mutex::default(),
            };
            guard(&self.sessions).insert(sid, Arc::new(session));
        }
    }

    pub fn infos(&self) -> Vec<SessionInfo> {
        guard(&self.sessions).values().map(|s| s.info()).collect()
    }

    /// Alle Sitzungen (auch startende).
    pub fn all(&self) -> Vec<Arc<Session>> {
        guard(&self.sessions).values().cloned().collect()
    }

    pub fn get(&self, sid: &str) -> SessionResult<Arc<Session>> {
        match guard(&self.sessions).get(sid) {
            Some(s) if s.state().state == "running" => Ok(s.clone()),
            _ => Err(SessionError::new(
                "Sitzung unbekannt oder nicht gestartet.",
                "unknown_session",
            )),
        }
    }

    pub fn touch(&self, session: &Session) {
        session.state().last_activity = (self.clock)();
    }

    /// Griff für die Werkzeuge (rs-mcp) auf eine laufende Sitzung.
    pub fn handle(self: &Arc<Self>, sid: &str) -> SessionResult<SessionHandle> {
        Ok(SessionHandle {
            manager: self.clone(),
            session: self.get(sid)?,
        })
    }

    // --- Start und Stopp -----------------------------------------------------------------

    pub async fn start(&self, frame: &SessionStart) {
        let sid = frame.session.clone();
        let existing = guard(&self.sessions).get(&sid).cloned();
        if existing.is_some() || !guard(&self.starting).insert(sid.clone()) {
            if let Some(session) = existing {
                let _ = self.report(&session).await;
            }
            return;
        }
        let result = self.start_inner(frame).await;
        guard(&self.starting).remove(&sid);
        match result {
            Ok(session) => {
                let _ = self.report(&session).await;
            }
            Err(e) => {
                tracing::warn!("Sitzung {sid} nicht gestartet: {}", e.message);
                self.cleanup(&sid).await;
                guard(&self.sessions).remove(&sid);
                let _ = self
                    .send(
                        "session_state",
                        json!({"session": sid, "state": "failed", "error": chars(&e.message, 1000)}),
                    )
                    .await;
            }
        }
    }

    async fn report(&self, session: &Session) -> SessionResult<()> {
        let (state, limits, port) = {
            let st = session.state();
            (st.state.clone(), st.limits.as_value(), st.preview_port)
        };
        let limits = if state == "running" {
            limits
        } else {
            Value::Null
        };
        self.send(
            "session_state",
            json!({"session": session.id, "state": state, "limits": limits, "preview_port": port}),
        )
        .await
    }

    async fn start_inner(&self, frame: &SessionStart) -> SessionResult<Arc<Session>> {
        let sid = frame.session.clone();
        let limits = Limits {
            cpus: frame.cpus,
            memory_mb: frame.memory_mb,
            pids: frame.pids,
            tmpfs_mb: frame.tmpfs_mb,
            network: frame.network.clone(),
        };
        let server_images = guard(&self.server_images).clone();
        template::check_image(
            Some(&frame.image),
            server_images.as_deref(),
            &self.bounds.images,
        )?;
        template::check_limits(&limits, &self.bounds)?;
        if guard(&self.sessions)
            .values()
            .any(|s| s.workspace == frame.workspace)
        {
            return Err(SessionError::new(
                "Arbeitsstand ist schon in einer Sitzung geöffnet.",
                "busy",
            ));
        }
        let allow: Vec<(String, u16)> = frame
            .allow
            .iter()
            .flatten()
            .map(|a| (a.host.clone(), a.port))
            .collect();
        if limits.network == "allowlist" && allow.is_empty() {
            return Err(SessionError::new(
                "Positivliste ohne Einträge.",
                "not_allowed",
            ));
        }
        let (scratch, scratch_local) = self.scratch(frame)?;
        let spec = json!({
            "image": frame.image,
            "scratch": [scratch.mode, scratch.size_mb, scratch.source,
                        scratch_local.as_ref().map(|p| p.to_string_lossy().into_owned())],
            "limits": limits.as_value(),
            "command_seconds": frame.command_seconds,
            "output_bytes": frame.output_bytes,
            "idle_minutes": frame.idle_minutes,
            "allow": allow.iter().map(|(h, p)| json!([h, p])).collect::<Vec<_>>(),
        });
        let session = Arc::new(Session {
            id: sid.clone(),
            workspace: frame.workspace.clone(),
            spec: spec.clone(),
            container: template::work_name(&sid),
            image: Some(frame.image.clone()),
            scratch: Some(scratch.clone()),
            scratch_local,
            lock: Arc::default(),
            mutable: Mutex::new(SessionMut {
                state: "starting".into(),
                limits: limits.clone(),
                preview_port: None,
                preview_token: None,
                preview_dir: None,
                relay_ip: None,
                relay_targets: allow.clone(),
                head: None,
                last_activity: (self.clock)(),
                scratch_used: 0,
            }),
            ptys: Mutex::default(),
            pty_tasks: Mutex::default(),
            helpers: Mutex::default(),
        });
        guard(&self.sessions).insert(sid.clone(), session.clone());
        self.create_networks(&sid, &limits.network).await?;
        if limits.network == "allowlist" {
            let ip = self.start_relay(&sid, &frame.image, &allow).await?;
            session.state().relay_ip = Some(ip);
        }
        self.start_container(&session, &frame.image, &spec).await?;
        let info = self.engine.inspect_container(&session.container).await?;
        let port = session.state().preview_port.map(u64::from);
        let source = scratch.source.as_deref().filter(|_| scratch.is_local());
        let problems = template::check_inspect(&info, &limits, port, source);
        if !problems.is_empty() {
            return Err(SessionError::new(
                format!("Abweichende Grenzen: {}", problems.join("; ")),
                "not_allowed",
            ));
        }
        session.state().limits = template::actual_limits(&info, &limits.network);
        self.load_workspace(&session, frame.bundle.as_ref()).await?;
        session.state().state = "running".into();
        Ok(session)
    }

    /// Zusätzliche Labels aus der Konfiguration; eigene Labels gehen vor.
    fn with_labels(&self, body: &mut Value) {
        let own = body["labels"].as_object().cloned().unwrap_or_default();
        let mut labels: Map<String, Value> = self
            .labels
            .iter()
            .map(|(k, v)| (k.clone(), json!(v)))
            .collect();
        labels.extend(own);
        body["labels"] = Value::Object(labels);
    }

    fn net_labels(&self, sid: &str, mode: &str) -> BTreeMap<String, String> {
        let mut labels = self.labels.clone();
        labels.insert(template::LABEL_SESSION.into(), sid.into());
        if mode == "default" {
            labels.insert(template::LABEL_NETWORK.into(), "default".into());
        }
        labels
    }

    async fn create_networks(&self, sid: &str, mode: &str) -> SessionResult<()> {
        let labels = self.net_labels(sid, mode);
        self.engine
            .create_network(&template::network_body(
                &template::network_name(sid, "net"),
                mode != "default",
                &labels,
            ))
            .await?;
        if mode == "allowlist" {
            self.engine
                .create_network(&template::network_body(
                    &template::network_name(sid, "rel"),
                    true,
                    &labels,
                ))
                .await?;
            self.engine
                .create_network(&template::network_body(
                    &template::network_name(sid, "out"),
                    false,
                    &labels,
                ))
                .await?;
        }
        Ok(())
    }

    async fn start_relay(
        &self,
        sid: &str,
        image: &str,
        allow: &[(String, u16)],
    ) -> SessionResult<String> {
        let targets: Vec<(String, u64)> = allow
            .iter()
            .map(|(h, p)| (h.clone(), u64::from(*p)))
            .collect();
        let mut body = template::relay_body(sid, image, &targets);
        self.with_labels(&mut body);
        let name = body["name"].as_str().unwrap_or_default().to_string();
        self.engine.create_container(&body).await?;
        self.engine.start_container(&name).await?;
        let info = self.engine.inspect_container(&name).await?;
        Ok(
            info["NetworkSettings"]["Networks"][&template::network_name(sid, "rel")]["IPAddress"]
                .as_str()
                .filter(|s| !s.is_empty())
                .unwrap_or("relay")
                .to_string(),
        )
    }

    /// Scratchpad der Sitzung: tmpfs oder `~/MultiGPT/Scratch/<uuid>/` des Rechners.
    ///
    /// Der Server nennt nur die UUID; den Ordner legt der Agent selbst an und prüft
    /// ihn (kein Symlink, direkt unter dem Scratch-Ordner).
    fn scratch(&self, frame: &SessionStart) -> SessionResult<(Scratch, Option<PathBuf>)> {
        let mode = frame
            .scratch_mode
            .as_deref()
            .filter(|m| !m.is_empty())
            .unwrap_or("container");
        let size_mb = frame.scratch_mb.filter(|v| *v > 0).unwrap_or(1024);
        if mode != "local" {
            return Ok((
                Scratch {
                    mode: "container".into(),
                    size_mb: size_mb.min(self.bounds.max_tmpfs_mb),
                    source: None,
                },
                None,
            ));
        }
        let (Some(root), Some(host)) = (&self.scratch_dir, &self.scratch_host_dir) else {
            return Err(SessionError::new(
                "Lokales Scratchpad ist auf diesem Rechner nicht eingerichtet.",
                "not_allowed",
            ));
        };
        let uuid = frame
            .workspace_uuid
            .as_deref()
            .filter(|u| template::UUID_RE.is_match(u))
            .ok_or_else(|| {
                SessionError::new("Lokales Scratchpad ohne Arbeitsstand-UUID.", "not_allowed")
            })?;
        let os = |e: std::io::Error| {
            SessionError::new(format!("Scratch-Ordner nicht nutzbar ({e})."), "engine")
        };
        let meta = std::fs::symlink_metadata(root).map_err(os)?;
        if !meta.is_dir() {
            return Err(SessionError::new(
                "Scratch-Ordner ist kein Verzeichnis.",
                "not_allowed",
            ));
        }
        let target = root.join(uuid);
        {
            use std::os::unix::fs::DirBuilderExt;
            match std::fs::DirBuilder::new().mode(0o700).create(&target) {
                Ok(()) => {}
                Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {}
                Err(e) => return Err(os(e)),
            }
        }
        if !std::fs::symlink_metadata(&target).map_err(os)?.is_dir() {
            return Err(SessionError::new(
                "Scratch-Ordner ist kein Verzeichnis (Symlink?).",
                "not_allowed",
            ));
        }
        Ok((
            Scratch {
                mode: "local".into(),
                size_mb,
                source: Some(format!("{host}/{uuid}")),
            },
            Some(target),
        ))
    }

    /// Belegung des lokalen Scratchpads in Byte (wie `du`); `None` ohne lokales.
    pub fn scratch_usage(&self, session: &Session) -> Option<u64> {
        let root = session.scratch_local.as_ref()?;
        session.scratch.as_ref()?;
        let total = du(root);
        session.state().scratch_used = total;
        Some(total)
    }

    pub fn scratch_limit(&self, session: &Session) -> Option<u64> {
        session.scratch_local.as_ref()?;
        Some(session.scratch.as_ref()?.size_mb << 20)
    }

    /// Lokales Scratchpad über der Grenze? (Ein Bind-Mount hat keine Quota.)
    pub fn scratch_full(&self, session: &Session) -> bool {
        match (self.scratch_usage(session), self.scratch_limit(session)) {
            (Some(used), Some(limit)) => used >= limit,
            _ => false,
        }
    }

    /// Hinweis, wenn das lokale Scratchpad über seiner Grenze liegt.
    pub fn scratch_note(&self, session: &Session) -> String {
        if !self.scratch_full(session) {
            return String::new();
        }
        let used = session.state().scratch_used;
        let size = session.scratch.as_ref().map(|s| s.size_mb).unwrap_or(0);
        format!(
            "\n[Scratchpad belegt {} MB, Grenze {size} MB: Schreiben ist gesperrt, bis Dateien in /scratch gelöscht sind (bash: rm).]",
            used >> 20
        )
    }

    fn free_ports(&self) -> Vec<u16> {
        let used: HashSet<u16> = guard(&self.sessions)
            .values()
            .filter_map(|s| s.state().preview_port)
            .collect();
        protocol::PREVIEW_PORTS
            .filter(|p| !used.contains(p))
            .collect()
    }

    async fn start_container(
        &self,
        session: &Session,
        image: &str,
        spec: &Value,
    ) -> SessionResult<()> {
        let net = template::network_name(&session.id, "net");
        let limits = session.state().limits.clone();
        let mut last_error: Option<EngineError> = None;
        for port in self.free_ports() {
            let mut body = template::work_body(
                &session.id,
                &session.workspace,
                image,
                &limits,
                &net,
                Some(u64::from(port)),
                spec,
                session.scratch.as_ref(),
            );
            self.with_labels(&mut body);
            self.engine.create_container(&body).await?;
            if let Err(e) = self.engine.start_container(&session.container).await {
                let _ = self.engine.remove_container(&session.container).await;
                if e.0.contains("address already in use") || e.0.contains("bind") {
                    last_error = Some(e);
                    continue;
                }
                return Err(e.into());
            }
            session.state().preview_port = Some(port);
            return Ok(());
        }
        let reason = last_error
            .map(|e| e.0)
            .unwrap_or_else(|| "alle belegt".into());
        Err(SessionError::new(
            format!("Kein freier Vorschau-Port in 18080–18089 ({reason})."),
            "limit",
        ))
    }

    pub async fn stop(&self, sid: &str, _reason: &str) {
        let session = guard(&self.sessions).remove(sid);
        if let Some(session) = session {
            session.state().state = "stopping".into();
            self.close_session_io(&session).await;
        }
        self.cleanup(sid).await;
        let _ = self
            .send("session_state", json!({"session": sid, "state": "stopped"}))
            .await;
    }

    /// Terminals der Sitzung schließen (MCP-Pipes und Telnet schließt `mcp`).
    pub async fn close_session_io(&self, session: &Session) {
        let tasks: Vec<JoinHandle<()>> =
            guard(&session.pty_tasks).drain().map(|(_, t)| t).collect();
        for task in tasks {
            task.abort();
        }
        let ptys: Vec<Arc<dyn Pty>> = guard(&session.ptys).drain().map(|(_, p)| p).collect();
        for pty in ptys {
            pty.close().await;
        }
    }

    /// Verbindung weg: Terminals aller Sitzungen schließen; Container bleiben.
    pub async fn close_all_io(&self) {
        for session in self.all() {
            self.close_session_io(&session).await;
        }
    }

    /// Hilfscontainer für einen ssh/telnet-Aufruf starten (nur am Relay-Netz).
    pub async fn open_helper(&self, session: &Session) -> SessionResult<String> {
        let image = session
            .image
            .as_deref()
            .ok_or_else(|| SessionError::new("Image der Sitzung unbekannt.", "internal"))?;
        let call = random_hex(8);
        let mut body = template::tool_body(&session.id, &call, image);
        self.with_labels(&mut body);
        let name = body["name"].as_str().unwrap_or_default().to_string();
        self.engine.create_container(&body).await?;
        guard(&session.helpers).insert(name.clone());
        if let Err(e) = self.engine.start_container(&name).await {
            self.close_helper(session, &name).await;
            return Err(e.into());
        }
        Ok(name)
    }

    pub async fn close_helper(&self, session: &Session, name: &str) {
        guard(&session.helpers).remove(name);
        let _ = self.engine.remove_container(name).await;
    }

    /// Nur eigene Objekte der Sitzung entfernen (Label + Namensmuster prüft die Engine).
    async fn cleanup(&self, sid: &str) {
        let mut names = vec![
            template::work_name(sid),
            template::container_name(sid, "relay", None),
        ];
        if let Ok(list) = self.engine.list_containers().await {
            for item in list {
                if item.labels.get(template::LABEL_ROLE).map(String::as_str) == Some("tool")
                    && item.labels.get(template::LABEL_SESSION).map(String::as_str) == Some(sid)
                {
                    names.push(item.name);
                }
            }
        }
        for name in names {
            let _ = self.engine.remove_container(&name).await;
        }
        for kind in ["net", "rel", "out"] {
            let _ = self
                .engine
                .remove_network(&template::network_name(sid, kind))
                .await;
        }
    }

    pub async fn stop_all(&self) {
        let ids: Vec<String> = guard(&self.sessions).keys().cloned().collect();
        for sid in ids {
            self.stop(&sid, "replaced").await;
        }
    }

    /// Sitzungen ohne Aktivität seit `idle_minutes` stoppen (8k, Absicherung); dabei
    /// die Belegung des lokalen Scratchpads nachmessen.
    pub async fn check_idle(&self) {
        let now = (self.clock)();
        for session in self.all() {
            if session.scratch_local.is_some() {
                let root = session.scratch_local.clone().unwrap();
                if let Ok(total) = tokio::task::spawn_blocking(move || du(&root)).await {
                    session.state().scratch_used = total;
                }
            }
            let idle = session.spec["idle_minutes"]
                .as_u64()
                .filter(|v| *v > 0)
                .unwrap_or(15) as f64
                * 60.0;
            let busy = session.lock.try_lock().is_err() || session.pty_count() > 0;
            let (running, last) = {
                let st = session.state();
                (st.state == "running", st.last_activity)
            };
            if running && !busy && now - last > idle {
                self.stop(&session.id, "idle").await;
            }
        }
    }

    // --- mgpt-fs ---------------------------------------------------------------------------

    /// `mgpt-fs` im Container aufrufen; liefert die Antwort (ohne Rohdaten).
    pub async fn fs(
        &self,
        session: &Session,
        op: &str,
        payload: &[u8],
        mut fields: Map<String, Value>,
    ) -> SessionResult<Value> {
        let limit = self.scratch_limit(session);
        if let (true, Some(limit)) = (matches!(op, "write" | "edit"), limit) {
            // mgpt-fs prüft vor dem Schreiben nach /scratch.
            fields.insert("scratch_limit".into(), json!(limit));
        }
        Ok(self.fs_raw(session, op, payload, fields).await?.0)
    }

    /// `mgpt-fs` mit Rohdaten nach der Antwortzeile (`bundle_out`, `file_out`).
    pub async fn fs_raw(
        &self,
        session: &Session,
        op: &str,
        payload: &[u8],
        fields: Map<String, Value>,
    ) -> SessionResult<(Value, Vec<u8>)> {
        let mut request = Map::new();
        request.insert("op".into(), json!(op));
        request.extend(fields);
        let mut stdin = serde_json::to_vec(&Value::Object(request)).unwrap_or_default();
        stdin.push(b'\n');
        stdin.extend_from_slice(payload);
        let max_output = if op == "bundle_out" {
            protocol::MAX_BUNDLE_BYTES as usize
        } else {
            32 * 1024 * 1024
        };
        let res = self
            .engine
            .exec(
                &session.container,
                &["mgpt-fs".to_string()],
                ExecOptions {
                    stdin: Some(stdin),
                    timeout: FS_TIMEOUT,
                    max_output,
                    workdir: "/workspace".into(),
                },
            )
            .await?;
        if res.timed_out {
            return Err(SessionError::new(
                "Zeitlimit im Container überschritten.",
                "timeout",
            ));
        }
        let split = res
            .stdout
            .iter()
            .position(|b| *b == b'\n')
            .unwrap_or(res.stdout.len());
        let (line, rest) = res.stdout.split_at(split);
        let rest = rest.get(1..).unwrap_or_default().to_vec();
        let Ok(result) = serde_json::from_slice::<Value>(line) else {
            let err = tail(String::from_utf8_lossy(&res.stderr).trim(), 300);
            let why = if err.is_empty() {
                res.code
                    .map(|c| c.to_string())
                    .unwrap_or_else(|| "None".into())
            } else {
                err
            };
            return Err(SessionError::new(
                format!("mgpt-fs antwortet nicht ({why})."),
                "engine",
            ));
        };
        if result["ok"] != json!(true) {
            let message = match &result["error"] {
                Value::String(s) if !s.is_empty() => s.clone(),
                Value::Null => "Fehler in mgpt-fs.".into(),
                other => other.to_string(),
            };
            return Err(SessionError::new(message, "not_allowed"));
        }
        Ok((result, rest))
    }

    // --- Arbeitsstand (git-Bundle) -----------------------------------------------------

    async fn load_workspace(
        &self,
        session: &Session,
        bundle: Option<&BundleInfo>,
    ) -> SessionResult<()> {
        let Some(bundle) = bundle else {
            self.fs(session, "init", b"", Map::new()).await?;
            session.state().head = None;
            return Ok(());
        };
        let data = self.download(session, bundle).await?;
        let mut fields = Map::new();
        fields.insert("size".into(), json!(data.len()));
        let result = self.fs(session, "bundle_in", &data, fields).await?;
        session.state().head = result["head"].as_str().map(str::to_string);
        Ok(())
    }

    async fn download(&self, session: &Session, bundle: &BundleInfo) -> SessionResult<Vec<u8>> {
        let transfer = format!("dl-{}", random_hex(8));
        let (tx, rx) = oneshot::channel();
        let max = bundle.size.min(protocol::MAX_BUNDLE_BYTES);
        guard(&self.downloads).insert(
            transfer.clone(),
            (BundleAssembler::new(transfer.clone(), max), Some(tx)),
        );
        let result = async {
            self.send(
                "bundle_get",
                json!({"session": session.id, "transfer": transfer}),
            )
            .await?;
            match tokio::time::timeout(self.bundle_timeout, rx).await {
                Ok(Ok(result)) => result,
                _ => Err(SessionError::new(
                    "Arbeitsstand kam nicht rechtzeitig an.",
                    "timeout",
                )),
            }
        }
        .await;
        guard(&self.downloads).remove(&transfer);
        let data = result?;
        if data.len() as u64 != bundle.size {
            return Err(SessionError::new(
                "Arbeitsstand hat die falsche Größe.",
                "bundle",
            ));
        }
        Ok(data)
    }

    pub async fn on_bundle_chunk(&self, frame: &BundleChunk) -> SessionResult<()> {
        enum Outcome {
            Unknown,
            Failed(String, Option<oneshot::Sender<SessionResult<Vec<u8>>>>),
            Done(Vec<u8>, Option<oneshot::Sender<SessionResult<Vec<u8>>>>),
            Pending,
        }
        let outcome = {
            let mut downloads = guard(&self.downloads);
            match downloads.get_mut(&frame.transfer) {
                None => Outcome::Unknown,
                Some((assembler, tx)) => match assembler.add(frame) {
                    Err(e) => Outcome::Failed(e.message, tx.take()),
                    Ok(Some(data)) => Outcome::Done(data, tx.take()),
                    Ok(None) => Outcome::Pending,
                },
            }
        };
        match outcome {
            Outcome::Unknown => {
                self.send(
                    "error",
                    json!({"code": "bundle", "message": "Unbekannte Übertragung.",
                           "session": frame.session, "ref": frame.transfer}),
                )
                .await
            }
            Outcome::Failed(message, tx) => {
                let sent = self
                    .send(
                        "bundle_ack",
                        json!({"session": frame.session, "transfer": frame.transfer,
                               "ok": false, "error": message}),
                    )
                    .await;
                if let Some(tx) = tx {
                    let _ = tx.send(Err(SessionError::new(message, "bundle")));
                }
                sent
            }
            Outcome::Done(data, tx) => {
                let sent = self
                    .send(
                        "bundle_ack",
                        json!({"session": frame.session, "transfer": frame.transfer, "ok": true}),
                    )
                    .await;
                if let Some(tx) = tx {
                    let _ = tx.send(Ok(data));
                }
                sent
            }
            Outcome::Pending => Ok(()),
        }
    }

    pub fn on_bundle_ack(&self, frame: &BundleAck) {
        if let Some(tx) = guard(&self.uploads).remove(&frame.transfer) {
            let _ = tx.send(frame.clone());
        }
    }

    /// Nach einer Änderung: commit und neues Bundle hochladen (8r).
    ///
    /// Liefert einen kurzen Hinweis für das Werkzeugergebnis ("" ohne Änderung).
    pub async fn save(&self, session: &Session, message: &str) -> SessionResult<String> {
        let mut fields = Map::new();
        fields.insert("message".into(), json!(message));
        let result = self.fs(session, "commit", b"", fields).await?;
        if result["changed"] != json!(true) {
            return Ok(String::new());
        }
        let (meta, data) = self.fs_raw(session, "bundle_out", b"", Map::new()).await?;
        if data.is_empty() {
            return Ok(String::new());
        }
        let head = meta["head"].as_str().map(str::to_string);
        let transfer = format!("up-{}", random_hex(8));
        let (tx, rx) = oneshot::channel();
        guard(&self.uploads).insert(transfer.clone(), tx);
        let outcome = async {
            for (seq, part, last) in protocol::split_bundle(&data, self.chunk_bytes()) {
                let mut fields = json!({
                    "session": session.id,
                    "transfer": transfer,
                    "seq": seq,
                    "data": protocol::b64encode(part),
                    "last": last,
                });
                if last {
                    fields["size"] = json!(data.len());
                    fields["sha256"] = meta["sha256"].clone();
                    fields["head"] = json!(head);
                    fields["message"] = json!(chars(message, protocol::MAX_TEXT));
                }
                self.send("bundle_chunk", fields).await?;
            }
            Ok::<_, SessionError>(tokio::time::timeout(self.bundle_timeout, rx).await)
        }
        .await;
        guard(&self.uploads).remove(&transfer);
        let ack = match outcome? {
            Ok(Ok(ack)) => ack,
            _ => return Ok("\n[Arbeitsstand: Server hat das Speichern nicht bestätigt.]".into()),
        };
        if !ack.ok {
            let why = ack
                .error
                .filter(|e| !e.is_empty())
                .unwrap_or_else(|| "abgelehnt".into());
            return Ok(format!("\n[Arbeitsstand nicht gespeichert: {why}]"));
        }
        session.state().head = ack.head.or(head);
        Ok(String::new())
    }

    // --- Terminal ----------------------------------------------------------------------

    /// PTY-Ausgabe als `pty_data` streamen. Liefert den Exit-Code.
    pub async fn pump(&self, session: &Session, pty_id: &str, handle: &dyn Pty) -> Option<i64> {
        let mut seq: u64 = 0;
        loop {
            let data = handle.read().await;
            if data.is_empty() {
                break;
            }
            self.touch(session);
            for part in data.chunks(protocol::PTY_BYTES) {
                let sent = self
                    .send(
                        "pty_data",
                        json!({"session": session.id, "pty": pty_id, "seq": seq,
                               "data": protocol::b64encode(part)}),
                    )
                    .await;
                if sent.is_err() {
                    break;
                }
                seq += 1;
            }
        }
        let code = handle.wait().await;
        guard(&session.ptys).remove(pty_id);
        let _ = self
            .send(
                "pty_exit",
                json!({"session": session.id, "pty": pty_id, "code": exit_code(code)}),
            )
            .await;
        code
    }

    /// Browser-Terminal (`bash -i`) im Arbeitscontainer.
    pub async fn pty_open(self: &Arc<Self>, frame: &PtyOpen) -> SessionResult<()> {
        let session = self.get(&frame.session)?;
        if session.has_pty(&frame.pty) {
            return Err(SessionError::new("Terminal existiert schon.", "busy"));
        }
        let argv: Vec<String> = ["bash", "--noprofile", "--norc", "-i"]
            .iter()
            .map(|s| s.to_string())
            .collect();
        let handle: Arc<dyn Pty> = Arc::from(
            self.engine
                .exec_pty(
                    &session.container,
                    &argv,
                    frame.cols,
                    frame.rows,
                    "/workspace",
                )
                .await?,
        );
        guard(&session.ptys).insert(frame.pty.clone(), handle.clone());
        self.touch(&session);
        let manager = self.clone();
        let pty_id = frame.pty.clone();
        let task_session = session.clone();
        let task = tokio::spawn(async move {
            manager.pump(&task_session, &pty_id, handle.as_ref()).await;
            guard(&task_session.pty_tasks).remove(&pty_id);
            handle.close().await;
        });
        guard(&session.pty_tasks).insert(frame.pty.clone(), task);
        Ok(())
    }

    fn pty(&self, session: &Session, id: &str) -> Option<Arc<dyn Pty>> {
        guard(&session.ptys).get(id).cloned()
    }

    pub async fn pty_input(&self, frame: &PtyInput) -> SessionResult<()> {
        let session = self.get(&frame.session)?;
        let handle = self
            .pty(&session, &frame.pty)
            .ok_or_else(|| SessionError::new("Terminal unbekannt.", "unknown_session"))?;
        self.touch(&session);
        let data = protocol::b64decode(&frame.data)
            .map_err(|e| SessionError::new(e.message, "invalid_frame"))?;
        handle.write(&data).await?;
        Ok(())
    }

    pub async fn pty_resize(&self, frame: &PtyResize) -> SessionResult<()> {
        let session = self.get(&frame.session)?;
        if let Some(handle) = self.pty(&session, &frame.pty) {
            handle.resize(frame.cols, frame.rows).await?;
        }
        Ok(())
    }

    pub async fn pty_close(&self, frame: &PtyClose) -> SessionResult<()> {
        let session = self.get(&frame.session)?;
        if let Some(handle) = self.pty(&session, &frame.pty) {
            let _ = handle.write(b"\x04").await;
            handle.close().await;
        }
        Ok(())
    }

    // --- Vorschau ----------------------------------------------------------------------

    /// Vorschau-Server im Container starten (8p/8x); liefert `{url, port, token}`.
    pub async fn start_preview(
        &self,
        session: &Session,
        path: Option<&str>,
    ) -> SessionResult<Value> {
        let directory = path.filter(|p| !p.is_empty()).unwrap_or(".").to_string();
        let mut fields = Map::new();
        fields.insert("pattern".into(), json!("*"));
        fields.insert("path".into(), json!(directory));
        // Nur Prüfung: Verzeichnis liegt unter /workspace.
        self.fs(session, "glob", b"", fields).await?;
        let (port, token) = {
            let st = session.state();
            (st.preview_port, st.preview_token.clone())
        };
        let Some(port) = port else {
            return Err(SessionError::new(
                "Für diese Sitzung ist kein Vorschau-Port frei.",
                "limit",
            ));
        };
        let token = token.unwrap_or_else(|| token_urlsafe(24));
        let argv: Vec<String> = [
            "mgpt-serve",
            "--token",
            &token,
            "--dir",
            &directory,
            "--port",
            "8080",
        ]
        .iter()
        .map(|s| s.to_string())
        .collect();
        self.engine
            .exec_detached(&session.container, &argv, "/workspace")
            .await?;
        {
            let mut st = session.state();
            st.preview_token = Some(token.clone());
            st.preview_dir = Some(directory);
        }
        self.send(
            "preview",
            json!({"session": session.id, "running": true, "port": port, "token": token}),
        )
        .await?;
        let url = format!("http://127.0.0.1:{port}/{token}/");
        Ok(json!({"url": url, "port": port, "token": token}))
    }

    /// Vorschau über den Rückkanal (8p): Datei per `mgpt-fs fetch`, nie Verzeichnislisten.
    pub async fn preview_request(&self, frame: &PreviewRequest) -> SessionResult<()> {
        let session = guard(&self.sessions).get(&frame.session).cloned();
        let mut status: u16 = 404;
        let mut body: Vec<u8> = Vec::new();
        let mut ctype = "text/plain; charset=utf-8".to_string();
        if let Some(session) = session {
            let (running, dir, token) = {
                let st = session.state();
                (
                    st.state == "running",
                    st.preview_dir.clone(),
                    st.preview_token.clone(),
                )
            };
            if let (true, Some(dir)) = (running, dir) {
                let mut path = frame.path.clone();
                let prefix = format!("/{}/", token.unwrap_or_else(|| "None".into()));
                if let Some(rest) = path.strip_prefix(&prefix) {
                    path = format!("/{rest}");
                }
                let mut fields = Map::new();
                fields.insert("dir".into(), json!(dir));
                fields.insert("path".into(), json!(path));
                let result = self
                    .fs(&session, "fetch", b"", fields)
                    .await
                    .unwrap_or_else(|_| json!({"status": 500}));
                status = result["status"]
                    .as_u64()
                    .filter(|s| *s != 0)
                    .and_then(|s| u16::try_from(s).ok())
                    .unwrap_or(500);
                if status == 200 {
                    use base64::Engine as _;
                    body = base64::engine::general_purpose::STANDARD
                        .decode(result["data"].as_str().unwrap_or(""))
                        .unwrap_or_default();
                    ctype = result["content_type"]
                        .as_str()
                        .filter(|s| !s.is_empty())
                        .unwrap_or("application/octet-stream")
                        .to_string();
                }
                self.touch(&session);
            }
        }
        if status != 200 {
            body = format!("{status}\n").into_bytes();
        }
        let mut headers = json!({
            "Content-Type": ctype,
            "Content-Security-Policy": "sandbox allow-scripts allow-forms allow-modals; default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob:; connect-src 'none'",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        });
        if frame.method == "HEAD" {
            headers["Content-Length"] = json!(body.len().to_string());
            body.clear();
        }
        let parts: Vec<&[u8]> = if body.is_empty() {
            vec![&[]]
        } else {
            body.chunks(protocol::PREVIEW_BYTES).collect()
        };
        let count = parts.len();
        for (seq, part) in parts.into_iter().enumerate() {
            let first = seq == 0;
            self.send(
                "preview_response",
                json!({
                    "session": frame.session,
                    "request": frame.request,
                    "seq": seq,
                    "status": if first { json!(status) } else { Value::Null },
                    "headers": if first { headers.clone() } else { Value::Null },
                    "data": if part.is_empty() { Value::Null } else { json!(protocol::b64encode(part)) },
                    "last": seq + 1 == count,
                }),
            )
            .await?;
        }
        Ok(())
    }
}

/// Adresse von MultiGPT aus `MGPT_URL` (`wss://host/runner/ws` → `https://host`).
pub fn base_url(url: &str) -> String {
    let url = url.trim_end_matches('/');
    let url = url.strip_suffix("/runner/ws").unwrap_or(url);
    if let Some(rest) = url.strip_prefix("wss://") {
        format!("https://{rest}")
    } else if let Some(rest) = url.strip_prefix("ws://") {
        format!("http://{rest}")
    } else {
        url.to_string()
    }
}

/// Belegung eines Ordners in Byte (Blöcke wie `du`, Symlinks nicht folgen).
pub fn du(root: &std::path::Path) -> u64 {
    use std::os::unix::fs::MetadataExt;
    let mut total = 0;
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let Ok(entries) = std::fs::read_dir(&dir) else {
            continue;
        };
        for entry in entries.flatten() {
            let Ok(meta) = entry.path().symlink_metadata() else {
                continue;
            };
            if meta.is_dir() {
                stack.push(entry.path());
            } else {
                total += meta.blocks() * 512;
            }
        }
    }
    total
}

// --- Griff für die Werkzeuge ---------------------------------------------------------

/// Sitzung aus Sicht der Werkzeuge (`mcp::host::ToolHost`).
#[derive(Clone)]
pub struct SessionHandle {
    pub manager: Arc<SessionManager>,
    pub session: Arc<Session>,
}

/// PTY eines Werkzeugs; beim `close` meldet es sich bei der Sitzung ab.
pub struct SessionPty {
    inner: Arc<dyn Pty>,
    session: Arc<Session>,
    id: String,
}

impl ToolPty for SessionPty {
    async fn read(&self) -> Option<Vec<u8>> {
        let data = self.inner.read().await;
        if data.is_empty() { None } else { Some(data) }
    }

    async fn write(&self, data: &[u8]) -> ToolResult<()> {
        self.inner
            .write(data)
            .await
            .map_err(|e| ToolError::new(e.0))
    }

    async fn wait(&self) -> Option<i32> {
        exit_code(self.inner.wait().await)
    }

    async fn close(&self) {
        guard(&self.session.ptys).remove(&self.id);
        self.inner.close().await;
    }
}

impl SessionHandle {
    fn target(&self, target: Target<'_>) -> ToolResult<(String, &'static str)> {
        match target {
            Target::Work => Ok((self.session.container.clone(), "/workspace")),
            Target::Helper(name) => {
                // Nur Hilfscontainer, die diese Sitzung selbst geöffnet hat.
                if !guard(&self.session.helpers).contains(name) {
                    return Err(ToolError::new("Hilfscontainer unbekannt."));
                }
                Ok((name.to_string(), "/tmp"))
            }
        }
    }
}

impl ToolHost for SessionHandle {
    type Pty = SessionPty;

    fn session_id(&self) -> String {
        self.session.id.clone()
    }

    fn limits(&self) -> ToolLimits {
        ToolLimits {
            command_seconds: self.session.command_seconds(),
            output_bytes: self.session.output_bytes(),
            tmpfs_mb: self.session.state().limits.tmpfs_mb,
        }
    }

    fn lock(&self) -> Arc<tokio::sync::Mutex<()>> {
        self.session.lock.clone()
    }

    fn touch(&self) {
        self.manager.touch(&self.session);
    }

    async fn fs(&self, op: &str, fields: Map<String, Value>) -> ToolResult<Value> {
        Ok(self.manager.fs(&self.session, op, b"", fields).await?)
    }

    async fn fs_raw(&self, op: &str, fields: Map<String, Value>) -> ToolResult<(Value, Vec<u8>)> {
        Ok(self.manager.fs_raw(&self.session, op, b"", fields).await?)
    }

    async fn save(&self, message: &str) -> String {
        match self.manager.save(&self.session, message).await {
            Ok(note) => note,
            Err(e) => format!("\n[Arbeitsstand nicht gespeichert: {}]", e.message),
        }
    }

    async fn scratch_full(&self) -> bool {
        let handle = self.clone();
        tokio::task::spawn_blocking(move || handle.manager.scratch_full(&handle.session))
            .await
            .unwrap_or(false)
    }

    async fn scratch_note(&self) -> String {
        let handle = self.clone();
        tokio::task::spawn_blocking(move || handle.manager.scratch_note(&handle.session))
            .await
            .unwrap_or_default()
    }

    fn scratch_size_mb(&self) -> Option<u64> {
        self.session.scratch_local.as_ref()?;
        self.session.scratch.as_ref().map(|s| s.size_mb)
    }

    async fn scratch_rest(&self) -> Option<u64> {
        let handle = self.clone();
        tokio::task::spawn_blocking(move || {
            let limit = handle.manager.scratch_limit(&handle.session)?;
            let used = handle.manager.scratch_usage(&handle.session).unwrap_or(0);
            Some(limit.saturating_sub(used))
        })
        .await
        .ok()
        .flatten()
    }

    async fn exec_pty(
        &self,
        pty_id: &str,
        target: Target<'_>,
        argv: Vec<String>,
    ) -> ToolResult<SessionPty> {
        let (name, workdir) = self.target(target)?;
        let inner: Arc<dyn Pty> = Arc::from(
            self.manager
                .engine
                .exec_pty(&name, &argv, TOOL_COLS, TOOL_ROWS, workdir)
                .await
                .map_err(|e| ToolError::new(e.0))?,
        );
        guard(&self.session.ptys).insert(pty_id.to_string(), inner.clone());
        self.manager.touch(&self.session);
        Ok(SessionPty {
            inner,
            session: self.session.clone(),
            id: pty_id.to_string(),
        })
    }

    async fn exec(
        &self,
        target: Target<'_>,
        argv: Vec<String>,
        stdin: Vec<u8>,
        timeout_s: u64,
        max_output: usize,
    ) -> ToolResult<ExecOutput> {
        let (name, workdir) = self.target(target)?;
        let res = self
            .manager
            .engine
            .exec(
                &name,
                &argv,
                ExecOptions {
                    // Ohne Eingabe kein stdin (Podman liefert sonst kein EOF).
                    stdin: (!stdin.is_empty()).then_some(stdin),
                    timeout: Duration::from_secs(timeout_s.max(1)),
                    max_output,
                    workdir: workdir.into(),
                },
            )
            .await
            .map_err(|e| ToolError::new(e.0))?;
        Ok(ExecOutput {
            code: res
                .code
                .map(|c| c.clamp(i32::MIN as i64, i32::MAX as i64) as i32),
            stdout: res.stdout,
            stderr: res.stderr,
            timed_out: res.timed_out,
        })
    }

    async fn start_preview(&self, path: Option<&str>) -> ToolResult<Value> {
        Ok(self.manager.start_preview(&self.session, path).await?)
    }

    async fn open_helper(&self) -> ToolResult<String> {
        Ok(self.manager.open_helper(&self.session).await?)
    }

    async fn close_helper(&self, name: &str) {
        self.manager.close_helper(&self.session, name).await;
    }

    fn relay(&self) -> Option<Relay> {
        let st = self.session.state();
        if st.limits.network != "allowlist" {
            return None;
        }
        Some(Relay {
            ip: st.relay_ip.clone()?,
            targets: st.relay_targets.clone(),
        })
    }

    async fn send(&self, kind: &str, fields: Value) -> ToolResult<()> {
        self.manager
            .outbox
            .send(kind, fields)
            .await
            .map_err(|e| ToolError::new(e.to_string()))
    }

    fn chunk_bytes(&self) -> usize {
        self.manager.chunk_bytes()
    }

    fn server_url(&self) -> String {
        base_url(&self.manager.server_url)
    }

    async fn download(&self, url: &str, bearer: &str, max_bytes: usize) -> ToolResult<Vec<u8>> {
        let downloader = guard(&self.manager.downloader).clone();
        match downloader {
            Some(d) => d
                .download(url, bearer, max_bytes)
                .await
                .map_err(ToolError::new),
            None => Err(ToolError::new(
                "Download ist in diesem Agent nicht verfügbar.",
            )),
        }
    }

    fn allowed_services(&self) -> Option<Vec<String>> {
        self.manager.services()
    }
}

#[cfg(test)]
mod tests;
