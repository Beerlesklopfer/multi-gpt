//! Umschlag `signed` (Protokoll 2): Jeder Rahmen des Servers ist mit dem
//! Installationsschlüssel signiert, gebunden an die `nonce` des eigenen `hello`.
//!
//! Signierte Bytes: `FRAME_CONTEXT + nonce + "\n" + payload` (payload = exakte
//! Bytes des inneren JSON-Textes, im Rahmen als Base64). Kein kanonisches JSON.
//! Der Agent prüft die Signatur, *bevor* er den inneren Rahmen parst.

use ed25519_dalek::{Signature, Signer, SigningKey, VerifyingKey};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use super::{
    CLOSE_SIGNATURE, CLOSE_TOO_BIG, Frame, MAX_PAYLOAD_BYTES, NONCE_RE, ProtocolError,
    SIGNATURE_BYTES, Sender, Signed, b64decode, b64encode, encode_value, parse,
};

pub const FRAME_CONTEXT: &[u8] = b"multigpt-runner-frame/2\n";

/// Kennung eines Ed25519-Schlüssels: die ersten 16 Hex-Zeichen von SHA-256(roh).
pub fn key_id(public_raw: &[u8]) -> String {
    let mut id = hex::encode(Sha256::digest(public_raw));
    id.truncate(16);
    id
}

/// Die signierten Bytes eines Umschlags.
pub fn signed_message(nonce: &str, payload: &[u8]) -> Result<Vec<u8>, ProtocolError> {
    if !NONCE_RE.is_match(nonce) {
        return Err(ProtocolError::with_code("nonce ungültig", CLOSE_SIGNATURE));
    }
    let mut message = Vec::with_capacity(FRAME_CONTEXT.len() + nonce.len() + 1 + payload.len());
    message.extend_from_slice(FRAME_CONTEXT);
    message.extend_from_slice(nonce.as_bytes());
    message.push(b'\n');
    message.extend_from_slice(payload);
    Ok(message)
}

fn sig_error(text: &str) -> ProtocolError {
    ProtocolError::with_code(text, CLOSE_SIGNATURE)
}

/// Umschlag prüfen und den inneren Rahmen (geprüft, Absender Server) liefern.
///
/// `frame` ist der äußere, bereits geprüfte Rahmen. Fehler der Signatur tragen
/// `CLOSE_SIGNATURE`; ein ungültiger innerer Rahmen den Code seiner Prüfung.
pub fn open_signed(frame: &Frame, nonce: &str, key: &VerifyingKey) -> Result<Frame, ProtocolError> {
    open_signed_with(frame, nonce, &key_id(key.as_bytes()), key)
}

/// Wie [`open_signed`], mit ausdrücklich erwarteter Kennung `expected_key`
/// (wie `open_signed(frame, nonce, key_id, verify)` in Python).
pub fn open_signed_with(
    frame: &Frame,
    nonce: &str,
    expected_key: &str,
    key: &VerifyingKey,
) -> Result<Frame, ProtocolError> {
    let Frame::Signed(Signed {
        key: kid,
        payload,
        sig,
    }) = frame
    else {
        return Err(sig_error(&format!("Rahmen {} ohne Signatur", frame.kind())));
    };
    if kid != expected_key {
        return Err(sig_error("Rahmen mit fremdem Schlüssel signiert"));
    }
    let payload = b64decode(payload)?;
    let message = signed_message(nonce, &payload)?;
    let valid = b64decode(sig)
        .ok()
        .and_then(|raw| Signature::from_slice(&raw).ok())
        .is_some_and(|signature| key.verify_strict(&message, &signature).is_ok());
    if !valid {
        return Err(sig_error("Signatur des Servers ungültig"));
    }
    let text = std::str::from_utf8(&payload)
        .map_err(|_| ProtocolError::new("Innerer Rahmen: kein UTF-8"))?;
    let inner = parse(text, Some(Sender::Server)).map_err(|e| {
        ProtocolError::with_code(format!("Innerer Rahmen: {}", e.message), e.close_code)
    })?;
    if matches!(inner, Frame::Signed(_)) {
        return Err(sig_error("Umschlag im Umschlag"));
    }
    Ok(inner)
}

/// Rahmen (JSON-Text, schon geprüft) in den Umschlag `signed` packen
/// (Server-Seite; im Agent nur für Tests und Testserver).
pub fn wrap_signed(text: &str, nonce: &str, key: &SigningKey) -> Result<String, ProtocolError> {
    let payload = text.as_bytes();
    if payload.len() > MAX_PAYLOAD_BYTES {
        return Err(ProtocolError::with_code("Rahmen zu groß", CLOSE_TOO_BIG));
    }
    let signature = key.sign(&signed_message(nonce, payload)?);
    let sig = signature.to_bytes();
    debug_assert_eq!(sig.len(), SIGNATURE_BYTES);
    encode_value(
        "signed",
        Some(Sender::Server),
        json!({
            "key": key_id(key.verifying_key().as_bytes()),
            "payload": b64encode(payload),
            "sig": b64encode(&sig),
        }),
    )
}

/// Öffentlichen Schlüssel aus einer Zeile Base64 (32 Byte roh) lesen.
pub fn verifying_key_from_b64(text: &str) -> Option<VerifyingKey> {
    let raw = b64decode(text.trim()).ok()?;
    let raw: [u8; 32] = raw.try_into().ok()?;
    VerifyingKey::from_bytes(&raw).ok()
}

/// Hilfe für Tests: Server-Rahmen bauen und signieren.
pub fn sign_server_frame(
    kind: &str,
    fields: Value,
    nonce: &str,
    key: &SigningKey,
) -> Result<String, ProtocolError> {
    let inner = encode_value(kind, Some(Sender::Server), fields)?;
    wrap_signed(&inner, nonce, key)
}

#[cfg(test)]
mod tests {
    use super::super::parse;
    use super::*;

    fn key(seed: u8) -> SigningKey {
        SigningKey::from_bytes(&[seed; 32])
    }

    fn welcome() -> Value {
        json!({"runner": 1, "protocol": 2, "server_version": "0.4", "heartbeat_seconds": 30,
               "images": [], "max_frame_bytes": 4194304, "chunk_bytes": 1048576,
               "services": ["read"], "catalog": "valid"})
    }

    #[test]
    fn roundtrip_and_failures() {
        let nonce = "ab".repeat(32);
        let sk = key(7);
        let vk = sk.verifying_key();
        let text = sign_server_frame("welcome", welcome(), &nonce, &sk).unwrap();
        let outer = parse(&text, Some(Sender::Server)).unwrap();
        let Frame::Welcome(w) = open_signed(&outer, &nonce, &vk).unwrap() else {
            panic!("welcome erwartet");
        };
        assert_eq!(w.services, vec!["read"]);
        // andere nonce (Replay aus einer anderen Verbindung)
        let err = open_signed(&outer, &"cd".repeat(32), &vk).unwrap_err();
        assert_eq!(err.close_code, CLOSE_SIGNATURE);
        // fremder Schlüssel
        let err = open_signed(&outer, &nonce, &key(8).verifying_key()).unwrap_err();
        assert_eq!(err.message, "Rahmen mit fremdem Schlüssel signiert");
        // unsigniert
        let plain = encode_value("welcome", Some(Sender::Server), welcome()).unwrap();
        let err = open_signed(&parse(&plain, None).unwrap(), &nonce, &vk).unwrap_err();
        assert_eq!(err.close_code, CLOSE_SIGNATURE);
        // veränderte Nutzlast
        let Frame::Signed(mut s) = outer.clone() else {
            unreachable!()
        };
        s.payload = b64encode(plain.replace("\"runner\":1", "\"runner\":2").as_bytes());
        let err = open_signed(&Frame::Signed(s), &nonce, &vk).unwrap_err();
        assert_eq!(err.message, "Signatur des Servers ungültig");
    }

    #[test]
    fn nested_and_invalid_inner() {
        let nonce = "ab".repeat(32);
        let sk = key(1);
        let inner = sign_server_frame("welcome", welcome(), &nonce, &sk).unwrap();
        let nested = wrap_signed(&inner, &nonce, &sk).unwrap();
        let err =
            open_signed(&parse(&nested, None).unwrap(), &nonce, &sk.verifying_key()).unwrap_err();
        assert_eq!(err.message, "Umschlag im Umschlag");
        let bad = wrap_signed(r#"{"type":"welcome"}"#, &nonce, &sk).unwrap();
        let err =
            open_signed(&parse(&bad, None).unwrap(), &nonce, &sk.verifying_key()).unwrap_err();
        assert!(err.message.starts_with("Innerer Rahmen: "));
        assert_eq!(err.close_code, super::super::CLOSE_PROTOCOL);
    }

    #[test]
    fn key_id_and_b64() {
        let sk = key(3);
        let b64 = b64encode(sk.verifying_key().as_bytes());
        assert_eq!(
            verifying_key_from_b64(&format!("{b64}\n")).unwrap(),
            sk.verifying_key()
        );
        assert!(verifying_key_from_b64("abc").is_none());
        assert_eq!(key_id(b"").len(), 16);
    }
}
