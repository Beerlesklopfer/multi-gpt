//! Eingebetteter Dienstkatalog (siehe `build.rs`).
//!
//! Der Agent schickt ihn im `hello` (Text, Signatur, key_id); der Server prüft
//! die Signatur gegen die Release-Schlüssel und meldet in `welcome.services`,
//! welche Dienste angeboten werden dürfen.

use crate::config::CatalogInfo;

mod generated {
    include!(concat!(env!("OUT_DIR"), "/catalog_gen.rs"));
}

pub use generated::{CATALOG, CATALOG_KEY, CATALOG_SIG};

/// Der eingebettete Katalog fürs hello.
pub fn embedded() -> CatalogInfo {
    CatalogInfo {
        text: CATALOG.to_string(),
        sig: CATALOG_SIG.map(str::to_string),
        key: CATALOG_KEY.map(str::to_string),
    }
}

/// Ist der eingebettete Katalog signiert (Release-Build)?
pub fn is_signed() -> bool {
    CATALOG_SIG.is_some()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn embedded_catalog_is_json() {
        let value: serde_json::Value = serde_json::from_str(CATALOG).unwrap();
        assert_eq!(value["format"], 1);
        assert_eq!(CATALOG_SIG.is_some(), CATALOG_KEY.is_some());
        assert!(CATALOG.len() <= crate::protocol::MAX_CATALOG_BYTES);
        assert_eq!(embedded().text, CATALOG);
    }
}
