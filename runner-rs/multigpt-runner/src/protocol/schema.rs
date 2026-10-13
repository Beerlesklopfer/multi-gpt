//! Prüfung der Rahmen, Feld für Feld wie `FRAMES` in `protocol.py`.
//!
//! [`validate`] arbeitet auf `serde_json::Value` und liefert den normalisierten
//! Rahmen: Fehlende optionale Felder stehen darin als `null` (wie `None` in
//! Python), verschachtelte Objekte ebenso. Danach lässt er sich ohne weitere
//! Prüfung in [`Frame`] umwandeln.

use std::collections::HashMap;
use std::sync::LazyLock;

use base64::Engine as _;
use regex::Regex;
use serde_json::{Map, Value};

use super::*;

/// Prüfung eines Feldes.
#[derive(Debug, Clone)]
enum Check {
    Str {
        max: usize,
        pattern: Option<&'static Regex>,
    },
    Int {
        lo: i128,
        hi: i128,
    },
    Num {
        lo: f64,
        hi: f64,
    },
    Bool,
    Enum(&'static [&'static str]),
    B64(usize),
    List(Box<Check>, usize),
    Object(Vec<Field>),
    Headers,
    JsonRpc,
}

#[derive(Debug, Clone)]
struct Field {
    name: &'static str,
    check: Check,
    required: bool,
}

struct FrameSpec {
    senders: &'static [Sender],
    fields: Vec<Field>,
}

fn req(name: &'static str, check: Check) -> Field {
    Field {
        name,
        check,
        required: true,
    }
}

fn opt(name: &'static str, check: Check) -> Field {
    Field {
        name,
        check,
        required: false,
    }
}

fn s(max: usize) -> Check {
    Check::Str { max, pattern: None }
}

fn sp(max: usize, pattern: &'static LazyLock<Regex>) -> Check {
    Check::Str {
        max,
        pattern: Some(LazyLock::force(pattern)),
    }
}

fn int(lo: i128, hi: i128) -> Check {
    Check::Int { lo, hi }
}

fn id() -> Check {
    sp(MAX_ID, &ID_RE)
}

fn sha256() -> Check {
    sp(64, &SHA256_RE)
}

fn text() -> Check {
    s(MAX_TEXT)
}

fn preview_port() -> Check {
    int(PREVIEW_PORTS.start as i128, PREVIEW_PORTS.end as i128 - 1)
}

fn engine() -> Check {
    Check::Object(vec![
        req("name", Check::Enum(ENGINES)),
        req("version", s(64)),
        req("rootless", Check::Bool),
    ])
}

fn session_info() -> Check {
    Check::Object(vec![
        req("session", id()),
        opt("workspace", id()),
        req("state", Check::Enum(SESSION_STATES)),
        opt("preview_port", preview_port()),
    ])
}

fn allow() -> Check {
    Check::Object(vec![
        req("host", sp(253, &HOST_RE)),
        req("port", int(1, 65535)),
    ])
}

fn limits() -> Check {
    Check::Object(vec![
        req("cpus", Check::Num { lo: 0.1, hi: 64.0 }),
        req("memory_mb", int(64, 262144)),
        req("pids", int(16, 65536)),
        req("tmpfs_mb", int(16, 65536)),
        req("network", Check::Enum(NETWORK_MODES)),
    ])
}

fn bundle_info() -> Check {
    Check::Object(vec![
        req("size", int(1, MAX_BUNDLE_BYTES as i128)),
        req("sha256", sha256()),
        opt("head", sp(64, &COMMIT_RE)),
    ])
}

const AGENT: &[Sender] = &[Sender::Agent];
const SERVER: &[Sender] = &[Sender::Server];
const BOTH: &[Sender] = &[Sender::Agent, Sender::Server];

static FRAMES: LazyLock<HashMap<&'static str, FrameSpec>> = LazyLock::new(build_frames);

fn build_frames() -> HashMap<&'static str, FrameSpec> {
    let max_seq = (MAX_BUNDLE_BYTES / 1024) as i128;
    let max_bundle = MAX_BUNDLE_BYTES as i128;
    let mut m = HashMap::new();
    let mut add = |kind: &'static str, senders: &'static [Sender], fields: Vec<Field>| {
        m.insert(kind, FrameSpec { senders, fields });
    };
    add(
        "hello",
        AGENT,
        vec![
            req("token", sp(128, &TOKEN_RE)),
            req("protocol", int(1, 1000)),
            req("agent_version", s(64)),
            req("engine", engine()),
            req("hostname", s(253)),
            opt("sessions", Check::List(Box::new(session_info()), 100)),
            req("nonce", sp(64, &NONCE_RE)),
            opt("catalog", s(MAX_CATALOG_BYTES)),
            opt("catalog_sig", Check::B64(SIGNATURE_BYTES)),
            opt("catalog_key", sp(16, &KEY_ID_RE)),
        ],
    );
    add(
        "welcome",
        SERVER,
        vec![
            req("runner", int(1, i64::MAX as i128)),
            req("protocol", int(1, 1000)),
            req("server_version", s(64)),
            req("heartbeat_seconds", int(5, 600)),
            req("images", Check::List(Box::new(sp(320, &IMAGE_RE)), 20)),
            req("max_frame_bytes", int(1024, MAX_FRAME_BYTES as i128)),
            req("chunk_bytes", int(1024, CHUNK_BYTES as i128)),
            req(
                "services",
                Check::List(Box::new(sp(64, &SERVICE_RE)), MAX_SERVICES),
            ),
            req("catalog", Check::Enum(CATALOG_STATES)),
        ],
    );
    add(
        "signed",
        SERVER,
        vec![
            req("key", sp(16, &KEY_ID_RE)),
            req("payload", Check::B64(MAX_PAYLOAD_BYTES)),
            req("sig", Check::B64(SIGNATURE_BYTES)),
        ],
    );
    add(
        "heartbeat",
        AGENT,
        vec![req("sessions", Check::List(Box::new(session_info()), 100))],
    );
    add(
        "session_start",
        SERVER,
        vec![
            req("session", id()),
            req("workspace", id()),
            req("image", sp(320, &IMAGE_RE)),
            req("cpus", Check::Num { lo: 0.1, hi: 64.0 }),
            req("memory_mb", int(64, 262144)),
            req("pids", int(16, 65536)),
            req("tmpfs_mb", int(16, 65536)),
            req("network", Check::Enum(NETWORK_MODES)),
            opt("allow", Check::List(Box::new(allow()), 50)),
            req("command_seconds", int(1, 86400)),
            req("output_bytes", int(1024, 16 * 1024 * 1024)),
            req("idle_minutes", int(1, 1440)),
            opt("bundle", bundle_info()),
            opt("scratch_mode", Check::Enum(SCRATCH_MODES)),
            opt("scratch_mb", int(1, 1024 * 1024)),
            opt("workspace_uuid", sp(36, &UUID_RE)),
        ],
    );
    add(
        "session_state",
        AGENT,
        vec![
            req("session", id()),
            req("state", Check::Enum(SESSION_STATES)),
            opt("limits", limits()),
            opt("preview_port", preview_port()),
            opt("error", text()),
        ],
    );
    add(
        "session_stop",
        SERVER,
        vec![
            req("session", id()),
            req("reason", Check::Enum(STOP_REASONS)),
        ],
    );
    add(
        "mcp",
        BOTH,
        vec![
            req("session", id()),
            req("pipe", id()),
            req("message", Check::JsonRpc),
        ],
    );
    add(
        "bundle_get",
        AGENT,
        vec![req("session", id()), req("transfer", id())],
    );
    add(
        "bundle_chunk",
        BOTH,
        vec![
            req("session", id()),
            req("transfer", id()),
            req("seq", int(0, max_seq)),
            req("data", Check::B64(CHUNK_BYTES)),
            req("last", Check::Bool),
            opt("size", int(0, max_bundle)),
            opt("sha256", sha256()),
            opt("head", sp(64, &COMMIT_RE)),
            opt("message", text()),
        ],
    );
    add(
        "file_chunk",
        AGENT,
        vec![
            req("session", id()),
            req("transfer", id()),
            req("seq", int(0, max_seq)),
            req("data", Check::B64(CHUNK_BYTES)),
            req("last", Check::Bool),
            opt("size", int(0, max_bundle)),
            opt("sha256", sha256()),
            opt("name", s(255)),
            opt("mime", sp(100, &MIME_RE)),
        ],
    );
    add(
        "bundle_ack",
        BOTH,
        vec![
            req("session", id()),
            req("transfer", id()),
            req("ok", Check::Bool),
            opt("head", sp(64, &COMMIT_RE)),
            opt("error", text()),
        ],
    );
    add(
        "pty_open",
        SERVER,
        vec![
            req("session", id()),
            req("pty", id()),
            req("cols", int(10, 1000)),
            req("rows", int(5, 500)),
        ],
    );
    add(
        "pty_input",
        SERVER,
        vec![
            req("session", id()),
            req("pty", id()),
            req("data", Check::B64(PTY_BYTES)),
        ],
    );
    add(
        "pty_resize",
        SERVER,
        vec![
            req("session", id()),
            req("pty", id()),
            req("cols", int(10, 1000)),
            req("rows", int(5, 500)),
        ],
    );
    add(
        "pty_close",
        SERVER,
        vec![req("session", id()), req("pty", id())],
    );
    add(
        "pty_data",
        AGENT,
        vec![
            req("session", id()),
            req("pty", id()),
            req("seq", int(0, 1 << 53)),
            req("data", Check::B64(PTY_BYTES)),
        ],
    );
    add(
        "pty_exit",
        AGENT,
        vec![
            req("session", id()),
            req("pty", id()),
            opt("code", int(-255, 255)),
        ],
    );
    add(
        "preview",
        AGENT,
        vec![
            req("session", id()),
            req("running", Check::Bool),
            opt("port", preview_port()),
            opt("token", sp(128, &TOKEN_RE)),
        ],
    );
    add(
        "preview_request",
        SERVER,
        vec![
            req("session", id()),
            req("request", id()),
            req("method", Check::Enum(HTTP_METHODS)),
            req("path", s(2048)),
            opt("headers", Check::Headers),
        ],
    );
    add(
        "preview_response",
        AGENT,
        vec![
            req("session", id()),
            req("request", id()),
            req("seq", int(0, 100000)),
            opt("status", int(100, 599)),
            opt("headers", Check::Headers),
            opt("data", Check::B64(PREVIEW_BYTES)),
            req("last", Check::Bool),
        ],
    );
    add(
        "error",
        BOTH,
        vec![
            req("code", Check::Enum(ERROR_CODES)),
            req("message", text()),
            opt("session", id()),
            opt("ref", id()),
        ],
    );
    add(
        "close",
        SERVER,
        vec![
            req("reason", Check::Enum(CLOSE_FRAME_REASONS)),
            opt("message", text()),
        ],
    );
    m
}

/// Alle Rahmentypen des Protokolls.
pub fn frame_types() -> Vec<&'static str> {
    let mut kinds: Vec<_> = FRAMES.keys().copied().collect();
    kinds.sort_unstable();
    kinds
}

/// Darf `sender` diesen Rahmentyp schicken? `None` bei unbekanntem Typ.
pub fn allowed_sender(kind: &str, sender: Sender) -> Option<bool> {
    FRAMES.get(kind).map(|spec| spec.senders.contains(&sender))
}

fn fail(name: &str, text: &str) -> ProtocolError {
    ProtocolError::new(format!("Feld {name}: {text}"))
}

/// Base64 wie `base64.b64decode(..., validate=True)` (Python 3.13): Padding Pflicht,
/// nicht-kanonische Restbits erlaubt, keine Leerzeichen.
static B64: LazyLock<base64::engine::GeneralPurpose> = LazyLock::new(|| {
    base64::engine::GeneralPurpose::new(
        &base64::alphabet::STANDARD,
        base64::engine::GeneralPurposeConfig::new()
            .with_decode_allow_trailing_bits(true)
            .with_decode_padding_mode(base64::engine::DecodePaddingMode::RequireCanonical),
    )
});

pub(crate) fn b64_decode_strict(text: &str) -> Result<Vec<u8>, base64::DecodeError> {
    B64.decode(text)
}

fn int_value(value: &Value) -> Option<i128> {
    match value {
        Value::Number(n) => n
            .as_i64()
            .map(i128::from)
            .or_else(|| n.as_u64().map(i128::from)),
        _ => None,
    }
}

impl Check {
    fn apply(&self, value: &Value, name: &str) -> Result<Value, ProtocolError> {
        match self {
            Check::Str { max, pattern } => {
                let Value::String(text) = value else {
                    return Err(fail(name, "Text erwartet"));
                };
                if text.chars().count() > *max {
                    return Err(fail(name, &format!("länger als {max} Zeichen")));
                }
                if let Some(pattern) = pattern
                    && !pattern.is_match(text)
                {
                    return Err(fail(name, "ungültiges Format"));
                }
                Ok(value.clone())
            }
            Check::Int { lo, hi } => {
                let Some(number) = int_value(value) else {
                    return Err(fail(name, "Ganzzahl erwartet"));
                };
                if number < *lo || number > *hi {
                    return Err(fail(name, &format!("außerhalb {lo}…{hi}")));
                }
                Ok(value.clone())
            }
            Check::Num { lo, hi } => {
                let Some(number) = value.as_f64().filter(|_| value.is_number()) else {
                    return Err(fail(name, "Zahl erwartet"));
                };
                if !number.is_finite() || number < *lo || number > *hi {
                    return Err(fail(name, &format!("außerhalb {lo}…{hi}")));
                }
                Ok(value.clone())
            }
            Check::Bool => match value {
                Value::Bool(_) => Ok(value.clone()),
                _ => Err(fail(name, "true/false erwartet")),
            },
            Check::Enum(choices) => match value {
                Value::String(text) if choices.contains(&text.as_str()) => Ok(value.clone()),
                _ => Err(fail(name, &format!("erlaubt: {}", choices.join(", ")))),
            },
            Check::B64(max_raw) => {
                let Value::String(text) = value else {
                    return Err(fail(name, "Base64-Text erwartet"));
                };
                let limit = 4 * max_raw.div_ceil(3);
                if text.chars().count() > limit {
                    return Err(fail(name, &format!("mehr als {max_raw} Byte")));
                }
                let raw =
                    b64_decode_strict(text).map_err(|_| fail(name, "kein gültiges Base64"))?;
                if raw.len() > *max_raw {
                    return Err(fail(name, &format!("mehr als {max_raw} Byte")));
                }
                Ok(value.clone())
            }
            Check::List(item, max_items) => {
                let Value::Array(items) = value else {
                    return Err(fail(name, "Liste erwartet"));
                };
                if items.len() > *max_items {
                    return Err(fail(name, &format!("mehr als {max_items} Einträge")));
                }
                items
                    .iter()
                    .enumerate()
                    .map(|(i, entry)| item.apply(entry, &format!("{name}[{i}]")))
                    .collect::<Result<Vec<_>, _>>()
                    .map(Value::Array)
            }
            Check::Object(fields) => {
                let Value::Object(data) = value else {
                    return Err(fail(name, "Objekt erwartet"));
                };
                check_fields(data, fields, name).map(Value::Object)
            }
            Check::Headers => check_headers(value, name),
            Check::JsonRpc => check_jsonrpc(value, name),
        }
    }
}

fn check_headers(value: &Value, name: &str) -> Result<Value, ProtocolError> {
    let Value::Object(map) = value else {
        return Err(fail(name, "Objekt erwartet"));
    };
    if map.len() > 50 {
        return Err(fail(name, "mehr als 50 Einträge"));
    }
    for (key, item) in map {
        if !HEADER_NAME_RE.is_match(key) {
            return Err(fail(name, "ungültiger Header-Name"));
        }
        let ok = matches!(item, Value::String(text)
            if text.chars().count() <= 4096 && !text.contains('\r') && !text.contains('\n'));
        if !ok {
            return Err(fail(&format!("{name}.{key}"), "ungültiger Wert"));
        }
    }
    Ok(value.clone())
}

/// MCP-Nachricht: JSON-RPC-2.0-Objekt (Anfrage, Benachrichtigung, Antwort).
fn check_jsonrpc(value: &Value, name: &str) -> Result<Value, ProtocolError> {
    let Value::Object(map) = value else {
        return Err(fail(name, "JSON-RPC-Objekt erwartet"));
    };
    if map.get("jsonrpc") != Some(&Value::String("2.0".into())) {
        return Err(fail(name, "jsonrpc muss 2.0 sein"));
    }
    if let Some(method) = map.get("method") {
        if !matches!(method, Value::String(m) if !m.is_empty()) {
            return Err(fail(name, "method ungültig"));
        }
    } else if !map.contains_key("result") && !map.contains_key("error") {
        return Err(fail(name, "weder method noch result/error"));
    }
    if let Some(id) = map.get("id") {
        let ok = match id {
            Value::Null | Value::String(_) => true,
            Value::Number(n) => n.is_i64() || n.is_u64(),
            _ => false,
        };
        if !ok {
            return Err(fail(name, "id ungültig"));
        }
    }
    Ok(value.clone())
}

fn check_fields(
    data: &Map<String, Value>,
    spec: &[Field],
    where_: &str,
) -> Result<Map<String, Value>, ProtocolError> {
    let mut unknown: Vec<&str> = data
        .keys()
        .map(String::as_str)
        .filter(|key| !spec.iter().any(|f| f.name == *key))
        .collect();
    if !unknown.is_empty() {
        unknown.sort_unstable();
        return Err(ProtocolError::new(format!(
            "{where_}: unbekannte Felder {}",
            unknown.join(", ")
        )));
    }
    let mut out = Map::new();
    for field in spec {
        match data.get(field.name) {
            None | Some(Value::Null) => {
                if field.required {
                    return Err(fail(&format!("{where_}.{}", field.name), "fehlt"));
                }
                out.insert(field.name.to_string(), Value::Null);
            }
            Some(value) => {
                let checked = field
                    .check
                    .apply(value, &format!("{where_}.{}", field.name))?;
                out.insert(field.name.to_string(), checked);
            }
        }
    }
    Ok(out)
}

fn is_null(frame: &Map<String, Value>, name: &str) -> bool {
    frame.get(name).is_none_or(Value::is_null)
}

/// Prüfungen über mehrere Felder (`_semantic`).
fn semantic(kind: &str, frame: &Map<String, Value>) -> Result<(), ProtocolError> {
    let flag = |name: &str| frame.get(name) == Some(&Value::Bool(true));
    match kind {
        "bundle_chunk" | "file_chunk" if flag("last") => {
            for name in ["size", "sha256"] {
                if is_null(frame, name) {
                    return Err(fail(&format!("{kind}.{name}"), "fehlt im letzten Stück"));
                }
            }
        }
        "session_start" => {
            let allowlist = frame.get("network") == Some(&Value::String("allowlist".into()));
            let has_allow = matches!(frame.get("allow"), Some(Value::Array(a)) if !a.is_empty());
            if !allowlist && has_allow {
                return Err(fail("session_start.allow", "nur mit network=allowlist"));
            }
        }
        "preview_response" => {
            if frame.get("seq").and_then(Value::as_i64) == Some(0) && is_null(frame, "status") {
                return Err(fail("preview_response.status", "fehlt im ersten Stück"));
            }
        }
        "preview" => {
            if flag("running") && (is_null(frame, "port") || is_null(frame, "token")) {
                return Err(fail(
                    "preview",
                    "port und token nötig, wenn die Vorschau läuft",
                ));
            }
        }
        "hello" => {
            let sig = !is_null(frame, "catalog_sig");
            if sig != !is_null(frame, "catalog_key") {
                return Err(fail("hello.catalog_sig", "nur zusammen mit catalog_key"));
            }
            if sig && is_null(frame, "catalog") {
                return Err(fail("hello.catalog_sig", "ohne catalog"));
            }
            if let Some(Value::String(catalog)) = frame.get("catalog")
                && catalog.len() > MAX_CATALOG_BYTES
            {
                return Err(fail(
                    "hello.catalog",
                    &format!("mehr als {MAX_CATALOG_BYTES} Byte"),
                ));
            }
        }
        "signed" => exact_b64_len(frame, "signed.sig", "sig")?,
        _ => {}
    }
    if kind == "hello" && !is_null(frame, "catalog_sig") {
        exact_b64_len(frame, "hello.catalog_sig", "catalog_sig")?;
    }
    Ok(())
}

fn exact_b64_len(frame: &Map<String, Value>, label: &str, name: &str) -> Result<(), ProtocolError> {
    let raw = frame
        .get(name)
        .and_then(Value::as_str)
        .and_then(|text| b64_decode_strict(text).ok());
    if raw.map(|r| r.len()) != Some(SIGNATURE_BYTES) {
        return Err(fail(
            label,
            &format!("genau {SIGNATURE_BYTES} Byte erwartet"),
        ));
    }
    Ok(())
}

/// Rahmen (bereits als JSON-Wert) prüfen und normalisiert zurückgeben.
///
/// `sender` prüft zusätzlich die Richtung.
pub fn validate(data: &Value, sender: Option<Sender>) -> Result<Value, ProtocolError> {
    let Value::Object(map) = data else {
        return Err(ProtocolError::new("Rahmen muss ein JSON-Objekt sein"));
    };
    let kind = match map.get("type") {
        Some(Value::String(kind)) if FRAMES.contains_key(kind.as_str()) => kind.as_str(),
        _ => return Err(ProtocolError::new("unbekannter Rahmentyp")),
    };
    let spec = &FRAMES[kind];
    if let Some(sender) = sender
        && !spec.senders.contains(&sender)
    {
        return Err(ProtocolError::new(format!(
            "Rahmen {kind} ist von {sender} nicht erlaubt"
        )));
    }
    let body: Map<String, Value> = map
        .iter()
        .filter(|(key, _)| key.as_str() != "type")
        .map(|(key, value)| (key.clone(), value.clone()))
        .collect();
    let mut frame = Map::new();
    frame.insert("type".into(), Value::String(kind.into()));
    frame.extend(check_fields(&body, &spec.fields, kind)?);
    semantic(kind, &frame)?;
    Ok(Value::Object(frame))
}

/// Empfangenen Textrahmen prüfen und normalisiert als JSON-Wert liefern.
pub fn parse_value(text: &str, sender: Option<Sender>) -> Result<Value, ProtocolError> {
    if text.len() > MAX_FRAME_BYTES {
        return Err(ProtocolError::with_code("Rahmen zu groß", CLOSE_TOO_BIG));
    }
    let data: Value =
        super::strict_json::from_str(text).map_err(|_| ProtocolError::new("kein gültiges JSON"))?;
    validate(&data, sender)
}

/// Empfangenen Textrahmen prüfen und als typisierten [`Frame`] liefern.
pub fn parse(text: &str, sender: Option<Sender>) -> Result<Frame, ProtocolError> {
    let value = parse_value(text, sender)?;
    Frame::from_validated(value)
}

/// Rahmen aus Typ und Feldern bauen, prüfen und als JSON-Text liefern.
///
/// Wie `encode(kind, sender, **fields)`: Felder mit `null` entfallen.
pub fn encode_value(
    kind: &str,
    sender: Option<Sender>,
    fields: Value,
) -> Result<String, ProtocolError> {
    let mut data = Map::new();
    data.insert("type".into(), Value::String(kind.into()));
    match fields {
        Value::Object(map) => {
            for (key, value) in map {
                if !value.is_null() && key != "type" {
                    data.insert(key, value);
                }
            }
        }
        Value::Null => {}
        _ => return Err(ProtocolError::new("Felder müssen ein JSON-Objekt sein")),
    }
    let data = Value::Object(data);
    validate(&data, sender)?;
    let text = serde_json::to_string(&data).map_err(|e| ProtocolError::new(e.to_string()))?;
    if text.len() > MAX_FRAME_BYTES {
        return Err(ProtocolError::with_code("Rahmen zu groß", CLOSE_TOO_BIG));
    }
    Ok(text)
}

/// Typisierten Rahmen prüfen und als JSON-Text liefern.
pub fn encode(frame: &Frame, sender: Option<Sender>) -> Result<String, ProtocolError> {
    let value = serde_json::to_value(frame).map_err(|e| ProtocolError::new(e.to_string()))?;
    let Value::Object(mut map) = value else {
        return Err(ProtocolError::new("Rahmen muss ein JSON-Objekt sein"));
    };
    let kind = match map.remove("type") {
        Some(Value::String(kind)) => kind,
        _ => return Err(ProtocolError::new("unbekannter Rahmentyp")),
    };
    encode_value(&kind, sender, Value::Object(map))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn hello() -> Value {
        json!({
            "type": "hello",
            "token": "a".repeat(32),
            "protocol": 1,
            "agent_version": "0.1.0",
            "engine": {"name": "podman", "version": "5.4", "rootless": true},
            "hostname": "pc",
            "nonce": "0".repeat(64),
        })
    }

    #[test]
    fn normalizes_optional_fields() {
        let out = validate(&hello(), Some(Sender::Agent)).unwrap();
        assert_eq!(out["sessions"], Value::Null);
    }

    #[test]
    fn direction_is_checked() {
        let err = validate(&hello(), Some(Sender::Server)).unwrap_err();
        assert_eq!(err.message, "Rahmen hello ist von server nicht erlaubt");
    }

    #[test]
    fn unknown_fields_and_types() {
        let mut frame = hello();
        frame["extra"] = Value::Null;
        assert!(
            validate(&frame, None)
                .unwrap_err()
                .message
                .contains("unbekannte Felder extra")
        );
        assert!(validate(&json!({"type": "nope"}), None).is_err());
        assert!(validate(&json!([1]), None).is_err());
    }

    #[test]
    fn bool_is_not_int_and_float_is_not_int() {
        let mut frame = hello();
        frame["protocol"] = json!(true);
        assert!(validate(&frame, None).is_err());
        frame["protocol"] = json!(1.0);
        assert!(validate(&frame, None).is_err());
    }

    #[test]
    fn length_counts_characters() {
        let mut frame = hello();
        frame["hostname"] = json!("ä".repeat(253));
        assert!(validate(&frame, None).is_ok());
        frame["hostname"] = json!("ä".repeat(254));
        assert!(validate(&frame, None).is_err());
    }

    #[test]
    fn base64_like_python() {
        assert!(b64_decode_strict("QR==").is_ok());
        assert!(b64_decode_strict("QQ").is_err());
        assert!(b64_decode_strict("QQ=").is_err());
        assert!(b64_decode_strict("QQ===").is_err());
        assert!(b64_decode_strict("QU JD").is_err());
        assert!(b64_decode_strict("").unwrap().is_empty());
    }

    #[test]
    fn too_big_has_close_code() {
        let text = " ".repeat(MAX_FRAME_BYTES + 1);
        assert_eq!(
            parse_value(&text, None).unwrap_err().close_code,
            CLOSE_TOO_BIG
        );
    }

    #[test]
    fn encode_drops_nulls() {
        let text = encode_value(
            "error",
            Some(Sender::Agent),
            json!({"code": "internal", "message": "x", "session": null}),
        )
        .unwrap();
        assert_eq!(text, r#"{"code":"internal","message":"x","type":"error"}"#);
    }
}
