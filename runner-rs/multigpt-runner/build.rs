//! Dienstkatalog ins Programm einbetten.
//!
//! - `MULTIGPT_CATALOG`, `MULTIGPT_CATALOG_SIG`, `MULTIGPT_CATALOG_KEY` alle
//!   gesetzt (CI beim Release): genau diese Bytes und die Signatur (Datei mit
//!   einer Zeile Base64) samt key_id (16 hex) einbetten.
//! - keine gesetzt (Entwicklung): `runner-rs/catalog.json` unsigniert einbetten.
//! - nur teilweise gesetzt: Abbruch.

use std::env;
use std::fs;
use std::path::{Path, PathBuf};

const MAX_CATALOG_BYTES: usize = 256 * 1024;

fn main() {
    for name in [
        "MULTIGPT_CATALOG",
        "MULTIGPT_CATALOG_SIG",
        "MULTIGPT_CATALOG_KEY",
    ] {
        println!("cargo:rerun-if-env-changed={name}");
    }
    let var = |name: &str| env::var(name).ok().filter(|v| !v.is_empty());
    let out = PathBuf::from(env::var("OUT_DIR").expect("OUT_DIR"));
    let manifest = PathBuf::from(env::var("CARGO_MANIFEST_DIR").expect("CARGO_MANIFEST_DIR"));
    let (catalog, sig, key) = match (
        var("MULTIGPT_CATALOG"),
        var("MULTIGPT_CATALOG_SIG"),
        var("MULTIGPT_CATALOG_KEY"),
    ) {
        (Some(catalog), Some(sig), Some(key)) => {
            let sig_path = PathBuf::from(&sig);
            println!("cargo:rerun-if-changed={sig}");
            let sig = fs::read_to_string(&sig_path)
                .unwrap_or_else(|e| panic!("MULTIGPT_CATALOG_SIG {}: {e}", sig_path.display()));
            let sig = sig.trim().to_string();
            let sig_ok = sig.len() == 88
                && sig.ends_with("==")
                && sig[..86]
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'+' || b == b'/');
            assert!(
                sig_ok,
                "MULTIGPT_CATALOG_SIG: keine Ed25519-Signatur (Base64, 64 Byte)"
            );
            let key_ok =
                key.len() == 16 && key.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'));
            assert!(key_ok, "MULTIGPT_CATALOG_KEY: key_id muss 16 hex sein");
            (PathBuf::from(catalog), Some(sig), Some(key))
        }
        (None, None, None) => (manifest.join("../catalog.json"), None, None),
        _ => panic!(
            "MULTIGPT_CATALOG, MULTIGPT_CATALOG_SIG und MULTIGPT_CATALOG_KEY nur zusammen setzen"
        ),
    };
    println!("cargo:rerun-if-changed={}", catalog.display());
    let bytes = read_catalog(&catalog, sig.is_some());
    assert!(
        bytes.len() <= MAX_CATALOG_BYTES,
        "Dienstkatalog größer als {MAX_CATALOG_BYTES} Byte"
    );
    let text = String::from_utf8(bytes).expect("Dienstkatalog ist kein UTF-8");
    fs::write(out.join("catalog.json"), &text).expect("OUT_DIR schreibbar");
    let code = format!(
        "/// Eingebetteter Dienstkatalog (exakte Bytes).\n\
         pub const CATALOG: &str = include_str!(concat!(env!(\"OUT_DIR\"), \"/catalog.json\"));\n\
         /// Signatur des Release-Schlüssels (Base64) oder `None` (Entwicklung).\n\
         pub const CATALOG_SIG: Option<&str> = {sig:?};\n\
         /// key_id des Release-Schlüssels.\n\
         pub const CATALOG_KEY: Option<&str> = {key:?};\n"
    );
    fs::write(out.join("catalog_gen.rs"), code).expect("OUT_DIR schreibbar");
}

fn read_catalog(path: &Path, signed: bool) -> Vec<u8> {
    match fs::read(path) {
        Ok(bytes) => bytes,
        Err(e) if signed => panic!("MULTIGPT_CATALOG {}: {e}", path.display()),
        Err(_) => {
            println!(
                "cargo:warning=runner-rs/catalog.json fehlt – leerer Dienstkatalog eingebettet"
            );
            br#"{"format":1,"release":"dev","services":[]}"#.to_vec()
        }
    }
}
