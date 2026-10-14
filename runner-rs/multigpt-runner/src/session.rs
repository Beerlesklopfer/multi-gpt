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
    AptOutput, ExecOutput, Relay, Target, ToolError, ToolHost, ToolLimits, ToolPty, ToolResult,
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
/// Zeitlimit je apt-Schritt (update bzw. install) in Sekunden.
pub const APT_TIMEOUT: Duration = Duration::from_secs(900);
/// Zeitlimit für das Entfernen der setuid-Bits.
pub const STRIP_TIMEOUT: Duration = Duration::from_secs(300);
/// Paketlisten nach so vielen Sekunden neu laden.
pub const APT_UPDATE_SECONDS: f64 = 3600.0;
/// Ausgabe eines apt-Schritts, die der Agent liest (der Rest wird verworfen).
pub const APT_OUTPUT_BYTES: usize = 1024 * 1024;
/// Liste der apt-Pakete im Arbeitsstand (eine Zeile je Paket).
pub const APT_PACKAGES_FILE: &str = ".mgpt/apt-packages";
/// Ordner, die nie ins Bundle gehören (pip/npm/cargo installieren dorthin).
pub const GITIGNORE_ENTRIES: [&str; 3] = [".venv/", "node_modules/", "target/"];

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
    /// Gemessene beschreibbare Schicht in Byte (Modus `packages`, je Heartbeat).
    pub rootfs_used: Option<u64>,
    /// Zeitpunkt des letzten erfolgreichen `apt-get update` (Uhr des Managers).
    pub apt_updated: Option<f64>,
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
            rootfs_used_mb: st
                .limits
                .packages()
                .then(|| st.rootfs_used.unwrap_or(0).div_ceil(1 << 20).min(1_048_576)),
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

    /// Netzmodus „Paketquellen“ (gelockerte Vorlage, Proxy, `apt_install`).
    pub fn packages(&self) -> bool {
        self.state().limits.packages()
    }

    /// Beschreibbare Schicht an bzw. über ihrer Grenze (letzte Messung)? Dann
    /// lehnen die schreibenden Werkzeuge und `apt_install` ab.
    pub fn rootfs_full(&self) -> bool {
        self.rootfs_compare(|used, limit| used >= limit)
    }

    /// Schicht über der Grenze: Sitzung wird beendet (wie der Server bei `mismatch`).
    pub fn rootfs_over(&self) -> bool {
        self.rootfs_compare(|used, limit| used > limit)
    }

    fn rootfs_compare(&self, test: impl Fn(u64, u64) -> bool) -> bool {
        let st = self.state();
        match (st.limits.rootfs_mb, st.rootfs_used) {
            (Some(limit), Some(used)) if st.limits.packages() => {
                test(used.div_ceil(1 << 20), limit)
            }
            _ => false,
        }
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
                    rootfs_used: None,
                    apt_updated: None,
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
                if session.packages() {
                    self.prepare_packages(&session).await;
                }
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
        let packages = frame.network == "packages";
        let limits = Limits {
            cpus: frame.cpus,
            memory_mb: frame.memory_mb,
            pids: frame.pids,
            tmpfs_mb: frame.tmpfs_mb,
            network: frame.network.clone(),
            rootfs_mb: if packages { frame.rootfs_mb } else { None },
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
        let package_hosts = self.package_hosts(frame)?;
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
            "package_hosts": package_hosts,
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
                rootfs_used: None,
                apt_updated: None,
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
        let proxy_url = if packages {
            Some(self.start_proxy(&sid, &frame.image, &package_hosts).await?)
        } else {
            None
        };
        self.start_container(&session, &frame.image, &spec, proxy_url.as_deref())
            .await?;
        let info = self.engine.inspect_container(&session.container).await?;
        let port = session.state().preview_port.map(u64::from);
        let source = scratch.source.as_deref().filter(|_| scratch.is_local());
        let problems =
            template::check_inspect(&info, &limits, port, source, &self.bounds.expose_bind);
        if !problems.is_empty() {
            return Err(SessionError::new(
                format!("Abweichende Grenzen: {}", problems.join("; ")),
                "not_allowed",
            ));
        }
        session.state().limits = Limits {
            rootfs_mb: limits.rootfs_mb,
            ..template::actual_limits(&info, &limits.network)
        };
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
        if mode == "packages" {
            // Ausgang nur für den Proxy; der Arbeitscontainer bleibt am internen Netz.
            self.engine
                .create_network(&template::network_body(
                    &template::network_name(sid, "out"),
                    false,
                    &labels,
                ))
                .await?;
        }
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

    /// Proxy für Paketquellen starten; liefert seine Adresse im Sitzungsnetz.
    async fn start_proxy(&self, sid: &str, image: &str, hosts: &[String]) -> SessionResult<String> {
        let mut body = template::proxy_body(sid, image, hosts);
        self.with_labels(&mut body);
        let name = body["name"].as_str().unwrap_or_default().to_string();
        self.engine.create_container(&body).await?;
        self.engine.start_container(&name).await?;
        let info = self.engine.inspect_container(&name).await?;
        let ip =
            info["NetworkSettings"]["Networks"][&template::network_name(sid, "net")]["IPAddress"]
                .as_str()
                .unwrap_or("");
        template::proxy_url(ip)
            .ok_or_else(|| SessionError::new("Proxy hat keine Adresse im Sitzungsnetz.", "engine"))
    }

    /// Hosts des Proxys (Modus `packages`): erneut geprüft, sonst leer.
    fn package_hosts(&self, frame: &SessionStart) -> SessionResult<Vec<String>> {
        let hosts = frame.package_hosts.clone().unwrap_or_default();
        if frame.network != "packages" {
            if !hosts.is_empty() || frame.rootfs_mb.is_some() {
                return Err(SessionError::new(
                    "Paket-Hosts nur im Modus Paketquellen.",
                    "not_allowed",
                ));
            }
            return Ok(Vec::new());
        }
        let valid = !hosts.is_empty()
            && hosts.len() <= template::MAX_PACKAGE_HOSTS
            && hosts.iter().all(|h| template::is_package_host(h));
        if !valid {
            return Err(SessionError::new("Paket-Hosts ungültig.", "not_allowed"));
        }
        if frame.rootfs_mb.is_none() {
            return Err(SessionError::new(
                "Grenze der Container-Schicht fehlt.",
                "not_allowed",
            ));
        }
        if frame.allow.as_ref().is_some_and(|a| !a.is_empty()) {
            return Err(SessionError::new(
                "Positivliste und Paketquellen schließen sich aus.",
                "not_allowed",
            ));
        }
        Ok(hosts)
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
        proxy_url: Option<&str>,
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
                proxy_url,
                &self.bounds.expose_bind,
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
            format!("Kein freier Vorschau-Port in 10080–10089 ({reason})."),
            "limit",
        ))
    }

    pub async fn stop(&self, sid: &str, reason: &str) {
        self.stop_with_error(sid, reason, None).await;
    }

    /// Wie [`Self::stop`], mit Grund für den Nutzer im Rahmen `session_state`.
    pub async fn stop_with_error(&self, sid: &str, _reason: &str, error: Option<&str>) {
        let session = guard(&self.sessions).remove(sid);
        if let Some(session) = session {
            session.state().state = "stopping".into();
            self.close_session_io(&session).await;
        }
        self.cleanup(sid).await;
        let mut fields = json!({"session": sid, "state": "stopped"});
        if let Some(error) = error {
            fields["error"] = json!(error);
        }
        let _ = self.send("session_state", fields).await;
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
            if session.packages() && session.state().state == "running" {
                self.measure_rootfs(&session).await;
                if session.rootfs_over() {
                    tracing::warn!(
                        "Sitzung {}: Container-Schicht über der Grenze, Sitzung wird beendet.",
                        session.id
                    );
                    self.stop_with_error(
                        &session.id,
                        "mismatch",
                        Some("Container-Schicht über der Grenze (Paketquellen)."),
                    )
                    .await;
                    continue;
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

    // --- Paketquellen (apt, .gitignore, Schicht) -----------------------------------------

    /// Größe der beschreibbaren Schicht messen und merken (Byte).
    pub async fn measure_rootfs(&self, session: &Session) -> Option<u64> {
        match self.engine.container_size(&session.container).await {
            Ok(size) => {
                session.state().rootfs_used = Some(size);
                Some(size)
            }
            Err(e) => {
                tracing::warn!("Schicht von {} nicht messbar: {e}", session.id);
                None
            }
        }
    }

    /// Fester Befehl als root im Arbeitscontainer (nur Modus `packages`).
    async fn root_exec(
        &self,
        session: &Session,
        argv: Vec<String>,
        timeout: Duration,
    ) -> SessionResult<crate::engine::ExecResult> {
        if !session.packages() {
            return Err(SessionError::new(
                "Paketquellen sind in dieser Sitzung nicht freigegeben.",
                "not_allowed",
            ));
        }
        let argv = template::with_timeout(&argv, timeout.as_secs());
        template::check_root_argv(&argv)?;
        Ok(self
            .engine
            .exec(
                &session.container,
                &argv,
                ExecOptions {
                    stdin: None,
                    // Etwas Luft für `timeout -k 5` im Container.
                    timeout: timeout + Duration::from_secs(10),
                    max_output: APT_OUTPUT_BYTES,
                    workdir: "/".into(),
                    root: true,
                },
            )
            .await?)
    }

    /// apt-Pakete installieren: bei Bedarf `apt-get update`, dann `apt-get install`,
    /// danach setuid-Bits entfernen. Mit `record` stehen die Namen anschließend in
    /// `.mgpt/apt-packages` (Commit macht der Aufrufer).
    pub async fn apt_install(
        &self,
        session: &Session,
        packages: &[String],
        record: bool,
    ) -> SessionResult<AptReport> {
        let install = template::apt_install_argv(packages)?;
        if session.rootfs_full() {
            return Err(SessionError::new(
                "Container-Schicht über der Grenze: keine weiteren Pakete möglich.",
                "limit",
            ));
        }
        let mut report = AptReport::default();
        let stale = session
            .state()
            .apt_updated
            .is_none_or(|t| (self.clock)() - t > APT_UPDATE_SECONDS);
        if stale {
            let res = self
                .root_exec(session, template::apt_update_argv(), APT_TIMEOUT)
                .await?;
            report.add("apt-get update", &res);
            if res.timed_out || res.code != Some(0) {
                report.code = if res.timed_out { None } else { res.code };
                report.timed_out = res.timed_out;
                return Ok(report);
            }
            session.state().apt_updated = Some((self.clock)());
        }
        let res = self.root_exec(session, install, APT_TIMEOUT).await?;
        report.add("apt-get install", &res);
        report.code = res.code;
        report.timed_out = res.timed_out;
        // Auch nach einem Fehler: halb installierte Pakete könnten setuid-Bits setzen.
        let strip = self
            .root_exec(session, template::strip_setuid_argv(), STRIP_TIMEOUT)
            .await?;
        if strip.timed_out || strip.code != Some(0) {
            report.add("setuid-Bits entfernen", &strip);
        }
        self.measure_rootfs(session).await;
        if report.ok() && record {
            self.record_packages(session, packages).await?;
            self.ensure_gitignore(session).await?;
        }
        Ok(report)
    }

    /// Textdatei im Arbeitsstand lesen; `None`, wenn sie fehlt oder nicht lesbar ist.
    async fn read_text(&self, session: &Session, path: &str) -> Option<String> {
        let mut fields = Map::new();
        fields.insert("path".into(), json!(path));
        fields.insert("max_bytes".into(), json!(256 * 1024));
        let (_, data) = self.fs_raw(session, "file_out", b"", fields).await.ok()?;
        Some(String::from_utf8_lossy(&data).into_owned())
    }

    async fn write_text(&self, session: &Session, path: &str, content: &str) -> SessionResult<()> {
        let mut fields = Map::new();
        fields.insert("path".into(), json!(path));
        fields.insert("content".into(), json!(content));
        self.fs(session, "write", b"", fields).await.map(|_| ())
    }

    /// Gültige Paketnamen aus `.mgpt/apt-packages` (Kommentare und Fremdes ignoriert).
    pub async fn recorded_packages(&self, session: &Session) -> Vec<String> {
        let text = self
            .read_text(session, APT_PACKAGES_FILE)
            .await
            .unwrap_or_default();
        parse_packages(&text)
    }

    /// Namen in `.mgpt/apt-packages` aufnehmen (sortiert, ohne Doppelte).
    async fn record_packages(&self, session: &Session, packages: &[String]) -> SessionResult<()> {
        let mut all: std::collections::BTreeSet<String> =
            self.recorded_packages(session).await.into_iter().collect();
        let before = all.len();
        all.extend(packages.iter().cloned());
        if all.len() == before && self.read_text(session, APT_PACKAGES_FILE).await.is_some() {
            return Ok(());
        }
        let mut text = String::from(
            "# apt-Pakete dieses Arbeitsstands (MultiGPT installiert sie beim Sitzungsstart nach)\n",
        );
        for name in all.iter().take(template::MAX_APT_PACKAGES_TOTAL) {
            text.push_str(name);
            text.push('\n');
        }
        self.write_text(session, APT_PACKAGES_FILE, &text).await
    }

    /// `.venv/`, `node_modules/` und `target/` in `.gitignore` sicherstellen.
    /// Liefert true, wenn die Datei geändert wurde.
    pub async fn ensure_gitignore(&self, session: &Session) -> SessionResult<bool> {
        let text = self
            .read_text(session, ".gitignore")
            .await
            .unwrap_or_default();
        let missing = missing_ignores(&text);
        if missing.is_empty() {
            return Ok(false);
        }
        let mut out = text;
        if !out.is_empty() && !out.ends_with('\n') {
            out.push('\n');
        }
        for entry in missing {
            out.push_str(entry);
            out.push('\n');
        }
        self.write_text(session, ".gitignore", &out).await?;
        Ok(true)
    }

    /// Nach dem Start im Modus `packages`: `.gitignore` sicherstellen und die Pakete
    /// aus `.mgpt/apt-packages` nachinstallieren. Hält die Sperre der Sitzung, damit
    /// Werkzeuge erst danach laufen. Fehler beenden die Sitzung nicht.
    pub async fn prepare_packages(&self, session: &Session) {
        let lock = session.lock.clone();
        let _guard = lock.lock().await;
        if let Err(e) = self.ensure_gitignore(session).await {
            tracing::warn!("Sitzung {}: .gitignore nicht ergänzt: {e}", session.id);
        }
        let packages = self.recorded_packages(session).await;
        if !packages.is_empty() {
            tracing::info!(
                "Sitzung {}: installiere {} apt-Paket(e) aus {APT_PACKAGES_FILE} nach.",
                session.id,
                packages.len()
            );
            match self.apt_install(session, &packages, false).await {
                Ok(report) if report.ok() => {}
                Ok(report) => tracing::warn!(
                    "Sitzung {}: Nachinstallieren fehlgeschlagen: {}",
                    session.id,
                    tail(report.output.trim(), 500)
                ),
                Err(e) => tracing::warn!("Sitzung {}: Nachinstallieren: {e}", session.id),
            }
        }
        if let Ok(note) = self.save(session, "Paketquellen: .gitignore").await
            && !note.is_empty()
        {
            tracing::warn!("Sitzung {}: {}", session.id, note.trim());
        }
        self.touch(session);
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
                    root: false,
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

/// Ergebnis von `apt_install` (Ausgabe der Schritte, Exit-Code des letzten).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct AptReport {
    pub code: Option<i64>,
    pub timed_out: bool,
    pub output: String,
}

impl AptReport {
    pub fn ok(&self) -> bool {
        !self.timed_out && self.code == Some(0)
    }

    fn add(&mut self, step: &str, res: &crate::engine::ExecResult) {
        self.output.push_str(&format!("$ {step}\n"));
        self.output.push_str(&String::from_utf8_lossy(&res.stdout));
        let err = String::from_utf8_lossy(&res.stderr);
        if !err.trim().is_empty() {
            self.output.push_str(&err);
        }
        if !self.output.ends_with('\n') {
            self.output.push('\n');
        }
    }
}

/// Paketnamen aus `.mgpt/apt-packages`: je Zeile ein Name, `#` leitet Kommentare
/// ein; ungültige Namen fallen weg (die Datei kann das Modell ändern).
pub fn parse_packages(text: &str) -> Vec<String> {
    let mut seen = std::collections::BTreeSet::new();
    text.lines()
        .map(|line| line.split('#').next().unwrap_or("").trim())
        .filter(|name| template::APT_PACKAGE_RE.is_match(name))
        .filter(|name| seen.insert(name.to_string()))
        .take(template::MAX_APT_PACKAGES_TOTAL)
        .map(str::to_string)
        .collect()
}

/// Einträge aus [`GITIGNORE_ENTRIES`], die in `.gitignore` noch fehlen.
pub fn missing_ignores(text: &str) -> Vec<&'static str> {
    let lines: HashSet<&str> = text.lines().map(str::trim).collect();
    GITIGNORE_ENTRIES
        .iter()
        .copied()
        .filter(|entry| {
            let bare = entry.trim_end_matches('/');
            ![
                *entry,
                bare,
                &format!("/{entry}") as &str,
                &format!("/{bare}") as &str,
            ]
            .iter()
            .any(|form| lines.contains(form))
        })
        .collect()
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
                    root: false,
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

    fn packages(&self) -> bool {
        self.session.packages()
    }

    fn rootfs_full(&self) -> bool {
        self.session.rootfs_full()
    }

    async fn apt_install(&self, packages: Vec<String>) -> ToolResult<AptOutput> {
        let report = self
            .manager
            .apt_install(&self.session, &packages, true)
            .await?;
        Ok(AptOutput {
            code: report
                .code
                .map(|c| c.clamp(i32::MIN as i64, i32::MAX as i64) as i32),
            timed_out: report.timed_out,
            output: report.output,
        })
    }
}

#[cfg(test)]
mod tests;
