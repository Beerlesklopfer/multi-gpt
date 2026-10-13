//! Sitzungen mit der FakeEngine (Port von `tests/test_runner_agent_session.py`,
//! soweit Sitzung, Netz, Arbeitsstand, Terminal, Vorschau und Scratchpad).
//!
//! Die Gegenseite spielt ein kleiner „Server“ am Outbox-Kanal: er beantwortet
//! `bundle_get` mit Stücken und bestätigt hochgeladene Bundles.

use std::collections::{BTreeSet, HashMap};
use std::sync::atomic::AtomicBool;

use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::Message;

use super::*;
use crate::engine::FakeEngine;
use crate::protocol::{Frame, Sender};

const IMAGE: &str = "ghcr.io/beerlesklopfer/multigpt-runner-work:1@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const OTHER_IMAGE: &str = "docker.io/library/alpine@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const UUID: &str = "0b5f2c1e-7d4a-4c1b-9a8e-3f2d1c0b9a87";

fn config() -> Config {
    let mut cfg = Config::new("wss://mgpt.example/runner/ws");
    cfg.images.insert(IMAGE.to_string());
    cfg.labels
        .insert("multigpt.test".to_string(), "1".to_string());
    cfg
}

fn server_frame(kind: &str, fields: Value) -> Frame {
    let text = protocol::encode_value(kind, Some(Sender::Server), fields).expect("Rahmen");
    protocol::parse(&text, Some(Sender::Server)).expect("Rahmen")
}

#[derive(Default)]
struct Server {
    frames: Mutex<Vec<Value>>,
    bundles: Mutex<HashMap<String, Vec<u8>>>,
    workspace_of: Mutex<HashMap<String, String>>,
    uploads: Mutex<HashMap<String, BundleAssembler>>,
    reject_upload: AtomicBool,
}

struct Harness<E: Engine + 'static = FakeEngine> {
    engine: Arc<E>,
    manager: Arc<SessionManager>,
    server: Arc<Server>,
    clock: Arc<Mutex<f64>>,
    image: String,
}

impl Harness {
    fn new(cfg: Config) -> Self {
        let engine = Arc::new(FakeEngine::new(Bounds::from(&cfg)));
        Harness::with_engine(cfg, engine)
    }
}

impl<E: Engine + 'static> Harness<E> {
    fn with_engine(cfg: Config, engine: Arc<E>) -> Self {
        let (outbox, rx) = Outbox::channel(100_000);
        let clock = Arc::new(Mutex::new(1000.0));
        let mut manager = SessionManager::new(engine.clone(), &cfg, outbox);
        let c = clock.clone();
        manager.clock = Arc::new(move || *guard(&c));
        manager.bundle_timeout = Duration::from_secs(20);
        manager.set_server_images(Some(cfg.images.iter().cloned().collect()));
        let manager = Arc::new(manager);
        let server = Arc::new(Server::default());
        tokio::spawn(serve(rx, manager.clone(), server.clone()));
        Self {
            engine,
            manager,
            server,
            clock,
            image: cfg.images.iter().next().cloned().unwrap_or_default(),
        }
    }

    fn frames(&self, kind: &str) -> Vec<Value> {
        guard(&self.server.frames)
            .iter()
            .filter(|f| f["type"] == kind)
            .cloned()
            .collect()
    }

    fn last(&self, kind: &str) -> Value {
        self.frames(kind).pop().unwrap_or(Value::Null)
    }

    fn start_frame(&self, session: &str, workspace: &str, extra: Value) -> SessionStart {
        let mut fields = json!({
            "session": session, "workspace": workspace, "image": self.image,
            "cpus": 1.0, "memory_mb": 512, "pids": 256, "tmpfs_mb": 64,
            "network": "none", "command_seconds": 60, "output_bytes": 4096, "idle_minutes": 15,
        });
        for (k, v) in extra.as_object().unwrap() {
            fields[k] = v.clone();
        }
        let stored = guard(&self.server.bundles).get(workspace).cloned();
        if let (None, Some(data)) = (fields.get("bundle"), stored) {
            fields["bundle"] =
                json!({"size": data.len(), "sha256": protocol::bundle::sha256_hex(&data)});
        }
        guard(&self.server.workspace_of).insert(session.into(), workspace.into());
        match server_frame("session_start", fields) {
            Frame::SessionStart(f) => f,
            other => panic!("{other:?}"),
        }
    }

    async fn start(&self, session: &str, workspace: &str, extra: Value) -> Value {
        let frame = self.start_frame(session, workspace, extra);
        self.manager.start(&frame).await;
        self.settle().await;
        self.last("session_state")
    }

    /// Warten, bis der „Server“ alle Rahmen gesehen hat.
    async fn settle(&self) {
        tokio::time::sleep(Duration::from_millis(30)).await;
    }

    fn handle(&self, sid: &str) -> SessionHandle {
        self.manager.handle(sid).expect("Sitzung")
    }

    /// Datei schreiben und speichern (wie das Werkzeug `write`).
    async fn write(&self, sid: &str, path: &str, content: &str) -> ToolResult<String> {
        let h = self.handle(sid);
        let mut fields = Map::new();
        fields.insert("path".into(), json!(path));
        fields.insert("content".into(), json!(content));
        h.fs("write", fields).await?;
        Ok(h.save(&format!("write {path}")).await)
    }

    async fn read(&self, sid: &str, path: &str) -> ToolResult<Value> {
        let mut fields = Map::new();
        fields.insert("path".into(), json!(path));
        self.handle(sid).fs("read", fields).await
    }

    fn work(&self, sid: &str) -> String {
        template::work_name(sid)
    }
}

async fn serve(mut rx: mpsc::Receiver<Message>, manager: Arc<SessionManager>, server: Arc<Server>) {
    while let Some(message) = rx.recv().await {
        let Message::Text(text) = message else {
            continue;
        };
        let frame: Value = serde_json::from_str(text.as_str()).unwrap();
        guard(&server.frames).push(frame.clone());
        match frame["type"].as_str().unwrap() {
            "bundle_get" => {
                let (manager, server) = (manager.clone(), server.clone());
                tokio::spawn(async move {
                    let sid = frame["session"].as_str().unwrap().to_string();
                    let ws = guard(&server.workspace_of)[&sid].clone();
                    let data = guard(&server.bundles)[&ws].clone();
                    for (seq, part, last) in protocol::split_bundle(&data, 1024) {
                        let mut fields = json!({
                            "session": sid, "transfer": frame["transfer"], "seq": seq,
                            "data": protocol::b64encode(part), "last": last,
                        });
                        if last {
                            fields["size"] = json!(data.len());
                            fields["sha256"] = json!(protocol::bundle::sha256_hex(&data));
                        }
                        let Frame::BundleChunk(chunk) = server_frame("bundle_chunk", fields) else {
                            unreachable!()
                        };
                        manager.on_bundle_chunk(&chunk).await.unwrap();
                    }
                });
            }
            "bundle_chunk" => {
                let transfer = frame["transfer"].as_str().unwrap().to_string();
                let Frame::BundleChunk(chunk) =
                    protocol::parse(text.as_str(), Some(Sender::Agent)).unwrap()
                else {
                    unreachable!()
                };
                let done = guard(&server.uploads)
                    .entry(transfer.clone())
                    .or_insert_with(|| {
                        BundleAssembler::new(transfer.clone(), protocol::MAX_BUNDLE_BYTES)
                    })
                    .add(&chunk)
                    .unwrap();
                if let Some(data) = done {
                    let reject = server.reject_upload.load(Ordering::SeqCst);
                    let sid = chunk.session.clone();
                    if !reject {
                        let ws = guard(&server.workspace_of)[&sid].clone();
                        guard(&server.bundles).insert(ws, data);
                    }
                    let Frame::BundleAck(ack) = server_frame(
                        "bundle_ack",
                        json!({"session": sid, "transfer": transfer, "ok": !reject,
                               "head": if reject { Value::Null } else { json!(chunk.head) },
                               "error": if reject { json!("zu groß") } else { Value::Null }}),
                    ) else {
                        unreachable!()
                    };
                    manager.on_bundle_ack(&ack);
                }
            }
            _ => {}
        }
    }
}

// --- Start und Stopp ------------------------------------------------------------------

#[tokio::test]
async fn session_start_reports_actual_limits() {
    let h = Harness::new(config());
    let state = h.start("s1", "w1", json!({})).await;
    assert_eq!(state["state"], "running", "{state}");
    assert_eq!(
        state["limits"],
        json!({"cpus": 1.0, "memory_mb": 512, "pids": 256, "tmpfs_mb": 64, "network": "none"})
    );
    assert_eq!(state["preview_port"], 18080);
    let net = template::network_name("s1", "net");
    {
        let st = h.engine.state();
        assert_eq!(st.networks[&net]["internal"], json!(true));
        assert_eq!(st.networks[&net]["labels"]["multigpt.test"], "1");
        let body = &st.created[0];
        assert_eq!(body["Networks"], json!({ net.clone(): {} }));
        assert_eq!(body["labels"]["multigpt.test"], "1");
        assert_eq!(
            template::check_body(body, &h.engine.bounds),
            Ok(template::Role::Work)
        );
    }
    let infos = h.manager.infos();
    assert_eq!(infos.len(), 1);
    assert_eq!(
        (
            infos[0].session.as_str(),
            infos[0].workspace.as_deref(),
            infos[0].state.as_str(),
            infos[0].preview_port
        ),
        ("s1", Some("w1"), "running", Some(18080))
    );
    // git init im leeren Arbeitsstand
    let ws = h.engine.workspace(&h.work("s1")).unwrap();
    assert!(ws.join(".git").is_dir());
    h.manager.stop("s1", "user").await;
    h.settle().await;
    assert_eq!(
        h.last("session_state"),
        json!({"type": "session_state", "session": "s1", "state": "stopped"})
    );
    let st = h.engine.state();
    assert!(st.containers.is_empty() && st.networks.is_empty());
}

#[tokio::test]
async fn session_start_rejects_foreign_image_and_limits() {
    for (change, needle) in [
        (json!({"image": OTHER_IMAGE}), "Positivliste"),
        (json!({"cpus": 64.0}), "CPU"),
        (json!({"memory_mb": 262144}), "RAM"),
        (json!({"network": "allowlist"}), "Positivliste ohne"),
    ] {
        let h = Harness::new(config());
        let state = h.start("s1", "w1", change).await;
        assert_eq!(state["state"], "failed");
        assert!(state["error"].as_str().unwrap().contains(needle), "{state}");
        let st = h.engine.state();
        assert!(st.created.is_empty() && st.networks.is_empty());
    }
}

#[tokio::test]
async fn image_must_also_be_in_server_list() {
    let h = Harness::new(config());
    h.manager.set_server_images(Some(vec![OTHER_IMAGE.into()]));
    let state = h.start("s1", "w1", json!({})).await;
    assert_eq!(state["state"], "failed");
    assert!(state["error"].as_str().unwrap().contains("Server"));
}

#[tokio::test]
async fn deviating_actual_limits_stop_the_container() {
    let h = Harness::new(config());
    let name = h.work("s1");
    h.engine.state().tamper.insert(
        name.clone(),
        json!({"Memory": 4096u64 << 20, "MemorySwap": 4096u64 << 20}),
    );
    let state = h.start("s1", "w1", json!({})).await;
    assert_eq!(state["state"], "failed");
    let error = state["error"].as_str().unwrap();
    assert!(
        error.contains("Abweichende Grenzen") && error.contains("RAM"),
        "{error}"
    );
    let st = h.engine.state();
    assert!(st.removed.contains(&name) && st.containers.is_empty());
    assert!(h.manager.infos().is_empty());
}

#[tokio::test]
async fn workspace_only_in_one_session_and_busy_port_is_skipped() {
    let h = Harness::new(config());
    h.engine.state().busy_ports.insert(18080);
    assert_eq!(h.start("s1", "w1", json!({})).await["preview_port"], 18081);
    let state = h.start("s2", "w1", json!({})).await;
    assert_eq!(state["state"], "failed");
    assert!(
        state["error"]
            .as_str()
            .unwrap()
            .contains("schon in einer Sitzung")
    );
    let state = h.start("s3", "w3", json!({})).await;
    assert_eq!(state["state"], "running");
    assert_eq!(state["preview_port"], 18082);
    // Zweiter Start derselben Sitzung meldet nur den Zustand.
    let state = h.start("s1", "w1", json!({})).await;
    assert_eq!(
        (state["session"].as_str(), state["state"].as_str()),
        (Some("s1"), Some("running"))
    );
}

#[tokio::test]
async fn allowlist_starts_relay_outside_work_container() {
    let h = Harness::new(config());
    let state = h
        .start(
            "s1",
            "w1",
            json!({"network": "allowlist", "allow": [{"host": "192.168.1.20", "port": 22}]}),
        )
        .await;
    assert_eq!(state["state"], "running", "{state}");
    assert_eq!(state["limits"]["network"], "allowlist");
    {
        let st = h.engine.state();
        let by_role = |role: &str| {
            st.created
                .iter()
                .find(|b| b["labels"][template::LABEL_ROLE] == role)
                .cloned()
                .unwrap()
        };
        let (relay, work) = (by_role("relay"), by_role("work"));
        assert_eq!(
            relay["command"].as_array().unwrap()[3..],
            [json!("192.168.1.20:22")]
        );
        let nets: BTreeSet<String> = relay["Networks"]
            .as_object()
            .unwrap()
            .keys()
            .cloned()
            .collect();
        assert_eq!(
            nets,
            BTreeSet::from([
                template::network_name("s1", "rel"),
                template::network_name("s1", "out")
            ])
        );
        assert_eq!(
            work["Networks"]
                .as_object()
                .unwrap()
                .keys()
                .collect::<Vec<_>>(),
            [&template::network_name("s1", "net")]
        );
        assert_eq!(
            st.networks[&template::network_name("s1", "net")]["internal"],
            json!(true)
        );
        assert_eq!(
            st.networks[&template::network_name("s1", "rel")]["internal"],
            json!(true)
        );
        assert_eq!(
            st.networks[&template::network_name("s1", "out")]["internal"],
            json!(false)
        );
    }
    let relay = h.handle("s1").relay().expect("Relay");
    assert_eq!(relay.targets, vec![("192.168.1.20".to_string(), 22)]);
    h.manager.stop("s1", "user").await;
    let st = h.engine.state();
    assert!(st.containers.is_empty() && st.networks.is_empty());
}

#[tokio::test]
async fn adopt_running_containers_after_restart() {
    let h = Harness::new(config());
    h.start(
        "s1",
        "w1",
        json!({"network": "allowlist", "allow": [{"host": "10.0.0.5", "port": 23}]}),
    )
    .await;
    let (outbox, _rx) = Outbox::channel(16);
    let second = SessionManager::new(h.engine.clone(), &config(), outbox);
    second.adopt().await;
    let infos = second.infos();
    assert_eq!(infos.len(), 1);
    assert_eq!(infos[0].state, "running");
    assert_eq!(infos[0].preview_port, Some(18080));
    assert_eq!(infos[0].workspace.as_deref(), Some("w1"));
    let session = second.get("s1").unwrap();
    assert_eq!(session.state().limits.memory_mb, 512);
    assert_eq!(
        session.state().relay_targets,
        vec![("10.0.0.5".to_string(), 23)]
    );
    assert_eq!(session.command_seconds(), 60);
}

#[tokio::test]
async fn adopt_removes_containers_without_spec() {
    let h = Harness::new(config());
    h.start("s1", "w1", json!({})).await;
    let name = h.work("s1");
    h.engine.state().containers.get_mut(&name).unwrap().body["labels"][template::LABEL_SPEC] =
        json!("kaputt");
    let (outbox, _rx) = Outbox::channel(16);
    let second = SessionManager::new(h.engine.clone(), &config(), outbox);
    second.adopt().await;
    assert!(second.infos().is_empty());
    let st = h.engine.state();
    assert!(st.containers.is_empty() && st.networks.is_empty());
}

#[tokio::test]
async fn idle_sessions_are_stopped() {
    let h = Harness::new(config());
    h.start("s1", "w1", json!({"idle_minutes": 1})).await;
    h.manager.check_idle().await;
    assert!(h.manager.get("s1").is_ok());
    // Beschäftigt (Werkzeug läuft): kein Stopp
    *guard(&h.clock) += 61.0;
    let lock = h.manager.get("s1").unwrap().lock.clone();
    let held = lock.lock().await;
    h.manager.check_idle().await;
    assert!(h.manager.get("s1").is_ok());
    drop(held);
    h.manager.check_idle().await;
    assert!(h.manager.get("s1").is_err());
    h.settle().await;
    assert_eq!(h.last("session_state")["state"], "stopped");
}

// --- Arbeitsstand -------------------------------------------------------------------

#[tokio::test]
async fn file_changes_commit_and_upload_bundle() {
    let h = Harness::new(config());
    h.start("s1", "w1", json!({})).await;
    assert_eq!(
        h.write("s1", "app.py", "print('hallo')\n").await.unwrap(),
        ""
    );
    h.settle().await;
    let upload = h.frames("bundle_chunk");
    let last = upload.last().unwrap();
    assert_eq!(last["last"], true);
    assert_eq!(last["message"], "write app.py");
    assert_eq!(last["head"].as_str().unwrap().len(), 40);
    assert!(guard(&h.server.bundles).contains_key("w1"));
    let read = h.read("s1", "app.py").await.unwrap();
    assert!(read.to_string().contains("hallo"), "{read}");
    // Keine Änderung: kein weiteres Bundle
    assert_eq!(h.handle("s1").save("nichts").await, "");
    h.settle().await;
    assert_eq!(
        h.frames("bundle_chunk")
            .iter()
            .filter(|f| f["last"] == true)
            .count(),
        1
    );
    assert_eq!(
        h.manager.get("s1").unwrap().state().head.as_deref(),
        last["head"].as_str()
    );
}

#[tokio::test]
async fn bundle_is_loaded_into_new_session() {
    let h = Harness::new(config());
    h.start("s1", "w1", json!({})).await;
    h.write("s1", "notes/a.md", "# A\n").await.unwrap();
    h.manager.stop("s1", "user").await;
    let state = h.start("s2", "w1", json!({})).await;
    assert_eq!(state["state"], "running", "{state}");
    assert_eq!(h.last("bundle_get")["session"], "s2");
    assert!(h.frames("bundle_ack").iter().any(|f| f["ok"] == true));
    let read = h.read("s2", "notes/a.md").await.unwrap();
    assert!(read.to_string().contains("# A"));
}

#[tokio::test]
async fn broken_bundle_fails_the_start() {
    let h = Harness::new(config());
    guard(&h.server.bundles).insert("w1".into(), b"kein bundle".to_vec());
    let state = h.start("s1", "w1", json!({})).await;
    assert_eq!(state["state"], "failed");
    assert!(
        state["error"].as_str().unwrap().contains("git clone"),
        "{state}"
    );
    assert!(h.engine.state().containers.is_empty());
    assert!(h.engine.state().networks.is_empty());
}

#[tokio::test]
async fn bad_chunk_is_rejected_with_ack() {
    let h = Harness::new(config());
    let (tx, rx) = oneshot::channel();
    guard(&h.manager.downloads)
        .insert("dl-1".into(), (BundleAssembler::new("dl-1", 100), Some(tx)));
    let Frame::BundleChunk(chunk) = server_frame(
        "bundle_chunk",
        json!({"session": "s1", "transfer": "dl-1", "seq": 0,
               "data": protocol::b64encode(b"abc"), "last": true,
               "size": 3, "sha256": "0".repeat(64)}),
    ) else {
        unreachable!()
    };
    h.manager.on_bundle_chunk(&chunk).await.unwrap();
    h.settle().await;
    let ack = h.last("bundle_ack");
    assert_eq!(
        (ack["ok"].clone(), ack["transfer"].clone()),
        (json!(false), json!("dl-1"))
    );
    assert!(ack["error"].as_str().unwrap().contains("Prüfsumme"));
    let err = rx.await.unwrap().unwrap_err();
    assert_eq!(err.code, "bundle");
}

#[tokio::test]
async fn unknown_transfer_is_reported() {
    let h = Harness::new(config());
    let Frame::BundleChunk(chunk) = server_frame(
        "bundle_chunk",
        json!({"session": "s1", "transfer": "dl-x", "seq": 0, "data": "", "last": true,
               "size": 0, "sha256": protocol::bundle::sha256_hex(b"")}),
    ) else {
        unreachable!()
    };
    h.manager.on_bundle_chunk(&chunk).await.unwrap();
    h.settle().await;
    assert_eq!(h.last("error")["code"], "bundle");
    assert_eq!(h.last("error")["ref"], "dl-x");
}

#[tokio::test]
async fn rejected_upload_is_reported() {
    let h = Harness::new(config());
    h.server.reject_upload.store(true, Ordering::SeqCst);
    h.start("s1", "w1", json!({})).await;
    let note = h.write("s1", "a.txt", "x").await.unwrap();
    assert!(note.contains("nicht gespeichert: zu groß"), "{note}");
}

#[tokio::test]
async fn path_outside_workspace_is_a_tool_error() {
    let h = Harness::new(config());
    h.start("s1", "w1", json!({})).await;
    let err = h.write("s1", "/etc/passwd", "x").await.unwrap_err();
    assert!(err.message.contains("/workspace"), "{err}");
}

// --- Terminal und Vorschau --------------------------------------------------------------

#[tokio::test]
async fn terminal_from_browser() {
    let h = Harness::new(config());
    h.start("s1", "w1", json!({})).await;
    let Frame::PtyOpen(open) = server_frame(
        "pty_open",
        json!({"session": "s1", "pty": "t1", "cols": 80, "rows": 24}),
    ) else {
        unreachable!()
    };
    h.manager.pty_open(&open).await.unwrap();
    assert!(h.manager.pty_open(&open).await.is_err());
    let Frame::PtyInput(input) = server_frame(
        "pty_input",
        json!({"session": "s1", "pty": "t1", "data": protocol::b64encode(b"echo $((6*7)); exit\n")}),
    ) else {
        unreachable!()
    };
    h.manager.pty_input(&input).await.unwrap();
    for _ in 0..200 {
        if h.frames("pty_exit").iter().any(|f| f["pty"] == "t1") {
            break;
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
    let out: Vec<u8> = h
        .frames("pty_data")
        .iter()
        .filter(|f| f["pty"] == "t1")
        .flat_map(|f| protocol::b64decode(f["data"].as_str().unwrap()).unwrap())
        .collect();
    assert!(String::from_utf8_lossy(&out).contains("42"));
    assert_eq!(h.last("pty_exit")["code"], 0);
    assert_eq!(h.manager.get("s1").unwrap().pty_count(), 0);
}

#[tokio::test]
async fn terminal_close_and_stop_end_the_pty() {
    let h = Harness::new(config());
    h.start("s1", "w1", json!({})).await;
    let Frame::PtyOpen(open) = server_frame(
        "pty_open",
        json!({"session": "s1", "pty": "t2", "cols": 80, "rows": 24}),
    ) else {
        unreachable!()
    };
    h.manager.pty_open(&open).await.unwrap();
    let Frame::PtyResize(resize) = server_frame(
        "pty_resize",
        json!({"session": "s1", "pty": "t2", "cols": 100, "rows": 30}),
    ) else {
        unreachable!()
    };
    h.manager.pty_resize(&resize).await.unwrap();
    let Frame::PtyClose(close) = server_frame("pty_close", json!({"session": "s1", "pty": "t2"}))
    else {
        unreachable!()
    };
    h.manager.pty_close(&close).await.unwrap();
    for _ in 0..200 {
        if h.frames("pty_exit").iter().any(|f| f["pty"] == "t2") {
            break;
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
    assert!(h.frames("pty_exit").iter().any(|f| f["pty"] == "t2"));
    // Unbekannte Sitzung bzw. Terminal
    let Frame::PtyInput(input) = server_frame(
        "pty_input",
        json!({"session": "s1", "pty": "nix", "data": ""}),
    ) else {
        unreachable!()
    };
    assert_eq!(
        h.manager.pty_input(&input).await.unwrap_err().code,
        "unknown_session"
    );
}

async fn preview_get<E: Engine + 'static>(h: &Harness<E>, path: &str, method: &str) -> Value {
    let request = format!("r{}", guard(&h.server.frames).len());
    let Frame::PreviewRequest(frame) = server_frame(
        "preview_request",
        json!({"session": "s1", "request": request, "method": method, "path": path}),
    ) else {
        unreachable!()
    };
    h.manager.preview_request(&frame).await.unwrap();
    h.settle().await;
    h.last("preview_response")
}

#[tokio::test]
async fn serve_and_preview_over_back_channel() {
    let h = Harness::new(config());
    h.start("s1", "w1", json!({})).await;
    assert_eq!(preview_get(&h, "/index.html", "GET").await["status"], 404);
    h.write("s1", "site/index.html", "<h1>Vorschau</h1>")
        .await
        .unwrap();
    let info = h.handle("s1").start_preview(Some("site")).await.unwrap();
    let token = info["token"].as_str().unwrap().to_string();
    assert_eq!(info["port"], 18080);
    assert_eq!(info["url"], format!("http://127.0.0.1:18080/{token}/"));
    assert!(token.len() >= 32);
    h.settle().await;
    let preview = h.last("preview");
    assert_eq!(
        (preview["running"].clone(), preview["token"].clone()),
        (json!(true), json!(token))
    );
    let detached = h.engine.state().containers[&h.work("s1")].detached.clone();
    assert_eq!(
        detached.last().unwrap()[..3],
        ["mgpt-serve".to_string(), "--token".into(), token.clone()]
    );
    let resp = preview_get(&h, "/index.html", "GET").await;
    assert_eq!(
        (resp["status"].clone(), resp["last"].clone()),
        (json!(200), json!(true))
    );
    assert_eq!(
        protocol::b64decode(resp["data"].as_str().unwrap()).unwrap(),
        b"<h1>Vorschau</h1>"
    );
    assert!(
        resp["headers"]["Content-Security-Policy"]
            .as_str()
            .unwrap()
            .contains("sandbox")
    );
    assert_eq!(
        preview_get(&h, &format!("/{token}/index.html"), "GET").await["status"],
        200
    );
    assert_eq!(preview_get(&h, "/", "GET").await["status"], 200);
    assert_eq!(preview_get(&h, "/../app.py", "GET").await["status"], 404);
    assert_eq!(preview_get(&h, "/fehlt.html", "GET").await["status"], 404);
    let head = preview_get(&h, "/index.html", "HEAD").await;
    assert!(head.get("data").is_none());
    assert_eq!(head["headers"]["Content-Length"], "17");
    // Vorschau außerhalb von /workspace: abgelehnt
    assert!(h.handle("s1").start_preview(Some("/etc")).await.is_err());
}

// --- Scratchpad --------------------------------------------------------------------------

fn local_config(root: &std::path::Path) -> Config {
    let mut cfg = config();
    cfg.scratch_dir = Some(root.to_path_buf());
    cfg.scratch_host_dir = Some(root.to_path_buf());
    cfg
}

#[tokio::test]
async fn container_scratch_is_tmpfs() {
    let h = Harness::new(config());
    h.start("s1", "w1", json!({"scratch_mb": 100000})).await;
    let created = h.engine.state().created.clone();
    let scratch = created[0]["mounts"]
        .as_array()
        .unwrap()
        .iter()
        .find(|m| m["destination"] == "/scratch")
        .cloned()
        .unwrap();
    assert_eq!(scratch["type"], "tmpfs");
    // Größe auf die lokale tmpfs-Grenze gekappt
    assert!(
        scratch["options"]
            .as_array()
            .unwrap()
            .contains(&json!("size=8192m"))
    );
    assert_eq!(h.handle("s1").scratch_size_mb(), None);
    assert_eq!(h.handle("s1").scratch_rest().await, None);
}

#[tokio::test]
async fn local_scratch_is_the_only_bind_mount_with_hard_limit() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().join("home/MultiGPT/Scratch");
    std::fs::create_dir_all(&root).unwrap();
    let h = Harness::new(local_config(&root));
    let state = h
        .start(
            "s1",
            "w1",
            json!({"scratch_mode": "local", "scratch_mb": 1, "workspace_uuid": UUID}),
        )
        .await;
    assert_eq!(state["state"], "running", "{state}");
    {
        let st = h.engine.state();
        let body = &st.created[0];
        let binds: Vec<&Value> = body["mounts"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|m| m["type"] == "bind")
            .collect();
        assert_eq!(
            binds,
            [&json!({"destination": "/scratch", "type": "bind",
                     "source": format!("{}/{UUID}", root.display()),
                     "options": ["rbind", "rw", "noexec", "nosuid", "nodev"]})]
        );
        assert_eq!(
            body["userns"],
            json!({"nsmode": "keep-id", "value": "uid=1000,gid=1000"})
        );
    }
    assert!(root.join(UUID).is_dir());
    h.write("s1", "/scratch/a.txt", "x").await.unwrap();
    assert_eq!(
        std::fs::read_to_string(root.join(UUID).join("a.txt")).unwrap(),
        "x"
    );
    // mgpt-fs prüft vor dem Schreiben: Datei über der Grenze wird abgelehnt
    let err = h
        .write("s1", "/scratch/b.txt", &"y".repeat(2 << 20))
        .await
        .unwrap_err();
    assert!(err.message.contains("Scratchpad voll"), "{err}");
    let handle = h.handle("s1");
    assert_eq!(handle.scratch_size_mb(), Some(1));
    let rest = handle.scratch_rest().await.unwrap();
    assert!(rest > 0 && rest <= 1 << 20);
    assert!(!handle.scratch_full().await);
    assert_eq!(handle.scratch_note().await, "");
    // Über der Grenze (z. B. von außen befüllt): Hinweis, Rest 0
    std::fs::write(root.join(UUID).join("big.bin"), vec![7u8; 2 << 20]).unwrap();
    assert!(handle.scratch_full().await);
    assert!(
        handle
            .scratch_note()
            .await
            .contains("Scratchpad belegt 2 MB, Grenze 1 MB")
    );
    assert_eq!(handle.scratch_rest().await, Some(0));
    // Messung je Heartbeat
    h.manager.check_idle().await;
    assert!(h.manager.get("s1").unwrap().state().scratch_used >= 2 << 20);
    std::fs::remove_file(root.join(UUID).join("big.bin")).unwrap();
    assert!(!handle.scratch_full().await);
}

#[tokio::test]
async fn local_scratch_checks() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().join("home/MultiGPT/Scratch");
    std::fs::create_dir_all(&root).unwrap();
    let plain = Harness::new(config());
    let state = plain
        .start(
            "s1",
            "w1",
            json!({"scratch_mode": "local", "workspace_uuid": UUID}),
        )
        .await;
    assert_eq!(state["state"], "failed");
    assert!(
        state["error"]
            .as_str()
            .unwrap()
            .contains("nicht eingerichtet")
    );
    let h = Harness::new(local_config(&root));
    let state = h.start("s1", "w1", json!({"scratch_mode": "local"})).await;
    assert_eq!(state["state"], "failed");
    assert!(state["error"].as_str().unwrap().contains("UUID"));
    std::os::unix::fs::symlink(tmp.path(), root.join(UUID)).unwrap();
    let state = h
        .start(
            "s2",
            "w2",
            json!({"scratch_mode": "local", "workspace_uuid": UUID}),
        )
        .await;
    assert_eq!(state["state"], "failed");
    assert!(
        state["error"].as_str().unwrap().contains("Symlink"),
        "{state}"
    );
}

// --- Hilfscontainer, PTY der Werkzeuge, Aufräumen ----------------------------------------

#[tokio::test]
async fn helper_container_only_on_relay_network_and_removed_with_session() {
    let h = Harness::new(config());
    h.start(
        "s1",
        "w1",
        json!({"network": "allowlist", "allow": [{"host": "192.168.1.20", "port": 22}]}),
    )
    .await;
    let handle = h.handle("s1");
    let helper = handle.open_helper().await.unwrap();
    {
        let st = h.engine.state();
        let body = &st.containers[&helper].body;
        assert_eq!(body["labels"][template::LABEL_ROLE], "tool");
        assert_eq!(body["labels"]["multigpt.test"], "1");
        assert_eq!(
            body["Networks"]
                .as_object()
                .unwrap()
                .keys()
                .collect::<Vec<_>>(),
            [&template::network_name("s1", "rel")]
        );
        let dests: Vec<&str> = body["mounts"]
            .as_array()
            .unwrap()
            .iter()
            .map(|m| m["destination"].as_str().unwrap())
            .collect();
        assert_eq!(dests, ["/tmp"]);
    }
    // Befehle laufen nur in eigenen Hilfscontainern dieser Sitzung.
    let out = handle
        .exec(
            Target::Helper(&helper),
            vec!["sh".into(), "-c".into(), "pwd".into()],
            Vec::new(),
            10,
            1000,
        )
        .await
        .unwrap();
    assert_eq!(out.code, Some(0));
    assert!(
        String::from_utf8_lossy(&out.stdout)
            .trim_end()
            .ends_with("/tmp")
    );
    assert!(
        handle
            .exec(
                Target::Helper("mgpt-ses-x"),
                vec!["true".into()],
                Vec::new(),
                10,
                1000
            )
            .await
            .is_err()
    );
    // Zweiter Hilfscontainer bleibt offen und wird mit der Sitzung entfernt.
    let second = handle.open_helper().await.unwrap();
    handle.close_helper(&helper).await;
    assert!(!h.engine.state().containers.contains_key(&helper));
    assert!(
        handle
            .exec(
                Target::Helper(&helper),
                vec!["true".into()],
                Vec::new(),
                10,
                1000
            )
            .await
            .is_err()
    );
    h.manager.stop("s1", "user").await;
    let st = h.engine.state();
    assert!(!st.containers.contains_key(&second));
    assert!(st.containers.is_empty() && st.networks.is_empty());
}

#[tokio::test]
async fn tool_pty_is_registered_until_closed() {
    let h = Harness::new(config());
    h.start("s1", "w1", json!({})).await;
    let handle = h.handle("s1");
    let pty = handle
        .exec_pty(
            "run-1",
            Target::Work,
            vec!["sh".into(), "-c".into(), "echo da; sleep 30".into()],
        )
        .await
        .unwrap();
    let session = h.manager.get("s1").unwrap();
    assert!(session.has_pty("run-1"));
    let first = pty.read().await.unwrap();
    assert!(String::from_utf8_lossy(&first).contains("da"));
    // Laufendes PTY zählt als beschäftigt.
    *guard(&h.clock) += 10_000.0;
    h.manager.check_idle().await;
    assert!(h.manager.get("s1").is_ok());
    pty.close().await;
    assert!(!session.has_pty("run-1"));
    assert_eq!(pty.read().await, None);
    let out = handle
        .exec(
            Target::Work,
            vec!["sh".into(), "-c".into(), "cat; exit 4".into()],
            b"eingabe".to_vec(),
            10,
            3,
        )
        .await
        .unwrap();
    assert_eq!(
        (out.code, out.stdout.as_slice()),
        (Some(4), b"ein".as_slice())
    );
    let out = handle
        .exec(
            Target::Work,
            vec!["sleep".into(), "5".into()],
            Vec::new(),
            1,
            100,
        )
        .await
        .unwrap();
    assert!(out.timed_out);
}

#[tokio::test]
async fn limits_and_urls_for_the_tools() {
    let h = Harness::new(config());
    h.start(
        "s1",
        "w1",
        json!({"command_seconds": 30, "output_bytes": 2048}),
    )
    .await;
    let handle = h.handle("s1");
    assert_eq!(
        handle.limits(),
        ToolLimits {
            command_seconds: 30,
            output_bytes: 2048,
            tmpfs_mb: 64
        }
    );
    assert_eq!(handle.server_url(), "https://mgpt.example");
    assert_eq!(
        base_url("ws://localhost:8000/runner/ws"),
        "http://localhost:8000"
    );
    assert!(handle.relay().is_none());
    assert!(
        handle
            .download("https://mgpt.example/runner/download/amd64", "t", 10)
            .await
            .is_err()
    );
    assert!(h.manager.handle("s9").is_err());
}

#[tokio::test]
async fn stop_all_and_template_error_on_start() {
    let h = Harness::new(config());
    h.start("s1", "w1", json!({})).await;
    h.start("s2", "w2", json!({})).await;
    h.manager.stop_all().await;
    assert!(h.manager.infos().is_empty());
    assert!(h.engine.state().containers.is_empty());
    // Start scheitert am Container: Netz wird wieder entfernt
    h.engine.state().fail_start.insert(h.work("s3"));
    let state = h.start("s3", "w3", json!({})).await;
    assert_eq!(state["state"], "failed");
    let st = h.engine.state();
    assert!(st.containers.is_empty() && st.networks.is_empty());
}

#[test]
fn du_counts_blocks_without_following_symlinks() {
    let tmp = tempfile::tempdir().unwrap();
    std::fs::create_dir(tmp.path().join("sub")).unwrap();
    std::fs::write(tmp.path().join("sub/a"), vec![1u8; 10_000]).unwrap();
    std::os::unix::fs::symlink("/usr", tmp.path().join("link")).unwrap();
    let used = du(tmp.path());
    assert!((10_000..100_000).contains(&used), "{used}");
}

// --- Paketquellen (Modus packages) ------------------------------------------------------

fn packages_extra() -> Value {
    json!({
        "network": "packages",
        "package_hosts": ["deb.debian.org", "pypi.org"],
        "rootfs_mb": 512,
    })
}

fn created_by_role(h: &Harness, role: &str) -> Value {
    h.engine
        .state()
        .created
        .iter()
        .find(|b| b["labels"][template::LABEL_ROLE] == role)
        .cloned()
        .unwrap()
}

fn workspace_file(h: &Harness, sid: &str, path: &str) -> Option<String> {
    let dir = h.engine.workspace(&h.work(sid)).ok()?;
    std::fs::read_to_string(dir.join(path)).ok()
}

#[tokio::test]
async fn packages_mode_starts_proxy_and_relaxed_work_container() {
    let h = Harness::new(config());
    let state = h.start("s1", "w1", packages_extra()).await;
    assert_eq!(state["state"], "running", "{state}");
    assert_eq!(state["limits"]["network"], "packages");
    assert_eq!(state["limits"]["rootfs_mb"], 512);
    let proxy = created_by_role(&h, "relay");
    let work = created_by_role(&h, "work");
    assert_eq!(
        proxy["command"],
        json!([
            "mgpt-relay",
            "--proxy",
            "3128",
            "deb.debian.org",
            "pypi.org"
        ])
    );
    assert_eq!(proxy["read_only_filesystem"], json!(true));
    assert!(proxy.get("cap_add").is_none());
    let nets: BTreeSet<String> = proxy["Networks"]
        .as_object()
        .unwrap()
        .keys()
        .cloned()
        .collect();
    let (net, out) = (
        template::network_name("s1", "net"),
        template::network_name("s1", "out"),
    );
    assert_eq!(nets, BTreeSet::from([net.clone(), out.clone()]));
    assert_eq!(work["Networks"], json!({ net.clone(): {} }));
    assert_eq!(work["read_only_filesystem"], json!(false));
    assert_eq!(work["cap_add"], json!(template::CAPS_PACKAGES));
    assert_eq!(work["no_new_privileges"], json!(true));
    assert_eq!(work["labels"][template::LABEL_NETWORK], "packages");
    let url = work["env"]["HTTPS_PROXY"].as_str().unwrap().to_string();
    assert!(
        url.starts_with("http://10.89.") && url.ends_with(":3128"),
        "{url}"
    );
    for key in template::PROXY_ENV_KEYS {
        assert_eq!(work["env"][key], json!(url));
    }
    assert_eq!(work["env"]["NO_PROXY"], template::NO_PROXY);
    {
        let st = h.engine.state();
        assert_eq!(st.networks[&net]["internal"], json!(true));
        assert_eq!(st.networks[&out]["internal"], json!(false));
        assert!(
            !st.networks
                .contains_key(&template::network_name("s1", "rel"))
        );
    }
    // .gitignore steht nach dem Start im Arbeitsstand und ist gespeichert.
    let ignore = workspace_file(&h, "s1", ".gitignore").unwrap();
    for entry in GITIGNORE_ENTRIES {
        assert!(ignore.lines().any(|l| l == entry), "{ignore}");
    }
    assert!(!h.frames("bundle_chunk").is_empty());
    // Keine Pakete vermerkt: kein apt beim Start.
    assert!(h.engine.state().root_calls.is_empty());
    assert_eq!(h.manager.infos()[0].rootfs_used_mb, Some(0));
    h.manager.stop("s1", "user").await;
    let st = h.engine.state();
    assert!(st.containers.is_empty() && st.networks.is_empty());
}

#[tokio::test]
async fn packages_fields_are_checked_again_by_the_agent() {
    let h = Harness::new(config());
    // Ohne Hosts (am Schema vorbei) bzw. mit ungültigem Host: Start schlägt fehl.
    for hosts in [None, Some(vec!["10.0.0.1".to_string()]), Some(vec![])] {
        let mut frame = h.start_frame("s1", "w1", packages_extra());
        frame.package_hosts = hosts;
        h.manager.start(&frame).await;
        h.settle().await;
        let state = h.last("session_state");
        assert_eq!(state["state"], "failed", "{state}");
    }
    let mut frame = h.start_frame("s1", "w1", packages_extra());
    frame.rootfs_mb = None;
    h.manager.start(&frame).await;
    h.settle().await;
    assert_eq!(h.last("session_state")["state"], "failed");
    // Hosts ohne Modus packages ebenso.
    let mut frame = h.start_frame("s1", "w1", json!({}));
    frame.package_hosts = Some(vec!["pypi.org".into()]);
    h.manager.start(&frame).await;
    h.settle().await;
    assert_eq!(h.last("session_state")["state"], "failed");
    assert!(h.engine.state().containers.is_empty());
}

#[tokio::test]
async fn apt_install_runs_fixed_root_commands_and_records_packages() {
    let h = Harness::new(config());
    h.start("s1", "w1", packages_extra()).await;
    let handle = h.handle("s1");
    let out = handle
        .apt_install(vec!["jq".into(), "ripgrep".into()])
        .await
        .unwrap();
    assert_eq!(out.code, Some(0), "{}", out.output);
    assert!(out.output.contains("$ apt-get update"), "{}", out.output);
    let out = handle.apt_install(vec!["jq".into()]).await.unwrap();
    assert!(!out.output.contains("apt-get update"), "{}", out.output);
    let calls: Vec<Vec<String>> = h
        .engine
        .state()
        .root_calls
        .iter()
        .map(|(_, argv)| argv.clone())
        .collect();
    let mut install = template::apt_install_argv(&["jq".into(), "ripgrep".into()]).unwrap();
    assert_eq!(
        calls,
        vec![
            template::apt_update_argv(),
            install.clone(),
            template::strip_setuid_argv(),
            {
                install.pop();
                install
            },
            template::strip_setuid_argv(),
        ]
    );
    let recorded = workspace_file(&h, "s1", APT_PACKAGES_FILE).unwrap();
    assert_eq!(parse_packages(&recorded), vec!["jq", "ripgrep"]);
    // Paketlisten werden nach einer Stunde neu geladen.
    *guard(&h.clock) += APT_UPDATE_SECONDS + 1.0;
    let out = handle.apt_install(vec!["jq".into()]).await.unwrap();
    assert!(out.output.contains("$ apt-get update"), "{}", out.output);
}

#[tokio::test]
async fn apt_install_failure_is_not_recorded() {
    let h = Harness::new(config());
    h.start("s1", "w1", packages_extra()).await;
    h.engine
        .state()
        .fake_commands
        .insert("apt-get".into(), "echo E: kaputt >&2; exit 100".into());
    let out = h
        .handle("s1")
        .apt_install(vec!["gibtsnicht".into()])
        .await
        .unwrap();
    // update schlägt schon fehl: kein install, kein Eintrag.
    assert_eq!(out.code, Some(100));
    assert!(out.output.contains("E: kaputt"), "{}", out.output);
    assert!(workspace_file(&h, "s1", APT_PACKAGES_FILE).is_none());
}

#[tokio::test]
async fn apt_install_refused_outside_packages_mode() {
    let h = Harness::new(config());
    h.start("s1", "w1", json!({})).await;
    let err = h
        .handle("s1")
        .apt_install(vec!["jq".into()])
        .await
        .unwrap_err();
    assert!(err.message.contains("Paketquellen"), "{err}");
    assert!(h.engine.state().root_calls.is_empty());
    // Auch die Engine selbst lässt root im schreibgeschützten Container nicht zu.
    let res = h
        .engine
        .exec(
            &h.work("s1"),
            &template::apt_update_argv(),
            ExecOptions {
                root: true,
                ..ExecOptions::default()
            },
        )
        .await;
    assert!(res.is_err());
}

#[tokio::test]
async fn recorded_packages_are_installed_on_next_start() {
    let h = Harness::new(config());
    h.start("s1", "w1", packages_extra()).await;
    h.handle("s1")
        .apt_install(vec!["jq".into(), "sqlite3".into()])
        .await
        .unwrap();
    assert!(h.handle("s1").save("apt_install").await.is_empty());
    h.manager.stop("s1", "user").await;
    h.engine.state().root_calls.clear();
    let state = h.start("s2", "w1", packages_extra()).await;
    assert_eq!(state["state"], "running", "{state}");
    let calls: Vec<Vec<String>> = h
        .engine
        .state()
        .root_calls
        .iter()
        .map(|(_, argv)| argv.clone())
        .collect();
    assert_eq!(
        calls,
        vec![
            template::apt_update_argv(),
            template::apt_install_argv(&["jq".into(), "sqlite3".into()]).unwrap(),
            template::strip_setuid_argv(),
        ]
    );
}

#[tokio::test]
async fn rootfs_is_measured_and_enforced() {
    let h = Harness::new(config());
    h.start("s1", "w1", packages_extra()).await;
    let work = h.work("s1");
    h.engine.state().sizes.insert(work.clone(), 300 << 20);
    h.manager.check_idle().await;
    assert_eq!(h.manager.infos()[0].rootfs_used_mb, Some(300));
    assert!(!h.handle("s1").rootfs_full());
    // An der Grenze: apt gesperrt, Sitzung läuft weiter.
    h.engine.state().sizes.insert(work.clone(), 512 << 20);
    h.manager.check_idle().await;
    assert!(h.handle("s1").rootfs_full());
    let err = h
        .handle("s1")
        .apt_install(vec!["jq".into()])
        .await
        .unwrap_err();
    assert!(err.message.contains("Container-Schicht"), "{err}");
    // Darüber: Sitzung wird beendet.
    h.engine.state().sizes.insert(work, (512 << 20) + 1);
    h.manager.check_idle().await;
    h.settle().await;
    assert!(h.manager.infos().is_empty());
    let state = h.last("session_state");
    assert_eq!(state["state"], "stopped");
    assert!(
        state["error"]
            .as_str()
            .unwrap_or("")
            .contains("Container-Schicht")
    );
}

#[test]
fn package_list_and_gitignore_helpers() {
    assert_eq!(
        parse_packages("# Kommentar\njq\n  ripgrep # Suche\nBad\n-o\njq\n\nlibssl-dev\n"),
        vec!["jq", "ripgrep", "libssl-dev"]
    );
    assert_eq!(missing_ignores(""), GITIGNORE_ENTRIES.to_vec());
    assert_eq!(
        missing_ignores("/node_modules\n.venv\n*.pyc\n"),
        vec!["target/"]
    );
    assert!(missing_ignores(".venv/\nnode_modules/\ntarget/\n").is_empty());
}

// --- Echter Podman (Feature podman-tests) -----------------------------------------------

/// Tests mit echtem Podman: `cargo test --features podman-tests -- session::tests::podman`.
///
/// Nutzt das lokal gebaute Arbeits-Image (`localhost/mgpt-test-work:dev` bzw.
/// `MGPT_TEST_WORK_IMAGE`) und legt ausschließlich eigene Objekte an: Labels
/// `multigpt.test=1` und `multigpt.test.run=<Kennung dieses Laufs>` zusätzlich zu
/// `multigpt.runner=1`. Am Ende werden nur Objekte dieses Laufs entfernt; andere
/// Container (z. B. n8n) bleiben unberührt.
#[cfg(feature = "podman-tests")]
mod podman {
    use std::process::Command;
    use std::sync::LazyLock;

    use super::*;
    use crate::podman::PodmanEngine;

    static RUN_ID: LazyLock<String> = LazyLock::new(|| random_hex(8));

    fn socket() -> Option<PathBuf> {
        let dir = std::env::var_os("XDG_RUNTIME_DIR")?;
        let path = PathBuf::from(dir).join("podman/podman.sock");
        path.exists().then_some(path)
    }

    fn work_image() -> Option<String> {
        let wanted = std::env::var("MGPT_TEST_WORK_IMAGE")
            .unwrap_or_else(|_| "localhost/mgpt-test-work:dev".into());
        if wanted.contains("@sha256:") {
            return Some(wanted);
        }
        let out = Command::new("podman")
            .args([
                "image",
                "inspect",
                &wanted,
                "--format",
                "{{json .RepoDigests}}",
            ])
            .output()
            .ok()?;
        if !out.status.success() {
            return None;
        }
        let digests: Vec<String> = serde_json::from_slice(&out.stdout).ok()?;
        digests.into_iter().next()
    }

    /// Nur Objekte dieses Tests (Label multigpt.test.run) mit eigenem Namensmuster entfernen.
    struct Cleanup(String);

    impl Drop for Cleanup {
        fn drop(&mut self) {
            let filter = format!("label=multigpt.test.run={}", self.0);
            if let Ok(out) = Command::new("podman")
                .args([
                    "ps",
                    "-a",
                    "--filter",
                    &filter,
                    "--filter",
                    "label=multigpt.runner=1",
                ])
                .args(["--format", "{{.Names}}"])
                .output()
            {
                for name in String::from_utf8_lossy(&out.stdout).split_whitespace() {
                    if template::NAME_RE.is_match(name) {
                        let _ = Command::new("podman")
                            .args(["rm", "-f", "-t", "0", name])
                            .output();
                    }
                }
            }
            if let Ok(out) = Command::new("podman")
                .args([
                    "network",
                    "ls",
                    "--filter",
                    &filter,
                    "--format",
                    "{{.Name}}",
                ])
                .output()
            {
                for name in String::from_utf8_lossy(&out.stdout).split_whitespace() {
                    if template::NETWORK_RE.is_match(name) {
                        let _ = Command::new("podman")
                            .args(["network", "rm", name])
                            .output();
                    }
                }
            }
        }
    }

    struct Setup {
        harness: Harness<PodmanEngine>,
        _cleanup: Cleanup,
    }

    fn setup(test: u32, local_root: Option<&std::path::Path>) -> Option<Setup> {
        let run = format!("{}-{test}", *RUN_ID);
        let (Some(socket), Some(image)) = (socket(), work_image()) else {
            eprintln!("Podman-Socket oder Arbeits-Image fehlt – Test übersprungen.");
            return None;
        };
        let mut cfg = Config::new("wss://mgpt.example/runner/ws");
        cfg.images.insert(image);
        cfg.labels.insert("multigpt.test".into(), "1".into());
        cfg.labels.insert("multigpt.test.run".into(), run.clone());
        if let Some(root) = local_root {
            cfg.scratch_dir = Some(root.to_path_buf());
            cfg.scratch_host_dir = Some(root.to_path_buf());
        }
        let engine = Arc::new(PodmanEngine::new(Bounds::from(&cfg), socket));
        Some(Setup {
            harness: Harness::with_engine(cfg, engine),
            _cleanup: Cleanup(run),
        })
    }

    fn sid(n: u32) -> String {
        format!("rp-{}-{n}", *RUN_ID)
    }

    async fn sh<E: Engine + 'static>(
        h: &Harness<E>,
        sid: &str,
        target: Target<'_>,
        cmd: &str,
    ) -> String {
        let out = h
            .handle(sid)
            .exec(
                target,
                vec!["bash".into(), "-c".into(), cmd.into()],
                Vec::new(),
                60,
                1 << 20,
            )
            .await
            .expect("exec");
        format!(
            "{}{}",
            String::from_utf8_lossy(&out.stdout),
            String::from_utf8_lossy(&out.stderr)
        )
    }

    async fn http_get(port: u16, path: &str) -> (u16, Vec<u8>) {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let Ok(mut stream) = tokio::net::TcpStream::connect(("127.0.0.1", port)).await else {
            return (0, Vec::new());
        };
        let request = format!("GET {path} HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n");
        if stream.write_all(request.as_bytes()).await.is_err() {
            return (0, Vec::new());
        }
        let mut data = Vec::new();
        let _ = stream.read_to_end(&mut data).await;
        let text = String::from_utf8_lossy(&data);
        let status = text
            .split(' ')
            .nth(1)
            .and_then(|s| s.parse().ok())
            .unwrap_or(0);
        let body = data
            .windows(4)
            .position(|w| w == b"\r\n\r\n")
            .map(|i| data[i + 4..].to_vec())
            .unwrap_or_default();
        (status, body)
    }

    async fn run<F: std::future::Future<Output = ()>>(f: F) {
        tokio::time::timeout(Duration::from_secs(300), f)
            .await
            .expect("Zeitlimit");
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn podman_work_container_end_to_end() {
        let Some(setup) = setup(1, None) else { return };
        let h = &setup.harness;
        run(async {
            let s1 = sid(1);
            let state = h
                .start(&s1, "pw1", json!({"cpus": 1.0, "memory_mb": 256}))
                .await;
            assert_eq!(state["state"], "running", "{state}");
            assert_eq!(state["limits"]["memory_mb"], 256);
            assert_eq!(state["limits"]["cpus"], 1.0);
            let name = template::work_name(&s1);
            let info = h.engine.inspect_container(&name).await.unwrap();
            assert_eq!(info["Config"]["Labels"]["multigpt.test"], "1");
            let session = h.manager.get(&s1).unwrap();
            let (limits, port) = {
                let st = session.state();
                (st.limits.clone(), st.preview_port)
            };
            assert!(template::check_inspect(&info, &limits, port.map(u64::from), None).is_empty());

            // Dateien, git und Python im Container
            assert_eq!(h.write(&s1, "main.py", "print(6*7)\n").await.unwrap(), "");
            h.settle().await;
            assert!(guard(&h.server.bundles).contains_key("pw1"));
            let out = sh(h, &s1, Target::Work, "python3 main.py").await;
            assert!(out.contains("42"), "{out}");
            let out = sh(h, &s1, Target::Work, "id -u; git log --oneline | wc -l").await;
            assert_eq!(out.lines().next(), Some("1000"), "{out}");

            // Netz aus: keine Route, kein DNS
            let out = sh(
                h,
                &s1,
                Target::Work,
                "timeout 5 bash -c 'echo > /dev/tcp/1.1.1.1/443' && echo OFFEN || echo ZU; \
                 getent hosts example.com || echo KEIN-DNS",
            )
            .await;
            assert!(out.contains("ZU") && !out.contains("OFFEN") && out.contains("KEIN-DNS"), "{out}");

            // Keine Geheimnisse des Rechners: Umgebung und Dateisystem
            let out = sh(h, &s1, Target::Work, "env | cut -d= -f1 | sort | tr '\\n' ' '").await;
            for secret in ["SSH_AUTH_SOCK", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"] {
                assert!(!out.contains(secret), "{out}");
            }
            let out = sh(h, &s1, Target::Work, "touch /etc/x 2>&1 | head -1").await;
            assert!(out.contains("Read-only"), "{out}");

            // Pfad hinaus wird abgelehnt
            let err = h.read(&s1, "/etc/passwd").await.unwrap_err();
            assert!(err.message.contains("/workspace"), "{err}");
            sh(h, &s1, Target::Work, "ln -s /etc/passwd leak").await;
            let err = h.read(&s1, "leak").await.unwrap_err();
            assert!(err.message.contains("Symlink"), "{err}");

            // Terminal über den Rückkanal
            let Frame::PtyOpen(open) = server_frame(
                "pty_open",
                json!({"session": s1, "pty": "t1", "cols": 80, "rows": 24}),
            ) else {
                unreachable!()
            };
            h.manager.pty_open(&open).await.unwrap();
            let Frame::PtyInput(input) = server_frame(
                "pty_input",
                json!({"session": s1, "pty": "t1", "data": protocol::b64encode(b"echo $((6*7)); exit\n")}),
            ) else {
                unreachable!()
            };
            h.manager.pty_input(&input).await.unwrap();
            for _ in 0..200 {
                if h.frames("pty_exit").iter().any(|f| f["pty"] == "t1") {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(50)).await;
            }
            let out: Vec<u8> = h
                .frames("pty_data")
                .iter()
                .filter(|f| f["pty"] == "t1")
                .flat_map(|f| protocol::b64decode(f["data"].as_str().unwrap()).unwrap())
                .collect();
            assert!(String::from_utf8_lossy(&out).contains("42"));

            // Vorschau an 127.0.0.1 mit Token, ohne Verzeichnislisten
            h.write(&s1, "site/index.html", "<h1>Hi</h1>").await.unwrap();
            let info = h.handle(&s1).start_preview(Some("site")).await.unwrap();
            let port = info["port"].as_u64().unwrap() as u16;
            let token = info["token"].as_str().unwrap().to_string();
            let mut got = (0, Vec::new());
            for _ in 0..50 {
                got = http_get(port, &format!("/{token}/")).await;
                if got.0 == 200 {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(200)).await;
            }
            assert_eq!(got, (200, b"<h1>Hi</h1>".to_vec()));
            assert_eq!(http_get(port, "/index.html").await.0, 404);
            assert_eq!(http_get(port, &format!("/{token}/../main.py")).await.0, 404);

            h.manager.stop(&s1, "user").await;
            let names: Vec<String> = h
                .engine
                .list_containers()
                .await
                .unwrap()
                .into_iter()
                .map(|c| c.name)
                .collect();
            assert!(!names.contains(&name));

            // Neuer Container lädt den Stand aus dem Bundle
            let s2 = sid(2);
            let state = h.start(&s2, "pw1", json!({})).await;
            assert_eq!(state["state"], "running", "{state}");
            let out = sh(h, &s2, Target::Work, "python3 main.py; git log --oneline").await;
            assert!(out.contains("42") && out.contains("write main.py"), "{out}");
            h.manager.stop(&s2, "user").await;
        })
        .await;
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn podman_allowlist_relay_only_for_helper() {
        let Some(setup) = setup(2, None) else { return };
        let h = &setup.harness;
        run(async {
            let s3 = sid(3);
            let state = h
                .start(
                    &s3,
                    "pw3",
                    json!({"network": "allowlist", "allow": [{"host": "192.0.2.10", "port": 22}]}),
                )
                .await;
            assert_eq!(state["state"], "running", "{state}");
            let relay = h.handle(&s3).relay().expect("Relay");
            assert!(
                relay.ip.starts_with(|c: char| c.is_ascii_digit()),
                "{relay:?}"
            );
            // Arbeitscontainer: weder Relay noch draußen erreichbar
            let out = sh(
                h,
                &s3,
                Target::Work,
                &format!(
                    "timeout 5 bash -c 'echo > /dev/tcp/{}/20000' && echo RELAY-OK; \
                     timeout 5 bash -c 'echo > /dev/tcp/1.1.1.1/443' && echo OFFEN || echo ZU",
                    relay.ip
                ),
            )
            .await;
            assert!(!out.contains("RELAY-OK") && !out.contains("OFFEN"), "{out}");
            // Hilfscontainer: nur das Relay, nur der freigegebene Port
            let handle = h.handle(&s3);
            let helper = handle.open_helper().await.unwrap();
            let out = sh(
                h,
                &s3,
                Target::Helper(&helper),
                &format!(
                    "timeout 5 bash -c 'echo > /dev/tcp/{ip}/20000' && echo RELAY-OK; \
                     timeout 5 bash -c 'echo > /dev/tcp/{ip}/20001' && echo ANDERER; \
                     timeout 5 bash -c 'echo > /dev/tcp/1.1.1.1/443' && echo OFFEN || echo ZU; \
                     id -u; ls /workspace 2>&1 | head -1",
                    ip = relay.ip
                ),
            )
            .await;
            assert!(
                out.contains("RELAY-OK") && !out.contains("ANDERER") && !out.contains("OFFEN"),
                "{out}"
            );
            assert!(out.contains("1000"), "{out}");
            // Zugang nur im Hilfscontainer (head -c, kein EOF nötig)
            let secret = format!("Geheim-{}", *RUN_ID);
            let out = handle
                .exec(
                    Target::Helper(&helper),
                    vec![
                        "sh".into(),
                        "-c".into(),
                        format!("umask 077; head -c {} > /tmp/z; cat /tmp/z", secret.len()),
                    ],
                    secret.clone().into_bytes(),
                    30,
                    1000,
                )
                .await
                .unwrap();
            assert_eq!(String::from_utf8_lossy(&out.stdout), secret);
            let out = sh(
                h,
                &s3,
                Target::Work,
                &format!("grep -rs {secret} /tmp /workspace /scratch; echo fertig"),
            )
            .await;
            assert_eq!(out.trim(), "fertig");
            handle.close_helper(&helper).await;
            let left: Vec<String> = h
                .engine
                .list_containers()
                .await
                .unwrap()
                .into_iter()
                .filter(|c| c.labels.get(template::LABEL_ROLE).map(String::as_str) == Some("tool"))
                .filter(|c| c.labels.get(template::LABEL_SESSION) == Some(&s3))
                .map(|c| c.name)
                .collect();
            assert!(left.is_empty(), "{left:?}");
            h.manager.stop(&s3, "user").await;
            let mine: Vec<String> = h
                .engine
                .list_containers()
                .await
                .unwrap()
                .into_iter()
                .filter(|c| c.labels.get(template::LABEL_SESSION) == Some(&s3))
                .map(|c| c.name)
                .collect();
            assert!(mine.is_empty(), "{mine:?}");
        })
        .await;
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn podman_foreign_objects_are_never_removed() {
        let Some(setup) = setup(3, None) else { return };
        let h = &setup.harness;
        assert!(h.engine.remove_container("n8n").await.is_err());
        assert!(h.engine.remove_network("podman").await.is_err());
        // Eigenes Namensmuster, aber kein eigenes Objekt: nichts passiert bzw. Fehler.
        let fake_name = template::work_name(&format!("nie-{}", *RUN_ID));
        assert!(h.engine.remove_container(&fake_name).await.is_ok());
        let info = h.engine.info().await.unwrap();
        assert_eq!(info.name, "podman");
        assert!(info.rootless);
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn podman_local_scratch() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("MultiGPT/Scratch");
        std::fs::create_dir_all(&root).unwrap();
        let Some(setup) = setup(4, Some(&root)) else {
            return;
        };
        let h = &setup.harness;
        run(async {
            let s4 = sid(4);
            let state = h
                .start(
                    &s4,
                    "pw4",
                    json!({"scratch_mode": "local", "scratch_mb": 1, "workspace_uuid": UUID}),
                )
                .await;
            assert_eq!(state["state"], "running", "{state}");
            let out = sh(
                h,
                &s4,
                Target::Work,
                "id -u; echo daten > /scratch/out.txt; printf '#!/bin/sh\\necho X' > /scratch/r.sh; \
                 chmod +x /scratch/r.sh; /scratch/r.sh || echo NOEXEC",
            )
            .await;
            assert_eq!(out.lines().next(), Some("1000"), "{out}");
            assert!(out.contains("NOEXEC"), "{out}");
            let file = root.join(UUID).join("out.txt");
            assert_eq!(std::fs::read_to_string(&file).unwrap(), "daten\n");
            use std::os::unix::fs::MetadataExt;
            // SAFETY: getuid hat keine Vorbedingungen.
            let uid = unsafe { libc::getuid() };
            assert_eq!(std::fs::metadata(&file).unwrap().uid(), uid);
            // Harte Grenze: mgpt-fs lehnt zu große Dateien ab
            let err = h
                .write(&s4, "/scratch/b.txt", &"y".repeat(2 << 20))
                .await
                .unwrap_err();
            assert!(err.message.contains("Scratchpad voll"), "{err}");
            h.manager.stop(&s4, "user").await;
        })
        .await;
    }

    /// Netzmodus Paketquellen mit echtem Netz (deb.debian.org): Proxy, apt als root,
    /// keine setuid-Programme, Messung der Schicht. Ohne Netz: `MGPT_TEST_NO_NETWORK=1`.
    #[tokio::test(flavor = "multi_thread")]
    async fn podman_packages_proxy_and_apt() {
        if std::env::var_os("MGPT_TEST_NO_NETWORK").is_some() {
            eprintln!("MGPT_TEST_NO_NETWORK gesetzt – Test übersprungen.");
            return;
        }
        let Some(setup) = setup(5, None) else { return };
        let h = &setup.harness;
        tokio::time::timeout(Duration::from_secs(900), async {
            let s5 = sid(5);
            let state = h
                .start(
                    &s5,
                    "pw5",
                    json!({"network": "packages", "rootfs_mb": 1024,
                           "package_hosts": ["deb.debian.org", "security.debian.org", "pypi.org"],
                           "command_seconds": 300}),
                )
                .await;
            assert_eq!(state["state"], "running", "{state}");
            assert_eq!(state["limits"]["network"], "packages");
            assert_eq!(state["limits"]["rootfs_mb"], 1024);
            // Ist-Werte des echten Containers passen zur gelockerten Vorlage.
            eprintln!("pkg-test Schritt 1: {:?}", std::time::Instant::now());
            let name = template::work_name(&s5);
            let info = h.engine.inspect_container(&name).await.unwrap();
            let limits = h.manager.get(&s5).unwrap().state().limits.clone();
            let port = h.manager.get(&s5).unwrap().state().preview_port;
            assert_eq!(
                template::check_inspect(&info, &limits, port.map(u64::from), None),
                Vec::<String>::new()
            );
            // Nutzer 1000: Proxy nur zu freigegebenen Hosts, direkt gar nicht.
            eprintln!("pkg-test Schritt 2: {:?}", std::time::Instant::now());
            let probe = r#"
import os, socket, urllib.request, urllib.error
def get(url):
    try:
        return urllib.request.urlopen(url, timeout=30).status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return type(e).__name__
print("debian", get("http://deb.debian.org/debian/dists/trixie/Release"))
print("fremd", get("http://example.com/"))
print("https-fremd", get("https://example.com/"))
print("https-pypi", get("https://pypi.org/simple/pip/"))
s = socket.socket(); s.settimeout(5)
try:
    s.connect(("1.1.1.1", 80)); print("direkt offen")
except OSError as e:
    print("direkt zu")
"#;
            let out = sh(
                h,
                &s5,
                Target::Work,
                &format!("id -u; python3 -c '{probe}'"),
            )
            .await;
            assert!(out.starts_with("1000\n"), "{out}");
            assert!(out.contains("debian 200"), "{out}");
            assert!(out.contains("fremd 403"), "{out}");
            assert!(out.contains("https-fremd"), "{out}");
            assert!(!out.contains("https-fremd 200"), "{out}");
            assert!(out.contains("https-pypi 200"), "{out}");
            assert!(out.contains("direkt zu"), "{out}");
            // Podman gibt cap_add auch Nicht-root-Nutzern: Nutzer 1000 muss trotzdem
            eprintln!("pkg-test Schritt 3: {:?}", std::time::Instant::now());
            // ohne Capabilities laufen (setpriv in jedem exec), auch im Terminal.
            eprintln!("pkg-test Schritt 4: {:?}", std::time::Instant::now());
            let out = sh(
                h,
                &s5,
                Target::Work,
                "grep -E '^Cap(Eff|Prm|Amb)' /proc/self/status",
            )
            .await;
            assert_eq!(out.matches("0000000000000000").count(), 3, "{out}");
            // Nutzer 1000 kann das beschreibbare Wurzeldateisystem nicht verändern.
            eprintln!("pkg-test Schritt 5: {:?}", std::time::Instant::now());
            let out = sh(
                h,
                &s5,
                Target::Work,
                "touch /usr/bin/x 2>&1 || echo GESPERRT",
            )
            .await;
            assert!(out.contains("GESPERRT"), "{out}");
            // apt als root mit fester Befehlszeile.
            eprintln!("pkg-test Schritt 6: {:?}", std::time::Instant::now());
            let res = h
                .handle(&s5)
                .apt_install(vec!["jq".into(), "sudo".into()])
                .await
                .unwrap();
            assert_eq!(res.code, Some(0), "{}", res.output);
            let out = sh(
                h,
                &s5,
                Target::Work,
                "echo '{\"a\": 1}' | jq .a; find / -xdev -type f -perm /6000 2>/dev/null | head; \
                 sudo -n true 2>/dev/null && echo SUDO-OK || echo SUDO-NEIN",
            )
            .await;
            assert!(out.starts_with("1\n"), "{out}");
            assert!(out.contains("SUDO-NEIN"), "{out}");
            assert!(!out.contains("/usr/bin/sudo"), "{out}");
            let session = h.manager.get(&s5).unwrap();
            let recorded = h.manager.recorded_packages(&session).await;
            assert_eq!(recorded, vec!["jq", "sudo"]);
            // Schicht gemessen (heartbeat), unter der Grenze.
            eprintln!("pkg-test Schritt 7: {:?}", std::time::Instant::now());
            h.manager.check_idle().await;
            let used = h.manager.infos()[0].rootfs_used_mb.unwrap();
            assert!(used > 0 && used < 1024, "{used}");
            // Nach dem Neustart werden die Pakete aus .mgpt/apt-packages nachinstalliert.
            eprintln!("pkg-test Schritt 8: {:?}", std::time::Instant::now());
            assert!(h.handle(&s5).save("apt_install").await.is_empty());
            h.manager.stop(&s5, "user").await;
            let s6 = sid(6);
            let state = h
                .start(
                    &s6,
                    "pw5",
                    json!({"network": "packages", "rootfs_mb": 1024,
                           "package_hosts": ["deb.debian.org", "security.debian.org"]}),
                )
                .await;
            assert_eq!(state["state"], "running", "{state}");
            // prepare_packages hält die Sperre; danach ist jq da.
            eprintln!("pkg-test Schritt 9: {:?}", std::time::Instant::now());
            let _ = h.manager.get(&s6).unwrap().lock.lock().await;
            let out = sh(h, &s6, Target::Work, "command -v jq; cat .gitignore").await;
            assert!(out.contains("/usr/bin/jq"), "{out}");
            assert!(out.contains("node_modules/"), "{out}");
            h.manager.stop(&s6, "user").await;
        })
        .await
        .expect("Zeitlimit");
    }
}
