//! Podman-Engine über die libpod-REST-API am Unix-Socket (M14-08), Port von
//! `runner_agent/podman.py`.
//!
//! Eigener schlanker Client statt eines SDK: JSON-Aufrufe mit hyper (HTTP/1.1 je
//! Aufruf über eine neue Socket-Verbindung), `exec` mit übernommener Verbindung
//! (Hijack, `Upgrade: tcp`) über einen rohen Unix-Socket für bidirektionale
//! Ströme bzw. das PTY. Jeder Körper geht vorher durch die feste Vorlage;
//! gelöscht werden nur Objekte mit dem Label `multigpt.runner=1` und eigenem
//! Namensmuster.

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use async_trait::async_trait;
use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper::{Method, Request};
use hyper_util::rt::TokioIo;
use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;
use tokio::net::unix::{OwnedReadHalf, OwnedWriteHalf};
use tokio::sync::{Mutex, Notify};

use crate::engine::{
    ContainerInfo, Engine, EngineError, EngineInfo, EngineResult, ExecOptions, ExecResult, Pty,
    is_own, labels_of,
};
use crate::template::{self, Bounds};

/// Basis der libpod-API (Podman ≥ 4 versteht v5-Pfade).
pub const API: &str = "/v5.0.0/libpod";
const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
const REQUEST_TIMEOUT: Duration = Duration::from_secs(120);
/// `/info` sammelt Speicherstatistiken und braucht auf belasteten Rechnern länger.
const INFO_TIMEOUT: Duration = Duration::from_secs(150);
/// Ziehen eines fehlenden Arbeits-Images (einige hundert MB).
const PULL_TIMEOUT: Duration = Duration::from_secs(1800);

/// Prozentkodierung für Pfadsegmente und Abfragewerte (alles außer unreserved).
pub fn quote(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for byte in text.bytes() {
        if byte.is_ascii_alphanumeric() || b"-._~".contains(&byte) {
            out.push(byte as char);
        } else {
            out.push_str(&format!("%{byte:02X}"));
        }
    }
    out
}

fn query(params: &[(&str, String)]) -> String {
    if params.is_empty() {
        return String::new();
    }
    let parts: Vec<String> = params
        .iter()
        .map(|(k, v)| format!("{}={}", quote(k), quote(v)))
        .collect();
    format!("?{}", parts.join("&"))
}

/// Fehlermeldung aus einer Podman-Antwort (gekürzt, ohne Geheimnisse).
fn message(status: u16, body: &[u8]) -> String {
    let text = match serde_json::from_slice::<Value>(body) {
        Ok(data) => {
            let pick = |k: &str| {
                data.get(k)
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty())
                    .map(str::to_string)
            };
            pick("message")
                .or_else(|| pick("cause"))
                .unwrap_or_default()
        }
        Err(_) => String::from_utf8_lossy(body).into_owned(),
    };
    let short: String = text.chars().take(300).collect();
    format!("Podman {status}: {short}")
}

/// Antwort eines JSON-Aufrufs.
#[derive(Debug, Clone)]
pub struct Response {
    pub status: u16,
    pub body: Bytes,
}

impl Response {
    pub fn json(&self) -> EngineResult<Value> {
        serde_json::from_slice(&self.body)
            .map_err(|_| EngineError::new("Podman: Antwort ist kein JSON."))
    }
}

pub struct PodmanEngine {
    pub bounds: Bounds,
    pub socket_path: PathBuf,
}

impl PodmanEngine {
    pub fn new(bounds: Bounds, socket_path: impl Into<PathBuf>) -> Self {
        Self {
            bounds,
            socket_path: socket_path.into(),
        }
    }

    async fn connect(&self) -> EngineResult<UnixStream> {
        match tokio::time::timeout(CONNECT_TIMEOUT, UnixStream::connect(&self.socket_path)).await {
            Ok(Ok(stream)) => Ok(stream),
            _ => Err(EngineError::new("Podman nicht erreichbar.")),
        }
    }

    /// JSON-Aufruf; Status außerhalb `ok` wird zum Fehler mit der Meldung von Podman.
    /// Fehlendes Image ziehen (`policy=missing`). Aufrufer prüft vorher die Vorlage;
    /// hier zusätzlich: nur Referenzen mit Digest.
    pub async fn ensure_image(&self, image: &str) -> EngineResult<()> {
        if !image.contains("@sha256:") {
            return Err(EngineError::new("Image ohne Digest wird nicht gezogen."));
        }
        let exists = self
            .request(
                Method::GET,
                &format!("/images/{}/exists", quote(image)),
                &[],
                None,
                &[204, 404],
            )
            .await?;
        if exists.status == 204 {
            return Ok(());
        }
        tracing::info!("Ziehe Arbeits-Image {image} …");
        let response = self
            .request_within(
                PULL_TIMEOUT,
                Method::POST,
                "/images/pull",
                &[
                    ("reference", image.to_string()),
                    ("policy", "missing".to_string()),
                    ("quiet", "true".to_string()),
                ],
                None,
                &[200],
            )
            .await?;
        // Antwort ist ein Strom von JSON-Zeilen; ein Fehler steht im Feld "error".
        let failure = response
            .body
            .split(|b| *b == b'\n')
            .filter_map(|line| serde_json::from_slice::<Value>(line).ok())
            .filter_map(|value| {
                value
                    .get("error")
                    .and_then(Value::as_str)
                    .map(str::to_string)
            })
            .find(|error| !error.is_empty());
        if let Some(error) = failure {
            return Err(EngineError::new(format!(
                "Arbeits-Image konnte nicht gezogen werden: {error}"
            )));
        }
        Ok(())
    }

    pub async fn request(
        &self,
        method: Method,
        path: &str,
        params: &[(&str, String)],
        body: Option<&Value>,
        ok: &[u16],
    ) -> EngineResult<Response> {
        self.request_within(REQUEST_TIMEOUT, method, path, params, body, ok)
            .await
    }

    /// Wie [`Self::request`] mit eigenem Zeitlimit.
    pub async fn request_within(
        &self,
        limit: Duration,
        method: Method,
        path: &str,
        params: &[(&str, String)],
        body: Option<&Value>,
        ok: &[u16],
    ) -> EngineResult<Response> {
        let uri = format!("{API}{path}{}", query(params));
        let call = async {
            let stream = self.connect().await?;
            let (mut sender, conn) = hyper::client::conn::http1::handshake(TokioIo::new(stream))
                .await
                .map_err(|e| EngineError::new(format!("Podman nicht erreichbar: {e}")))?;
            let driver = tokio::spawn(async move {
                let _ = conn.await;
            });
            let payload = match body {
                Some(value) => Bytes::from(serde_json::to_vec(value).unwrap_or_default()),
                None => Bytes::new(),
            };
            let mut builder = Request::builder()
                .method(method)
                .uri(uri)
                .header("Host", "podman");
            if body.is_some() {
                builder = builder.header("Content-Type", "application/json");
            }
            let request = builder
                .body(Full::new(payload))
                .map_err(|e| EngineError::new(format!("Anfrage ungültig: {e}")))?;
            let response = sender
                .send_request(request)
                .await
                .map_err(|e| EngineError::new(format!("Podman nicht erreichbar: {e}")))?;
            let status = response.status().as_u16();
            let bytes = response
                .into_body()
                .collect()
                .await
                .map_err(|e| EngineError::new(format!("Podman nicht erreichbar: {e}")))?
                .to_bytes();
            driver.abort();
            Ok::<_, EngineError>(Response {
                status,
                body: bytes,
            })
        };
        let response = tokio::time::timeout(limit, call)
            .await
            .map_err(|_| EngineError::new("Podman nicht erreichbar: Zeitlimit."))??;
        if !ok.contains(&response.status) {
            return Err(EngineError::new(message(response.status, &response.body)));
        }
        Ok(response)
    }

    async fn exec_create(
        &self,
        name: &str,
        argv: &[String],
        stdin: bool,
        tty: bool,
        workdir: &str,
    ) -> EngineResult<String> {
        let body = json!({
            "AttachStdin": stdin,
            "AttachStdout": true,
            "AttachStderr": true,
            "Cmd": argv,
            "Tty": tty,
            "WorkingDir": workdir,
            "User": template::USER,
            "Privileged": false,
        });
        let response = self
            .request(
                Method::POST,
                &format!("/containers/{}/exec", quote(name)),
                &[],
                Some(&body),
                &[201],
            )
            .await?;
        response.json()?["Id"]
            .as_str()
            .map(str::to_string)
            .ok_or_else(|| EngineError::new("exec: keine Kennung von Podman."))
    }

    /// `exec/{id}/start` mit übernommener Verbindung (roh, bidirektional).
    async fn hijack(&self, exec_id: &str, tty: bool, cols: u16, rows: u16) -> EngineResult<Stream> {
        let mut body = json!({"Detach": false, "Tty": tty});
        if tty {
            body["h"] = json!(rows);
            body["w"] = json!(cols);
        }
        let payload = serde_json::to_vec(&body).unwrap_or_default();
        let stream = self.connect().await?;
        let (read, mut write) = stream.into_split();
        let head = format!(
            "POST {API}/exec/{}/start HTTP/1.1\r\nHost: podman\r\nContent-Type: application/json\r\n\
             Content-Length: {}\r\nConnection: Upgrade\r\nUpgrade: tcp\r\n\r\n",
            quote(exec_id),
            payload.len()
        );
        let mut request = head.into_bytes();
        request.extend_from_slice(&payload);
        write
            .write_all(&request)
            .await
            .map_err(|_| EngineError::new("Podman nicht erreichbar."))?;
        let mut reader = BufReader::with_capacity(1 << 16, read);
        let read_head = async {
            let mut head = Vec::new();
            loop {
                let mut line = Vec::new();
                let n = reader.read_until(b'\n', &mut line).await.ok()?;
                if n == 0 || head.len() > 64 * 1024 {
                    return None;
                }
                let end = line == b"\r\n" || line == b"\n";
                head.extend_from_slice(&line);
                if end {
                    return Some(head);
                }
            }
        };
        let head = match tokio::time::timeout(Duration::from_secs(30), read_head).await {
            Ok(Some(head)) => head,
            _ => return Err(EngineError::new("exec: keine Antwort von Podman.")),
        };
        let text = String::from_utf8_lossy(&head);
        let status_line = text.lines().next().unwrap_or("").to_string();
        let code = status_line.split(' ').nth(1).unwrap_or("");
        if code != "101" && code != "200" {
            let short: String = status_line.chars().take(200).collect();
            return Err(EngineError::new(format!("exec: {short}")));
        }
        Ok(Stream {
            reader: Mutex::new(Some(reader)),
            writer: Mutex::new(Some(write)),
            tty,
            closed: Notify::new(),
            is_closed: AtomicBool::new(false),
        })
    }

    async fn exit_code(&self, exec_id: &str) -> Option<i64> {
        for _ in 0..50 {
            let data = self
                .request(
                    Method::GET,
                    &format!("/exec/{}/json", quote(exec_id)),
                    &[],
                    None,
                    &[200],
                )
                .await
                .ok()?
                .json()
                .ok()?;
            if data["Running"] != json!(true) {
                return data["ExitCode"].as_i64();
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        None
    }
}

/// Übernommene HTTP-Verbindung eines `exec`.
struct Stream {
    reader: Mutex<Option<BufReader<OwnedReadHalf>>>,
    writer: Mutex<Option<OwnedWriteHalf>>,
    tty: bool,
    closed: Notify,
    is_closed: AtomicBool,
}

impl Stream {
    /// (Strom, Daten); Strom 1 = stdout, 2 = stderr; (0, leer) am Ende.
    async fn read_frame(&self) -> (u8, Vec<u8>) {
        if self.is_closed.load(Ordering::SeqCst) {
            return (0, Vec::new());
        }
        let read = async {
            let mut guard = self.reader.lock().await;
            let Some(reader) = guard.as_mut() else {
                return (0, Vec::new());
            };
            if self.tty {
                let mut buf = vec![0u8; 65536];
                return match reader.read(&mut buf).await {
                    Ok(0) | Err(_) => (0, Vec::new()),
                    Ok(n) => {
                        buf.truncate(n);
                        (1, buf)
                    }
                };
            }
            let mut header = [0u8; 8];
            if reader.read_exact(&mut header).await.is_err() {
                return (0, Vec::new());
            }
            let size = u32::from_be_bytes([header[4], header[5], header[6], header[7]]) as usize;
            let mut data = vec![0u8; size];
            let mut filled = 0;
            while filled < size {
                match reader.read(&mut data[filled..]).await {
                    Ok(0) | Err(_) => break,
                    Ok(n) => filled += n,
                }
            }
            data.truncate(filled);
            let kind = if header[0] == 2 { 2 } else { 1 };
            (kind, data)
        };
        tokio::select! {
            frame = read => frame,
            () = self.closed.notified() => (0, Vec::new()),
        }
    }

    async fn write(&self, data: &[u8]) -> EngineResult<()> {
        let mut guard = self.writer.lock().await;
        if let Some(writer) = guard.as_mut() {
            // Verbindungsfehler wie in Python stillschweigend (Prozess ist beendet).
            let _ = writer.write_all(data).await;
            let _ = writer.flush().await;
        }
        Ok(())
    }

    async fn close(&self) {
        self.is_closed.store(true, Ordering::SeqCst);
        self.closed.notify_waiters();
        if let Some(mut writer) = self.writer.lock().await.take() {
            let _ = writer.shutdown().await;
        }
        if let Ok(mut reader) = self.reader.try_lock() {
            reader.take();
        }
    }
}

struct PodmanPty {
    engine: PodmanEngine,
    exec_id: String,
    stream: Stream,
}

#[async_trait]
impl Pty for PodmanPty {
    async fn read(&self) -> Vec<u8> {
        self.stream.read_frame().await.1
    }

    async fn write(&self, data: &[u8]) -> EngineResult<()> {
        self.stream.write(data).await
    }

    async fn resize(&self, cols: u16, rows: u16) -> EngineResult<()> {
        self.engine
            .request(
                Method::POST,
                &format!("/exec/{}/resize", quote(&self.exec_id)),
                &[("h", rows.to_string()), ("w", cols.to_string())],
                None,
                &[200, 201],
            )
            .await
            .map(|_| ())
    }

    async fn wait(&self) -> Option<i64> {
        self.engine.exit_code(&self.exec_id).await
    }

    async fn close(&self) {
        self.stream.close().await;
    }
}

#[async_trait]
impl Engine for PodmanEngine {
    async fn info(&self) -> EngineResult<EngineInfo> {
        let data = self
            .request_within(INFO_TIMEOUT, Method::GET, "/info", &[], None, &[200])
            .await?
            .json()?;
        let version = data["version"]["Version"]
            .as_str()
            .filter(|s| !s.is_empty())
            .unwrap_or("?");
        let rootless = data["host"]["security"]["rootless"] == json!(true);
        Ok(EngineInfo {
            name: "podman".into(),
            version: version.chars().take(64).collect(),
            rootless,
        })
    }

    async fn create_network(&self, body: &Value) -> EngineResult<()> {
        template::check_network_body(body)?;
        self.request(Method::POST, "/networks/create", &[], Some(body), &[200])
            .await
            .map(|_| ())
    }

    async fn remove_network(&self, name: &str) -> EngineResult<()> {
        if !template::NETWORK_RE.is_match(name) {
            return Err(EngineError::new("Fremdes Netz wird nicht angefasst."));
        }
        let path = format!("/networks/{}", quote(name));
        let response = self
            .request(Method::GET, &format!("{path}/json"), &[], None, &[200, 404])
            .await?;
        if response.status == 404 {
            return Ok(());
        }
        if !is_own(&response.json()?["labels"]) {
            return Err(EngineError::new("Fremdes Netz wird nicht angefasst."));
        }
        self.request(Method::DELETE, &path, &[], None, &[200, 404])
            .await
            .map(|_| ())
    }

    async fn create_container(&self, body: &Value) -> EngineResult<()> {
        template::check_body(body, &self.bounds)?;
        // Nur das eben geprüfte Image (mit Digest, aus der Positivliste) bei Bedarf ziehen.
        if let Some(image) = body.get("image").and_then(Value::as_str) {
            self.ensure_image(image).await?;
        }
        self.request(Method::POST, "/containers/create", &[], Some(body), &[201])
            .await
            .map(|_| ())
    }

    async fn start_container(&self, name: &str) -> EngineResult<()> {
        self.request(
            Method::POST,
            &format!("/containers/{}/start", quote(name)),
            &[],
            None,
            &[204, 304],
        )
        .await
        .map(|_| ())
    }

    async fn remove_container(&self, name: &str) -> EngineResult<()> {
        if !template::NAME_RE.is_match(name) {
            return Err(EngineError::new("Fremder Container wird nicht angefasst."));
        }
        let path = format!("/containers/{}", quote(name));
        let response = self
            .request(Method::GET, &format!("{path}/json"), &[], None, &[200, 404])
            .await?;
        if response.status == 404 {
            return Ok(());
        }
        if !is_own(&response.json()?["Config"]["Labels"]) {
            return Err(EngineError::new("Fremder Container wird nicht angefasst."));
        }
        self.request(
            Method::DELETE,
            &path,
            &[
                ("force", "true".into()),
                ("v", "true".into()),
                ("timeout", "2".into()),
            ],
            None,
            &[200, 204, 404],
        )
        .await
        .map(|_| ())
    }

    async fn inspect_container(&self, name: &str) -> EngineResult<Value> {
        self.request(
            Method::GET,
            &format!("/containers/{}/json", quote(name)),
            &[],
            None,
            &[200],
        )
        .await?
        .json()
    }

    async fn list_containers(&self) -> EngineResult<Vec<ContainerInfo>> {
        let filters = json!({"label": [format!("{}=1", template::LABEL_RUNNER)]}).to_string();
        let data = self
            .request(
                Method::GET,
                "/containers/json",
                &[("all", "true".into()), ("filters", filters)],
                None,
                &[200],
            )
            .await?
            .json()?;
        let mut out = Vec::new();
        for item in data.as_array().into_iter().flatten() {
            let Some(first) = item["Names"].as_array().and_then(|n| n.first()) else {
                continue;
            };
            if !is_own(&item["Labels"]) {
                continue;
            }
            out.push(ContainerInfo {
                name: first
                    .as_str()
                    .unwrap_or("")
                    .trim_start_matches('/')
                    .to_string(),
                labels: labels_of(&item["Labels"]),
                running: item["State"] == "running",
            });
        }
        Ok(out)
    }

    async fn exec(
        &self,
        name: &str,
        argv: &[String],
        opts: ExecOptions,
    ) -> EngineResult<ExecResult> {
        let exec_id = self
            .exec_create(name, argv, opts.stdin.is_some(), false, &opts.workdir)
            .await?;
        let stream = self.hijack(&exec_id, false, 80, 24).await?;
        let mut out = Vec::new();
        let mut err = Vec::new();
        let mut truncated = false;
        let max = opts.max_output;
        let pump = async {
            if let Some(input) = opts.stdin.as_deref().filter(|i| !i.is_empty()) {
                stream.write(input).await?;
            }
            loop {
                let (kind, data) = stream.read_frame().await;
                if kind == 0 {
                    return Ok::<_, EngineError>(());
                }
                let target = if kind == 1 { &mut out } else { &mut err };
                let room = max.saturating_sub(target.len());
                if data.len() > room {
                    truncated = true;
                }
                target.extend_from_slice(&data[..data.len().min(room)]);
            }
        };
        let outcome = tokio::time::timeout(opts.timeout, pump).await;
        match outcome {
            Err(_) => {
                stream.close().await;
                return Ok(ExecResult {
                    code: None,
                    stdout: out,
                    stderr: err,
                    truncated,
                    timed_out: true,
                });
            }
            Ok(Err(e)) => {
                stream.close().await;
                return Err(e);
            }
            Ok(Ok(())) => {}
        }
        stream.close().await;
        let code = self.exit_code(&exec_id).await;
        Ok(ExecResult {
            code,
            stdout: out,
            stderr: err,
            truncated,
            timed_out: false,
        })
    }

    async fn exec_detached(&self, name: &str, argv: &[String], workdir: &str) -> EngineResult<()> {
        let exec_id = self.exec_create(name, argv, false, false, workdir).await?;
        self.request(
            Method::POST,
            &format!("/exec/{}/start", quote(&exec_id)),
            &[],
            Some(&json!({"Detach": true, "Tty": false})),
            &[200, 204],
        )
        .await
        .map(|_| ())
    }

    async fn exec_pty(
        &self,
        name: &str,
        argv: &[String],
        cols: u16,
        rows: u16,
        workdir: &str,
    ) -> EngineResult<Box<dyn Pty>> {
        let exec_id = self.exec_create(name, argv, true, true, workdir).await?;
        let stream = self.hijack(&exec_id, true, cols, rows).await?;
        Ok(Box::new(PodmanPty {
            engine: PodmanEngine::new(self.bounds.clone(), self.socket_path.clone()),
            exec_id,
            stream,
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn quote_encodes_everything_but_unreserved() {
        assert_eq!(quote("mgpt-ses-0123abcd.x_~"), "mgpt-ses-0123abcd.x_~");
        assert_eq!(quote("a/b c"), "a%2Fb%20c");
        assert_eq!(
            query(&[("filters", r#"{"label":["multigpt.runner=1"]}"#.into())]),
            "?filters=%7B%22label%22%3A%5B%22multigpt.runner%3D1%22%5D%7D"
        );
    }

    #[test]
    fn error_message_prefers_message_then_cause() {
        assert_eq!(
            message(500, br#"{"message":"kaputt","cause":"x"}"#),
            "Podman 500: kaputt"
        );
        assert_eq!(message(404, br#"{"cause":"fehlt"}"#), "Podman 404: fehlt");
        assert_eq!(message(400, b"roh"), "Podman 400: roh");
        assert_eq!(message(400, "x".repeat(400).as_bytes()).len(), 300 + 12);
    }

    #[tokio::test]
    async fn foreign_objects_are_never_touched() {
        // Kein Socket nötig: Namensmuster wird vor jedem Aufruf geprüft.
        let engine = PodmanEngine::new(Bounds::default(), "/nonexistent/podman.sock");
        assert!(engine.remove_container("n8n").await.is_err());
        assert!(engine.remove_network("podman").await.is_err());
        assert!(
            engine
                .create_container(&json!({"name": "n8n"}))
                .await
                .is_err()
        );
        assert!(
            engine
                .create_network(&json!({"name": "podman"}))
                .await
                .is_err()
        );
    }

    /// Kleiner libpod-Nachbau am Unix-Socket: prüft Pfade/Methoden und das Hijack-Protokoll.
    async fn fake_podman(dir: &std::path::Path) -> PathBuf {
        use tokio::net::UnixListener;
        let path = dir.join("podman.sock");
        let listener = UnixListener::bind(&path).unwrap();
        tokio::spawn(async move {
            loop {
                let Ok((stream, _)) = listener.accept().await else {
                    return;
                };
                tokio::spawn(async move {
                    let (read, mut write) = stream.into_split();
                    let mut reader = BufReader::new(read);
                    let mut head = String::new();
                    loop {
                        let mut line = String::new();
                        if reader.read_line(&mut line).await.unwrap_or(0) == 0 {
                            return;
                        }
                        let end = line == "\r\n";
                        head.push_str(&line);
                        if end {
                            break;
                        }
                    }
                    let length: usize = head
                        .lines()
                        .find_map(|l| {
                            l.to_ascii_lowercase()
                                .strip_prefix("content-length: ")
                                .map(|v| v.trim().parse().unwrap_or(0))
                        })
                        .unwrap_or(0);
                    let mut body = vec![0u8; length];
                    reader.read_exact(&mut body).await.unwrap();
                    let first = head.lines().next().unwrap().to_string();
                    let reply = |status: &str, body: &str| {
                        format!(
                            "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}",
                            body.len()
                        )
                    };
                    let text = if first.starts_with("GET /v5.0.0/libpod/info ") {
                        reply("200 OK", r#"{"version":{"Version":"5.4.2"},"host":{"security":{"rootless":true}}}"#)
                    } else if first.starts_with("GET /v5.0.0/libpod/containers/json?") {
                        assert!(first.contains("filters=%7B%22label%22"));
                        reply(
                            "200 OK",
                            r#"[{"Names":["/mgpt-ses-73475cb40a568e8d"],"Labels":{"multigpt.runner":"1","multigpt.role":"work"},"State":"running"},
                                {"Names":["/n8n"],"Labels":{},"State":"running"}]"#,
                        )
                    } else if first.starts_with("GET /v5.0.0/libpod/containers/mgpt-ses-73475cb40a568e8d/json ") {
                        reply("200 OK", r#"{"Config":{"Labels":{"multigpt.runner":"1"}}}"#)
                    } else if first.starts_with("GET /v5.0.0/libpod/containers/mgpt-ses-0000000000000000/json ") {
                        reply("200 OK", r#"{"Config":{"Labels":{}}}"#)
                    } else if first.starts_with("DELETE /v5.0.0/libpod/containers/mgpt-ses-73475cb40a568e8d?force=true&v=true&timeout=2 ") {
                        reply("200 OK", "[]")
                    } else if first.starts_with("POST /v5.0.0/libpod/containers/mgpt-ses-73475cb40a568e8d/exec ") {
                        let req: Value = serde_json::from_slice(&body).unwrap();
                        assert_eq!(req["User"], "1000:1000");
                        assert_eq!(req["Privileged"], false);
                        reply("201 Created", r#"{"Id":"e1"}"#)
                    } else if first.starts_with("POST /v5.0.0/libpod/exec/e1/start ") {
                        assert!(head.contains("Upgrade: tcp"));
                        let mut out = b"HTTP/1.1 101 UPGRADED\r\nContent-Type: application/vnd.docker.multiplexed-stream\r\nConnection: Upgrade\r\nUpgrade: tcp\r\n\r\n".to_vec();
                        write.write_all(&out).await.unwrap();
                        out.clear();
                        // stdin lesen (eine Zeile), dann stdout und stderr als Rahmen.
                        let mut line = String::new();
                        reader.read_line(&mut line).await.unwrap();
                        for (kind, data) in [(1u8, format!("echo:{line}")), (2u8, "warn".into())] {
                            out.push(kind);
                            out.extend_from_slice(&[0, 0, 0]);
                            out.extend_from_slice(&(data.len() as u32).to_be_bytes());
                            out.extend_from_slice(data.as_bytes());
                        }
                        write.write_all(&out).await.unwrap();
                        return;
                    } else if first.starts_with("GET /v5.0.0/libpod/exec/e1/json ") {
                        reply("200 OK", r#"{"Running":false,"ExitCode":3}"#)
                    } else {
                        reply("500 Internal Server Error", r#"{"message":"unerwartet"}"#)
                    };
                    let _ = write.write_all(text.as_bytes()).await;
                });
            }
        });
        path
    }

    #[tokio::test]
    async fn rest_calls_and_hijacked_exec() {
        let dir =
            std::env::temp_dir().join(format!("mgpt-podman-test-{:016x}", rand::random::<u64>()));
        std::fs::create_dir_all(&dir).unwrap();
        let socket = fake_podman(&dir).await;
        let engine = PodmanEngine::new(Bounds::default(), &socket);
        let info = engine.info().await.unwrap();
        assert_eq!(
            info,
            EngineInfo {
                name: "podman".into(),
                version: "5.4.2".into(),
                rootless: true
            }
        );
        let list = engine.list_containers().await.unwrap();
        assert_eq!(list.len(), 1);
        assert_eq!(list[0].name, "mgpt-ses-73475cb40a568e8d");
        assert!(list[0].running);
        engine
            .remove_container("mgpt-ses-73475cb40a568e8d")
            .await
            .unwrap();
        // Eigenes Namensmuster, aber ohne Label: wird nicht gelöscht.
        assert!(
            engine
                .remove_container("mgpt-ses-0000000000000000")
                .await
                .is_err()
        );
        let result = engine
            .exec(
                "mgpt-ses-73475cb40a568e8d",
                &["mgpt-fs".into()],
                ExecOptions {
                    stdin: Some(b"hallo\n".to_vec()),
                    ..ExecOptions::default()
                },
            )
            .await
            .unwrap();
        assert_eq!(result.stdout, b"echo:hallo\n");
        assert_eq!(result.stderr, b"warn");
        assert_eq!(result.code, Some(3));
        assert!(!result.truncated && !result.timed_out);
        let err = engine.inspect_container("unbekannt").await.unwrap_err();
        assert!(err.0.starts_with("Podman 500: unerwartet"), "{err}");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
