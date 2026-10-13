//! Gemeinsame Testvektoren (Python ↔ Rust) für das Rahmenprotokoll.
//!
//! Die Dateien unter `runner-rs/testdata/frames/{valid,invalid}/` erzeugt
//! `python -m tests.runner_vectors` aus `multigpt/runner/protocol.py`; Format im
//! Kopf von `tests/runner_vectors.py`. Rust muss dieselben Rahmen annehmen bzw.
//! ablehnen; angenommene Rahmen müssen ohne `null`-Felder `canonical` entsprechen.

use std::path::{Path, PathBuf};

use multigpt_runner::protocol::{self, Sender};
use serde_json::Value;

fn frames_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../testdata/frames")
}

fn load(dir: &str) -> Vec<(String, Value)> {
    let path = frames_dir().join(dir);
    let mut out: Vec<(String, Value)> = std::fs::read_dir(&path)
        .unwrap_or_else(|e| panic!("{}: {e}", path.display()))
        .map(|entry| entry.unwrap().path())
        .filter(|p| p.extension().is_some_and(|e| e == "json"))
        .map(|p| {
            let text = std::fs::read_to_string(&p).unwrap();
            let name = p.file_stem().unwrap().to_string_lossy().into_owned();
            (name, serde_json::from_str(&text).unwrap())
        })
        .collect();
    out.sort_by(|a, b| a.0.cmp(&b.0));
    out
}

fn sender(entry: &Value) -> Option<Sender> {
    entry["sender"]
        .as_str()
        .map(|s| Sender::from_name(s).expect("sender"))
}

/// Text des Rahmens wie in `tests/runner_vectors.py` (`compact`, `padded_text`).
fn text_of(entry: &Value) -> String {
    if let Some(text) = entry["text"].as_str() {
        return text.to_string();
    }
    let frame = entry["frame"].clone();
    let Some(total) = entry["pad_total_bytes"].as_u64() else {
        return serde_json::to_string(&frame).unwrap();
    };
    let base = serde_json::to_string(&frame).unwrap().len();
    let mut data = frame;
    data["message"]["params"]["pad"] = Value::String("a".repeat(total as usize - base));
    let text = serde_json::to_string(&data).unwrap();
    assert_eq!(text.len() as u64, total);
    text
}

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

/// Gleichheit mit numerischem Vergleich der Zahlen (2 == 2.0).
fn same(a: &Value, b: &Value) -> bool {
    match (a, b) {
        (Value::Number(x), Value::Number(y)) => x.as_f64() == y.as_f64(),
        (Value::Array(x), Value::Array(y)) => {
            x.len() == y.len() && x.iter().zip(y).all(|(p, q)| same(p, q))
        }
        (Value::Object(x), Value::Object(y)) => {
            x.len() == y.len() && x.iter().all(|(k, v)| y.get(k).is_some_and(|w| same(v, w)))
        }
        _ => a == b,
    }
}

#[test]
fn valid_vectors_are_accepted() {
    let cases = load("valid");
    assert!(
        cases.len() > 50,
        "zu wenige gültige Vektoren: {}",
        cases.len()
    );
    let mut failures = Vec::new();
    for (name, entry) in &cases {
        let text = text_of(entry);
        match protocol::parse_value(&text, sender(entry)) {
            Ok(parsed) => {
                // Aufgefüllte Rahmen: erwartet ist der aufgefüllte Text selbst.
                let expected = match entry.get("canonical") {
                    Some(canonical) => canonical.clone(),
                    None if entry.get("pad_total_bytes").is_some() => {
                        serde_json::from_str(&text).unwrap()
                    }
                    None => entry["frame"].clone(),
                };
                let got = strip_nulls(parsed.clone());
                if !same(&got, &strip_nulls(expected.clone())) {
                    failures.push(format!("{name}: normalisiert anders als erwartet"));
                }
                // Der typisierte Rahmen muss sich bauen und wieder gleich senden lassen.
                match protocol::Frame::from_validated(parsed) {
                    Ok(frame) => {
                        let again = protocol::encode(&frame, None)
                            .and_then(|t| protocol::parse_value(&t, None))
                            .map(strip_nulls);
                        if !again.as_ref().is_ok_and(|v| same(v, &got)) {
                            failures.push(format!("{name}: typisiert {again:?}"));
                        }
                    }
                    Err(e) => failures.push(format!("{name}: typisiert {e}")),
                }
            }
            Err(e) => failures.push(format!("{name}: abgelehnt ({})", e.message)),
        }
    }
    assert!(
        failures.is_empty(),
        "{} Fehler:\n{}",
        failures.len(),
        failures.join("\n")
    );
}

#[test]
fn invalid_vectors_are_rejected() {
    let cases = load("invalid");
    assert!(
        cases.len() > 50,
        "zu wenige ungültige Vektoren: {}",
        cases.len()
    );
    let mut failures = Vec::new();
    for (name, entry) in &cases {
        let text = text_of(entry);
        match protocol::parse_value(&text, sender(entry)) {
            Ok(_) => failures.push(format!("{name}: angenommen ({})", entry["note"])),
            Err(e) => {
                let expected = entry["close_code"].as_u64().unwrap_or(4400);
                if u64::from(e.close_code) != expected {
                    failures.push(format!(
                        "{name}: Close-Code {} statt {expected}",
                        e.close_code
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
