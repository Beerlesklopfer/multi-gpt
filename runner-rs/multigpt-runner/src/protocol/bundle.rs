//! Bundle- und Dateiübertragung in Stücken (`bundle_chunk`, `file_chunk`).

use base64::Engine as _;
use sha2::{Digest, Sha256};

use super::{CHUNK_BYTES, CLOSE_TOO_BIG, MAX_BUNDLE_BYTES, ProtocolError, schema};

/// Base64 (Standardalphabet mit Padding).
pub fn b64encode(data: &[u8]) -> String {
    base64::engine::general_purpose::STANDARD.encode(data)
}

/// Base64 streng wie `base64.b64decode(..., validate=True)`.
pub fn b64decode(text: &str) -> Result<Vec<u8>, ProtocolError> {
    schema::b64_decode_strict(text).map_err(|_| ProtocolError::new("kein gültiges Base64"))
}

/// Bundle in Stücke teilen: Liste von (seq, Rohdaten, last).
pub fn split_bundle(data: &[u8], chunk_bytes: usize) -> Vec<(u64, &[u8], bool)> {
    if data.is_empty() {
        return vec![(0, data, true)];
    }
    let chunk_bytes = chunk_bytes.max(1);
    let count = data.len().div_ceil(chunk_bytes);
    data.chunks(chunk_bytes)
        .enumerate()
        .map(|(seq, part)| (seq as u64, part, seq + 1 == count))
        .collect()
}

/// Ein empfangenes Stück (Felder von `bundle_chunk` bzw. `file_chunk`).
#[derive(Debug, Clone, Copy)]
pub struct Chunk<'a> {
    pub transfer: &'a str,
    pub seq: u64,
    pub data: &'a str,
    pub last: bool,
    pub size: Option<u64>,
    pub sha256: Option<&'a str>,
}

impl<'a> From<&'a super::BundleChunk> for Chunk<'a> {
    fn from(f: &'a super::BundleChunk) -> Self {
        Chunk {
            transfer: &f.transfer,
            seq: f.seq,
            data: &f.data,
            last: f.last,
            size: f.size,
            sha256: f.sha256.as_deref(),
        }
    }
}

impl<'a> From<&'a super::FileChunk> for Chunk<'a> {
    fn from(f: &'a super::FileChunk) -> Self {
        Chunk {
            transfer: &f.transfer,
            seq: f.seq,
            data: &f.data,
            last: f.last,
            size: f.size,
            sha256: f.sha256.as_deref(),
        }
    }
}

/// Stücke einer Übertragung zusammensetzen.
///
/// `add` liefert `None`, solange Stücke fehlen, und am Ende die geprüften Bytes.
/// Fehler (Reihenfolge, Größe, Prüfsumme) mit deutscher Meldung für `bundle_ack.error`.
#[derive(Debug)]
pub struct BundleAssembler {
    pub transfer: String,
    pub max_bytes: u64,
    pub next_seq: u64,
    pub size: u64,
    hash: Sha256,
    parts: Vec<u8>,
}

impl BundleAssembler {
    pub fn new(transfer: impl Into<String>, max_bytes: u64) -> Self {
        Self {
            transfer: transfer.into(),
            max_bytes: max_bytes.min(MAX_BUNDLE_BYTES),
            next_seq: 0,
            size: 0,
            hash: Sha256::new(),
            parts: Vec::new(),
        }
    }

    pub fn add<'a>(
        &mut self,
        chunk: impl Into<Chunk<'a>>,
    ) -> Result<Option<Vec<u8>>, ProtocolError> {
        let chunk = chunk.into();
        if chunk.transfer != self.transfer {
            return Err(ProtocolError::new(
                "Stück gehört zu einer anderen Übertragung",
            ));
        }
        if chunk.seq != self.next_seq {
            return Err(ProtocolError::new("Stücke in falscher Reihenfolge"));
        }
        let part = b64decode(chunk.data)?;
        self.size += part.len() as u64;
        if self.size > self.max_bytes {
            return Err(ProtocolError::with_code(
                format!("Bundle größer als {} Byte", self.max_bytes),
                CLOSE_TOO_BIG,
            ));
        }
        self.hash.update(&part);
        self.parts.extend_from_slice(&part);
        self.next_seq += 1;
        if !chunk.last {
            return Ok(None);
        }
        if chunk.size != Some(self.size) {
            return Err(ProtocolError::new("Größe des Bundles stimmt nicht"));
        }
        let digest = hex::encode(self.hash.clone().finalize());
        if chunk.sha256 != Some(digest.as_str()) {
            return Err(ProtocolError::new("Prüfsumme des Bundles stimmt nicht"));
        }
        Ok(Some(std::mem::take(&mut self.parts)))
    }
}

/// SHA-256 als Hex-Text.
pub fn sha256_hex(data: &[u8]) -> String {
    hex::encode(Sha256::digest(data))
}

/// Standardgröße der Stücke.
pub const DEFAULT_CHUNK: usize = CHUNK_BYTES;

#[cfg(test)]
mod tests {
    use super::*;

    fn chunk<'a>(
        seq: u64,
        data: &'a str,
        last: bool,
        size: Option<u64>,
        sha: Option<&'a str>,
    ) -> Chunk<'a> {
        Chunk {
            transfer: "t1",
            seq,
            data,
            last,
            size,
            sha256: sha,
        }
    }

    #[test]
    fn split_and_assemble() {
        let data: Vec<u8> = (0..2500u32).map(|i| i as u8).collect();
        let parts = split_bundle(&data, 1024);
        assert_eq!(parts.len(), 3);
        assert!(parts[2].2 && !parts[1].2);
        let sha = sha256_hex(&data);
        let mut asm = BundleAssembler::new("t1", 10_000);
        let encoded: Vec<String> = parts.iter().map(|p| b64encode(p.1)).collect();
        let mut out = None;
        for (i, (seq, _, last)) in parts.iter().enumerate() {
            out = asm
                .add(chunk(*seq, &encoded[i], *last, Some(2500), Some(&sha)))
                .unwrap();
        }
        assert_eq!(out.unwrap(), data);
        assert_eq!(split_bundle(b"", 10), vec![(0, &b""[..], true)]);
    }

    #[test]
    fn errors() {
        let mut asm = BundleAssembler::new("t1", 2);
        assert!(asm.add(chunk(1, "", false, None, None)).is_err());
        let err = asm.add(chunk(0, "QUJD", false, None, None)).unwrap_err();
        assert_eq!(err.close_code, CLOSE_TOO_BIG);
        let mut asm = BundleAssembler::new("t1", 100);
        let err = asm
            .add(chunk(0, "QUJD", true, Some(3), Some(&"0".repeat(64))))
            .unwrap_err();
        assert_eq!(err.message, "Prüfsumme des Bundles stimmt nicht");
    }
}
