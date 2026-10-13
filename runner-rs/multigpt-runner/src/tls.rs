//! TLS des Agents: normale Prüfung gegen die System-CAs oder festgehaltener
//! Fingerprint (Frage 8i, wie `runner_agent/tls.py`).
//!
//! Mit Fingerprint prüft der Agent den SHA-256 des Server-Zertifikats schon im
//! Handshake – also bevor ein einziges Byte des `hello` (mit Token) gesendet wird.
//! Kette und Rechnername werden dann nicht geprüft (selbst signiertes snakeoil),
//! die Handshake-Signatur sehr wohl (Besitz des privaten Schlüssels).

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, OnceLock};

use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::crypto::{CryptoProvider, WebPkiSupportedAlgorithms};
use rustls::pki_types::{CertificateDer, ServerName, UnixTime};
use rustls::{ClientConfig, DigitallySignedStruct, RootCertStore, SignatureScheme};
use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq;

/// SHA-256 eines Zertifikats (DER) als Hex-Text.
pub fn fingerprint(der: &[u8]) -> String {
    hex::encode(Sha256::digest(der))
}

fn provider() -> Arc<CryptoProvider> {
    Arc::new(rustls::crypto::ring::default_provider())
}

/// System-CAs, einmal je Prozess geladen.
fn system_roots() -> Arc<RootCertStore> {
    static ROOTS: OnceLock<Arc<RootCertStore>> = OnceLock::new();
    ROOTS
        .get_or_init(|| {
            let mut store = RootCertStore::empty();
            let loaded = rustls_native_certs::load_native_certs();
            for err in &loaded.errors {
                tracing::warn!("System-CAs teilweise nicht lesbar: {err}");
            }
            let (_added, _ignored) = store.add_parsable_certificates(loaded.certs);
            Arc::new(store)
        })
        .clone()
}

/// Ergebnis von [`client_config`]: Konfiguration und Merker „Fingerprint passte nicht“.
pub struct TlsSetup {
    pub config: Arc<ClientConfig>,
    /// Wird gesetzt, wenn der Handshake am festgehaltenen Fingerprint scheiterte.
    pub pin_mismatch: Arc<AtomicBool>,
}

impl TlsSetup {
    pub fn pin_failed(&self) -> bool {
        self.pin_mismatch.load(Ordering::SeqCst)
    }
}

/// TLS-Konfiguration für eine Verbindung zum Server (TLS 1.2 und 1.3).
///
/// `extra_roots` ergänzt die System-CAs (nur Tests mit eigener CA).
pub fn client_config(pin: Option<&str>, extra_roots: &[CertificateDer<'static>]) -> TlsSetup {
    let provider = provider();
    let pin_mismatch = Arc::new(AtomicBool::new(false));
    let builder = ClientConfig::builder_with_provider(provider.clone())
        .with_safe_default_protocol_versions()
        .expect("ring unterstützt TLS 1.2/1.3");
    let config = match pin {
        None => {
            let mut roots = (*system_roots()).clone();
            for cert in extra_roots {
                let _ = roots.add(cert.clone());
            }
            builder.with_root_certificates(roots).with_no_client_auth()
        }
        Some(pin) => builder
            .dangerous()
            .with_custom_certificate_verifier(Arc::new(PinVerifier {
                pin: pin.to_ascii_lowercase(),
                algorithms: provider.signature_verification_algorithms,
                mismatch: pin_mismatch.clone(),
            }))
            .with_no_client_auth(),
    };
    TlsSetup {
        config: Arc::new(config),
        pin_mismatch,
    }
}

/// Prüft nur den Fingerprint des Endzertifikats (und die Handshake-Signatur).
#[derive(Debug)]
struct PinVerifier {
    pin: String,
    algorithms: WebPkiSupportedAlgorithms,
    mismatch: Arc<AtomicBool>,
}

impl ServerCertVerifier for PinVerifier {
    fn verify_server_cert(
        &self,
        end_entity: &CertificateDer<'_>,
        _intermediates: &[CertificateDer<'_>],
        _server_name: &ServerName<'_>,
        _ocsp_response: &[u8],
        _now: UnixTime,
    ) -> Result<ServerCertVerified, rustls::Error> {
        let actual = fingerprint(end_entity.as_ref());
        if bool::from(actual.as_bytes().ct_eq(self.pin.as_bytes())) {
            Ok(ServerCertVerified::assertion())
        } else {
            self.mismatch.store(true, Ordering::SeqCst);
            Err(rustls::Error::InvalidCertificate(
                rustls::CertificateError::ApplicationVerificationFailure,
            ))
        }
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls12_signature(message, cert, dss, &self.algorithms)
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls13_signature(message, cert, dss, &self.algorithms)
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.algorithms.supported_schemes()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fingerprint_hex() {
        assert_eq!(
            fingerprint(b""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
    }

    #[test]
    fn pin_verifier_compares() {
        let der = CertificateDer::from(vec![1u8, 2, 3]);
        let setup = client_config(Some(&fingerprint(&[9])), &[]);
        let verifier = PinVerifier {
            pin: fingerprint(&[1, 2, 3]),
            algorithms: provider().signature_verification_algorithms,
            mismatch: setup.pin_mismatch.clone(),
        };
        let name = ServerName::try_from("h").unwrap();
        assert!(
            verifier
                .verify_server_cert(&der, &[], &name, &[], UnixTime::now())
                .is_ok()
        );
        assert!(!setup.pin_failed());
        let other = CertificateDer::from(vec![4u8]);
        assert!(
            verifier
                .verify_server_cert(&other, &[], &name, &[], UnixTime::now())
                .is_err()
        );
        assert!(setup.pin_failed());
    }
}
