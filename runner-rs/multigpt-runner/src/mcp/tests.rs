//! Tests der Werkzeuge und des MCP-Servers mit einer Test-Sitzung (`FakeHost`).
//!
//! Wie die FakeEngine des Python-Agents laufen Befehle als lokale Prozesse in
//! eigenen Verzeichnissen: `mgpt-fs` ist das echte `runner_agent/mgpt_fs.py`
//! (mit `MGPT_WORKSPACE`/`MGPT_SCRATCH`), `bash` läuft lokal, `ssh`/`telnet`
//! werden durch Skripte ersetzt. Rahmen an den Server landen in einem Mitschnitt.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use base64::Engine as _;
use serde_json::{Map, Value, json};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::process::{Child, ChildStdin, ChildStdout, Command};

use super::host::*;
use super::setup;
use super::tools::{self, SessionTools, clean_output, split_chunks, summary};
use super::{CATALOG_JSON, McpRouter};

const MGPT_FS: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../../runner_agent/mgpt_fs.py");

/// `mgpt-fs` des Arbeits-Images (Umgebung `MGPT_FS_PY` geht vor).
fn mgpt_fs() -> PathBuf {
    std::env::var_os("MGPT_FS_PY").map_or_else(|| PathBuf::from(MGPT_FS), PathBuf::from)
}
const SECRET: &str =
    "-----BEGIN OPENSSH PRIVATE KEY-----\ngeheim-nur-im-test\n-----END OPENSSH PRIVATE KEY-----";
const TOKEN: &str = "mgrt_abcdefghij_0123456789abcdefghijABCDEFGHIJ0123456789xyz";
const SERVER_KEY: &str = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=";

// --- Test-Sitzung --------------------------------------------------------------------

struct FakePty {
    stdout: tokio::sync::Mutex<ChildStdout>,
    stdin: tokio::sync::Mutex<Option<ChildStdin>>,
    child: tokio::sync::Mutex<Child>,
    id: String,
    open: Arc<Mutex<Vec<String>>>,
}

impl ToolPty for FakePty {
    async fn read(&self) -> Option<Vec<u8>> {
        let mut buffer = vec![0u8; 4096];
        match self.stdout.lock().await.read(&mut buffer).await {
            Ok(0) | Err(_) => None,
            Ok(n) => {
                buffer.truncate(n);
                Some(buffer)
            }
        }
    }

    async fn write(&self, data: &[u8]) -> ToolResult<()> {
        let mut stdin = self.stdin.lock().await;
        let pipe = stdin
            .as_mut()
            .ok_or_else(|| ToolError::new("PTY geschlossen"))?;
        pipe.write_all(data)
            .await
            .map_err(|e| ToolError::new(e.to_string()))?;
        pipe.flush()
            .await
            .map_err(|e| ToolError::new(e.to_string()))
    }

    async fn wait(&self) -> Option<i32> {
        self.child
            .lock()
            .await
            .wait()
            .await
            .ok()
            .and_then(|s| s.code())
    }

    async fn close(&self) {
        self.stdin.lock().await.take();
        let _ = self.child.lock().await.start_kill();
        self.open.lock().unwrap().retain(|id| id != &self.id);
    }
}

#[derive(Default)]
struct Recorded {
    frames: Vec<(String, Value)>,
    saves: Vec<String>,
    execs: Vec<(String, Vec<String>)>,
    helpers_opened: Vec<String>,
    helpers_closed: Vec<String>,
    apt: Vec<Vec<String>>,
}

struct FakeHost {
    sid: String,
    root: PathBuf,
    limits: ToolLimits,
    relay: Option<Relay>,
    allowed: Option<Vec<String>>,
    scratch_mb: Option<u64>,
    scratch_used: Mutex<u64>,
    fake_commands: HashMap<String, String>,
    downloads: HashMap<String, Vec<u8>>,
    lock: Arc<tokio::sync::Mutex<()>>,
    rec: Mutex<Recorded>,
    open_ptys: Arc<Mutex<Vec<String>>>,
    helper_counter: AtomicUsize,
    packages: bool,
    rootfs_full: bool,
}

impl FakeHost {
    fn new(root: &Path) -> Self {
        std::fs::create_dir_all(root.join("workspace")).unwrap();
        std::fs::create_dir_all(root.join("scratch")).unwrap();
        std::fs::create_dir_all(root.join("tmp")).unwrap();
        Self {
            sid: "s1".into(),
            root: root.to_path_buf(),
            limits: ToolLimits {
                command_seconds: 60,
                output_bytes: 4096,
                tmpfs_mb: 64,
            },
            relay: None,
            allowed: None,
            scratch_mb: None,
            scratch_used: Mutex::new(0),
            fake_commands: HashMap::new(),
            downloads: HashMap::new(),
            lock: Arc::new(tokio::sync::Mutex::new(())),
            rec: Mutex::new(Recorded::default()),
            open_ptys: Arc::new(Mutex::new(Vec::new())),
            helper_counter: AtomicUsize::new(0),
            packages: false,
            rootfs_full: false,
        }
    }

    fn workspace(&self) -> PathBuf {
        self.root.join("workspace")
    }

    fn frames(&self, kind: &str) -> Vec<Value> {
        let rec = self.rec.lock().unwrap();
        rec.frames
            .iter()
            .filter(|(k, _)| k == kind)
            .map(|(_, v)| v.clone())
            .collect()
    }

    fn helper_dir(&self, name: &str) -> PathBuf {
        self.root.join("helpers").join(name)
    }

    /// argv lokal ausführbar machen: Ersatzskripte, /tmp des Hilfscontainers.
    fn local_argv(&self, target: Target<'_>, argv: Vec<String>) -> (Vec<String>, PathBuf) {
        let (dir, tmp) = match target {
            Target::Work => (self.workspace(), self.root.join("tmp")),
            Target::Helper(name) => (self.helper_dir(name), self.helper_dir(name)),
        };
        let tmp = tmp.to_string_lossy().to_string();
        let mut out: Vec<String> = argv
            .into_iter()
            .map(|a| a.replace("/tmp/", &format!("{tmp}/")))
            .collect();
        if let Some(pos) = out.iter().position(|a| self.fake_commands.contains_key(a)) {
            let script = self.fake_commands[&out[pos]].clone();
            let rest: Vec<String> = out.split_off(pos + 1);
            out.pop();
            out.extend(["sh".into(), "-c".into(), script, "fake".into()]);
            out.extend(rest);
        }
        (out, dir)
    }

    async fn run_fs(&self, op: &str, fields: Map<String, Value>) -> ToolResult<(Value, Vec<u8>)> {
        let mut request = fields;
        request.insert("op".into(), op.into());
        let mut child = Command::new("python3")
            .arg("-I")
            .arg(mgpt_fs())
            .env("MGPT_WORKSPACE", self.workspace())
            .env("MGPT_SCRATCH", self.root.join("scratch"))
            .env("MGPT_TMP", self.root.join("tmp"))
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .expect("python3");
        let mut line = serde_json::to_vec(&Value::Object(request)).unwrap();
        line.push(b'\n');
        child.stdin.take().unwrap().write_all(&line).await.unwrap();
        let out = child.wait_with_output().await.unwrap();
        let split = out
            .stdout
            .iter()
            .position(|b| *b == b'\n')
            .unwrap_or(out.stdout.len());
        let result: Value = serde_json::from_slice(&out.stdout[..split])
            .unwrap_or_else(|_| panic!("mgpt-fs: {}", String::from_utf8_lossy(&out.stderr)));
        if result["ok"] != json!(true) {
            return Err(ToolError::new(
                result["error"].as_str().unwrap_or("Fehler in mgpt-fs."),
            ));
        }
        let rest = out.stdout.get(split + 1..).unwrap_or_default().to_vec();
        Ok((result, rest))
    }
}

impl ToolHost for FakeHost {
    type Pty = FakePty;

    fn session_id(&self) -> String {
        self.sid.clone()
    }

    fn limits(&self) -> ToolLimits {
        self.limits.clone()
    }

    fn lock(&self) -> Arc<tokio::sync::Mutex<()>> {
        self.lock.clone()
    }

    fn touch(&self) {}

    async fn fs(&self, op: &str, mut fields: Map<String, Value>) -> ToolResult<Value> {
        if matches!(op, "write" | "edit")
            && let Some(mb) = self.scratch_mb
        {
            fields.insert("scratch_limit".into(), (mb << 20).into());
        }
        Ok(self.run_fs(op, fields).await?.0)
    }

    async fn fs_raw(&self, op: &str, fields: Map<String, Value>) -> ToolResult<(Value, Vec<u8>)> {
        self.run_fs(op, fields).await
    }

    async fn save(&self, message: &str) -> String {
        match self
            .run_fs(
                "commit",
                json!({"message": message}).as_object().unwrap().clone(),
            )
            .await
        {
            Ok((result, _)) if result["changed"] == json!(true) => {
                self.rec.lock().unwrap().saves.push(message.to_string());
                String::new()
            }
            Ok(_) => String::new(),
            Err(err) => format!("\n[Arbeitsstand nicht gespeichert: {err}]"),
        }
    }

    async fn scratch_full(&self) -> bool {
        self.scratch_mb
            .is_some_and(|mb| *self.scratch_used.lock().unwrap() >= mb << 20)
    }

    async fn scratch_note(&self) -> String {
        if self.scratch_full().await {
            "\n[Scratchpad voll]".into()
        } else {
            String::new()
        }
    }

    fn scratch_size_mb(&self) -> Option<u64> {
        self.scratch_mb
    }

    async fn scratch_rest(&self) -> Option<u64> {
        self.scratch_mb
            .map(|mb| (mb << 20).saturating_sub(*self.scratch_used.lock().unwrap()))
    }

    async fn exec_pty(
        &self,
        pty_id: &str,
        target: Target<'_>,
        argv: Vec<String>,
    ) -> ToolResult<FakePty> {
        let (argv, dir) = self.local_argv(target, argv);
        self.rec.lock().unwrap().execs.push((
            match target {
                Target::Work => "work".into(),
                Target::Helper(n) => n.to_string(),
            },
            argv.clone(),
        ));
        let mut child = Command::new(&argv[0])
            .args(&argv[1..])
            .current_dir(dir)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .kill_on_drop(true)
            .spawn()
            .map_err(|e| ToolError::new(format!("Container-Fehler: {e}")))?;
        self.open_ptys.lock().unwrap().push(pty_id.to_string());
        Ok(FakePty {
            stdout: tokio::sync::Mutex::new(child.stdout.take().unwrap()),
            stdin: tokio::sync::Mutex::new(child.stdin.take()),
            child: tokio::sync::Mutex::new(child),
            id: pty_id.to_string(),
            open: self.open_ptys.clone(),
        })
    }

    async fn exec(
        &self,
        target: Target<'_>,
        argv: Vec<String>,
        stdin: Vec<u8>,
        timeout_s: u64,
        _max_output: usize,
    ) -> ToolResult<ExecOutput> {
        let (argv, dir) = self.local_argv(target, argv);
        let name = match target {
            Target::Work => "work".to_string(),
            Target::Helper(n) => n.to_string(),
        };
        self.rec.lock().unwrap().execs.push((name, argv.clone()));
        let mut child = Command::new(&argv[0])
            .args(&argv[1..])
            .current_dir(dir)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|e| ToolError::new(format!("Container-Fehler: {e}")))?;
        let mut pipe = child.stdin.take().unwrap();
        tokio::spawn(async move {
            let _ = pipe.write_all(&stdin).await;
        });
        let out = tokio::time::timeout(Duration::from_secs(timeout_s), child.wait_with_output())
            .await
            .map_err(|_| ToolError::new("Zeitlimit"))?
            .map_err(|e| ToolError::new(e.to_string()))?;
        Ok(ExecOutput {
            code: out.status.code(),
            stdout: out.stdout,
            stderr: out.stderr,
            timed_out: false,
        })
    }

    async fn start_preview(&self, path: Option<&str>) -> ToolResult<Value> {
        self.fs(
            "glob",
            json!({"pattern": "*", "path": path.unwrap_or(".")})
                .as_object()
                .unwrap()
                .clone(),
        )
        .await?;
        Ok(json!({"url": "http://127.0.0.1:24080/", "port": 24080, "token": "tok"}))
    }

    async fn open_helper(&self) -> ToolResult<String> {
        let n = self.helper_counter.fetch_add(1, Ordering::SeqCst) + 1;
        let name = format!("mgpt-tol-s1-{n}");
        std::fs::create_dir_all(self.helper_dir(&name)).unwrap();
        self.rec.lock().unwrap().helpers_opened.push(name.clone());
        Ok(name)
    }

    async fn close_helper(&self, name: &str) {
        let _ = std::fs::remove_dir_all(self.helper_dir(name));
        self.rec
            .lock()
            .unwrap()
            .helpers_closed
            .push(name.to_string());
    }

    fn relay(&self) -> Option<Relay> {
        self.relay.clone()
    }

    async fn send(&self, kind: &str, fields: Value) -> ToolResult<()> {
        let Value::Object(map) = fields else {
            panic!("Rahmen ohne Objekt");
        };
        let map: Map<String, Value> = map.into_iter().filter(|(_, v)| !v.is_null()).collect();
        self.rec
            .lock()
            .unwrap()
            .frames
            .push((kind.to_string(), Value::Object(map)));
        Ok(())
    }

    fn chunk_bytes(&self) -> usize {
        4
    }

    fn server_url(&self) -> String {
        "https://mgpt.example".into()
    }

    async fn download(&self, url: &str, bearer: &str, max_bytes: usize) -> ToolResult<Vec<u8>> {
        assert_eq!(bearer, TOKEN);
        let data = self
            .downloads
            .get(url)
            .cloned()
            .ok_or_else(|| ToolError::new("nicht gefunden"))?;
        assert!(data.len() <= max_bytes);
        Ok(data)
    }

    fn allowed_services(&self) -> Option<Vec<String>> {
        self.allowed.clone()
    }

    fn packages(&self) -> bool {
        self.packages
    }

    fn rootfs_full(&self) -> bool {
        self.rootfs_full
    }

    async fn apt_install(&self, packages: Vec<String>) -> ToolResult<AptOutput> {
        self.rec.lock().unwrap().apt.push(packages.clone());
        // Wie die Sitzung: Namen im Arbeitsstand vermerken (Commit macht das Werkzeug).
        let dir = self.root.join("workspace/.mgpt");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("apt-packages"), packages.join("\n") + "\n").unwrap();
        Ok(AptOutput {
            code: Some(if packages.iter().any(|p| p == "fehlt") {
                100
            } else {
                0
            }),
            timed_out: false,
            output: format!(
                "$ apt-get install\n\x1b[1mSetting up\x1b[0m {}\r\n",
                packages.join(" ")
            ),
        })
    }
}

fn setup_dir() -> (tempfile::TempDir, FakeHost) {
    let dir = tempfile::tempdir().unwrap();
    let host = FakeHost::new(dir.path());
    (dir, host)
}

async fn init(host: &FakeHost) {
    host.run_fs("init", Map::new()).await.unwrap();
}

async fn call(
    tools: &SessionTools<FakeHost>,
    name: &str,
    args: Value,
) -> ToolResult<(String, Option<Value>)> {
    tokio::time::timeout(Duration::from_secs(60), tools.call(name, &args))
        .await
        .expect("Zeitlimit des Tests")
}

fn decode(frames: &[Value]) -> Vec<u8> {
    frames
        .iter()
        .flat_map(|f| {
            base64::engine::general_purpose::STANDARD
                .decode(f["data"].as_str().unwrap())
                .unwrap()
        })
        .collect()
}

fn allowlisted(mut host: FakeHost, port: u16) -> FakeHost {
    host.relay = Some(Relay {
        ip: "10.89.7.2".into(),
        targets: vec![("192.168.1.20".into(), port)],
    });
    host
}

fn nas(port: u16) -> Value {
    json!({
        "name": "nas",
        "address": "192.168.1.20",
        "port": port,
        "user": "admin",
        "host_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI",
        "secret_type": "key",
        "secret": SECRET,
    })
}

// --- Hilfsfunktionen -----------------------------------------------------------------

#[test]
fn clean_output_removes_ansi_and_carriage_returns() {
    let raw = b"\x1b[1;32mgr\xc3\xbcn\x1b[0m\r\nzeile\rneu\x1b]0;titel\x07!";
    assert_eq!(clean_output(raw), "grün\nzeile\nneu!");
}

#[test]
fn summary_is_short_and_without_content() {
    let args = json!({"path": "app.py", "content": "geheim"});
    assert_eq!(summary("write", args.as_object().unwrap()), "write app.py");
    let args = json!({"command": "echo   hallo\n  welt"});
    assert_eq!(
        summary("bash", args.as_object().unwrap()),
        "bash: echo hallo welt"
    );
    let long = json!({"command": "x".repeat(400)});
    assert_eq!(
        summary("bash", long.as_object().unwrap()).chars().count(),
        156
    );
}

#[test]
fn chunks_like_split_bundle() {
    assert_eq!(split_chunks(b"", 4), vec![(0, &b""[..], true)]);
    let parts = split_chunks(b"0123456789", 4);
    assert_eq!(parts.len(), 3);
    assert!(parts[2].2 && !parts[1].2 && parts[2].1 == b"89");
}

#[test]
fn arguments_are_checked_like_python() {
    let args = json!({"a": true, "b": 1.0, "c": 5, "d": ""});
    let args = args.as_object().unwrap();
    assert!(
        tools::arg_int(args, "a", 1, 10).is_err(),
        "bool ist keine Zahl"
    );
    assert!(
        tools::arg_int(args, "b", 1, 10).is_err(),
        "1.0 ist keine Ganzzahl"
    );
    assert_eq!(tools::arg_int(args, "c", 1, 10).unwrap(), Some(5));
    assert!(tools::arg_int(args, "c", 6, 10).is_err());
    assert!(tools::arg_str(args, "d", false).is_err());
    assert_eq!(tools::arg_str(args, "d", true).unwrap(), "");
    assert!(tools::arg_str(args, "fehlt", true).is_err());
}

#[test]
fn catalog_lists_exactly_the_agent_tools() {
    let catalog: Value = serde_json::from_str(CATALOG_JSON).unwrap();
    assert_eq!(catalog["format"], 1);
    let mut names: Vec<&str> = catalog["services"]
        .as_array()
        .unwrap()
        .iter()
        .map(|s| s["name"].as_str().unwrap())
        .collect();
    names.sort_unstable();
    let mut tools = tools::tool_names();
    tools.sort_unstable();
    assert_eq!(names, tools);
    for service in catalog["services"].as_array().unwrap() {
        let kind = service["kind"].as_str().unwrap();
        assert!(["read", "edit", "command", "publish", "remote", "setup"].contains(&kind));
        assert_eq!(service["input_schema"]["type"], "object");
        assert!(!service["description"].as_str().unwrap().is_empty());
    }
    // Das Modell nennt nur den Hostnamen; Zugang und Kopplung setzt der Server ein.
    let setup = catalog["services"]
        .as_array()
        .unwrap()
        .iter()
        .find(|s| s["name"] == setup::NAME)
        .unwrap();
    assert_eq!(setup["kind"], "setup");
    assert_eq!(setup["input_schema"]["required"], json!(["host"]));
    assert!(!CATALOG_JSON.contains("\"ssh\""));
}

// --- Dateien -------------------------------------------------------------------------

#[tokio::test]
async fn file_tools_commit_only_after_changes() {
    let (_dir, host) = setup_dir();
    init(&host).await;
    let tools = SessionTools::new(Arc::new(host));
    let (text, _) = call(
        &tools,
        "write",
        json!({"path": "app.py", "content": "print('hallo')\n"}),
    )
    .await
    .unwrap();
    assert_eq!(text, "app.py angelegt (15 Byte).");
    let (text, _) = call(&tools, "read", json!({"path": "app.py"}))
        .await
        .unwrap();
    assert_eq!(text, "     1\tprint('hallo')");
    let (text, _) = call(
        &tools,
        "edit",
        json!({"path": "app.py", "old_string": "hallo", "new_string": "welt"}),
    )
    .await
    .unwrap();
    assert!(text.contains("1 Stelle"), "{text}");
    let (text, _) = call(&tools, "glob", json!({"pattern": "*.py"}))
        .await
        .unwrap();
    assert_eq!(text, "app.py");
    let (text, _) = call(&tools, "grep", json!({"pattern": "welt"}))
        .await
        .unwrap();
    assert_eq!(text, "app.py:1:print('welt')");
    let (text, _) = call(&tools, "grep", json!({"pattern": "fehlt"}))
        .await
        .unwrap();
    assert_eq!(text, "Keine Treffer.");
    // Lesen und Suchen ändern nichts: genau zwei Commits.
    assert_eq!(
        tools.host.rec.lock().unwrap().saves,
        vec!["write app.py", "edit app.py"]
    );
}

#[tokio::test]
async fn read_with_range_and_empty_file() {
    let (_dir, host) = setup_dir();
    init(&host).await;
    let tools = SessionTools::new(Arc::new(host));
    let content: String = (1..=10).map(|i| format!("z{i}\n")).collect();
    call(
        &tools,
        "write",
        json!({"path": "a.txt", "content": content}),
    )
    .await
    .unwrap();
    let (text, _) = call(
        &tools,
        "read",
        json!({"path": "a.txt", "offset": 3, "limit": 2}),
    )
    .await
    .unwrap();
    assert_eq!(text, "     3\tz3\n     4\tz4\n[Zeilen 3–4 von 10]");
    call(&tools, "write", json!({"path": "leer.txt", "content": ""}))
        .await
        .unwrap();
    let (text, _) = call(&tools, "read", json!({"path": "leer.txt"}))
        .await
        .unwrap();
    assert!(text.starts_with("(leere Datei"), "{text}");
    let err = call(&tools, "read", json!({"path": "a.txt", "offset": 0}))
        .await
        .unwrap_err();
    assert!(err.message.contains("zwischen 1 und"), "{err}");
}

#[tokio::test]
async fn edit_needs_unique_match() {
    let (_dir, host) = setup_dir();
    init(&host).await;
    let tools = SessionTools::new(Arc::new(host));
    call(
        &tools,
        "write",
        json!({"path": "b.txt", "content": "x x x"}),
    )
    .await
    .unwrap();
    let err = call(
        &tools,
        "edit",
        json!({"path": "b.txt", "old_string": "x", "new_string": "y"}),
    )
    .await
    .unwrap_err();
    assert!(err.message.contains("3"), "{err}");
    let (text, _) = call(
        &tools,
        "edit",
        json!({"path": "b.txt", "old_string": "x", "new_string": "y", "replace_all": true}),
    )
    .await
    .unwrap();
    assert!(text.contains("3 Stelle"), "{text}");
}

#[tokio::test]
async fn paths_outside_workspace_are_tool_errors() {
    let (_dir, host) = setup_dir();
    init(&host).await;
    let tools = SessionTools::new(Arc::new(host));
    for (name, args) in [
        ("write", json!({"path": "/etc/passwd", "content": "x"})),
        ("read", json!({"path": "../../etc/passwd"})),
    ] {
        let err = call(&tools, name, args).await.unwrap_err();
        assert!(err.message.contains("/workspace"), "{err}");
    }
    let err = call(&tools, "read", json!({"path": "/scratch/../etc/passwd"}))
        .await
        .unwrap_err();
    assert!(
        err.message.contains("/scratch") || err.message.contains("/workspace"),
        "{err}"
    );
    assert!(tools.host.rec.lock().unwrap().saves.is_empty());
}

#[tokio::test]
async fn unknown_tool_bad_arguments_and_services_filter() {
    let (_dir, mut host) = setup_dir();
    host.allowed = Some(vec!["read".into(), "glob".into()]);
    let tools = SessionTools::new(Arc::new(host));
    let err = call(&tools, "ssh", json!({})).await.unwrap_err();
    assert_eq!(err.message, "Unbekanntes Werkzeug: ssh");
    let err = call(&tools, "write", json!({"path": "a", "content": "b"}))
        .await
        .unwrap_err();
    assert!(err.message.contains("nicht freigegeben"), "{err}");
    let err = call(&tools, "read", json!(["path"])).await.unwrap_err();
    assert_eq!(err.message, "Argumente müssen ein Objekt sein.");
    let err = call(&tools, "read", json!({"path": 5})).await.unwrap_err();
    assert!(err.message.contains("„path“"), "{err}");
}

#[tokio::test]
async fn local_scratch_over_limit_blocks_write() {
    let (_dir, mut host) = setup_dir();
    host.scratch_mb = Some(1);
    *host.scratch_used.lock().unwrap() = 2 << 20;
    let tools = SessionTools::new(Arc::new(host));
    let err = call(
        &tools,
        "write",
        json!({"path": "/scratch/a", "content": "x"}),
    )
    .await
    .unwrap_err();
    assert!(
        err.message.contains("Scratchpad über der Grenze von 1 MB"),
        "{err}"
    );
    // bash darf weiter (Löschen), aber mit ulimit -f 0
    let (text, _) = call(&tools, "bash", json!({"command": "ulimit -f"}))
        .await
        .unwrap();
    assert!(text.starts_with("0\n"), "{text}");
}

#[tokio::test]
async fn scratch_files_and_publish() {
    let (_dir, host) = setup_dir();
    init(&host).await;
    let tools = SessionTools::new(Arc::new(host));
    let (text, _) = call(
        &tools,
        "write",
        json!({"path": "/scratch/report.txt", "content": "Ergebnis\n"}),
    )
    .await
    .unwrap();
    assert!(text.starts_with("/scratch/report.txt angelegt"), "{text}");
    // /scratch gehört nicht in git: kein Commit
    assert!(tools.host.rec.lock().unwrap().saves.is_empty());
    let (text, _) = call(&tools, "glob", json!({"pattern": "*", "path": "/scratch"}))
        .await
        .unwrap();
    assert_eq!(text, "/scratch/report.txt");
    let (text, info) = call(
        &tools,
        "publish",
        json!({"path": "/scratch/report.txt", "transfer": "tr-1", "max_bytes": 1000}),
    )
    .await
    .unwrap();
    assert!(
        text.contains("als „report.txt“ übertragen (9 Byte)"),
        "{text}"
    );
    assert_eq!(
        info.unwrap(),
        json!({"transfer": "tr-1", "size": 9, "sha256": setup::hex_sha256(b"Ergebnis\n"), "name": "report.txt"})
    );
    let chunks = tools.host.frames("file_chunk");
    assert_eq!(chunks.len(), 3, "Stücke zu 4 Byte");
    let last = chunks.last().unwrap();
    assert_eq!(last["last"], true);
    assert_eq!(last["name"], "report.txt");
    assert_eq!(last["mime"], "text/plain");
    assert_eq!(last["size"], 9);
    assert!(chunks[0].get("size").is_none() && chunks[0].get("name").is_none());
    assert_eq!(decode(&chunks), b"Ergebnis\n");
    let err = call(
        &tools,
        "publish",
        json!({"path": "/scratch/report.txt", "transfer": "tr-2", "max_bytes": 3}),
    )
    .await
    .unwrap_err();
    assert!(err.message.contains("größer"), "{err}");
    let err = call(
        &tools,
        "publish",
        json!({"path": "/scratch/report.txt", "transfer": "../x"}),
    )
    .await
    .unwrap_err();
    assert_eq!(err.message, "Übertragungs-ID ungültig.");
    // Anzeigename ohne Pfadtrenner
    let (_, info) = call(
        &tools,
        "publish",
        json!({"path": "/scratch/report.txt", "transfer": "tr-3", "name": " a/b\\c "}),
    )
    .await
    .unwrap();
    assert_eq!(info.unwrap()["name"], "a_b_c");
}

// --- Befehle -------------------------------------------------------------------------

#[tokio::test]
async fn bash_streams_pty_and_commits() {
    let (_dir, host) = setup_dir();
    init(&host).await;
    let tools = SessionTools::new(Arc::new(host));
    let (text, structured) = call(
        &tools,
        "bash",
        json!({"command": "echo hallo > out.txt; echo fertig"}),
    )
    .await
    .unwrap();
    assert!(
        text.contains("fertig") && text.ends_with("[Exit-Code 0]"),
        "{text}"
    );
    assert_eq!(
        structured.unwrap(),
        json!({"exit_code": 0, "truncated": false})
    );
    let data: Vec<Value> = tools
        .host
        .frames("pty_data")
        .into_iter()
        .filter(|f| f["pty"] == "run-1")
        .collect();
    assert!(String::from_utf8_lossy(&decode(&data)).contains("fertig"));
    assert_eq!(
        tools.host.frames("pty_exit").last().unwrap(),
        &json!({"session": "s1", "pty": "run-1", "code": 0})
    );
    assert!(tools.host.rec.lock().unwrap().saves[0].starts_with("bash: echo hallo"));
    let (_, structured) = call(&tools, "bash", json!({"command": "exit 3"}))
        .await
        .unwrap();
    assert_eq!(structured.unwrap()["exit_code"], 3);
    let (text, _) = call(&tools, "bash", json!({"command": "true"}))
        .await
        .unwrap();
    assert_eq!(text, "(keine Ausgabe)\n[Exit-Code 0]");
    // Hülle: timeout -k 5 <s> bash --noprofile --norc -c …
    let rec = tools.host.rec.lock().unwrap();
    assert_eq!(
        &rec.execs[0].1[..6],
        ["timeout", "-k", "5", "60", "bash", "--noprofile"]
    );
    assert!(
        tools.host.open_ptys.lock().unwrap().is_empty(),
        "PTY abgemeldet"
    );
}

#[tokio::test]
async fn bash_timeout_and_output_limit() {
    let (_dir, mut host) = setup_dir();
    host.limits.output_bytes = 1024;
    host.limits.command_seconds = 2;
    let tools = SessionTools::new(Arc::new(host));
    let (text, structured) = call(
        &tools,
        "bash",
        json!({"command": "sleep 30", "timeout_s": 600}),
    )
    .await
    .unwrap();
    assert!(text.contains("Zeitlimit von 2 s"), "{text}");
    assert_eq!(structured.unwrap()["exit_code"], 124);
    let (text, structured) = call(&tools, "bash", json!({"command": "yes x | head -c 50000"}))
        .await
        .unwrap();
    assert_eq!(structured.unwrap()["truncated"], true);
    assert!(
        text.contains("gekürzt") && text.len() < 2000,
        "{}",
        text.len()
    );
    let err = call(&tools, "bash", json!({"command": "true", "timeout_s": 601}))
        .await
        .unwrap_err();
    assert!(err.message.contains("zwischen 1 und 600"), "{err}");
}

#[tokio::test]
async fn serve_reports_preview_and_checks_directory() {
    let (_dir, host) = setup_dir();
    let tools = SessionTools::new(Arc::new(host));
    let (text, info) = call(&tools, "serve", json!({})).await.unwrap();
    assert!(
        text.starts_with("Vorschau läuft: http://127.0.0.1:24080/"),
        "{text}"
    );
    assert_eq!(info.unwrap()["port"], 24080);
    let err = call(&tools, "serve", json!({"path": "/etc"}))
        .await
        .unwrap_err();
    assert!(err.message.contains("/workspace"), "{err}");
}

// --- telnet und Einrichtung ------------------------------------------------------------

#[tokio::test]
async fn remote_tools_need_allowlist_and_host_key() {
    let (_dir, host) = setup_dir();
    let tools = SessionTools::new(Arc::new(host));
    let err = call(&tools, "telnet", json!({"host": nas(23), "input": "ls"}))
        .await
        .unwrap_err();
    assert!(err.message.contains("kein Netz"), "{err}");
    let (_dir, host) = setup_dir();
    let tools = SessionTools::new(Arc::new(allowlisted(host, 22)));
    let err = call(&tools, "telnet", json!({"host": nas(23), "input": "ls"}))
        .await
        .unwrap_err();
    assert!(err.message.contains("Positivliste"), "{err}");
    let err = call(&tools, "setup_host", json!({"host": "nas"}))
        .await
        .unwrap_err();
    assert!(err.message.contains("Host fehlt"), "{err}");
    let mut no_key = nas(22);
    no_key["host_key"] = json!("");
    let err = call(&tools, "setup_host", json!({"host": no_key}))
        .await
        .unwrap_err();
    assert!(err.message.contains("Host-Schlüssel"), "{err}");
    let mut password = nas(22);
    password["secret_type"] = json!("password");
    let err = call(&tools, "setup_host", json!({"host": password}))
        .await
        .unwrap_err();
    assert!(err.message.contains("nur mit SSH-Schlüssel"), "{err}");
    let err = call(&tools, "setup_host", json!({"host": nas(22)}))
        .await
        .unwrap_err();
    assert!(err.message.contains("Kopplungsdaten fehlen"), "{err}");
    assert!(tools.host.rec.lock().unwrap().helpers_opened.is_empty());
}

#[tokio::test]
async fn telnet_login_in_helper_container_and_password_masked() {
    let script = "printf 'login: '; read u; printf 'Password: '; read p; \
                  echo \"echo:$p\"; echo \"welcome $u\"; while read c; do echo \"out:$c\"; done";
    let (_dir, mut host) = setup_dir();
    host.fake_commands.insert("telnet".into(), script.into());
    let host = Arc::new(allowlisted(host, 23));
    let router = McpRouter::new();
    let tools = router.tools(&host);
    let mut target = nas(23);
    target["secret_type"] = json!("password");
    target["secret"] = json!("pw-geheim");
    let (text, _) = call(
        &tools,
        "telnet",
        json!({"host": target, "input": "show version"}),
    )
    .await
    .unwrap();
    assert!(
        text.contains("welcome admin") && text.contains("out:show version"),
        "{text}"
    );
    assert!(
        !text.contains("pw-geheim") && text.contains("echo:****"),
        "{text}"
    );
    let (text, _) = call(&tools, "telnet", json!({"host": target, "input": "weiter"}))
        .await
        .unwrap();
    assert_eq!(text, "out:weiter");
    {
        let rec = host.rec.lock().unwrap();
        assert_eq!(rec.helpers_opened.len(), 1, "Verbindung bleibt offen");
        assert!(
            rec.execs.iter().all(|(target, _)| target != "work"),
            "nie im Arbeitscontainer"
        );
        let telnet = &rec.execs[0].1;
        assert!(
            telnet.ends_with(&["fake".to_string(), "10.89.7.2".into(), "20000".into()]),
            "{telnet:?}"
        );
    }
    router.close_session("s1").await;
    let rec = host.rec.lock().unwrap();
    assert_eq!(rec.helpers_closed, rec.helpers_opened);
    for (_, frame) in &rec.frames {
        assert!(!frame.to_string().contains("pw-geheim"));
    }
}

/// Ersatz für ssh auf dem Zielsystem: wertet nur den entfernten Befehl aus.
const FAKE_SSH: &str = r#"for a; do last=$a; done
test -f "$MGPT_HELPER/.mgpt-ssh/key" || { echo "kein Schlüssel" >&2; exit 255; }
case "$last" in
  "uname -m") echo "${FAKE_ARCH:-x86_64}" ;;
  *"cat >"*) cat > "$MGPT_HELPER/received"; echo "${FAKE_SUM:-$(sha256sum "$MGPT_HELPER/received" | cut -d' ' -f1)}  datei" ;;
  *"rm -f"*) echo entfernt ;;
  *"pair --url"*) read t; read k; echo "gekoppelt mit $t"; echo "$last" > "$MGPT_HELPER/install" ;;
  *) echo "unbekannt: $last" >&2; exit 1 ;;
esac"#;

fn pairing(binary: &[u8]) -> Value {
    json!({
        "url": "https://mgpt.example",
        "token": TOKEN,
        "ca_sha256": "AB:".repeat(31) + "AB",
        "server_key": SERVER_KEY,
        "images": format!("localhost/mgpt-work@sha256:{}", "e".repeat(64)),
        "download": {
            "amd64": {"url": "https://public.example/runner/download/amd64", "sha256": setup::hex_sha256(binary)},
            "arm64": {"url": "https://mgpt.example/runner/download/arm64", "sha256": "0".repeat(64)},
        },
    })
}

fn setup_host(dir: &Path, script_env: &str) -> FakeHost {
    let mut host = FakeHost::new(dir);
    // $MGPT_HELPER: /tmp des Hilfscontainers (FakeHost bildet /tmp/ darauf ab)
    host.fake_commands.insert(
        "ssh".into(),
        format!("MGPT_HELPER=$(pwd); {script_env} {FAKE_SSH}"),
    );
    host.downloads.insert(
        "https://mgpt.example/runner/download/amd64".into(),
        b"\x7fELF-runner".to_vec(),
    );
    allowlisted(host, 22)
}

#[tokio::test]
async fn setup_host_transfers_checks_and_pairs() {
    let dir = tempfile::tempdir().unwrap();
    let host = Arc::new(setup_host(dir.path(), ""));
    let tools = SessionTools::new(host.clone());
    let binary = b"\x7fELF-runner";
    let watcher_dir = dir.path().join("helpers");
    let (text, info) = call(
        &tools,
        "setup_host",
        json!({"host": nas(22), "pairing": pairing(binary)}),
    )
    .await
    .unwrap();
    assert!(
        text.starts_with("nas eingerichtet: multigpt-runner (amd64"),
        "{text}"
    );
    assert!(
        text.contains("gekoppelt mit ****") && !text.contains(TOKEN),
        "{text}"
    );
    assert_eq!(
        info.unwrap(),
        json!({"arch": "amd64", "sha256": setup::hex_sha256(binary), "paired": true})
    );
    let rec = host.rec.lock().unwrap();
    // Alles im Hilfscontainer, der danach entfernt ist
    assert_eq!(rec.helpers_opened.len(), 1);
    assert_eq!(rec.helpers_closed, rec.helpers_opened);
    assert!(
        rec.execs
            .iter()
            .all(|(target, _)| target.starts_with("mgpt-tol-"))
    );
    assert!(!watcher_dir.join(&rec.helpers_opened[0]).exists());
    for (_, argv) in &rec.execs {
        let joined = argv.join(" ");
        assert!(
            !joined.contains(TOKEN) && !joined.contains("geheim-nur-im-test"),
            "{joined}"
        );
    }
    let ssh = rec
        .execs
        .iter()
        .find(|(_, argv)| argv.iter().any(|a| a.contains("uname")))
        .unwrap();
    for needle in [
        "StrictHostKeyChecking=yes",
        "BatchMode=yes",
        "PasswordAuthentication=no",
    ] {
        assert!(ssh.1.iter().any(|a| a == needle), "{needle}");
    }
    assert!(ssh.1.windows(2).any(|w| w[0] == "-p" && w[1] == "20000"));
    let install = rec.execs.last().unwrap().1.last().unwrap().clone();
    assert!(
        install.contains(&format!(
            "pair --url \"https://mgpt.example\" --images \"localhost/mgpt-work@sha256:{}\" \
             --ca-sha256 abab",
            "e".repeat(64)
        )),
        "{install}"
    );
    for (_, frame) in &rec.frames {
        assert!(!frame.to_string().contains(TOKEN));
    }
}

#[tokio::test]
async fn setup_host_aborts_on_checksum_mismatch() {
    // Download passt nicht zur Prüfsumme des Servers
    let dir = tempfile::tempdir().unwrap();
    let tools = SessionTools::new(Arc::new(setup_host(dir.path(), "")));
    let mut wrong = pairing(b"anderes Programm");
    wrong["download"]["amd64"]["sha256"] = json!(setup::hex_sha256(b"anderes Programm"));
    let err = call(
        &tools,
        "setup_host",
        json!({"host": nas(22), "pairing": wrong}),
    )
    .await
    .unwrap_err();
    assert!(
        err.message.contains("Prüfsumme des geladenen Programms"),
        "{err}"
    );
    // Ziel meldet eine andere Prüfsumme: Datei wird entfernt, keine Kopplung
    let dir = tempfile::tempdir().unwrap();
    let host = Arc::new(setup_host(
        dir.path(),
        &format!("FAKE_SUM={};", "1".repeat(64)),
    ));
    let tools = SessionTools::new(host.clone());
    let err = call(
        &tools,
        "setup_host",
        json!({"host": nas(22), "pairing": pairing(b"\x7fELF-runner")}),
    )
    .await
    .unwrap_err();
    assert!(err.message.contains("Zielsystem stimmt nicht"), "{err}");
    let rec = host.rec.lock().unwrap();
    assert!(
        rec.execs
            .iter()
            .any(|(_, a)| a.last().unwrap().contains("rm -f"))
    );
    assert!(
        !rec.execs
            .iter()
            .any(|(_, a)| a.last().unwrap().contains("pair --url"))
    );
    assert_eq!(rec.helpers_closed, rec.helpers_opened);
}

#[tokio::test]
async fn setup_host_rejects_foreign_download_and_unknown_arch() {
    let dir = tempfile::tempdir().unwrap();
    let tools = SessionTools::new(Arc::new(setup_host(dir.path(), "FAKE_ARCH=armv7l;")));
    let mut foreign = pairing(b"x");
    foreign["download"]["amd64"]["url"] = json!("https://mgpt.example/evil/amd64");
    let err = call(
        &tools,
        "setup_host",
        json!({"host": nas(22), "pairing": foreign}),
    )
    .await
    .unwrap_err();
    assert!(err.message.contains("/runner/download/amd64"), "{err}");
    let mut no_image = pairing(b"x");
    no_image["images"] = json!("localhost/mgpt-work:latest");
    let err = call(
        &tools,
        "setup_host",
        json!({"host": nas(22), "pairing": no_image}),
    )
    .await
    .unwrap_err();
    assert!(err.message.contains("Arbeits-Image"), "{err}");
    let mut bad_token = pairing(b"x");
    bad_token["token"] = json!("mgrt_x\nrm -rf /");
    let err = call(
        &tools,
        "setup_host",
        json!({"host": nas(22), "pairing": bad_token}),
    )
    .await
    .unwrap_err();
    assert!(err.message.contains("Token"), "{err}");
    let err = call(
        &tools,
        "setup_host",
        json!({"host": nas(22), "pairing": pairing(b"x")}),
    )
    .await
    .unwrap_err();
    assert!(
        err.message.contains("armv7l") && err.message.contains("nicht unterstützt"),
        "{err}"
    );
}

#[tokio::test]
async fn setup_host_reports_ssh_failure_without_secrets() {
    let dir = tempfile::tempdir().unwrap();
    let mut host = setup_host(dir.path(), "");
    host.fake_commands.insert(
        "ssh".into(),
        format!("echo \"Permission denied {TOKEN}\" >&2; exit 255"),
    );
    let tools = SessionTools::new(Arc::new(host));
    let err = call(
        &tools,
        "setup_host",
        json!({"host": nas(22), "pairing": pairing(b"x")}),
    )
    .await
    .unwrap_err();
    assert!(err.message.contains("SSH-Verbindung"), "{err}");
    assert!(
        err.message.contains("Permission denied ****") && !err.message.contains(TOKEN),
        "{err}"
    );
}

// --- MCP je pipe -----------------------------------------------------------------------

async fn reply(host: &FakeHost, id: i64) -> Value {
    for _ in 0..500 {
        if let Some(frame) = host
            .frames("mcp")
            .into_iter()
            .find(|f| f["message"]["id"] == json!(id))
        {
            assert_eq!(frame["session"], "s1");
            assert_eq!(frame["pipe"], "p1");
            return frame["message"].clone();
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    panic!("keine Antwort auf {id}");
}

#[tokio::test]
async fn mcp_pipe_lists_and_calls_tools() {
    let (_dir, host) = setup_dir();
    init(&host).await;
    let host = Arc::new(host);
    let router = McpRouter::new();
    let rpc = |message: Value| router.on_frame(host.clone(), "p1", message);
    rpc(json!({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "1"}},
    }))
    .await;
    let init = reply(&host, 1).await;
    assert_eq!(init["result"]["serverInfo"]["name"], "multigpt-runner");
    assert_eq!(init["result"]["protocolVersion"], "2025-06-18");
    rpc(json!({"jsonrpc": "2.0", "method": "notifications/initialized"})).await;
    rpc(json!({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})).await;
    let listed = reply(&host, 2).await;
    let mut names: Vec<String> = listed["result"]["tools"]
        .as_array()
        .unwrap()
        .iter()
        .map(|t| t["name"].as_str().unwrap().to_string())
        .collect();
    names.sort();
    assert_eq!(
        names,
        [
            "apt_install",
            "bash",
            "edit",
            "glob",
            "grep",
            "publish",
            "read",
            "serve",
            "setup_host",
            "telnet",
            "write"
        ]
    );
    rpc(json!({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
               "params": {"name": "write", "arguments": {"path": "x.txt", "content": "1"}}}))
    .await;
    let result = reply(&host, 3).await["result"].clone();
    assert_eq!(result["content"][0]["text"], "x.txt angelegt (1 Byte).");
    assert_ne!(result["isError"], true);
    assert_eq!(result["_meta"]["multigpt/untrusted"], true);
    rpc(json!({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
               "params": {"name": "read", "arguments": {"path": "../../etc/passwd"}}}))
    .await;
    let result = reply(&host, 4).await["result"].clone();
    assert_eq!(result["isError"], true);
    assert!(
        result["content"][0]["text"]
            .as_str()
            .unwrap()
            .contains("/workspace")
    );
    // Kaputte Nachricht mit id: Fehlerantwort statt Abbruch
    rpc(json!({"jsonrpc": "2.0", "id": 5, "method": 17})).await;
    let error = reply(&host, 5).await;
    assert_eq!(error["error"]["code"], -32600);
    rpc(json!({"jsonrpc": "2.0", "id": 6, "method": "tools/list"})).await;
    assert!(reply(&host, 6).await["result"]["tools"].is_array());
    assert_eq!(router.pipe_count("s1"), 1);
    router.close_session("s1").await;
    assert_eq!(router.pipe_count("s1"), 0);
}

#[tokio::test]
async fn mcp_tools_list_follows_welcome_services() {
    let (_dir, mut host) = setup_dir();
    host.allowed = Some(vec!["read".into(), "bash".into()]);
    let host = Arc::new(host);
    let router = McpRouter::new();
    router
        .on_frame(
            host.clone(),
            "p1",
            json!({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                           "clientInfo": {"name": "test", "version": "1"}},
            }),
        )
        .await;
    reply(&host, 1).await;
    router
        .on_frame(
            host.clone(),
            "p1",
            json!({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        )
        .await;
    router
        .on_frame(
            host.clone(),
            "p1",
            json!({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        )
        .await;
    let names: Vec<Value> = reply(&host, 2).await["result"]["tools"]
        .as_array()
        .unwrap()
        .iter()
        .map(|t| t["name"].clone())
        .collect();
    assert_eq!(names, vec![json!("read"), json!("bash")]);
    router.close_all().await;
}

// --- Paketquellen (apt_install) ------------------------------------------------------------

#[tokio::test]
async fn apt_install_only_in_packages_mode() {
    let (_dir, host) = setup_dir();
    let tools = SessionTools::new(Arc::new(host));
    let err = call(&tools, "apt_install", json!({"packages": ["jq"]}))
        .await
        .unwrap_err();
    assert!(err.message.contains("Paketquellen"), "{err}");
    assert!(tools.host.rec.lock().unwrap().apt.is_empty());
}

#[tokio::test]
async fn apt_install_checks_names_and_commits() {
    let (_dir, mut host) = setup_dir();
    host.packages = true;
    init(&host).await;
    let tools = SessionTools::new(Arc::new(host));
    for bad in [
        json!({}),
        json!({"packages": []}),
        json!({"packages": "jq"}),
        json!({"packages": ["-o", "jq"]}),
        json!({"packages": ["jq; rm -rf /"]}),
        json!({"packages": ["Jq"]}),
        json!({"packages": ["jq=1.0"]}),
        json!({"packages": ["../jq"]}),
        json!({"packages": [1]}),
        json!({"packages": (0..21).map(|i| format!("p{i}")).collect::<Vec<_>>()}),
    ] {
        let result = call(&tools, "apt_install", bad.clone()).await;
        assert!(result.is_err(), "{bad} angenommen");
    }
    assert!(tools.host.rec.lock().unwrap().apt.is_empty());
    let (text, structured) = call(
        &tools,
        "apt_install",
        json!({"packages": ["jq", "libssl-dev", "jq", "g++"]}),
    )
    .await
    .unwrap();
    assert!(text.contains("Setting up jq libssl-dev g++"), "{text}");
    assert!(!text.contains('\x1b'), "{text}");
    assert!(text.contains("[Exit-Code 0]"), "{text}");
    assert!(text.contains(".mgpt/apt-packages"), "{text}");
    let structured = structured.unwrap();
    assert_eq!(structured["packages"], json!(["jq", "libssl-dev", "g++"]));
    let rec = tools.host.rec.lock().unwrap();
    assert_eq!(rec.apt, vec![vec!["jq", "libssl-dev", "g++"]]);
    assert_eq!(rec.saves, vec!["apt_install: jq libssl-dev jq g++"]);
}

#[tokio::test]
async fn apt_install_failure_is_reported() {
    let (_dir, mut host) = setup_dir();
    host.packages = true;
    let tools = SessionTools::new(Arc::new(host));
    let (text, _) = call(&tools, "apt_install", json!({"packages": ["fehlt"]}))
        .await
        .unwrap();
    assert!(text.contains("[Exit-Code 100]"), "{text}");
    assert!(!text.contains("Installiert:"), "{text}");
}

#[tokio::test]
async fn full_rootfs_blocks_writes_and_apt_but_not_bash() {
    let (_dir, mut host) = setup_dir();
    host.packages = true;
    host.rootfs_full = true;
    let tools = SessionTools::new(Arc::new(host));
    for (name, args) in [
        ("write", json!({"path": "a", "content": "x"})),
        (
            "edit",
            json!({"path": "a", "old_string": "x", "new_string": "y"}),
        ),
        ("apt_install", json!({"packages": ["jq"]})),
    ] {
        let err = call(&tools, name, args).await.unwrap_err();
        assert!(err.message.contains("Container-Schicht"), "{name}: {err}");
    }
    let (text, _) = call(&tools, "bash", json!({"command": "echo frei"}))
        .await
        .unwrap();
    assert!(text.starts_with("frei"), "{text}");
    assert!(tools.host.rec.lock().unwrap().apt.is_empty());
}
