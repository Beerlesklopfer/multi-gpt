//! `FakeEngine`: Engine ohne Container für Tests (wie `FakeEngine` in `engine.py`).
//!
//! Jeder „Container“ ist ein Verzeichnis: `exec` führt den Befehl als lokalen
//! Prozess aus, mit `/workspace` = eigenes Temp-Verzeichnis (über
//! `MGPT_WORKSPACE`). Die Hilfsprogramme `mgpt-fs`/`mgpt-serve`/`mgpt-relay`
//! laufen als Python-Skripte aus `runner_agent/` (bzw. `MGPT_FAKE_HELPERS`). Alle
//! Körper werden wie bei Podman gegen die Vorlage geprüft und in `created`
//! festgehalten; `inspect` leitet die Ist-Werte daraus ab (`tamper` verfälscht sie).
//!
//! Nur mit `cfg(test)` bzw. Feature `fake-engine` gebaut – Release-Binaries
//! starten nie lokale Prozesse auf dem Rechner.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::{Arc, Mutex, MutexGuard};

use async_trait::async_trait;
use serde_json::{Value, json};
use tokio::io::unix::AsyncFd;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::process::{Child, Command};

use super::{
    ContainerInfo, Engine, EngineError, EngineInfo, EngineResult, ExecOptions, ExecResult, Pty,
    labels_of,
};
use crate::template::{self, Bounds};

const HELPERS: [(&str, &str); 3] = [
    ("mgpt-fs", "mgpt_fs.py"),
    ("mgpt-serve", "mgpt_serve.py"),
    ("mgpt-relay", "mgpt_relay.py"),
];

/// Ein „Container“ der FakeEngine.
#[derive(Debug, Clone)]
pub struct FakeBox {
    pub body: Value,
    pub running: bool,
    pub dir: PathBuf,
    pub scratch: Option<PathBuf>,
    /// Hintergrundbefehle (`exec_detached`), laufen im Test nicht wirklich.
    pub detached: Vec<Vec<String>>,
}

/// Zustand der FakeEngine (für Prüfungen und Manipulationen im Test).
#[derive(Debug, Default)]
pub struct FakeState {
    pub containers: BTreeMap<String, FakeBox>,
    pub networks: BTreeMap<String, Value>,
    pub created: Vec<Value>,
    pub removed: Vec<String>,
    /// Name → Änderungen an `HostConfig` in `inspect`.
    pub tamper: HashMap<String, Value>,
    pub fail_start: BTreeSet<String>,
    pub busy_ports: BTreeSet<u64>,
    /// Programm (z. B. `ssh`) durch ein Shell-Skript ersetzen; argv bleibt in `calls`.
    pub fake_commands: HashMap<String, String>,
    pub calls: Vec<(String, Vec<String>)>,
    /// Befehle als root (nur geprüfte apt-Befehle); laufen im Test nicht wirklich,
    /// außer `fake_commands` ersetzt das Programm.
    pub root_calls: Vec<(String, Vec<String>)>,
    /// Name → Größe der beschreibbaren Schicht (Byte) für `container_size`.
    pub sizes: HashMap<String, u64>,
}

pub struct FakeEngine {
    pub bounds: Bounds,
    pub root: PathBuf,
    pub helpers: PathBuf,
    pub python: String,
    state: Mutex<FakeState>,
    _tempdir: Option<tempfile_dir::TempDir>,
}

/// Kleines Temp-Verzeichnis ohne zusätzliche Abhängigkeit (wird beim Drop gelöscht).
mod tempfile_dir {
    use std::path::PathBuf;

    #[derive(Debug)]
    pub struct TempDir(pub PathBuf);

    impl TempDir {
        pub fn new() -> std::io::Result<Self> {
            let base = std::env::temp_dir();
            for _ in 0..16 {
                let name = format!("mgpt-fake-{:016x}", rand::random::<u64>());
                let path = base.join(name);
                match std::fs::create_dir(&path) {
                    Ok(()) => return Ok(Self(path)),
                    Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => continue,
                    Err(e) => return Err(e),
                }
            }
            Err(std::io::Error::other("kein Temp-Verzeichnis"))
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }
}

/// Lokaler Prozess: (argv, Umgebung, Arbeitsverzeichnis).
type LocalCommand = (Vec<String>, Vec<(String, String)>, PathBuf);

/// Ort der Hilfsprogramme (Python-Skripte des Arbeits-Images).
pub fn default_helpers() -> PathBuf {
    if let Some(dir) = std::env::var_os("MGPT_FAKE_HELPERS") {
        return PathBuf::from(dir);
    }
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../runner_agent")
}

impl FakeEngine {
    pub fn new(bounds: Bounds) -> Self {
        let tmp = tempfile_dir::TempDir::new().expect("Temp-Verzeichnis");
        let root = tmp.0.clone();
        Self {
            bounds,
            root,
            helpers: default_helpers(),
            python: "python3".into(),
            state: Mutex::new(FakeState::default()),
            _tempdir: Some(tmp),
        }
    }

    /// Zustand zum Prüfen bzw. Manipulieren im Test.
    pub fn state(&self) -> MutexGuard<'_, FakeState> {
        self.state.lock().unwrap_or_else(|e| e.into_inner())
    }

    fn get(&self, name: &str) -> EngineResult<FakeBox> {
        self.state()
            .containers
            .get(name)
            .cloned()
            .ok_or_else(|| EngineError::new(format!("Container nicht gefunden: {name}")))
    }

    /// `/workspace` des Containers in Sicht des Tests.
    pub fn workspace(&self, name: &str) -> EngineResult<PathBuf> {
        Ok(self.get(name)?.dir.join("workspace"))
    }

    /// Befehl in einen lokalen Prozess übersetzen: (argv, Umgebung, Arbeitsverzeichnis).
    fn local(&self, name: &str, argv: &[String], workdir: &str) -> EngineResult<LocalCommand> {
        let fake = self.get(name)?;
        if !fake.running {
            return Err(EngineError::new("Container läuft nicht."));
        }
        let ws = fake.dir.join("workspace");
        let tmp = fake.dir.join("tmp");
        // Zugangsdateien (ssh) im tmp des „Containers“, nie im /tmp des Rechners.
        let ssh = format!("{}/.mgpt-ssh", tmp.display());
        let mut argv: Vec<String> = argv
            .iter()
            .map(|a| a.replace("/tmp/.mgpt-ssh", &ssh))
            .collect();
        {
            let mut state = self.state();
            state.calls.push((name.to_string(), argv.clone()));
            if let Some(script) = argv
                .iter()
                .find_map(|t| state.fake_commands.get(t).cloned())
            {
                argv = vec!["sh".into(), "-c".into(), script];
            }
        }
        if let Some((_, script)) = HELPERS
            .iter()
            .find(|(h, _)| argv.first().map(String::as_str) == Some(*h))
        {
            let mut mapped = vec![
                self.python.clone(),
                self.helpers.join(script).to_string_lossy().into_owned(),
            ];
            mapped.extend(argv.into_iter().skip(1));
            argv = mapped;
        }
        let mut env: Vec<(String, String)> = template::ENV
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        let path = std::env::var("PATH").unwrap_or_else(|_| "/usr/bin:/bin".into());
        let scratch = fake
            .scratch
            .clone()
            .unwrap_or_else(|| fake.dir.join("kein-scratch"));
        for (k, v) in [
            ("PATH", path),
            ("HOME", tmp.to_string_lossy().into_owned()),
            ("MGPT_WORKSPACE", ws.to_string_lossy().into_owned()),
            ("MGPT_TMP", tmp.to_string_lossy().into_owned()),
            ("MGPT_SCRATCH", scratch.to_string_lossy().into_owned()),
            ("GIT_CONFIG_NOSYSTEM", "1".into()),
            ("GIT_CONFIG_GLOBAL", "/dev/null".into()),
        ] {
            env.retain(|(key, _)| key != k);
            env.push((k.to_string(), v));
        }
        let cwd = if workdir.starts_with("/workspace") {
            ws
        } else {
            tmp
        };
        Ok((argv, env, cwd))
    }

    fn command(argv: &[String], env: &[(String, String)], cwd: &Path) -> EngineResult<Command> {
        let Some(program) = argv.first() else {
            return Err(EngineError::new("Befehl fehlt."));
        };
        let mut cmd = Command::new(program);
        cmd.args(&argv[1..])
            .env_clear()
            .envs(env.iter().map(|(k, v)| (k, v)))
            .current_dir(cwd)
            .kill_on_drop(true);
        Ok(cmd)
    }
}

fn kill_group(child: &Child) {
    if let Some(pid) = child.id() {
        // SAFETY: Signal an die eigene Prozessgruppe des Kindes.
        unsafe {
            libc::kill(-(pid as i32), libc::SIGKILL);
        }
    }
}

#[async_trait]
impl Engine for FakeEngine {
    async fn info(&self) -> EngineResult<EngineInfo> {
        Ok(EngineInfo {
            name: "fake".into(),
            version: "0".into(),
            rootless: true,
        })
    }

    async fn create_network(&self, body: &Value) -> EngineResult<()> {
        template::check_network_body(body)?;
        let name = body["name"].as_str().unwrap_or_default().to_string();
        let mut state = self.state();
        if state.networks.contains_key(&name) {
            return Err(EngineError::new("Netz existiert schon."));
        }
        state.networks.insert(name, body.clone());
        Ok(())
    }

    async fn remove_network(&self, name: &str) -> EngineResult<()> {
        self.state().networks.remove(name);
        Ok(())
    }

    async fn create_container(&self, body: &Value) -> EngineResult<()> {
        template::check_body(body, &self.bounds)?;
        let name = body["name"].as_str().unwrap_or_default().to_string();
        let mut state = self.state();
        if state.containers.contains_key(&name) {
            return Err(EngineError::new("Container existiert schon."));
        }
        for net in body["Networks"]
            .as_object()
            .into_iter()
            .flatten()
            .map(|(k, _)| k)
        {
            if !state.networks.contains_key(net) {
                return Err(EngineError::new(format!("Netz fehlt: {net}")));
            }
        }
        let dir = self.root.join(&name);
        let io = |e: std::io::Error| EngineError::new(format!("Anlegen fehlgeschlagen: {e}"));
        std::fs::create_dir_all(dir.join("workspace")).map_err(io)?;
        std::fs::create_dir_all(dir.join("tmp")).map_err(io)?;
        let mut scratch = None;
        for mount in body["mounts"].as_array().into_iter().flatten() {
            if mount["destination"] == "/scratch" {
                // Bind: im Test ist die Host-Sicht gleich der Sicht des Agents.
                let path = if mount["type"] == "bind" {
                    PathBuf::from(mount["source"].as_str().unwrap_or_default())
                } else {
                    dir.join("scratch")
                };
                std::fs::create_dir_all(&path).map_err(io)?;
                scratch = Some(path);
            }
        }
        state.containers.insert(
            name,
            FakeBox {
                body: body.clone(),
                running: false,
                dir,
                scratch,
                detached: Vec::new(),
            },
        );
        state.created.push(body.clone());
        Ok(())
    }

    async fn start_container(&self, name: &str) -> EngineResult<()> {
        let mut state = self.state();
        if state.fail_start.contains(name) {
            return Err(EngineError::new("Start fehlgeschlagen."));
        }
        let busy = state.busy_ports.clone();
        let fake = state
            .containers
            .get_mut(name)
            .ok_or_else(|| EngineError::new(format!("Container nicht gefunden: {name}")))?;
        for port in fake.body["portmappings"].as_array().into_iter().flatten() {
            if port["host_port"]
                .as_u64()
                .is_some_and(|p| busy.contains(&p))
            {
                return Err(EngineError::new("address already in use"));
            }
        }
        fake.running = true;
        Ok(())
    }

    async fn remove_container(&self, name: &str) -> EngineResult<()> {
        let removed = {
            let mut state = self.state();
            let removed = state.containers.remove(name);
            if removed.is_some() {
                state.removed.push(name.to_string());
            }
            removed
        };
        if let Some(fake) = removed {
            let _ = std::fs::remove_dir_all(fake.dir);
        }
        Ok(())
    }

    async fn inspect_container(&self, name: &str) -> EngineResult<Value> {
        let fake = self.get(name)?;
        let body = &fake.body;
        let res = &body["resource_limits"];
        let mut tmpfs = serde_json::Map::new();
        let mut mounts = Vec::new();
        for m in body["mounts"].as_array().into_iter().flatten() {
            let options: Vec<&str> = m["options"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .collect();
            if m["type"] == "tmpfs" {
                let joined: Vec<&str> = options.iter().copied().filter(|o| *o != "U").collect();
                tmpfs.insert(
                    m["destination"].as_str().unwrap_or_default().to_string(),
                    json!(joined.join(",")),
                );
            } else if m["type"] == "bind" {
                let kept: Vec<&str> = options.iter().copied().filter(|o| *o != "rw").collect();
                mounts.push(json!({
                    "Type": "bind",
                    "Source": m["source"],
                    "Destination": m["destination"],
                    "Options": kept,
                }));
            }
        }
        let mut bindings = serde_json::Map::new();
        for p in body["portmappings"].as_array().into_iter().flatten() {
            bindings.insert(
                format!("{}/tcp", p["container_port"]),
                json!([{"HostIp": p["host_ip"], "HostPort": p["host_port"].to_string()}]),
            );
        }
        let mut host = json!({
            "CpuQuota": res["cpu"]["quota"],
            "CpuPeriod": res["cpu"]["period"],
            "Memory": res["memory"]["limit"],
            "MemorySwap": res["memory"]["swap"],
            "PidsLimit": res["pids"]["limit"],
            "Tmpfs": tmpfs,
            "ReadonlyRootfs": body["read_only_filesystem"],
            "Privileged": body["privileged"],
            "CapAdd": body.get("cap_add").cloned().unwrap_or(json!([])),
            "SecurityOpt": if body["no_new_privileges"] == json!(true) { json!(["no-new-privileges"]) } else { json!([]) },
            "NetworkMode": "bridge",
            "PidMode": "private",
            "IpcMode": "private",
            "UTSMode": "private",
            "Binds": [],
            "Devices": [],
            "PortBindings": bindings,
        });
        if let Some(changes) = self.state().tamper.get(name).and_then(Value::as_object) {
            for (k, v) in changes {
                host[k] = v.clone();
            }
        }
        let networks: serde_json::Map<String, Value> = body["Networks"]
            .as_object()
            .into_iter()
            .flatten()
            .map(|(k, _)| {
                // Feste IPv4 je Netz und Container (wie Podman, ohne echtes Netz).
                let n = |text: &str| {
                    u8::from_str_radix(&template::short_id(text)[..2], 16).unwrap_or(0)
                };
                let ip = format!("10.89.{}.{}", n(k) % 250, n(name) % 250 + 2);
                (k.clone(), json!({"IPAddress": ip}))
            })
            .collect();
        Ok(json!({
            "Name": name,
            "Config": {"User": body["user"], "Labels": body["labels"]},
            "HostConfig": host,
            "Mounts": mounts,
            "State": {
                "Running": fake.running,
                "Status": if fake.running { "running" } else { "created" },
            },
            "NetworkSettings": {"Networks": networks},
        }))
    }

    async fn container_size(&self, name: &str) -> EngineResult<u64> {
        self.get(name)?;
        Ok(self.state().sizes.get(name).copied().unwrap_or(0))
    }

    async fn list_containers(&self) -> EngineResult<Vec<ContainerInfo>> {
        Ok(self
            .state()
            .containers
            .iter()
            .map(|(name, fake)| ContainerInfo {
                name: name.clone(),
                labels: labels_of(&fake.body["labels"]),
                running: fake.running,
            })
            .collect())
    }

    async fn exec(
        &self,
        name: &str,
        argv: &[String],
        opts: ExecOptions,
    ) -> EngineResult<ExecResult> {
        if opts.root {
            template::check_root_argv(argv)?;
            let fake = self.get(name)?;
            if fake.body["read_only_filesystem"] != json!(false) {
                return Err(EngineError::new("Wurzeldateisystem schreibgeschützt."));
            }
            // `timeout -k 5 N` des Agents nicht mitschreiben (Tests vergleichen die apt-argv).
            let plain = if argv.first().map(String::as_str) == Some("timeout") {
                &argv[4..]
            } else {
                argv
            };
            let replaced = {
                let mut state = self.state();
                state.root_calls.push((name.to_string(), plain.to_vec()));
                plain
                    .first()
                    .and_then(|p| state.fake_commands.get(p).cloned())
            };
            if replaced.is_none() {
                return Ok(ExecResult {
                    code: Some(0),
                    stdout: b"(root im Test)\n".to_vec(),
                    ..ExecResult::default()
                });
            }
        }
        let (argv, env, cwd) = self.local(name, argv, &opts.workdir)?;
        let mut cmd = Self::command(&argv, &env, &cwd)?;
        cmd.stdin(if opts.stdin.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0);
        let mut child = cmd
            .spawn()
            .map_err(|e| EngineError::new(format!("exec fehlgeschlagen: {e}")))?;
        let stdin = child.stdin.take();
        let mut stdout = child.stdout.take().unwrap();
        let mut stderr = child.stderr.take().unwrap();
        let input = opts.stdin.unwrap_or_default();
        let feed = async move {
            if let Some(mut pipe) = stdin {
                let _ = pipe.write_all(&input).await;
            }
        };
        let read_out = async {
            let mut buf = Vec::new();
            let _ = stdout.read_to_end(&mut buf).await;
            buf
        };
        let read_err = async {
            let mut buf = Vec::new();
            let _ = stderr.read_to_end(&mut buf).await;
            buf
        };
        let all = async {
            let ((), out, err) = tokio::join!(feed, read_out, read_err);
            let status = child.wait().await;
            (out, err, status)
        };
        let outcome = tokio::time::timeout(opts.timeout, all).await;
        let Ok((out, err, status)) = outcome else {
            kill_group(&child);
            let _ = child.wait().await;
            return Ok(ExecResult {
                code: None,
                timed_out: true,
                ..ExecResult::default()
            });
        };
        let max = opts.max_output;
        let truncated = out.len() > max || err.len() > max;
        Ok(ExecResult {
            code: status.ok().and_then(|s| s.code()).map(i64::from),
            stdout: out[..out.len().min(max)].to_vec(),
            stderr: err[..err.len().min(max)].to_vec(),
            truncated,
            timed_out: false,
        })
    }

    async fn exec_detached(&self, name: &str, argv: &[String], workdir: &str) -> EngineResult<()> {
        // Nur festhalten: Hintergrunddienste (Vorschau) laufen im Test nicht wirklich.
        self.local(name, argv, workdir)?;
        if let Some(fake) = self.state().containers.get_mut(name) {
            fake.detached.push(argv.to_vec());
        }
        Ok(())
    }

    async fn exec_pty(
        &self,
        name: &str,
        argv: &[String],
        cols: u16,
        rows: u16,
        workdir: &str,
    ) -> EngineResult<Box<dyn Pty>> {
        let (argv, env, cwd) = self.local(name, argv, workdir)?;
        let (master, slave) = open_pty(cols, rows)?;
        let mut cmd = Self::command(&argv, &env, &cwd)?;
        let io = |e: std::io::Error| EngineError::new(format!("PTY: {e}"));
        cmd.stdin(Stdio::from(slave.try_clone().map_err(io)?))
            .stdout(Stdio::from(slave.try_clone().map_err(io)?))
            .stderr(Stdio::from(slave));
        // SAFETY: zwischen fork und exec nur async-signal-sichere Aufrufe.
        unsafe {
            cmd.pre_exec(|| {
                if libc::setsid() < 0 {
                    return Err(std::io::Error::last_os_error());
                }
                libc::ioctl(0, libc::TIOCSCTTY as _, 0);
                Ok(())
            });
        }
        let child = cmd
            .spawn()
            .map_err(|e| EngineError::new(format!("exec fehlgeschlagen: {e}")))?;
        let master = AsyncFd::new(master).map_err(io)?;
        Ok(Box::new(LocalPty {
            master: Arc::new(master),
            child: tokio::sync::Mutex::new(child),
            closed: tokio::sync::Notify::new(),
            is_closed: std::sync::atomic::AtomicBool::new(false),
            code: Mutex::new(None),
        }))
    }
}

fn open_pty(cols: u16, rows: u16) -> EngineResult<(OwnedFd, OwnedFd)> {
    let mut master: libc::c_int = -1;
    let mut slave: libc::c_int = -1;
    let size = libc::winsize {
        ws_row: rows,
        ws_col: cols,
        ws_xpixel: 0,
        ws_ypixel: 0,
    };
    // SAFETY: openpty schreibt zwei gültige Deskriptoren in master/slave.
    let rc = unsafe {
        libc::openpty(
            &mut master,
            &mut slave,
            std::ptr::null_mut(),
            std::ptr::null(),
            &size,
        )
    };
    if rc != 0 {
        return Err(EngineError::new("openpty fehlgeschlagen."));
    }
    // SAFETY: frisch geöffnete Deskriptoren, Besitz geht an OwnedFd über.
    let (master, slave) = unsafe { (OwnedFd::from_raw_fd(master), OwnedFd::from_raw_fd(slave)) };
    // SAFETY: fcntl auf eigenem Deskriptor.
    unsafe {
        let flags = libc::fcntl(master.as_raw_fd(), libc::F_GETFL);
        libc::fcntl(master.as_raw_fd(), libc::F_SETFL, flags | libc::O_NONBLOCK);
    }
    Ok((master, slave))
}

struct LocalPty {
    master: Arc<AsyncFd<OwnedFd>>,
    child: tokio::sync::Mutex<Child>,
    closed: tokio::sync::Notify,
    is_closed: std::sync::atomic::AtomicBool,
    code: Mutex<Option<i64>>,
}

impl LocalPty {
    async fn read_once(&self) -> Vec<u8> {
        loop {
            let Ok(mut guard) = self.master.readable().await else {
                return Vec::new();
            };
            let mut buf = vec![0u8; 65536];
            let result = guard.try_io(|fd| {
                // SAFETY: Lesen in einen eigenen Puffer passender Größe.
                let n = unsafe {
                    libc::read(fd.get_ref().as_raw_fd(), buf.as_mut_ptr().cast(), buf.len())
                };
                if n < 0 {
                    Err(std::io::Error::last_os_error())
                } else {
                    Ok(n as usize)
                }
            });
            match result {
                Ok(Ok(n)) => {
                    buf.truncate(n);
                    return buf; // 0 = Ende
                }
                // EIO: Kindprozess beendet
                Ok(Err(_)) => return Vec::new(),
                Err(_would_block) => continue,
            }
        }
    }
}

#[async_trait]
impl Pty for LocalPty {
    async fn read(&self) -> Vec<u8> {
        if self.is_closed.load(std::sync::atomic::Ordering::SeqCst) {
            return Vec::new();
        }
        tokio::select! {
            data = self.read_once() => data,
            () = self.closed.notified() => Vec::new(),
        }
    }

    async fn write(&self, data: &[u8]) -> EngineResult<()> {
        if self.is_closed.load(std::sync::atomic::Ordering::SeqCst) {
            return Ok(());
        }
        let mut rest = data;
        while !rest.is_empty() {
            let Ok(mut guard) = self.master.writable().await else {
                return Ok(());
            };
            let result = guard.try_io(|fd| {
                // SAFETY: Schreiben aus einem gültigen Puffer.
                let n = unsafe {
                    libc::write(fd.get_ref().as_raw_fd(), rest.as_ptr().cast(), rest.len())
                };
                if n < 0 {
                    Err(std::io::Error::last_os_error())
                } else {
                    Ok(n as usize)
                }
            });
            match result {
                Ok(Ok(n)) => rest = &rest[n..],
                Ok(Err(_)) => return Ok(()),
                Err(_would_block) => continue,
            }
        }
        Ok(())
    }

    async fn resize(&self, cols: u16, rows: u16) -> EngineResult<()> {
        let size = libc::winsize {
            ws_row: rows,
            ws_col: cols,
            ws_xpixel: 0,
            ws_ypixel: 0,
        };
        // SAFETY: ioctl mit gültiger winsize-Struktur.
        unsafe {
            libc::ioctl(self.master.get_ref().as_raw_fd(), libc::TIOCSWINSZ, &size);
        }
        Ok(())
    }

    async fn wait(&self) -> Option<i64> {
        if let Some(code) = *self.code.lock().unwrap_or_else(|e| e.into_inner()) {
            return Some(code);
        }
        let mut child = self.child.lock().await;
        let status = child.wait().await.ok()?;
        let code = status.code().map(i64::from).or_else(|| {
            std::os::unix::process::ExitStatusExt::signal(&status).map(|s| -i64::from(s))
        });
        *self.code.lock().unwrap_or_else(|e| e.into_inner()) = code;
        code
    }

    async fn close(&self) {
        self.is_closed
            .store(true, std::sync::atomic::Ordering::SeqCst);
        self.closed.notify_waiters();
        let mut child = self.child.lock().await;
        if matches!(child.try_wait(), Ok(None)) {
            kill_group(&child);
            let _ = child.wait().await;
        }
    }
}
