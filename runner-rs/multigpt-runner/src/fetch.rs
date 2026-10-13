//! Dateien vom eigenen Server laden (z. B. das Programm für ein Zielsystem unter
//! `/runner/download/<arch>`), mit derselben TLS-Prüfung wie die Verbindung.
//!
//! Erlaubt sind nur Adressen auf dem Rechner und Port aus `MGPT_URL`
//! (`https://`, bzw. `http://` nur mit `MGPT_INSECURE_DEV=1`). Keine
//! Weiterleitungen, Größe begrenzt, Zeitlimit.

use std::fmt;
use std::time::Duration;

use http_body_util::{BodyExt, Empty};
use hyper::Request;
use hyper::body::Bytes;
use hyper_util::rt::TokioIo;
use rustls::pki_types::ServerName;
use tokio::net::TcpStream;

use crate::VERSION;
use crate::config::Config;
use crate::tls;

/// Fehler beim Laden.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FetchError {
    /// Adresse nicht erlaubt (anderer Rechner, falsches Schema).
    NotAllowed(String),
    /// Zertifikat passt nicht zum festgehaltenen Fingerprint.
    Pin,
    /// Netz, TLS oder HTTP.
    Transport(String),
    /// Antwort nicht 200.
    Status(u16),
    /// Größer als erlaubt.
    TooLarge(u64),
    Timeout,
}

impl fmt::Display for FetchError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            FetchError::NotAllowed(why) => write!(f, "Adresse nicht erlaubt: {why}"),
            FetchError::Pin => {
                f.write_str("Zertifikat des Servers passt nicht zum festgehaltenen Fingerprint.")
            }
            FetchError::Transport(why) => write!(f, "Laden fehlgeschlagen: {why}"),
            FetchError::Status(code) => write!(f, "Server antwortet mit HTTP {code}"),
            FetchError::TooLarge(max) => write!(f, "Datei größer als {max} Byte"),
            FetchError::Timeout => f.write_str("Zeitüberschreitung beim Laden"),
        }
    }
}

impl std::error::Error for FetchError {}

/// Zerlegte Adresse.
#[derive(Debug, Clone, PartialEq, Eq)]
struct Target {
    secure: bool,
    host: String,
    port: u16,
    path: String,
}

fn split_url(url: &str) -> Option<Target> {
    let (scheme, rest) = url.split_once("://")?;
    let secure = match scheme.to_ascii_lowercase().as_str() {
        "https" | "wss" => true,
        "http" | "ws" => false,
        _ => return None,
    };
    let (netloc, path) = match rest.find(['/', '?', '#']) {
        Some(pos) => (&rest[..pos], &rest[pos..]),
        None => (rest, "/"),
    };
    if netloc.contains('@') || netloc.is_empty() {
        return None;
    }
    let (host, port) = if let Some(inner) = netloc.strip_prefix('[') {
        let (host, after) = inner.split_once(']')?;
        let port = match after.strip_prefix(':') {
            Some(p) => p.parse().ok()?,
            None if after.is_empty() => default_port(secure),
            None => return None,
        };
        (host.to_string(), port)
    } else {
        match netloc.rsplit_once(':') {
            Some((host, port)) => (host.to_string(), port.parse().ok()?),
            None => (netloc.to_string(), default_port(secure)),
        }
    };
    if host.is_empty() {
        return None;
    }
    let path = path.split('#').next().unwrap_or("/");
    let path = if path.starts_with('/') {
        path.to_string()
    } else {
        format!("/{path}")
    };
    Some(Target {
        secure,
        host: host.to_ascii_lowercase(),
        port,
        path,
    })
}

fn default_port(secure: bool) -> u16 {
    if secure { 443 } else { 80 }
}

/// Prüft, ob `url` auf den Server aus `MGPT_URL` zeigt; liefert das Ziel.
fn allowed_target(config: &Config, url: &str) -> Result<Target, FetchError> {
    let base = split_url(&config.url).ok_or_else(|| FetchError::NotAllowed("MGPT_URL".into()))?;
    let target =
        split_url(url).ok_or_else(|| FetchError::NotAllowed("ungültige Adresse".into()))?;
    if url
        .split_once("://")
        .is_some_and(|(s, _)| s.starts_with("ws"))
    {
        return Err(FetchError::NotAllowed("nur http(s)".into()));
    }
    if target.host != base.host || target.port != base.port {
        return Err(FetchError::NotAllowed(
            "anderer Rechner als MGPT_URL".into(),
        ));
    }
    if target.secure != base.secure || (!target.secure && !config.insecure_dev) {
        return Err(FetchError::NotAllowed("unverschlüsselt".into()));
    }
    Ok(target)
}

/// Datei per GET laden (höchstens `max_bytes`, Zeitlimit 120 s).
///
/// `bearer` geht als `Authorization: Bearer …` mit (nie geloggt).
pub async fn https_get(
    config: &Config,
    url: &str,
    bearer: Option<&str>,
    max_bytes: u64,
) -> Result<Vec<u8>, FetchError> {
    let target = allowed_target(config, url)?;
    tokio::time::timeout(
        Duration::from_secs(120),
        get(config, &target, bearer, max_bytes),
    )
    .await
    .map_err(|_| FetchError::Timeout)?
}

async fn get(
    config: &Config,
    target: &Target,
    bearer: Option<&str>,
    max_bytes: u64,
) -> Result<Vec<u8>, FetchError> {
    let transport = |e: &dyn fmt::Display| FetchError::Transport(e.to_string());
    let tcp = TcpStream::connect((target.host.as_str(), target.port))
        .await
        .map_err(|e| FetchError::Transport(format!("E/A ({})", e.kind())))?;
    let _ = tcp.set_nodelay(true);
    let host_header = if target.port == default_port(target.secure) {
        target.host.clone()
    } else if target.host.contains(':') {
        format!("[{}]:{}", target.host, target.port)
    } else {
        format!("{}:{}", target.host, target.port)
    };
    let mut request = Request::get(&target.path)
        .header("Host", host_header)
        .header("User-Agent", format!("multigpt-runner/{VERSION}"));
    if let Some(token) = bearer {
        let mut value = hyper::header::HeaderValue::from_str(&format!("Bearer {token}"))
            .map_err(|_| FetchError::NotAllowed("ungültiger Token".into()))?;
        value.set_sensitive(true);
        request = request.header(hyper::header::AUTHORIZATION, value);
    }
    let request = request
        .body(Empty::<Bytes>::new())
        .map_err(|e| transport(&e))?;
    if target.secure {
        let setup = tls::client_config(config.ca_sha256.as_deref(), &[]);
        let name = ServerName::try_from(target.host.clone()).map_err(|e| transport(&e))?;
        let connector = tokio_rustls::TlsConnector::from(setup.config.clone());
        let stream = match connector.connect(name, tcp).await {
            Ok(stream) => stream,
            Err(_) if setup.pin_failed() => return Err(FetchError::Pin),
            Err(e) => return Err(FetchError::Transport(format!("TLS ({})", e.kind()))),
        };
        send(TokioIo::new(stream), request, max_bytes).await
    } else {
        send(TokioIo::new(tcp), request, max_bytes).await
    }
}

async fn send<T>(
    io: T,
    request: Request<Empty<Bytes>>,
    max_bytes: u64,
) -> Result<Vec<u8>, FetchError>
where
    T: hyper::rt::Read + hyper::rt::Write + Unpin + Send + 'static,
{
    let transport = |e: &dyn fmt::Display| FetchError::Transport(e.to_string());
    let (mut sender, conn) = hyper::client::conn::http1::handshake(io)
        .await
        .map_err(|e| transport(&e))?;
    let driver = tokio::spawn(conn);
    let response = sender
        .send_request(request)
        .await
        .map_err(|e| transport(&e))?;
    let status = response.status().as_u16();
    if status != 200 {
        driver.abort();
        return Err(FetchError::Status(status));
    }
    let announced = response
        .headers()
        .get("content-length")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.parse::<u64>().ok());
    if announced.is_some_and(|n| n > max_bytes) {
        driver.abort();
        return Err(FetchError::TooLarge(max_bytes));
    }
    let mut body = response.into_body();
    let mut out = Vec::new();
    while let Some(frame) = body.frame().await {
        let frame = frame.map_err(|e| transport(&e))?;
        if let Some(data) = frame.data_ref() {
            if out.len() as u64 + data.len() as u64 > max_bytes {
                driver.abort();
                return Err(FetchError::TooLarge(max_bytes));
            }
            out.extend_from_slice(data);
        }
    }
    driver.abort();
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_own_server() {
        let mut config = Config::new("wss://mgpt.local/runner/ws");
        assert_eq!(
            allowed_target(&config, "https://mgpt.local/runner/download/amd64").unwrap(),
            Target {
                secure: true,
                host: "mgpt.local".into(),
                port: 443,
                path: "/runner/download/amd64".into()
            }
        );
        assert!(allowed_target(&config, "https://mgpt.local:443/x").is_ok());
        assert!(allowed_target(&config, "https://evil.example/x").is_err());
        assert!(allowed_target(&config, "https://mgpt.local:8443/x").is_err());
        assert!(allowed_target(&config, "http://mgpt.local/x").is_err());
        assert!(allowed_target(&config, "https://user@mgpt.local/x").is_err());
        assert!(allowed_target(&config, "wss://mgpt.local/x").is_err());
        config.url = "ws://127.0.0.1:8000/runner/ws".into();
        assert!(allowed_target(&config, "http://127.0.0.1:8000/x").is_err());
        config.insecure_dev = true;
        assert!(allowed_target(&config, "http://127.0.0.1:8000/x").is_ok());
        config.url = "wss://[::1]:8443/runner/ws".into();
        assert_eq!(
            allowed_target(&config, "https://[::1]:8443/a?b")
                .unwrap()
                .path,
            "/a?b"
        );
    }
}
