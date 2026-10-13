//! Gemeinsame Testvektoren für den Umschlag `signed` (Protokoll 2).
//!
//! Erzeugt mit `python -m tests.runner_signature_vectors` (fester Test-Schlüssel,
//! öffentlich, nur Tests). Ablauf wie im Agent: parse(text, Server) → open_signed.

use std::path::Path;

use multigpt_runner::protocol::{self, Sender, signed::verifying_key_from_b64};
use serde_json::Value;

fn strip_nulls(value: Value) -> Value {
    match value {
        Value::Object(map) => Value::Object(
            map.into_iter()
                .filter(|(_, v)| !v.is_null())
                .map(|(k, v)| (k, strip_nulls(v)))
                .collect(),
        ),
        Value::Array(items) => Value::Array(items.into_iter().map(strip_nulls).collect()),
        other => other,
    }
}

#[test]
fn signature_vectors() {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../testdata/signatures");
    let mut names: Vec<_> = std::fs::read_dir(&dir)
        .unwrap()
        .map(|e| e.unwrap().path())
        .filter(|p| p.extension().is_some_and(|e| e == "json"))
        .collect();
    names.sort();
    assert!(names.len() >= 10, "zu wenige Signatur-Vektoren");
    let mut failures = Vec::new();
    for path in names {
        let name = path.file_stem().unwrap().to_string_lossy().into_owned();
        let entry: Value = serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        let key = verifying_key_from_b64(entry["public_key"].as_str().unwrap()).unwrap();
        let nonce = entry["nonce"].as_str().unwrap();
        let key_id = entry["key_id"].as_str().unwrap();
        let text = entry["text"].as_str().unwrap();
        let result = protocol::parse(text, Some(Sender::Server))
            .and_then(|outer| protocol::open_signed_with(&outer, nonce, key_id, &key));
        match (entry["expect"].as_str().unwrap(), result) {
            ("ok", Ok(inner)) => {
                let got = strip_nulls(serde_json::to_value(&inner).unwrap());
                if got != strip_nulls(entry["inner"].clone()) {
                    failures.push(format!("{name}: innerer Rahmen {got}"));
                }
            }
            ("ok", Err(e)) => failures.push(format!("{name}: abgelehnt ({})", e.message)),
            (_, Ok(_)) => failures.push(format!("{name}: angenommen ({})", entry["note"])),
            (_, Err(e)) => {
                if Some(u64::from(e.close_code)) != entry["close_code"].as_u64() {
                    failures.push(format!(
                        "{name}: Close-Code {} ({})",
                        e.close_code, e.message
                    ));
                }
            }
        }
    }
    assert!(
        failures.is_empty(),
        "{} Fehler:\n{}",
        failures.len(),
        failures.join("\n")
    );
}
