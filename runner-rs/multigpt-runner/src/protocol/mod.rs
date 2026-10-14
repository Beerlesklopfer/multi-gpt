//! Rahmenprotokoll zwischen Runner-Agent und Runner-Dienst (Port von
//! `multigpt/runner/protocol.py`).
//!
//! Jeder WebSocket-Textrahmen ist genau ein JSON-Objekt mit dem Feld `type` und
//! den Feldern seines Typs. Unbekannte Typen und Felder werden abgewiesen, ebenso
//! Rahmen in der falschen Richtung. Optionale Felder dürfen fehlen oder `null`
//! sein. Die Prüfung ([`validate`], [`parse`]) folgt Feld für Feld der
//! Python-Referenz; gemeinsame Testvektoren liegen unter `runner-rs/testdata/frames/`.
//!
//! Bewusste Abweichungen (strenger als Python, betreffen nur fehlerhafte Rahmen):
//! - Muster prüfen bis zum Textende; Pythons `$` ließe ein abschließendes `\n` zu.
//! - JSON ohne `NaN`/`Infinity` und ohne einzelne UTF-16-Surrogate.

pub mod bundle;
pub mod frames;
pub mod schema;
pub mod signed;
pub mod strict_json;

use std::fmt;
use std::ops::Range;
use std::sync::LazyLock;

use regex::Regex;

pub use bundle::{BundleAssembler, b64decode, b64encode, split_bundle};
pub use frames::*;
pub use schema::{encode, encode_value, parse, parse_value, validate};
pub use signed::{
    FRAME_CONTEXT, key_id, open_signed, open_signed_with, signed_message, wrap_signed,
};

pub const PROTOCOL_VERSION: u32 = 2;

// --- Größen und Zeiten -------------------------------------------------------

/// Ein Rahmen (JSON-Text, UTF-8).
pub const MAX_FRAME_BYTES: usize = 4 * 1024 * 1024;
/// Innerer Rahmen im Umschlag `signed` (Base64 macht 4/3 daraus).
pub const MAX_PAYLOAD_BYTES: usize = 3 * 1024 * 1024 - 1024;
/// Dienstkatalog im hello (Text von catalog.json).
pub const MAX_CATALOG_BYTES: usize = 256 * 1024;
pub const MAX_SERVICES: usize = 100;
/// Rohdaten je Bundle-Stück.
pub const CHUNK_BYTES: usize = 1024 * 1024;
/// Obergrenze eines Bundles im Protokoll (die echte Grenze setzt der Server je Rolle).
pub const MAX_BUNDLE_BYTES: u64 = 1024 * 1024 * 1024;
/// Rohdaten je PTY-Rahmen.
pub const PTY_BYTES: usize = 64 * 1024;
/// Rohdaten je Vorschau-Stück.
pub const PREVIEW_BYTES: usize = 1024 * 1024;
/// Meldungen, Fehlertexte (Zeichen).
pub const MAX_TEXT: usize = 2000;
pub const MAX_ID: usize = 64;
/// Ed25519
pub const SIGNATURE_BYTES: usize = 64;
pub const PUBLIC_KEY_BYTES: usize = 32;

/// Sekunden bis zum ersten Rahmen `hello`.
pub const HELLO_TIMEOUT: u64 = 10;
pub const HEARTBEAT_SECONDS: u64 = 30;
/// Ports der Vorschau auf dem Rechner (Entscheidung 8x).
pub const PREVIEW_PORTS: Range<u16> = 10080..10090;
/// Gemeldete Ports (hello/session_state/preview) sind nur Auskunft; auch Container einer
/// früheren Version (18080–18089) werden gemeldet, ohne dass der Server das hello ablehnt.
pub const REPORTED_PORTS: (u16, u16) = (1024, 65535);

// --- Close-Codes ---------------------------------------------------------------

pub const CLOSE_NORMAL: u16 = 1000;
pub const CLOSE_GOING_AWAY: u16 = 1001;
/// Kein Close-Rahmen (Netz weg).
pub const CLOSE_ABNORMAL: u16 = 1006;
pub const CLOSE_SERVER_ERROR: u16 = 1011;
pub const CLOSE_RESTART: u16 = 1012;
pub const CLOSE_TRY_AGAIN: u16 = 1013;
pub const CLOSE_PROTOCOL: u16 = 4400;
pub const CLOSE_TOKEN: u16 = 4401;
pub const CLOSE_QUOTA: u16 = 4402;
pub const CLOSE_FORBIDDEN: u16 = 4403;
pub const CLOSE_HELLO_TIMEOUT: u16 = 4408;
pub const CLOSE_REPLACED: u16 = 4409;
pub const CLOSE_TOO_BIG: u16 = 4413;
pub const CLOSE_VERSION: u16 = 4426;
pub const CLOSE_RATE_LIMIT: u16 = 4429;
/// Nur Agent: Rahmen des Servers nicht (richtig) signiert.
pub const CLOSE_SIGNATURE: u16 = 4498;

/// Klartext zu einem Close-Code (wie `CLOSE_REASONS`).
pub fn close_reason(code: u16) -> Option<&'static str> {
    Some(match code {
        CLOSE_PROTOCOL => "Protokollfehler",
        CLOSE_TOKEN => "Token ungültig",
        CLOSE_QUOTA => "Kontingent erschöpft",
        CLOSE_FORBIDDEN => "Kein Recht",
        CLOSE_HELLO_TIMEOUT => "Keine Anmeldung",
        CLOSE_REPLACED => "Ersetzt durch neue Verbindung",
        CLOSE_TOO_BIG => "Rahmen zu groß",
        CLOSE_VERSION => "Protokollversion nicht unterstützt",
        CLOSE_RATE_LIMIT => "Zu viele Anfragen",
        CLOSE_SIGNATURE => "Signatur des Servers ungültig",
        _ => return None,
    })
}

/// Nach diesen Codes verbindet sich der Agent nicht neu (Mensch muss handeln).
pub const NO_RECONNECT: [u16; 5] = [
    CLOSE_TOKEN,
    CLOSE_FORBIDDEN,
    CLOSE_REPLACED,
    CLOSE_VERSION,
    CLOSE_SIGNATURE,
];
/// Nach diesen Codes neu verbinden mit Backoff; alle übrigen mit der längsten Wartezeit.
pub const RECONNECT: [u16; 5] = [
    CLOSE_ABNORMAL,
    CLOSE_SERVER_ERROR,
    CLOSE_RESTART,
    CLOSE_TRY_AGAIN,
    CLOSE_GOING_AWAY,
];

/// Darf der Agent nach diesem Close-Code neu verbinden?
pub fn should_reconnect(code: Option<u16>) -> bool {
    !matches!(code, Some(c) if NO_RECONNECT.contains(&c))
}

// --- Aufzählungen ----------------------------------------------------------------

/// Absender eines Rahmens (Richtung).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Sender {
    Agent,
    Server,
}

impl Sender {
    pub fn as_str(self) -> &'static str {
        match self {
            Sender::Agent => "agent",
            Sender::Server => "server",
        }
    }

    pub fn from_name(name: &str) -> Option<Sender> {
        match name {
            "agent" => Some(Sender::Agent),
            "server" => Some(Sender::Server),
            _ => None,
        }
    }
}

impl fmt::Display for Sender {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

pub const ENGINES: &[&str] = &["podman", "docker", "fake"];
pub const SESSION_STATES: &[&str] = &[
    "starting", "running", "stopping", "stopped", "failed", "unknown",
];
pub const STOP_REASONS: &[&str] = &[
    "user", "idle", "quota", "admin", "revoked", "mismatch", "replaced", "error",
];
pub const CLOSE_FRAME_REASONS: &[&str] = &[
    "revoked", "rights", "quota", "replaced", "shutdown", "protocol",
];
pub const NETWORK_MODES: &[&str] = &["none", "allowlist", "default", "packages"];
/// Grenzen der beschreibbaren Container-Schicht im Modus `packages` (MB).
pub const ROOTFS_MB: std::ops::RangeInclusive<u64> = 64..=1_048_576;
/// Höchstzahl der Paket-Hosts je Sitzung.
pub const MAX_PACKAGE_HOSTS: usize = 50;
pub const SCRATCH_MODES: &[&str] = &["container", "local"];
pub const ERROR_CODES: &[&str] = &[
    "invalid_frame",
    "unknown_session",
    "unknown_pipe",
    "not_allowed",
    "busy",
    "limit",
    "bundle",
    "engine",
    "offline",
    "timeout",
    "internal",
];
pub const HTTP_METHODS: &[&str] = &["GET", "HEAD"];
/// Ergebnis der Prüfung des Dienstkatalogs (`welcome.catalog`).
pub const CATALOG_STATES: &[&str] = &[
    "valid",
    "unsigned",
    "invalid",
    "unknown_key",
    "missing",
    "dev",
];
/// Art eines Dienstes im Katalog (Einstufung).
pub const SERVICE_KINDS: &[&str] = &["read", "edit", "command", "publish", "remote", "setup"];

// --- Muster -----------------------------------------------------------------------

fn re(pattern: &str) -> Regex {
    Regex::new(pattern).expect("festes Muster")
}

pub static ID_RE: LazyLock<Regex> = LazyLock::new(|| re(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$"));
pub static SHA256_RE: LazyLock<Regex> = LazyLock::new(|| re(r"^[0-9a-f]{64}$"));
/// SHA-1 oder SHA-256.
pub static COMMIT_RE: LazyLock<Regex> = LazyLock::new(|| re(r"^[0-9a-f]{40}([0-9a-f]{24})?$"));
pub static TOKEN_RE: LazyLock<Regex> = LazyLock::new(|| re(r"^[A-Za-z0-9_-]{16,128}$"));
/// Image nur mit Digest: `name[:tag]@sha256:<64 hex>`.
pub static IMAGE_RE: LazyLock<Regex> =
    LazyLock::new(|| re(r"^[a-z0-9][a-z0-9._/:-]{0,254}@sha256:[0-9a-f]{64}$"));
pub static HOST_RE: LazyLock<Regex> = LazyLock::new(|| re(r"^[A-Za-z0-9][A-Za-z0-9.:-]{0,252}$"));
/// Paket-Host (Modus `packages`): DNS-Name, klein, ohne Port, IP oder Wildcard;
/// Länge höchstens 253 prüft das Feld (die Python-Fassung nutzt dafür ein Lookahead).
pub static PACKAGE_HOST_RE: LazyLock<Regex> =
    LazyLock::new(|| re(r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"));
pub static HEADER_NAME_RE: LazyLock<Regex> = LazyLock::new(|| re(r"^[A-Za-z0-9-]{1,64}$"));
pub static UUID_RE: LazyLock<Regex> =
    LazyLock::new(|| re(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"));
pub static NONCE_RE: LazyLock<Regex> = LazyLock::new(|| re(r"^[0-9a-f]{64}$"));
pub static KEY_ID_RE: LazyLock<Regex> = LazyLock::new(|| re(r"^[0-9a-f]{16}$"));
pub static SERVICE_RE: LazyLock<Regex> = LazyLock::new(|| re(r"^[a-z][a-z0-9_]{0,63}$"));
pub static MIME_RE: LazyLock<Regex> =
    LazyLock::new(|| re(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$"));

/// Image mit Digest (`IMAGE_RE`)?
pub fn is_image(value: &str) -> bool {
    value.chars().count() <= 320 && IMAGE_RE.is_match(value)
}

/// Gültiger Token (`TOKEN_RE`)?
pub fn is_token(value: &str) -> bool {
    TOKEN_RE.is_match(value)
}

/// Gültige Kennung (`ID_RE`)?
pub fn is_id(value: &str) -> bool {
    ID_RE.is_match(value)
}

// --- Fehler ---------------------------------------------------------------------

/// Rahmen ungültig; `close_code` ist der passende Close-Code.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProtocolError {
    pub message: String,
    pub close_code: u16,
}

impl ProtocolError {
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            close_code: CLOSE_PROTOCOL,
        }
    }

    pub fn with_code(message: impl Into<String>, close_code: u16) -> Self {
        Self {
            message: message.into(),
            close_code,
        }
    }
}

impl fmt::Display for ProtocolError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for ProtocolError {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reconnect_rules() {
        for code in NO_RECONNECT {
            assert!(!should_reconnect(Some(code)));
        }
        assert!(should_reconnect(None));
        assert!(should_reconnect(Some(CLOSE_ABNORMAL)));
        assert!(should_reconnect(Some(CLOSE_QUOTA)));
        assert!(should_reconnect(Some(CLOSE_RATE_LIMIT)));
    }

    #[test]
    fn patterns() {
        assert!(is_image(&format!(
            "ghcr.io/x/y:1@sha256:{}",
            "a".repeat(64)
        )));
        assert!(!is_image("ghcr.io/x/y:1"));
        assert!(!is_id("abc\n"));
        assert!(is_id("run-1"));
        assert!(!is_token("short"));
        assert_eq!(close_reason(CLOSE_TOKEN), Some("Token ungültig"));
        assert_eq!(close_reason(1000), None);
    }
}
