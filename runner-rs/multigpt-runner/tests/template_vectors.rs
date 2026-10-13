//! Gemeinsame Testvektoren der festen Vorlage (Orakel: `runner_agent/template.py`).
//!
//! Liest alle Dateien unter `runner-rs/testdata/template/` (erzeugt mit
//! `python -m tests.runner_template_vectors`) und prüft sie gegen
//! `multigpt_runner::template`.

use std::collections::BTreeSet;
use std::path::PathBuf;

use multigpt_runner::template::{self, Bounds, Limits};
use serde_json::Value;

fn bounds(config: &Value) -> Bounds {
    Bounds {
        images: config["images"]
            .as_array()
            .unwrap()
            .iter()
            .map(|i| i.as_str().unwrap().to_string())
            .collect::<BTreeSet<_>>(),
        max_cpus: config["max_cpus"].as_f64().unwrap(),
        max_memory_mb: config["max_memory_mb"].as_u64().unwrap(),
        max_pids: config["max_pids"].as_u64().unwrap(),
        max_tmpfs_mb: config["max_tmpfs_mb"].as_u64().unwrap(),
        scratch_host_dir: config["scratch_host_dir"].as_str().map(str::to_string),
    }
}

/// Ergebnis eines Falls: Rolle bzw. "ok" oder "reject".
fn run(case: &Value) -> String {
    let cfg = bounds(&case["config"]);
    let ok_or_reject = |ok: bool| if ok { "ok" } else { "reject" }.to_string();
    match case["check"].as_str().unwrap() {
        "body" => match template::check_body(&case["body"], &cfg) {
            Ok(role) => role.as_str().to_string(),
            Err(_) => "reject".into(),
        },
        "network" => ok_or_reject(template::check_network_body(&case["body"]).is_ok()),
        "inspect" => {
            let expected = Limits::from_value(&case["expected"]).expect("expected");
            let problems = template::check_inspect(
                &case["info"],
                &expected,
                case["preview_port"].as_u64(),
                case["scratch_source"].as_str(),
            );
            ok_or_reject(problems.is_empty())
        }
        "limits" => match Limits::from_value(&case["limits"]) {
            Some(limits) => ok_or_reject(template::check_limits(&limits, &cfg).is_ok()),
            None => "reject".into(),
        },
        "image" => {
            let server: Option<Vec<String>> = case["server_images"].as_array().map(|list| {
                list.iter()
                    .map(|i| i.as_str().unwrap().to_string())
                    .collect()
            });
            ok_or_reject(
                template::check_image(case["image"].as_str(), server.as_deref(), &cfg.images)
                    .is_ok(),
            )
        }
        other => panic!("unbekannte Prüfung {other}"),
    }
}

#[test]
fn template_vectors() {
    let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../testdata/template");
    let mut files: Vec<PathBuf> = std::fs::read_dir(&dir)
        .unwrap_or_else(|e| panic!("{}: {e}", dir.display()))
        .map(|entry| entry.unwrap().path())
        .filter(|path| path.extension().is_some_and(|e| e == "json"))
        .collect();
    files.sort();
    assert!(files.len() >= 34, "zu wenige Vektoren in {}", dir.display());
    let mut failures = Vec::new();
    for path in &files {
        let case: Value = serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
        let want = case["expect"].as_str().unwrap();
        let got = run(&case);
        if got != want {
            failures.push(format!(
                "{}: erwartet {want}, erhalten {got} ({})",
                path.file_name().unwrap().to_string_lossy(),
                case["note"].as_str().unwrap_or("")
            ));
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}
