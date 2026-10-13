//! Verbindungstests gegen einen lokalen WebSocket-Server (wie
//! `tests/test_runner_agent_connection.py` für den Python-Agent).

mod common;

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use common::*;
use multigpt_runner::cli::exit_code;
use multigpt_runner::connection::{AgentOptions, CLOSE_TLS, StopHandle};
use multigpt_runner::fetch;
use multigpt_runner::protocol::{self, Frame};
use serde_json::json;

#[tokio::test]
async fn hello_welcome_and_heartbeat() {
    let mut server = Server::plain().await;
    let dir = tempfile::tempdir().unwrap();
    let mut agent = agent(config(&server.url, dir.path()), fast_options());
    let handler = agent.handler().clone();
    let stop = agent.stop_handle();
    let task = tokio::spawn(async move { (agent.run().await, agent) });

    let mut conn = server.accept().await;
    assert!(conn.headers.get("origin").is_none(), "kein Origin-Header");
    assert_eq!(
        conn.headers.get("user-agent").unwrap(),
        &format!("multigpt-runner/{}", multigpt_runner::VERSION)
    );
    assert!(
        conn.headers.get("sec-websocket-extensions").is_none(),
        "keine Kompression"
    );
    let hello = conn.hello().await;
    assert_eq!(hello["token"], TOKEN);
    assert_eq!(hello["protocol"], 2);
    assert_eq!(hello["hostname"], "testpc");
    assert_eq!(hello["engine"]["name"], "fake");
    assert_eq!(hello["sessions"][0]["session"], "s1");
    assert_eq!(hello["nonce"].as_str().unwrap().len(), 64);
    assert_eq!(hello["catalog"], multigpt_runner::catalog::CATALOG);

    conn.welcome(5).await;
    // Heartbeat nach heartbeat_seconds (kleinster erlaubter Wert: 5 s)
    let beat = conn.recv().await.unwrap();
    assert_eq!(beat["type"], "heartbeat");
    assert_eq!(beat["sessions"][0]["state"], "running");
    assert_eq!(
        handler.welcomes.lock().unwrap()[0].services,
        vec!["read", "bash"]
    );
    assert_eq!(handler.ticks.load(Ordering::SeqCst), 1);
    assert_eq!(handler.adopted.load(Ordering::SeqCst), 1);

    stop.stop();
    assert_eq!(conn.close_code().await, Some(1000));
    let (code, agent) = task.await.unwrap();
    assert_eq!(code, Some(1000));
    assert_eq!(exit_code(code, true), 0);
    assert_eq!(agent.handler().disconnects.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn token_rejected_stops_without_retry() {
    for code in [4401u16, 4403, 4409, 4426] {
        let mut server = Server::plain().await;
        let dir = tempfile::tempdir().unwrap();
        let mut agent = agent(config(&server.url, dir.path()), fast_options());
        let task = tokio::spawn(async move { (agent.run().await, agent) });
        let mut conn = server.accept().await;
        conn.hello().await;
        conn.close(code).await;
        drop(conn);
        let (last, agent) = task.await.unwrap();
        assert_eq!(last, Some(code));
        assert_eq!(agent.attempts, 1, "kein Neuversuch nach {code}");
        assert!(agent.delays.is_empty());
        assert_eq!(exit_code(last, false), 3);
    }
}

#[tokio::test]
async fn reconnects_with_backoff_and_resets_after_welcome() {
    let mut server = Server::plain().await;
    let dir = tempfile::tempdir().unwrap();
    let mut agent = agent(config(&server.url, dir.path()), fast_options());
    let task = tokio::spawn(async move { (agent.run().await, agent) });

    // 1: Server neu gestartet (1012) → Backoff 1 s
    let mut conn = server.accept().await;
    conn.hello().await;
    conn.close(1012).await;
    drop(conn);
    // 2: Ratenbegrenzung (4429, nicht in RECONNECT) → längste Wartezeit
    let mut conn = server.accept().await;
    conn.hello().await;
    conn.close(4429).await;
    drop(conn);
    // 3: welcome setzt das Backoff zurück, dann Abbruch ohne Close (1006)
    let mut conn = server.accept().await;
    conn.hello().await;
    conn.welcome(30).await;
    tokio::time::sleep(Duration::from_millis(100)).await;
    drop(conn);
    // 4: Token widerrufen
    let mut conn = server.accept().await;
    conn.hello().await;
    conn.close(4401).await;
    drop(conn);

    let (last, agent) = task.await.unwrap();
    assert_eq!(last, Some(4401));
    assert_eq!(agent.attempts, 4);
    assert_eq!(agent.delays, vec![1.0, 60.0, 1.0]);
}

#[tokio::test]
async fn unsigned_or_forged_frames_close_with_4498() {
    // unsignierter Rahmen nach dem hello
    let mut server = Server::plain().await;
    let dir = tempfile::tempdir().unwrap();
    let mut agent = agent(config(&server.url, dir.path()), fast_options());
    let task = tokio::spawn(async move { (agent.run().await, agent) });
    let mut conn = server.accept().await;
    conn.hello().await;
    let plain = protocol::encode_value(
        "welcome",
        Some(protocol::Sender::Server),
        welcome_fields(30),
    )
    .unwrap();
    conn.send_text(plain).await;
    assert_eq!(conn.close_code().await, Some(4498));
    let (last, agent) = task.await.unwrap();
    assert_eq!(last, Some(4498));
    assert_eq!(agent.attempts, 1);
    assert_eq!(exit_code(last, false), 3);

    // mit fremdem Schlüssel signiert
    let mut server = Server::plain().await;
    let mut agent = common::agent(config(&server.url, dir.path()), fast_options());
    let task = tokio::spawn(async move { agent.run().await });
    let mut conn = server.accept().await;
    conn.hello().await;
    conn.key = ed25519_dalek::SigningKey::from_bytes(&[9; 32]);
    conn.welcome(30).await;
    assert_eq!(conn.close_code().await, Some(4498));
    assert_eq!(task.await.unwrap(), Some(4498));

    // Signatur einer anderen Verbindung (Replay mit fremder nonce)
    let mut server = Server::plain().await;
    let mut agent = common::agent(config(&server.url, dir.path()), fast_options());
    let task = tokio::spawn(async move { agent.run().await });
    let mut conn = server.accept().await;
    conn.hello().await;
    conn.nonce = Some("0".repeat(64));
    conn.welcome(30).await;
    assert_eq!(conn.close_code().await, Some(4498));
    assert_eq!(task.await.unwrap(), Some(4498));
}

#[tokio::test]
async fn frames_dispatch_errors_and_invalid_frames() {
    let mut server = Server::plain().await;
    let dir = tempfile::tempdir().unwrap();
    let mut agent = agent(config(&server.url, dir.path()), fast_options());
    let handler = agent.handler().clone();
    let stop = agent.stop_handle();
    let task = tokio::spawn(async move { agent.run().await });
    let mut conn = server.accept().await;
    conn.hello().await;

    // vor welcome: ignoriert
    conn.send_signed("pty_close", json!({"session": "s1", "pty": "p0"}))
        .await;
    conn.welcome(30).await;
    // kaputter Rahmen: Fehler invalid_frame, Verbindung bleibt
    conn.send_text("{kein json".into()).await;
    let err = conn.recv().await.unwrap();
    assert_eq!(err["type"], "error");
    assert_eq!(err["code"], "invalid_frame");
    assert_eq!(err["message"], "kein gültiges JSON");
    // Handler-Fehler mit session und ref (pty)
    conn.send_signed(
        "pty_input",
        json!({"session": "s1", "pty": "p1", "data": "YQ=="}),
    )
    .await;
    let err = conn.recv().await.unwrap();
    assert_eq!(err["code"], "unknown_session");
    assert_eq!(err["session"], "s1");
    assert_eq!(err["ref"], "p1");
    // Panik im Handler (vor dem Aufzeichnen): internal, Agent läuft weiter
    conn.send_signed(
        "preview_request",
        json!({"session": "s1", "request": "r9", "method": "GET", "path": "/"}),
    )
    .await;
    let err = conn.recv().await.unwrap();
    assert_eq!(err["code"], "internal");
    assert_eq!(err["ref"], "r9");
    // close/error des Servers gehen nicht an den Handler
    conn.send_signed("close", json!({"reason": "shutdown"}))
        .await;
    conn.send_signed("mcp", json!({"session": "s1", "pipe": "p", "message": {"jsonrpc": "2.0", "method": "ping", "id": 1}}))
        .await;
    tokio::time::sleep(Duration::from_millis(200)).await;
    let kinds: Vec<&str> = handler
        .frames
        .lock()
        .unwrap()
        .iter()
        .map(Frame::kind)
        .collect();
    assert_eq!(kinds, vec!["pty_input", "mcp"]);
    stop.stop();
    assert_eq!(task.await.unwrap(), Some(1000));
}

#[tokio::test]
async fn outbox_sends_while_connected_only() {
    let mut server = Server::plain().await;
    let dir = tempfile::tempdir().unwrap();
    let mut agent = agent(config(&server.url, dir.path()), fast_options());
    let outbox = agent.outbox().clone();
    let stop = agent.stop_handle();
    let task = tokio::spawn(async move { agent.run().await });
    let mut conn = server.accept().await;
    conn.hello().await;
    conn.welcome(30).await;
    outbox
        .send(
            "session_state",
            json!({"session": "s1", "state": "running"}),
        )
        .await
        .unwrap();
    let state = conn.recv().await.unwrap();
    assert_eq!(state["type"], "session_state");
    stop.stop();
    task.await.unwrap();
    assert!(
        outbox
            .send("heartbeat", json!({"sessions": []}))
            .await
            .is_err()
    );
}

#[tokio::test]
async fn tls_pinning_before_hello() {
    // richtiger Fingerprint: hello kommt an
    let mut server = Server::tls().await;
    let dir = tempfile::tempdir().unwrap();
    let mut cfg = config(&server.url, dir.path());
    cfg.ca_sha256 = server.fingerprint.clone();
    let mut agent = agent(cfg, fast_options());
    let stop = agent.stop_handle();
    let task = tokio::spawn(async move { agent.run().await });
    let mut conn = server.accept().await;
    assert_eq!(conn.hello().await["token"], TOKEN);
    stop.stop();
    task.await.unwrap();

    // falscher Fingerprint: Abbruch im Handshake, kein hello, 1015 (längste Wartezeit)
    let mut server = Server::tls().await;
    let mut cfg = config(&server.url, dir.path());
    cfg.ca_sha256 = Some("0".repeat(64));
    let mut agent = common::agent(cfg, fast_options());
    assert_eq!(agent.run_once().await, CLOSE_TLS);
    assert!(
        server
            .try_accept(Duration::from_millis(300))
            .await
            .is_none()
    );
    assert_eq!(server.tcp_count.load(Ordering::SeqCst), 1);

    // ohne Fingerprint: selbst signiertes Zertifikat wird abgelehnt (1006)
    let mut server = Server::tls().await;
    let mut agent = common::agent(config(&server.url, dir.path()), fast_options());
    assert_eq!(agent.run_once().await, protocol::CLOSE_ABNORMAL);
    assert!(
        server
            .try_accept(Duration::from_millis(300))
            .await
            .is_none()
    );
}

#[tokio::test]
async fn missing_token_or_key_counts_as_4401() {
    let server = Server::plain().await;
    let dir = tempfile::tempdir().unwrap();
    let cfg = config(&server.url, dir.path());
    std::fs::remove_file(&cfg.server_key_file).unwrap();
    let mut agent = agent(cfg, fast_options());
    assert_eq!(agent.run().await, Some(protocol::CLOSE_TOKEN));
    assert_eq!(server.tcp_count.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn unreachable_server_backs_off() {
    // freier Port ohne Server
    let port = {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        listener.local_addr().unwrap().port()
    };
    let dir = tempfile::tempdir().unwrap();
    let url = format!("ws://127.0.0.1:{port}/runner/ws");
    // Die eingesetzte Uhr stoppt den Agent beim dritten Warten.
    let slot: Arc<Mutex<Option<StopHandle>>> = Arc::default();
    let calls = Arc::new(AtomicUsize::new(0));
    let (s, c) = (slot.clone(), calls.clone());
    let opts = AgentOptions {
        sleep: Arc::new(move |_| {
            if c.fetch_add(1, Ordering::SeqCst) == 2 {
                s.lock().unwrap().as_ref().unwrap().stop();
            }
            Box::pin(async {})
        }),
        ..fast_options()
    };
    let mut agent = agent(config(&url, dir.path()), opts);
    *slot.lock().unwrap() = Some(agent.stop_handle());
    let last = agent.run().await;
    assert_eq!(last, Some(protocol::CLOSE_ABNORMAL));
    assert_eq!(agent.attempts, 3);
    assert_eq!(agent.delays, vec![1.0, 2.0, 4.0]);
    assert_eq!(exit_code(last, true), 0);
}

#[tokio::test]
async fn engine_down_does_not_connect() {
    let server = Server::plain().await;
    let dir = tempfile::tempdir().unwrap();
    let mut agent = agent(config(&server.url, dir.path()), fast_options());
    agent.handler().engine_down.store(true, Ordering::SeqCst);
    // Engine-Zustand wird VOR dem Verbindungsaufbau geholt (hello sofort nach dem Handshake)
    assert_eq!(agent.run_once().await, protocol::CLOSE_TRY_AGAIN);
    assert_eq!(server.tcp_count.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn fetch_from_own_server_with_bearer() {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let server = tokio::spawn(async move {
        let mut requests = Vec::new();
        for body in ["binärdaten", "x".repeat(100).as_str()] {
            let (mut tcp, _) = listener.accept().await.unwrap();
            let mut buf = vec![0u8; 4096];
            let n = tcp.read(&mut buf).await.unwrap();
            requests.push(String::from_utf8_lossy(&buf[..n]).into_owned());
            let reply = format!(
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
            tcp.write_all(reply.as_bytes()).await.unwrap();
        }
        requests
    });
    let dir = tempfile::tempdir().unwrap();
    let cfg = config(&format!("ws://127.0.0.1:{port}/runner/ws"), dir.path());
    let url = format!("http://127.0.0.1:{port}/runner/download/amd64");
    let data = fetch::https_get(&cfg, &url, Some("geheim123"), 1000)
        .await
        .unwrap();
    assert_eq!(data, "binärdaten".as_bytes());
    let err = fetch::https_get(&cfg, &url, None, 10).await.unwrap_err();
    assert_eq!(err, fetch::FetchError::TooLarge(10));
    let requests = server.await.unwrap();
    assert!(requests[0].starts_with("GET /runner/download/amd64 HTTP/1.1\r\n"));
    assert!(
        requests[0]
            .to_ascii_lowercase()
            .contains("authorization: bearer geheim123\r\n")
    );
    assert!(!requests[1].to_ascii_lowercase().contains("authorization"));
    // fremder Rechner
    assert!(matches!(
        fetch::https_get(&cfg, "http://example.org/x", None, 10).await,
        Err(fetch::FetchError::NotAllowed(_))
    ));
}
